"""Two-instance OAuth flow tests for the shared Redis `client_storage`.

Simulates the multi-instance Cloud Run topology that broke 0.1.9–0.1.11:
two independent `build_auth()` proxies (instance A and instance B) that share
nothing in-process, mounted as two separate ASGI apps. A browser-like client
(one cookie jar) and a CLI-like client hop between the instances at every
step of the flow — registration, /authorize, consent GET/POST, IdP callback,
token exchange, and refresh — which is exactly the request pattern that
produced "Authorization session mismatch" / "Invalid or expired authorization
transaction" with the default per-instance file store.

With `SPEKOAI_OAUTH_REDIS_URL` both instances read the same (fake) Redis, so
every hop must succeed; the negative-control test runs the same flow with
isolated stores and reproduces the historical failure.

The upstream IdP (Better Auth) is faked at the `_create_upstream_oauth_client`
seam: token responses include a `refresh_token` + `offline_access` scope,
mirroring what Better Auth returns once `offline_access` is granted. Upstream
JWT verification (JWKS) is out of scope here — these tests cover token
*issuance* and *refresh*, not bearer-token validation of MCP requests.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from typing import Any
from urllib.parse import parse_qs, urlparse

import fakeredis
import fakeredis.aioredis
import httpx
import pytest
from cryptography.fernet import Fernet
from starlette.applications import Starlette

import spekoai_mcp.auth as auth_module
from spekoai_mcp.auth import build_auth

BASE_URL = "https://mcp.example.com"
CLIENT_REDIRECT = "http://localhost:1234/cb"

_OAUTH_ENV = {
    "SPEKOAI_OAUTH_ISSUER": "https://idp.example.com/api/auth/oauth2",
    "SPEKOAI_OAUTH_CLIENT_ID": "upstream-client-id",
    "SPEKOAI_OAUTH_CLIENT_SECRET": "upstream-client-secret",
    "SPEKOAI_MCP_BASE_URL": BASE_URL,
    "SPEKOAI_OAUTH_ADVERTISE_OFFLINE_ACCESS": "true",
    "SPEKOAI_OAUTH_REDIS_URL": "redis://shared.example:6379/0",
}


class _FakeUpstreamOAuth:
    """Stands in for authlib's AsyncOAuth2Client talking to Better Auth.

    `issue_refresh=False` models Better Auth's behavior when the granted
    scope does NOT contain offline_access: no refresh token in the response.
    """

    def __init__(self, issue_refresh: bool = True) -> None:
        self.client_secret = "upstream-client-secret"
        self.issue_refresh = issue_refresh
        self.fetch_calls: list[dict[str, Any]] = []
        self.refresh_calls: list[dict[str, Any]] = []
        self._counter = 0

    async def fetch_token(self, **kwargs: Any) -> dict[str, Any]:
        self.fetch_calls.append(kwargs)
        response = {
            "access_token": "upstream-access-1",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "openid profile email offline_access"
            if self.issue_refresh
            else "openid profile email",
        }
        if self.issue_refresh:
            response["refresh_token"] = "upstream-refresh-1"
        return response

    async def refresh_token(self, **kwargs: Any) -> dict[str, Any]:
        self.refresh_calls.append(kwargs)
        self._counter += 1
        return {
            "access_token": f"upstream-access-{self._counter + 1}",
            # Better Auth rotates the refresh token on every refresh grant.
            "refresh_token": f"upstream-refresh-{self._counter + 1}",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "openid profile email offline_access",
        }


def _build_instance(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: fakeredis.aioredis.FakeRedis | None,
    issue_refresh: bool = True,
) -> tuple[Starlette, Any, _FakeUpstreamOAuth]:
    """One simulated Cloud Run instance: fresh proxy + ASGI app.

    `redis_client=None` builds a legacy instance (no shared-state env, so
    FastMCP falls back to its default per-instance file store).
    """
    if redis_client is not None:
        monkeypatch.setattr(auth_module, "_create_redis_client", lambda url: redis_client)
    proxy = build_auth().server
    upstream = _FakeUpstreamOAuth(issue_refresh=issue_refresh)
    proxy._create_upstream_oauth_client = lambda: upstream  # instance seam
    app = Starlette(routes=proxy.get_routes(mcp_path="/mcp"))
    return app, proxy, upstream


class _InstanceRouter(httpx.AsyncBaseTransport):
    """Routes each request to instance "a" or "b", like a load balancer.

    Lets one httpx client (= one browser cookie jar) hop between the two
    simulated Cloud Run instances mid-flow.
    """

    def __init__(self, apps: dict[str, Starlette]) -> None:
        self._transports = {name: httpx.ASGITransport(app=app) for name, app in apps.items()}
        self.current = "a"

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._transports[self.current].handle_async_request(request)


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Hermetic: a developer shell exporting e.g. SPEKOAI_OAUTH_AUDIENCE would
    # otherwise change the resource/audience wiring under test.
    for name in (
        "SPEKOAI_OAUTH_AUDIENCE",
        "SPEKOAI_OAUTH_JWT_SIGNING_KEY",
        "SPEKOAI_OAUTH_REDIS_URL",
        "SPEKOAI_OAUTH_ADVERTISE_OFFLINE_ACCESS",
        "SPEKOAI_API_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in _OAUTH_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SPEKOAI_OAUTH_JWT_SIGNING_KEY", Fernet.generate_key().decode())


async def test_full_flow_hops_across_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every step lands on a different instance; shared Redis makes it work.

    This is the exact 0.1.9 failure mode (state written by one instance,
    verified by another), proven fixed: registration on A is visible on B,
    the transaction + consent state created on B completes on A, the auth
    code minted on A is exchangeable on B, and the refresh token issued by
    B refreshes on A.
    """
    server = fakeredis.FakeServer()
    redis_a = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    redis_b = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    app_a, _proxy_a, upstream_a = _build_instance(monkeypatch, redis_a)
    app_b, _proxy_b, upstream_b = _build_instance(monkeypatch, redis_b)

    router = _InstanceRouter({"a": app_a, "b": app_b})
    async with httpx.AsyncClient(transport=router, base_url=BASE_URL) as browser:
        # Metadata advertises offline_access (what makes clients request it).
        meta = (await browser.get("/.well-known/oauth-authorization-server")).json()
        assert "offline_access" in meta["scopes_supported"]
        assert "refresh_token" in meta["grant_types_supported"]
        # The protected-resource metadata matters even more: the MCP SDK's
        # scope selection is WWW-Authenticate scope -> PRM scopes_supported
        # -> client metadata scope, so PRM is what actually makes Claude
        # Code request offline_access. AS + PRM are populated by different
        # fastmcp code paths that merely share valid_scopes today.
        prm = (await browser.get("/.well-known/oauth-protected-resource/mcp")).json()
        assert "offline_access" in prm["scopes_supported"]

        # 1. DCR registration on instance A (no scope -> default_scopes).
        router.current = "a"
        reg = await browser.post(
            "/register",
            json={
                "redirect_uris": [CLIENT_REDIRECT],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "client_name": "cross-instance-test",
            },
        )
        assert reg.status_code == 201, reg.text
        client_id = reg.json()["client_id"]

        # 2. /authorize on instance B — the registration must be visible.
        router.current = "b"
        verifier, challenge = _pkce_pair()
        authz = await browser.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": CLIENT_REDIRECT,
                "state": "client-state-xyz",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": "openid profile email offline_access",
            },
        )
        assert authz.status_code == 302, authz.text
        consent_url = authz.headers["location"]
        assert "/consent?txn_id=" in consent_url
        txn_id = parse_qs(urlparse(consent_url).query)["txn_id"][0]

        # 3. Consent page renders on instance A (transaction written by B).
        router.current = "a"
        consent_page = await browser.get("/consent", params={"txn_id": txn_id})
        assert consent_page.status_code == 200, consent_page.text
        match = re.search(r'name="csrf_token" value="([^"]+)"', consent_page.text)
        assert match, "consent page must embed a CSRF token"
        csrf_token = match.group(1)

        # 4. Consent approved on instance B (CSRF cookie set by A must
        #    verify on B — stable cookie signing key + shared transaction).
        router.current = "b"
        approve = await browser.post(
            "/consent",
            data={"txn_id": txn_id, "action": "approve", "csrf_token": csrf_token},
        )
        assert approve.status_code == 302, approve.text
        upstream_url = approve.headers["location"]
        assert upstream_url.startswith(_OAUTH_ENV["SPEKOAI_OAUTH_ISSUER"] + "/authorize")
        upstream_query = parse_qs(urlparse(upstream_url).query)
        # offline_access is forwarded upstream — the precondition for Better
        # Auth to mint a refresh token.
        assert "offline_access" in upstream_query["scope"][0]
        assert upstream_query["state"] == [txn_id]

        # 5. IdP callback lands on instance A (consent approved on B).
        #    This is the exact hop that raised "Authorization session
        #    mismatch" with per-instance state.
        router.current = "a"
        callback = await browser.get(
            "/auth/callback", params={"code": "upstream-code", "state": txn_id}
        )
        assert callback.status_code == 302, callback.text
        client_cb = callback.headers["location"]
        assert client_cb.startswith(CLIENT_REDIRECT)
        cb_query = parse_qs(urlparse(client_cb).query)
        assert cb_query["state"] == ["client-state-xyz"]
        auth_code = cb_query["code"][0]
        assert upstream_a.fetch_calls, "instance A must exchange the upstream code"

        # 6. Token exchange on instance B (code minted by A) — the response
        #    must carry a refresh token end-to-end.
        router.current = "b"
        token = await browser.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "redirect_uri": CLIENT_REDIRECT,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        assert token.status_code == 200, token.text
        tokens = token.json()
        assert tokens["access_token"]
        assert tokens["refresh_token"], "no refresh token issued end-to-end"
        assert "offline_access" in tokens["scope"]

        # 7. Silent refresh on instance A with the refresh token issued by B.
        router.current = "a"
        refresh = await browser.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": client_id,
            },
        )
        assert refresh.status_code == 200, refresh.text
        refreshed = refresh.json()
        assert refreshed["access_token"] != tokens["access_token"]
        # Proxy rotates its refresh token on every use (one-time use).
        assert refreshed["refresh_token"] != tokens["refresh_token"]
        assert upstream_a.refresh_calls, "instance A must refresh upstream"
        # The upstream refresh leg must carry `resource` so Better Auth keeps
        # minting JWT (not opaque) access tokens after refresh.
        assert upstream_a.refresh_calls[0].get("resource") == f"{BASE_URL}/mcp"

        # 8. Rotation enforced: replaying the old refresh token fails.
        router.current = "b"
        replay = await browser.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": client_id,
            },
        )
        # The SDK maps a missing/rotated-out refresh token to 401
        # unauthorized_client; either 4xx proves one-time use.
        assert replay.status_code in {400, 401}
        assert upstream_b is not upstream_a  # sanity: distinct instances


async def test_isolated_stores_reproduce_the_0_1_9_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control: per-instance stores break the cross-instance flow.

    Same topology, but each instance gets its own Redis backend (standing in
    for the default per-instance FileTreeStore). The consent hop fails
    because the transaction written by one instance doesn't exist on the
    other — the failure #757 reverted 0.1.9–0.1.11 over.
    """
    redis_a = fakeredis.aioredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    redis_b = fakeredis.aioredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    app_a, _proxy_a, _ = _build_instance(monkeypatch, redis_a)
    app_b, _proxy_b, _ = _build_instance(monkeypatch, redis_b)

    router = _InstanceRouter({"a": app_a, "b": app_b})
    async with httpx.AsyncClient(transport=router, base_url=BASE_URL) as browser:
        router.current = "a"
        reg = await browser.post(
            "/register",
            json={
                "redirect_uris": [CLIENT_REDIRECT],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
        )
        assert reg.status_code == 201, reg.text
        client_id = reg.json()["client_id"]

        _verifier, challenge = _pkce_pair()
        authz = await browser.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": CLIENT_REDIRECT,
                "state": "client-state-xyz",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": "openid profile email offline_access",
            },
        )
        assert authz.status_code == 302, authz.text
        txn_id = parse_qs(urlparse(authz.headers["location"]).query)["txn_id"][0]

        # The consent hop to the other instance: transaction not found.
        router.current = "b"
        consent_page = await browser.get("/consent", params={"txn_id": txn_id})
        assert consent_page.status_code == 400
        assert "Invalid or expired transaction" in consent_page.text


async def test_grandfathered_client_passes_http_authorize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-0.1.9 registration (empty stored scope) passes /authorize over
    HTTP when requesting the full advertised scope set.

    Exercises the get_client normalization through the real
    AuthorizationHandler, not just via direct provider calls: without the
    normalization this redirects to the client with error=invalid_scope.
    """
    from fastmcp.server.auth.oauth_proxy.models import ProxyDCRClient
    from pydantic import AnyUrl

    redis = fakeredis.aioredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    app, proxy, _ = _build_instance(monkeypatch, redis)
    await proxy._client_store.put(
        key="grandfathered-http",
        value=ProxyDCRClient(
            client_id="grandfathered-http",
            client_secret=None,
            redirect_uris=[AnyUrl(CLIENT_REDIRECT)],
            grant_types=["authorization_code", "refresh_token"],
            scope="",
            token_endpoint_auth_method="none",
        ),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL
    ) as browser:
        _verifier, challenge = _pkce_pair()
        authz = await browser.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": "grandfathered-http",
                "redirect_uri": CLIENT_REDIRECT,
                "state": "s",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": "openid profile email offline_access",
            },
        )
        assert authz.status_code == 302, authz.text
        location = authz.headers["location"]
        assert "/consent?txn_id=" in location, (
            f"expected consent redirect, got error redirect: {location}"
        )


async def test_cimd_client_passes_http_authorize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CIMD client (URL client_id, like current Claude Code) may request
    the advertised scopes.

    On fastmcp 3.2.3 CIMD clients get a scope derived from required_scopes
    (empty for us; fixed upstream in 3.2.4, PrefectHQ/fastmcp#3836) — the
    get_client normalization must cover them too. The CIMD manager's
    document fetch is faked; the store-miss lookup path in get_client is
    real.
    """
    from fastmcp.server.auth.oauth_proxy.models import ProxyDCRClient
    from pydantic import AnyUrl

    cimd_client_id = "https://claude.ai/oauth/claude-code-client-metadata"
    redis = fakeredis.aioredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    app, proxy, _ = _build_instance(monkeypatch, redis)
    assert proxy._cimd_manager is not None

    async def _fake_cimd_lookup(client_id: str) -> ProxyDCRClient:
        # Claude Code's metadata document has NO scope field -> on 3.2.3 the
        # manager derives it from (empty) required_scopes.
        return ProxyDCRClient(
            client_id=client_id,
            client_secret=None,
            redirect_uris=[AnyUrl(CLIENT_REDIRECT)],
            grant_types=["authorization_code", "refresh_token"],
            scope="",
            token_endpoint_auth_method="none",
        )

    monkeypatch.setattr(proxy._cimd_manager, "get_client", _fake_cimd_lookup)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL
    ) as browser:
        _verifier, challenge = _pkce_pair()
        authz = await browser.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": cimd_client_id,
                "redirect_uri": CLIENT_REDIRECT,
                "state": "s",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": "openid profile email offline_access",
            },
        )
        assert authz.status_code == 302, authz.text
        location = authz.headers["location"]
        assert "/consent?txn_id=" in location, (
            f"expected consent redirect, got error redirect: {location}"
        )


async def test_consent_csrf_and_binding_still_reject_foreign_browsers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared storage must not weaken the confused-deputy protections.

    (a) A browser without the consent-state cookie cannot approve consent
    even with the correct form values (CSRF double-submit -> 403).
    (b) A browser without the consent-binding cookie cannot complete the IdP
    callback for someone else's approved transaction (-> 403).
    """
    redis = fakeredis.aioredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    app, _proxy, _ = _build_instance(monkeypatch, redis)
    transport = httpx.ASGITransport(app=app)

    async with (
        httpx.AsyncClient(transport=transport, base_url=BASE_URL) as victim,
        httpx.AsyncClient(transport=transport, base_url=BASE_URL) as attacker,
    ):
        reg = await victim.post(
            "/register",
            json={
                "redirect_uris": [CLIENT_REDIRECT],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
        )
        client_id = reg.json()["client_id"]

        _verifier, challenge = _pkce_pair()
        authz = await victim.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": CLIENT_REDIRECT,
                "state": "s",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": "openid profile email offline_access",
            },
        )
        txn_id = parse_qs(urlparse(authz.headers["location"]).query)["txn_id"][0]
        consent_page = await victim.get("/consent", params={"txn_id": txn_id})
        csrf_token = re.search(r'name="csrf_token" value="([^"]+)"', consent_page.text).group(1)

        # (a) Attacker knows txn_id + csrf but has no consent-state cookie.
        forged = await attacker.post(
            "/consent",
            data={"txn_id": txn_id, "action": "approve", "csrf_token": csrf_token},
        )
        assert forged.status_code == 403
        assert "Authorization session mismatch" in forged.text

        # Victim approves legitimately (cookie present).
        approve = await victim.post(
            "/consent",
            data={"txn_id": txn_id, "action": "approve", "csrf_token": csrf_token},
        )
        assert approve.status_code == 302

        # (b) Attacker (no binding cookie) tries to complete the callback.
        hijack = await attacker.get(
            "/auth/callback", params={"code": "upstream-code", "state": txn_id}
        )
        assert hijack.status_code == 403
        assert "Authorization session mismatch" in hijack.text


async def test_no_upstream_refresh_token_means_no_downstream_refresh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The proxy only mints a refresh token when the upstream returned one.

    Pins the causal chain: without offline_access granted upstream (Better
    Auth returns no refresh_token), the /token response must not contain
    one — so step 6 of the main E2E can't pass vacuously.
    """
    redis = fakeredis.aioredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    app, _proxy, _ = _build_instance(monkeypatch, redis, issue_refresh=False)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL
    ) as browser:
        reg = await browser.post(
            "/register",
            json={
                "redirect_uris": [CLIENT_REDIRECT],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
        )
        client_id = reg.json()["client_id"]

        verifier, challenge = _pkce_pair()
        authz = await browser.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": CLIENT_REDIRECT,
                "state": "s",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": "openid profile email",
            },
        )
        txn_id = parse_qs(urlparse(authz.headers["location"]).query)["txn_id"][0]
        consent_page = await browser.get("/consent", params={"txn_id": txn_id})
        csrf_token = re.search(r'name="csrf_token" value="([^"]+)"', consent_page.text).group(1)
        await browser.post(
            "/consent",
            data={"txn_id": txn_id, "action": "approve", "csrf_token": csrf_token},
        )
        callback = await browser.get(
            "/auth/callback", params={"code": "upstream-code", "state": txn_id}
        )
        auth_code = parse_qs(urlparse(callback.headers["location"]).query)["code"][0]

        token = await browser.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "redirect_uri": CLIENT_REDIRECT,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        assert token.status_code == 200, token.text
        assert not token.json().get("refresh_token")


async def test_legacy_env_sign_in_flow_still_works(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Acceptance criterion 1 as an executed flow, not just construction
    identity: with the three shared-state env vars unset, the single-instance
    sign-in flow (register -> authorize -> consent -> callback -> token)
    completes on the default FastMCP file store."""
    import fastmcp

    for name in (
        "SPEKOAI_OAUTH_JWT_SIGNING_KEY",
        "SPEKOAI_OAUTH_REDIS_URL",
        "SPEKOAI_OAUTH_ADVERTISE_OFFLINE_ACCESS",
    ):
        monkeypatch.delenv(name, raising=False)
    # Keep the default FileTreeStore out of the real user data dir.
    monkeypatch.setattr(fastmcp.settings, "home", tmp_path)

    app, _proxy, _ = _build_instance(monkeypatch, None, issue_refresh=False)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL
    ) as browser:
        meta = (await browser.get("/.well-known/oauth-authorization-server")).json()
        assert not meta.get("scopes_supported")  # legacy: nothing advertised

        reg = await browser.post(
            "/register",
            json={
                "redirect_uris": [CLIENT_REDIRECT],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
        )
        assert reg.status_code == 201, reg.text
        client_id = reg.json()["client_id"]

        # Legacy clients send no scope (nothing is advertised).
        verifier, challenge = _pkce_pair()
        authz = await browser.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": CLIENT_REDIRECT,
                "state": "s",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        assert authz.status_code == 302, authz.text
        txn_id = parse_qs(urlparse(authz.headers["location"]).query)["txn_id"][0]
        consent_page = await browser.get("/consent", params={"txn_id": txn_id})
        assert consent_page.status_code == 200, consent_page.text
        csrf_token = re.search(r'name="csrf_token" value="([^"]+)"', consent_page.text).group(1)
        approve = await browser.post(
            "/consent",
            data={"txn_id": txn_id, "action": "approve", "csrf_token": csrf_token},
        )
        assert approve.status_code == 302, approve.text
        callback = await browser.get(
            "/auth/callback", params={"code": "upstream-code", "state": txn_id}
        )
        assert callback.status_code == 302, callback.text
        auth_code = parse_qs(urlparse(callback.headers["location"]).query)["code"][0]

        token = await browser.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "redirect_uri": CLIENT_REDIRECT,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        assert token.status_code == 200, token.text
        assert token.json()["access_token"]  # sign-in works, no refresh token
        assert not token.json().get("refresh_token")
