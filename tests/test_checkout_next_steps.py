"""Payment refusals read the same on every tool and say how this session pays.

A 402 that a top-up clears (`INSUFFICIENT_CREDITS`, `DESTINATION_REQUIRES_CARD`,
`PHONE_NUMBER_PROVISIONING_CREDITS_REQUIRED`, `PHONE_NUMBER_CARD_REQUIRED`)
names its cause, then the one-tap `topUpUrl` where the surface may show one.
Without a link it names the tool or page this session can use. ChatGPT gets
neither a link nor a billing URL: OpenAI bans checkout initiation in a plugin.
The text states facts and names tools, and never suggests an amount.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import httpx
import pytest
from fastmcp.exceptions import ToolError

import spekoai_mcp.http_client as http_client
import spekoai_mcp.profiles as profiles
from spekoai_mcp import payment
from spekoai_mcp.action_tools import SPENDING_CAP_REACHED_NEXT_STEP, next_step_for_error
from spekoai_mcp.credit_exhaustion import balance_result_text
from spekoai_mcp.profiles import DEFAULT_PROFILE_ENV_VAR
from spekoai_mcp.server import create_server

TOP_UP_URL = "https://platform.speko.ai/v1/public/top-up/tu1.claims.mac"
TOOL = payment.TOP_UP_LINK_TOOL
# Buckets the server computes the same way (`payment-wall.spec.ts`).
TEXT_ORG = "org_0"
BUTTON_ORG = "org_6"

# (profile, auth_method, may show a link, serves the top-up link tool)
SURFACES = [
    ("customer", "oauth", True, True),
    ("customer", "api_key", True, True),
    ("muse", "oauth", True, True),
    ("connector", "oauth", True, True),
    ("chatgpt", "oauth", False, False),
    ("builder", "oauth", False, False),
    ("replit", "oauth", False, False),
    (None, "oauth", False, True),  # unset: local development and self-hosting
]


def _use(
    monkeypatch: pytest.MonkeyPatch,
    profile: str | None,
    auth_method: str,
    organization_id: str = TEXT_ORG,
) -> None:
    if profile is None:
        monkeypatch.delenv(DEFAULT_PROFILE_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, profile)
    token = SimpleNamespace(claims={"auth_method": auth_method, "organization_id": organization_id})
    monkeypatch.setattr(profiles, "get_access_token", lambda: token)


def _error(
    code: str, *, balance_usd: float | None = None, top_up_url: str | None = None
) -> http_client.SpekoApiError:
    return http_client.SpekoApiError(
        402,
        "refused",
        trace_id="req_402",
        code=code,
        balance_usd=balance_usd,
        top_up_url=top_up_url,
    )


def _assert_no_suggested_amount(text: str) -> None:
    # The allowed range is fine; a nudge toward one amount is not.
    amounts = set(re.findall(r"\$\d{1,3}(?:,\d{3})*(?:\.\d{2})?", text))
    assert amounts <= {"$5", "$10,000", "$0", "$0.09", "$-0.12", "$0.00"}, amounts


def _assert_no_script(text: str) -> None:
    # Facts and tool names, not instructions to the assistant.
    for phrase in ("Tell the user", "Ask the user", "Give the user", "Do not retry"):
        assert phrase not in text


PAYMENT_CODES = sorted(payment.PAYMENT_CODES)


@pytest.mark.parametrize(("profile", "auth_method", "linkable", "tool"), SURFACES)
@pytest.mark.parametrize("code", PAYMENT_CODES)
def test_payment_refusal_without_a_link(
    monkeypatch: pytest.MonkeyPatch,
    profile: str | None,
    auth_method: str,
    linkable: bool,
    tool: bool,
    code: str,
) -> None:
    _use(monkeypatch, profile, auth_method)
    assert profiles.session_can_call(TOOL) is tool
    step = next_step_for_error(_error(code), path="/v1/sessions/phone")
    assert "Retry the Speko MCP request" not in step
    assert "returns the same refusal" in step
    _assert_no_suggested_amount(step)
    _assert_no_script(step)
    if tool:
        assert TOOL in step
    elif profile == "chatgpt":
        assert "Speko console" in step
        assert http_client.BILLING_URL not in step
        assert "billing.checkout.create" not in step
    else:
        assert http_client.BILLING_URL in step


@pytest.mark.parametrize(("profile", "auth_method", "linkable", "tool"), SURFACES)
@pytest.mark.parametrize("code", PAYMENT_CODES)
def test_payment_refusal_with_a_link(
    monkeypatch: pytest.MonkeyPatch,
    profile: str | None,
    auth_method: str,
    linkable: bool,
    tool: bool,
    code: str,
) -> None:
    _use(monkeypatch, profile, auth_method)
    step = next_step_for_error(_error(code, top_up_url=TOP_UP_URL), path="/v1/sessions/phone")
    _assert_no_suggested_amount(step)
    _assert_no_script(step)
    assert (TOP_UP_URL in step) is linkable
    if linkable:
        assert "picks the amount, from $5" in step
        assert http_client.BILLING_URL not in step


def test_card_required_cause() -> None:
    step = payment.cause(_error("DESTINATION_REQUIRES_CARD"))
    assert "Free credit covers calls in the US and Canada." in step
    assert "one top-up of any amount, from $5" in step
    assert "Buying a phone number does not unlock this call." in step


@pytest.mark.parametrize(("organization_id", "button"), [(TEXT_ORG, False), (BUTTON_ORG, True)])
def test_button_line_follows_the_experiment_bucket(
    monkeypatch: pytest.MonkeyPatch, organization_id: str, button: bool
) -> None:
    _use(monkeypatch, "customer", "oauth", organization_id)
    step = next_step_for_error(
        _error("INSUFFICIENT_CREDITS", top_up_url=TOP_UP_URL), path="/v1/sessions"
    )
    assert TOP_UP_URL in step
    assert ("as an Add credit button" in step) is button


def test_buckets_are_stable_and_split() -> None:
    assert payment.paywall_arm(TEXT_ORG) == payment.TEXT_ARM
    assert payment.paywall_arm(BUTTON_ORG) == payment.BUTTON_ARM
    buttons = sum(payment.paywall_arm(f"org_{i}") == payment.BUTTON_ARM for i in range(10_000))
    assert 4_700 < buttons < 5_300


def test_out_of_credit_message_on_the_customer_host(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, "customer", "oauth")
    exc = _error("INSUFFICIENT_CREDITS", balance_usd=-0.12, top_up_url=TOP_UP_URL)
    text = http_client.credit_exhausted_message(exc)
    assert text.startswith(
        "This Speko workspace is out of credit (balance $-0.12), so the Speko API refused "
        "the request with 402 INSUFFICIENT_CREDITS."
    )
    assert TOP_UP_URL in text
    assert text.endswith("trace_id=req_402")
    _assert_no_script(text)


@pytest.mark.parametrize(("profile", "auth_method", "linkable", "tool"), SURFACES)
def test_spending_cap_step_never_points_at_the_billing_page(
    monkeypatch: pytest.MonkeyPatch,
    profile: str | None,
    auth_method: str,
    linkable: bool,
    tool: bool,
) -> None:
    _use(monkeypatch, profile, auth_method)
    step = next_step_for_error(
        _error("SPENDING_CAP_REACHED", top_up_url=TOP_UP_URL), path="/v1/sessions/phone"
    )
    assert "Adding credit does not clear it." in step
    assert "billing.spending_cap.update" in step
    assert http_client.BILLING_URL not in step
    assert TOP_UP_URL not in step
    if not profiles.session_can_call("billing.spending_cap.update"):
        assert step == SPENDING_CAP_REACHED_NEXT_STEP


def test_a_402_from_a_provider_keeps_the_generic_step(monkeypatch: pytest.MonkeyPatch) -> None:
    # A proxied provider billing failure is not the workspace's billing block.
    _use(monkeypatch, "customer", "oauth")
    step = next_step_for_error(_error("PROVIDER_PAYMENT_REQUIRED"), path="/v1/synthesize")
    assert TOOL not in step
    assert "billing.checkout.create" not in step


@pytest.mark.parametrize(("profile", "auth_method", "linkable", "tool"), SURFACES)
def test_zero_balance_read(
    monkeypatch: pytest.MonkeyPatch,
    profile: str | None,
    auth_method: str,
    linkable: bool,
    tool: bool,
) -> None:
    _use(monkeypatch, profile, auth_method)
    text = balance_result_text({"kind": "result", "data": {"balanceUsd": 0}}, "{...}")
    assert text.startswith("Credit balance is $0.00. This workspace is out of credit")
    assert text.endswith("\n{...}")
    assert (TOOL in text) is tool
    _assert_no_script(text)
    # A positive balance is just the number.
    assert balance_result_text({"data": {"balanceUsd": 3.5}}, "{...}") == "{...}"


@pytest.fixture
def platform(monkeypatch: pytest.MonkeyPatch):
    state = SimpleNamespace(paths=[], bodies=[], reply=(200, {}))

    def handler(request: httpx.Request) -> httpx.Response:
        state.paths.append(request.url.path)
        state.bodies.append(request.content)
        status, body = state.reply
        return httpx.Response(status, json=body, headers={"x-request-id": "req_1"})

    monkeypatch.setattr(http_client, "get_access_token", lambda: SimpleNamespace(token="sk_test"))
    http_client._TEST_TRANSPORT = httpx.MockTransport(handler)
    try:
        yield state
    finally:
        http_client._TEST_TRANSPORT = None


def _card_required(top_up_url: str | None) -> tuple[int, dict]:
    body = {
        "error": "Top up once to call China.",
        "code": "DESTINATION_REQUIRES_CARD",
        "reason": "abroad",
        "country": "CN",
        "hint": "Free credit covers calls in the US and Canada.",
        "billingUrl": "/settings/billing",
    }
    if top_up_url is not None:
        body["topUpUrl"] = top_up_url
    return 402, body


@pytest.mark.parametrize(
    ("top_up_url", "kept"),
    [
        (TOP_UP_URL, True),
        ("https://PLATFORM.speko.ai/v1/public/top-up/tu1.a.b", True),
        ("https://platform-staging.speko.dev/v1/public/top-up/tu1.a.b", True),
        ("http://platform.speko.ai/v1/public/top-up/tu1.a.b", False),
        ("https://evil.example/v1/public/top-up/tu1.a.b", False),
        ("https://platform.speko.ai.evil.example/top-up", False),
        ("https://platform.speko.ai@evil.example/top-up", False),
        ("https://evil.example\\@platform.speko.ai/v1/public/top-up/tu1.a.b", False),
        ("https://platform.speko.ai/v1/public/top-up/tu1.a.b\nhttps://evil.example", False),
        ("https://platform.speko.ai:8443/v1/public/top-up/tu1.a.b", False),
        ("https://user:pass@platform.speko.ai/v1/public/top-up/tu1.a.b", False),
        ("https://platform.speko.ai/v1/public/pay/tu1.a.b", False),
        ("https://platform.speko.ai/v1/public/top-up/tu1.a.b?amount=5", True),
        ("https://platform.speko.ai/v1/public/top-up/tu1.a.b?next=https://evil.example", False),
        ("/v1/public/top-up/tu1.a.b", False),
        ("javascript:alert(1)", False),
        (None, False),
    ],
)
async def test_api_error_keeps_only_a_dashboard_top_up_link(
    platform: SimpleNamespace, top_up_url: str | None, kept: bool
) -> None:
    platform.reply = _card_required(top_up_url)
    with pytest.raises(http_client.SpekoApiError) as raised:
        await http_client.call_speko_api("POST", "/v1/sessions/phone", {})
    assert raised.value.code == "DESTINATION_REQUIRES_CARD"
    assert raised.value.top_up_url == (top_up_url if kept else None)


async def test_raw_error_fallback_leaves_out_a_dropped_top_up_link(
    platform: SimpleNamespace,
) -> None:
    platform.reply = (
        402,
        {"code": "DESTINATION_REQUIRES_CARD", "topUpUrl": "https://evil.example/pay"},
    )
    with pytest.raises(http_client.SpekoApiError) as raised:
        await http_client.call_speko_api("POST", "/v1/sessions/phone", {})
    assert raised.value.top_up_url is None
    assert "DESTINATION_REQUIRES_CARD" in str(raised.value)
    assert "evil.example" not in str(raised.value)


async def test_phone_call_tool_gives_the_connector_user_the_link(
    monkeypatch: pytest.MonkeyPatch, platform: SimpleNamespace
) -> None:
    _use(monkeypatch, "connector", "oauth")
    platform.reply = _card_required(TOP_UP_URL)
    with pytest.raises(ToolError) as raised:
        await create_server().call_tool(
            "sessions.phone.create", {"body": {"to": "+8613800138000", "agentId": "agent_1"}}
        )
    text = str(raised.value)
    assert TOP_UP_URL in text
    assert "Buying a phone number does not unlock this call." in text
    assert "Top up once to call China." in text
    _assert_no_suggested_amount(text)


async def test_phone_call_tool_drops_a_foreign_link(
    monkeypatch: pytest.MonkeyPatch, platform: SimpleNamespace
) -> None:
    _use(monkeypatch, "connector", "oauth")
    platform.reply = _card_required("https://evil.example/pay")
    with pytest.raises(ToolError) as raised:
        await create_server().call_tool(
            "sessions.phone.create", {"body": {"to": "+8613800138000", "agentId": "agent_1"}}
        )
    text = str(raised.value)
    assert "evil.example" not in text
    assert TOOL in text


async def test_chatgpt_phone_refusal_has_no_payment_link(
    monkeypatch: pytest.MonkeyPatch, platform: SimpleNamespace
) -> None:
    _use(monkeypatch, "chatgpt", "oauth")
    platform.reply = _card_required(TOP_UP_URL)
    with pytest.raises(ToolError) as raised:
        await create_server().call_tool(
            "sessions.phone.create", {"body": {"to": "+8613800138000", "agentId": "agent_1"}}
        )
    text = str(raised.value)
    assert TOP_UP_URL not in text
    assert http_client.BILLING_URL not in text
    assert "Speko console" in text


async def test_manifest_tool_payment_refusal_reads_the_same(
    monkeypatch: pytest.MonkeyPatch, platform: SimpleNamespace
) -> None:
    # A manifest-generated tool used to surface this as FastMCP's raw
    # "Error calling tool ..." with no next step.
    _use(monkeypatch, "customer", "oauth")
    platform.reply = (
        402,
        {
            "error": {
                "code": "PHONE_NUMBER_PROVISIONING_CREDITS_REQUIRED",
                "message": "Add credits to buy a number.",
            },
            "topUpUrl": TOP_UP_URL,
        },
    )
    with pytest.raises(ToolError) as raised:
        await create_server().call_tool("receptionist.go_live", {"agent_id": "agent_1"})
    text = str(raised.value)
    assert "dedicated phone number" in text
    assert TOP_UP_URL in text
    assert "next_step=" in text


@pytest.mark.parametrize("profile", ["connector", "chatgpt", "customer"])
async def test_phone_call_tool_names_the_free_call(
    monkeypatch: pytest.MonkeyPatch, platform: SimpleNamespace, profile: str
) -> None:
    _use(monkeypatch, profile, "oauth")
    platform.reply = (201, {"sessionId": "sess_1", "freeCall": {"maxDurationSeconds": 120}})
    out = await create_server().call_tool(
        "sessions.phone.create", {"body": {"to": "+8613800138000", "agentId": "agent_1"}}
    )
    text = out.content[0].text
    assert "one free call abroad and it ends after 2 minutes" in text
    assert "sess_1" in text
    _assert_no_suggested_amount(text)


async def test_phone_call_tool_says_nothing_extra_on_an_ordinary_call(
    monkeypatch: pytest.MonkeyPatch, platform: SimpleNamespace
) -> None:
    _use(monkeypatch, "connector", "oauth")
    platform.reply = (201, {"sessionId": "sess_1"})
    out = await create_server().call_tool(
        "sessions.phone.create", {"body": {"to": "+12015551234", "agentId": "agent_1"}}
    )
    assert "free call" not in out.content[0].text


async def test_top_up_link_tool_returns_the_link_for_the_button(
    monkeypatch: pytest.MonkeyPatch, platform: SimpleNamespace
) -> None:
    _use(monkeypatch, "customer", "oauth")
    platform.reply = (
        200,
        {
            "url": TOP_UP_URL,
            "purpose": "generic",
            "expiresAt": "2026-11-16T00:00:00.000Z",
            "minAmountUsd": 5,
            "maxAmountUsd": 10000,
        },
    )
    out = await create_server().call_tool(TOOL, {})
    assert platform.paths == ["/v1/billing/top-up-link"]
    assert out.structured_content["url"] == TOP_UP_URL
    assert TOP_UP_URL in out.content[0].text


async def test_top_up_link_tool_refuses_a_foreign_link(
    monkeypatch: pytest.MonkeyPatch, platform: SimpleNamespace
) -> None:
    _use(monkeypatch, "customer", "oauth")
    platform.reply = (200, {"url": "https://evil.example/pay", "purpose": "generic"})
    with pytest.raises(ToolError) as raised:
        await create_server().call_tool(TOOL, {})
    assert "evil.example" not in str(raised.value)


@pytest.mark.parametrize(
    ("profile", "listed"),
    [
        ("customer", True),
        ("connector", True),
        ("muse", True),
        ("chatgpt", False),
        ("builder", False),
        ("replit", False),
    ],
)
async def test_top_up_link_tool_is_served_where_links_are_allowed(
    monkeypatch: pytest.MonkeyPatch, profile: str, listed: bool
) -> None:
    _use(monkeypatch, profile, "oauth")
    names = {tool.name for tool in await create_server().list_tools()}
    assert (TOOL in names) is listed


async def test_top_up_link_tool_renders_the_add_credit_card() -> None:
    tool = next(t for t in await create_server().list_tools() if t.name == TOOL)
    meta = tool.meta or {}
    assert "ui://speko/top-up.html" in str(meta)
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is False
    assert tool.annotations.destructiveHint is False


def test_add_credit_card_follows_the_mcp_apps_handshake() -> None:
    from importlib.resources import files

    html = (files("spekoai_mcp._apps") / "top_up.html").read_text(encoding="utf-8")
    # McpUiInitializeRequest requires these; the legacy pair rides along.
    assert "appInfo: APP_INFO" in html
    assert "appCapabilities: {}" in html
    assert 'request("ui/open-link", { url: url })' in html
    # The button only opens Speko's own one-tap links.
    assert "platform\\.speko\\.ai|platform-staging\\.speko\\.dev" in html
