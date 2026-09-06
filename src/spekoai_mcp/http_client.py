"""Authenticated Speko API relay for hosted MCP action tools."""

from __future__ import annotations

import json
import os
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode
from uuid import uuid4

import httpx
from fastmcp.server.dependencies import get_access_token

from spekoai_mcp.delegation import DelegationError, platform_bearer_token

DEFAULT_API_BASE = "https://api.speko.dev"
DEFAULT_ROUTER_BASE = "https://router.speko.dev"

_TEST_TRANSPORT: httpx.AsyncBaseTransport | None = None
_CURRENT_ACTION_ID: ContextVar[str | None] = ContextVar("speko_mcp_action_id", default=None)

# The inbound MCP client's User-Agent, forwarded to Platform so analytics can
# name the HARNESS (Claude Code, Cursor, …) rather than only the auth identity.
#
# A ContextVar rather than a parameter for the same reason _CURRENT_ACTION_ID is
# one: the value is set once per request by the ASGI middleware and has to reach
# _platform_headers through every tool handler in between, generated and
# handwritten alike, without threading an argument through all of them. The
# server runs stateless_http=True, so there is no session to hang it on.
_CURRENT_CLIENT_UA: ContextVar[str | None] = ContextVar("speko_mcp_client_ua", default=None)


class SpekoAuthError(RuntimeError):
    """Raised when a private MCP tool is called without MCP auth."""


class SpekoApiError(RuntimeError):
    """Clean exception for upstream API failures."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        trace_id: str | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(f"Speko API returned {status_code}: {message}")
        self.status_code = status_code
        self.message = message
        self.trace_id = trace_id
        # The upstream's machine-readable error code when it published one, so
        # a caller can branch on WHY a request was refused rather than parsing
        # the human message. None when the body carried no code.
        self.code = code


@dataclass(frozen=True)
class SpekoRawResponse:
    content: bytes
    content_type: str


@dataclass(frozen=True)
class RouterAudioResponse:
    """Raw audio plus the route the Router reported serving it."""

    content: bytes
    content_type: str
    provider: str | None
    model: str | None


def get_api_base() -> str:
    return (
        os.environ.get("SPEKOAI_API_URL")
        or os.environ.get("SPEKO_API_BASE")
        or os.environ.get("SPEKOAI_BASE_URL")
        or DEFAULT_API_BASE
    ).rstrip("/")


def get_router_base() -> str:
    return (
        os.environ.get("SPEKOAI_ROUTER_URL")
        or os.environ.get("SPEKO_ROUTER_URL")
        or DEFAULT_ROUTER_BASE
    ).rstrip("/")


def router_bearer_token() -> str | None:
    """The caller's own Speko API key, or None when this session has none.

    The Router takes opaque Speko API keys and nothing else: its control plane
    resolves a `sk_live_` key through Platform's `/v1/auth/api-key-context`,
    which deliberately refuses dashboard sessions and OAuth-delegated users so
    they can never be used as service credentials. An MCP session authenticated
    with OAuth therefore holds no credential the Router will accept — it
    presents a short-lived delegation JWT minted for the Platform audience —
    and its work has to stay on the Platform endpoint.
    """
    token = getattr(get_access_token(), "token", None)
    return token if isinstance(token, str) and token.startswith("sk_") else None


def path_segment(value: str | int) -> str:
    return quote(str(value), safe="")


def with_query(path: str, query: dict[str, Any | None]) -> str:
    clean = {key: value for key, value in query.items() if value not in (None, "") and value != []}
    return f"{path}?{urlencode(clean, doseq=True)}" if clean else path


def _bearer_token() -> str:
    access_token = get_access_token()
    if access_token is None:
        raise SpekoAuthError(
            "This tool requires the authenticated SpekoAI MCP endpoint. "
            "Connect /mcp with OAuth or Authorization: Bearer <Speko API key>."
        )
    try:
        return platform_bearer_token(access_token)
    except DelegationError as exc:
        raise SpekoAuthError(str(exc)) from exc


def set_current_action_id(action_id: str) -> Token[str | None]:
    """Bind a handwritten MCP tool name to its downstream Platform calls."""
    return _CURRENT_ACTION_ID.set(action_id)


def reset_current_action_id(token: Token[str | None]) -> None:
    _CURRENT_ACTION_ID.reset(token)


def set_current_client_ua(user_agent: str) -> Token[str | None]:
    """Bind the inbound User-Agent for the duration of one request."""
    return _CURRENT_CLIENT_UA.set(user_agent)


def reset_current_client_ua(token: Token[str | None]) -> None:
    _CURRENT_CLIENT_UA.reset(token)


def _platform_headers(
    *,
    action_id: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the common provenance envelope for every Platform relay."""
    from spekoai_mcp.profiles import current_profile

    access_token = get_access_token()
    client_id = getattr(access_token, "client_id", None)
    resolved_action_id = action_id or _CURRENT_ACTION_ID.get()
    headers = {
        "Authorization": f"Bearer {_bearer_token()}",
        "X-Speko-Source": "mcp",
        "X-Speko-MCP-Profile": current_profile() or "default",
        "X-Speko-Client": client_id if isinstance(client_id, str) else "unknown-mcp-client",
        # NEW, and deliberately NOT a redefinition of X-Speko-Client above.
        # That header is an auth identity by construction (an OAuth client_id or
        # "unknown-mcp-client") and is already persisted as
        # actionExecution.clientName, so repurposing it would silently change the
        # meaning of a live column. This one carries the harness instead; the
        # platform maps it to a readable bucket.
    }
    client_ua = _CURRENT_CLIENT_UA.get()
    if client_ua:
        headers["X-Speko-Client-UA"] = client_ua
    if resolved_action_id:
        headers["X-Speko-Action-Id"] = resolved_action_id
    if extra_headers:
        headers.update(extra_headers)
    return headers


def _error_details(resp: httpx.Response) -> tuple[str, str | None]:
    trace_id = resp.headers.get("x-request-id") or resp.headers.get("x-trace-id")
    try:
        payload = resp.json()
    except ValueError:
        return (resp.text.strip() or resp.reason_phrase)[:500], trace_id
    if isinstance(payload, dict):
        trace = payload.get("trace_id") or payload.get("traceId")
        if isinstance(trace, str) and trace:
            trace_id = trace
        detail = payload.get("error") or payload.get("message") or payload.get("detail")
        if isinstance(detail, dict):
            request_id = detail.get("requestId")
            if isinstance(request_id, str) and request_id:
                trace_id = request_id
            nested_message = detail.get("message")
            nested_code = detail.get("code")
            if isinstance(nested_message, str) and nested_message:
                prefix = f"{nested_code}: " if isinstance(nested_code, str) else ""
                return f"{prefix}{nested_message}"[:500], trace_id
        issues = _validation_issue_summary(payload.get("issues"))
        if isinstance(detail, str) and detail:
            if issues:
                return f"{detail}: {issues}"[:500], trace_id
            return detail[:500], trace_id
    return json.dumps(payload)[:500], trace_id


def _validation_issue_summary(value: Any) -> str | None:
    if not isinstance(value, list) or not value:
        return None
    parts: list[str] = []
    for item in value[:5]:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        message = item.get("message")
        if not isinstance(message, str) or not message:
            continue
        if isinstance(path, str) and path:
            parts.append(f"{path}: {message}")
        else:
            parts.append(message)
    if not parts:
        return None
    suffix = "" if len(value) <= 5 else f"; and {len(value) - 5} more"
    return "; ".join(parts) + suffix


async def _call_speko_api(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    api_base = get_api_base()
    url = f"{api_base}/{path.lstrip('/')}"
    try:
        async with httpx.AsyncClient(
            timeout=60.0,
            follow_redirects=True,
            transport=_TEST_TRANSPORT,
        ) as client:
            resp = await client.request(
                method.upper(),
                url,
                headers=_platform_headers(),
                json=body,
            )
    except httpx.HTTPError as exc:
        raise SpekoApiError(0, f"Unable to reach SpekoAI API at {api_base}: {exc}") from exc
    if resp.status_code >= 400:
        message, trace_id = _error_details(resp)
        raise SpekoApiError(resp.status_code, message, trace_id=trace_id)
    if not resp.content:
        return {}
    try:
        payload = resp.json()
    except ValueError as exc:
        raise SpekoApiError(resp.status_code, "Speko API returned a non-JSON response.") from exc
    if not isinstance(payload, dict):
        raise SpekoApiError(resp.status_code, "Speko API returned an unexpected JSON response.")
    return payload


async def call_speko_api(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return await _call_speko_api(method, path, body)


async def call_action(
    action_id: str,
    body: dict[str, Any],
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Invoke the generic Platform action endpoint with MCP provenance."""
    headers = _platform_headers(action_id=action_id)
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    api_base = get_api_base()
    url = f"{api_base}/v1/actions/{path_segment(action_id)}"
    try:
        async with httpx.AsyncClient(
            timeout=60.0,
            follow_redirects=True,
            transport=_TEST_TRANSPORT,
        ) as client:
            response = await client.request("POST", url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise SpekoApiError(0, f"Unable to reach SpekoAI API at {api_base}: {exc}") from exc
    if response.status_code >= 400:
        message, trace_id = _error_details(response)
        raise SpekoApiError(response.status_code, message, trace_id=trace_id)
    payload = response.json()
    if not isinstance(payload, dict):
        raise SpekoApiError(response.status_code, "Speko action returned an unexpected response.")
    return payload


async def call_speko_api_any(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> Any:
    api_base = get_api_base()
    url = f"{api_base}/{path.lstrip('/')}"
    try:
        async with httpx.AsyncClient(
            timeout=60.0,
            follow_redirects=True,
            transport=_TEST_TRANSPORT,
        ) as client:
            resp = await client.request(
                method.upper(),
                url,
                headers=_platform_headers(),
                json=body,
            )
    except httpx.HTTPError as exc:
        raise SpekoApiError(0, f"Unable to reach SpekoAI API at {api_base}: {exc}") from exc
    if resp.status_code >= 400:
        message, trace_id = _error_details(resp)
        raise SpekoApiError(resp.status_code, message, trace_id=trace_id)
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError as exc:
        raise SpekoApiError(resp.status_code, "Speko API returned a non-JSON response.") from exc


async def _call_speko_api_raw(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> SpekoRawResponse:
    api_base = get_api_base()
    url = f"{api_base}/{path.lstrip('/')}"
    try:
        async with httpx.AsyncClient(
            timeout=60.0,
            follow_redirects=True,
            transport=_TEST_TRANSPORT,
        ) as client:
            resp = await client.request(
                method.upper(),
                url,
                headers=_platform_headers(),
                json=body,
            )
    except httpx.HTTPError as exc:
        raise SpekoApiError(0, f"Unable to reach SpekoAI API at {api_base}: {exc}") from exc
    if resp.status_code >= 400:
        message, trace_id = _error_details(resp)
        raise SpekoApiError(resp.status_code, message, trace_id=trace_id)
    return SpekoRawResponse(
        content=resp.content,
        content_type=resp.headers.get("content-type", "application/octet-stream"),
    )


async def call_speko_api_raw(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> SpekoRawResponse:
    return await _call_speko_api_raw(method, path, body)


async def post_speko_api_bytes(
    path: str,
    payload: bytes,
    *,
    content_type: str,
    extra_headers: dict[str, str] | None = None,
) -> SpekoRawResponse:
    """POST a raw body (audio) and return the raw response.

    `/v1/transcribe` takes the audio in the request body rather than JSON, and
    carries its routing intent in an `X-Speko-Intent` header, so neither
    `_call_speko_api` (JSON in, JSON out) nor `_call_speko_api_raw` (JSON in,
    bytes out) fits.
    """
    api_base = get_api_base()
    url = f"{api_base}/{path.lstrip('/')}"
    headers = _platform_headers(
        extra_headers={"Content-Type": content_type, **(extra_headers or {})}
    )
    try:
        async with httpx.AsyncClient(
            timeout=120.0,
            follow_redirects=True,
            transport=_TEST_TRANSPORT,
        ) as client:
            resp = await client.post(url, headers=headers, content=payload)
    except httpx.HTTPError as exc:
        raise SpekoApiError(0, f"Unable to reach SpekoAI API at {api_base}: {exc}") from exc
    if resp.status_code >= 400:
        message, trace_id = _error_details(resp)
        raise SpekoApiError(resp.status_code, message, trace_id=trace_id)
    return SpekoRawResponse(
        content=resp.content,
        content_type=resp.headers.get("content-type", "application/octet-stream"),
    )


def _router_user_agent() -> str:
    """A non-empty User-Agent. The Router answers a request without one 403."""
    client_ua = _CURRENT_CLIENT_UA.get()
    return f"spekoai-mcp ({client_ua})" if client_ua else "spekoai-mcp"


def _router_error_code(resp: httpx.Response) -> str | None:
    """The Router's `error.code`, or Platform's bare string `error`."""
    try:
        payload = resp.json()
    except ValueError:
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        code = error.get("code")
        return code if isinstance(code, str) and code else None
    return error if isinstance(error, str) and error else None


async def post_router_transcription(
    audio: bytes,
    *,
    request_payload: dict[str, Any],
    token: str,
    content_type: str = "audio/wav",
) -> dict[str, Any]:
    """POST one batch transcription to the Router and return its JSON.

    Three parts of this envelope are load-bearing and each fails in a way that
    names something else: `Idempotency-Key` is mandatory on every Router path
    (a missing one is a 400 raised before the body is read), a `User-Agent` must
    be present (a missing one is a bare 403 that explains nothing), and the two
    multipart parts have to be named `request` and `audio`.
    """
    base = get_router_base()
    url = f"{base}/v1/stt/transcriptions"
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": uuid4().hex,
        "User-Agent": _router_user_agent(),
        "Accept": "application/json",
    }
    files = {
        "request": (None, json.dumps(request_payload), "application/json"),
        "audio": ("audio.wav", audio, content_type),
    }
    try:
        async with httpx.AsyncClient(
            timeout=120.0,
            follow_redirects=True,
            transport=_TEST_TRANSPORT,
        ) as client:
            response = await client.post(url, headers=headers, files=files)
    except httpx.HTTPError as exc:
        raise SpekoApiError(0, f"Unable to reach the Speko Router at {base}: {exc}") from exc
    if response.status_code >= 400:
        message, trace_id = _error_details(response)
        raise SpekoApiError(
            response.status_code,
            message,
            trace_id=trace_id,
            code=_router_error_code(response),
        )
    payload = response.json()
    if not isinstance(payload, dict):
        raise SpekoApiError(
            response.status_code, "The Speko Router returned an unexpected response."
        )
    return payload


async def post_router_speech(
    request_payload: dict[str, Any],
    *,
    token: str,
) -> RouterAudioResponse:
    """POST one synthesis to the Router and return the audio it streams back.

    The response is a bare audio stream, so the route comes back in headers
    rather than a JSON envelope. `Idempotency-Key` and a non-empty `User-Agent`
    are as mandatory here as on every other Router path.
    """
    base = get_router_base()
    url = f"{base}/v1/tts/speech"
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": uuid4().hex,
        "User-Agent": _router_user_agent(),
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(
            timeout=120.0,
            follow_redirects=True,
            transport=_TEST_TRANSPORT,
        ) as client:
            response = await client.post(url, headers=headers, json=request_payload)
    except httpx.HTTPError as exc:
        raise SpekoApiError(0, f"Unable to reach the Speko Router at {base}: {exc}") from exc
    if response.status_code >= 400:
        message, trace_id = _error_details(response)
        raise SpekoApiError(
            response.status_code,
            message,
            trace_id=trace_id,
            code=_router_error_code(response),
        )
    return RouterAudioResponse(
        content=response.content,
        content_type=response.headers.get("content-type", "application/octet-stream"),
        provider=response.headers.get("speko-provider"),
        model=response.headers.get("speko-model"),
    )


def tool_error_message(exc: Exception, *, next_step: str) -> str:
    trace_id = getattr(exc, "trace_id", None) or "unavailable"
    return f"{exc}; trace_id={trace_id}; next_step={next_step}"


async def get_balance() -> dict[str, Any]:
    return await _call_speko_api("GET", "/v1/credits/balance")


async def create_agent(payload: dict[str, Any]) -> dict[str, Any]:
    return await _call_speko_api("POST", "/v1/agents", payload)


async def build_session_config(body: dict[str, Any]) -> dict[str, Any]:
    return await _call_speko_api("POST", "/v1/inference/sessionconfig", body)


async def parse_config(format_: str, raw: str) -> dict[str, Any]:
    return await _call_speko_api(
        "POST", "/v1/inference/parse-config", {"format": format_, "raw": raw}
    )


async def inspect_workspace(body: dict[str, Any]) -> dict[str, Any]:
    return await _call_speko_api("POST", "/v1/inference/inspect", body)


async def deploy_agent(
    agent_id: str,
    session_config: dict[str, Any],
    *,
    briefing_markdown: str | None = None,
) -> dict[str, Any]:
    return await _call_speko_api(
        "POST",
        f"/v1/agents/{path_segment(agent_id)}/deploy",
        {"session_config": session_config, "briefing_markdown": briefing_markdown},
    )


async def rollback_agent(agent_id: str, target_version_number: int) -> dict[str, Any]:
    return await _call_speko_api(
        "POST",
        f"/v1/agents/{path_segment(agent_id)}/rollback",
        {"target_version_number": target_version_number},
    )


async def create_test_session(
    *,
    agent_id: str | None,
    session_config: dict[str, Any] | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"mode": "cascade", "metadata": {"source": "mcp"}}
    if agent_id:
        body["agentId"] = agent_id
    if session_config:
        intent = session_config.get("intent")
        if isinstance(intent, dict):
            body["intent"] = intent
        for source_key, target_key in [
            ("voice", "voice"),
            ("systemPrompt", "systemPrompt"),
            ("firstMessage", "firstMessage"),
            ("sttOptions", "sttOptions"),
        ]:
            if source_key in session_config and session_config[source_key] is not None:
                body[target_key] = session_config[source_key]
        llm = session_config.get("llmOptions")
        if isinstance(llm, dict):
            body["llm"] = llm
        stack = session_config.get("stackPreferences")
        if isinstance(stack, dict):
            body["constraints"] = stack
    return await _call_speko_api("POST", "/v1/sessions", body)


async def list_agent_calls(agent_id: str, *, since: str | None, limit: int) -> dict[str, Any]:
    path = with_query(
        f"/v1/agents/{path_segment(agent_id)}/calls",
        {"since": since, "limit": limit},
    )
    return await _call_speko_api("GET", path)


async def get_call(call_id: str) -> dict[str, Any]:
    return await _call_speko_api("GET", f"/v1/calls/{path_segment(call_id)}")


async def list_agent_evals(agent_id: str) -> dict[str, Any]:
    return await _call_speko_api("GET", f"/v1/agents/{path_segment(agent_id)}/evals")


async def add_agent_eval(agent_id: str, body: dict[str, Any]) -> dict[str, Any]:
    return await _call_speko_api("POST", f"/v1/agents/{path_segment(agent_id)}/evals", body)


async def run_agent_eval(agent_id: str, eval_id: str) -> dict[str, Any]:
    return await _call_speko_api(
        "POST",
        f"/v1/agents/{path_segment(agent_id)}/evals/{path_segment(eval_id)}/run",
        {},
    )


async def render_agent_briefing(
    *,
    agent_id: str,
    template_id: str,
    version_id: str | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"agent_id": agent_id, "template_id": template_id}
    if version_id:
        body["version_id"] = version_id
    return await _call_speko_api("POST", "/v1/inference/briefing", body)


async def create_share_card(build_id: str, *, title: str | None = None) -> SpekoRawResponse:
    body = {"title": title} if title else {}
    return await _call_speko_api_raw(
        "POST", f"/v1/share/build/{path_segment(build_id)}/card.png", body
    )
