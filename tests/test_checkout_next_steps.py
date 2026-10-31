"""Billing blocks offer a checkout link where the session can create one.

When an agent gave a `billing.checkout.create` link, 84% of workspaces paid
within a day (prod 2026-10-12..25), but only 18% of the workspaces a 402
blocked were ever given one: no message named the tool. Every billing next
step now offers the link on a surface that serves the tool to this session
(customer and muse, OAuth sign-in) and keeps the billing page everywhere else.
No step suggests an amount: the user picks it.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import httpx
import pytest
from fastmcp.exceptions import ToolError

import spekoai_mcp.http_client as http_client
import spekoai_mcp.profiles as profiles
from spekoai_mcp.action_tools import (
    CREDIT_EXHAUSTED_NEXT_STEP,
    DESTINATION_REQUIRES_CARD_NEXT_STEP,
    PHONE_NUMBER_PROVISIONING_CREDITS_NEXT_STEP,
    SPENDING_CAP_REACHED_NEXT_STEP,
    next_step_for_error,
)
from spekoai_mcp.credit_exhaustion import balance_result_text
from spekoai_mcp.profiles import DEFAULT_PROFILE_ENV_VAR
from spekoai_mcp.server import create_server

# (profile, auth_method, can create a checkout link)
SURFACES = [
    ("customer", "oauth", True),
    ("muse", "oauth", True),
    ("customer", "api_key", False),
    ("chatgpt", "oauth", False),
    ("connector", "oauth", False),
    ("builder", "oauth", False),
    ("replit", "oauth", False),
    (None, "oauth", False),  # unset: local development and self-hosting
]


def _use(monkeypatch: pytest.MonkeyPatch, profile: str | None, auth_method: str) -> None:
    if profile is None:
        monkeypatch.delenv(DEFAULT_PROFILE_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, profile)
    token = SimpleNamespace(claims={"auth_method": auth_method})
    monkeypatch.setattr(profiles, "get_access_token", lambda: token)


def _error(code: str, *, balance_usd: float | None = None) -> http_client.SpekoApiError:
    return http_client.SpekoApiError(
        402, "refused", trace_id="req_402", code=code, balance_usd=balance_usd
    )


def _assert_no_suggested_amount(text: str) -> None:
    # The allowed range is fine; a nudge toward one amount is not.
    amounts = set(re.findall(r"\$[\d,]+", text))
    assert amounts <= {"$5", "$10,000", "$0"}, amounts


@pytest.mark.parametrize(("profile", "auth_method", "checkout"), SURFACES)
def test_checkout_offered_only_where_the_session_can_call_it(
    monkeypatch: pytest.MonkeyPatch, profile: str | None, auth_method: str, checkout: bool
) -> None:
    _use(monkeypatch, profile, auth_method)
    assert http_client.checkout_available() is checkout
    assert profiles.session_can_call("billing.checkout.create") is checkout


@pytest.mark.parametrize(("profile", "auth_method", "checkout"), SURFACES)
def test_card_required_step(
    monkeypatch: pytest.MonkeyPatch, profile: str | None, auth_method: str, checkout: bool
) -> None:
    _use(monkeypatch, profile, auth_method)
    step = next_step_for_error(_error("DESTINATION_REQUIRES_CARD"), path="/v1/sessions/phone")
    assert "One top-up of any amount" in step
    assert http_client.BILLING_URL in step
    assert "Adding credit alone" not in step
    _assert_no_suggested_amount(step)
    if checkout:
        assert http_client.CHECKOUT_STEP in step
        assert http_client.AFTER_PAYMENT_STEP in step
    else:
        assert step == DESTINATION_REQUIRES_CARD_NEXT_STEP
        assert "billing.checkout.create" not in step


@pytest.mark.parametrize(("profile", "auth_method", "checkout"), SURFACES)
def test_out_of_credit_message(
    monkeypatch: pytest.MonkeyPatch, profile: str | None, auth_method: str, checkout: bool
) -> None:
    _use(monkeypatch, profile, auth_method)
    exc = _error("INSUFFICIENT_CREDITS", balance_usd=-0.12)
    text = http_client.credit_exhausted_message(exc)
    assert text.startswith(
        "This Speko workspace is out of credit (balance $-0.12), so the Speko API refused "
        "the request with 402 INSUFFICIENT_CREDITS."
    )
    assert http_client.BILLING_URL in text
    assert text.endswith("trace_id=req_402")
    step = next_step_for_error(exc, path="/v1/sessions")
    if checkout:
        assert http_client.CHECKOUT_STEP in text
        assert http_client.AFTER_PAYMENT_STEP in text
        assert http_client.CHECKOUT_STEP in step
    else:
        # Byte-identical to the message before checkout links existed.
        assert text == (
            "This Speko workspace is out of credit (balance $-0.12), so the Speko API refused "
            "the request with 402 INSUFFICIENT_CREDITS. Tell the user to add credit at "
            f"{http_client.BILLING_URL} (a workspace owner or admin can top up or turn on "
            "auto top-up), then run this tool again. Retrying before credit is added fails "
            "the same way; trace_id=req_402"
        )
        assert step == CREDIT_EXHAUSTED_NEXT_STEP


@pytest.mark.parametrize(("profile", "auth_method", "checkout"), SURFACES)
def test_number_purchase_step(
    monkeypatch: pytest.MonkeyPatch, profile: str | None, auth_method: str, checkout: bool
) -> None:
    _use(monkeypatch, profile, auth_method)
    step = next_step_for_error(
        _error("PHONE_NUMBER_PROVISIONING_CREDITS_REQUIRED"), path="/v1/sessions/phone"
    )
    assert "Retry the Speko MCP request" not in step
    assert "dedicated phone number" in step
    _assert_no_suggested_amount(step)
    if checkout:
        assert http_client.CHECKOUT_STEP in step
    else:
        assert step == PHONE_NUMBER_PROVISIONING_CREDITS_NEXT_STEP


@pytest.mark.parametrize(("profile", "auth_method", "checkout"), SURFACES)
def test_spending_cap_step_never_points_at_the_billing_page(
    monkeypatch: pytest.MonkeyPatch, profile: str | None, auth_method: str, checkout: bool
) -> None:
    # billing.spending_cap.update has the same profiles and principals as checkout.
    _use(monkeypatch, profile, auth_method)
    step = next_step_for_error(_error("SPENDING_CAP_REACHED"), path="/v1/sessions/phone")
    assert "Adding credit does not clear it." in step
    assert "billing.spending_cap.update" in step
    assert http_client.BILLING_URL not in step
    assert "Retry the Speko MCP request" not in step
    if checkout:
        assert "Ask the user whether to raise or remove the cap" in step
    else:
        assert step == SPENDING_CAP_REACHED_NEXT_STEP


def test_a_402_from_a_provider_keeps_the_generic_step(monkeypatch: pytest.MonkeyPatch) -> None:
    # A proxied provider billing failure is not the workspace's billing block.
    _use(monkeypatch, "customer", "oauth")
    step = next_step_for_error(_error("PROVIDER_PAYMENT_REQUIRED"), path="/v1/synthesize")
    assert "billing.checkout.create" not in step


@pytest.mark.parametrize(("profile", "auth_method", "checkout"), SURFACES)
def test_zero_balance_read(
    monkeypatch: pytest.MonkeyPatch, profile: str | None, auth_method: str, checkout: bool
) -> None:
    _use(monkeypatch, profile, auth_method)
    text = balance_result_text({"kind": "result", "data": {"balanceUsd": 0}}, "{...}")
    assert text.startswith("Credit balance is $0.00. This workspace is out of credit")
    assert text.endswith("\n{...}")
    assert (http_client.CHECKOUT_STEP in text) is checkout
    assert http_client.BILLING_URL in text
    # A positive balance is just the number.
    assert balance_result_text({"data": {"balanceUsd": 3.5}}, "{...}") == "{...}"


@pytest.fixture
def card_required_api(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            402,
            json={
                "error": "Top up once to call this country.",
                "code": "DESTINATION_REQUIRES_CARD",
                "hint": "Free credit does not cover calls to this country.",
            },
            headers={"x-request-id": "req_402"},
        )

    monkeypatch.setattr(http_client, "get_access_token", lambda: SimpleNamespace(token="sk_test"))
    http_client._TEST_TRANSPORT = httpx.MockTransport(handler)
    try:
        yield paths
    finally:
        http_client._TEST_TRANSPORT = None


async def test_phone_call_tool_offers_the_link_on_the_customer_host(
    monkeypatch: pytest.MonkeyPatch, card_required_api: list[str]
) -> None:
    _use(monkeypatch, "customer", "oauth")
    with pytest.raises(ToolError) as raised:
        await create_server().call_tool(
            "sessions.phone.create", {"body": {"to": "+4915112345678", "agentId": "agent_1"}}
        )
    text = str(raised.value)
    assert "billing.checkout.create" in text
    assert "trace_id=req_402" in text
    assert card_required_api == ["/v1/sessions/phone"]
    _assert_no_suggested_amount(text)
