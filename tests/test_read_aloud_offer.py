"""The read-aloud call offer on `audio.synthesize` results, and its 50/50 holdout."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

import spekoai_mcp.action_tools as action_tools
import spekoai_mcp.http_client as http_client
import spekoai_mcp.profiles as profiles
import spekoai_mcp.read_aloud_offer as read_aloud_offer
from spekoai_mcp import attribution
from spekoai_mcp.http_client import SpekoRawResponse
from spekoai_mcp.profiles import BUILDER_PROFILE, DEFAULT_PROFILE_ENV_VAR
from spekoai_mcp.read_aloud_offer import (
    READ_ALOUD_OFFER_ENV_VAR,
    READ_ALOUD_OFFER_TEXT,
    experiment_arm,
    with_read_aloud_offer,
)

MP3 = b"ID3\x04\x00audio-bytes"
CLAUDE_UA = "claude-user/1.0"
# Fixed by the hash: these assertions guard against the bucketing changing.
OFFER_ORG = "org_1"
HOLDOUT_ORG = "org_3"
BASE_TEXT = f"Synthesized {len(MP3)} bytes of audio/mpeg."


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(attribution, "submit_event", captured.append)
    return captured


@pytest.fixture(autouse=True)
def synthesis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DEFAULT_PROFILE_ENV_VAR, raising=False)
    monkeypatch.setenv(READ_ALOUD_OFFER_ENV_VAR, "true")

    async def fake_raw(
        method: str, path: str, *, body: Any = None, timeout: float = 60.0
    ) -> SpekoRawResponse:
        return SpekoRawResponse(content=MP3, content_type="audio/mpeg")

    monkeypatch.setattr(http_client, "call_speko_api_raw", fake_raw)


def _sign_in(monkeypatch: pytest.MonkeyPatch, organization_id: str | None) -> None:
    claims = {"organization_id": organization_id} if organization_id else {}
    token = SimpleNamespace(claims=claims, client_id="oauth-client", subject="user_1")
    monkeypatch.setattr(read_aloud_offer, "get_access_token", lambda: token)
    monkeypatch.setattr(profiles, "get_access_token", lambda: token)


@pytest.fixture
def user_agent(monkeypatch: pytest.MonkeyPatch) -> Any:
    def set_ua(value: str) -> None:
        monkeypatch.setattr(read_aloud_offer, "current_client_ua", lambda: value)

    return set_ua


async def _synthesize() -> Any:
    return await action_tools.synthesize_speech(
        {"text": "Your table is ready.", "intent": {"language": "en"}}
    )


async def test_claude_hosted_offer_arm_appends_the_line_and_logs_exposure(
    monkeypatch: pytest.MonkeyPatch, user_agent: Any, events: list[dict[str, Any]]
) -> None:
    user_agent(CLAUDE_UA)
    _sign_in(monkeypatch, OFFER_ORG)

    out = await _synthesize()

    assert out.content[0].text == f"{BASE_TEXT}\n{READ_ALOUD_OFFER_TEXT}"
    assert out.structured_content["content_type"] == "audio/mpeg"
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "mcp_experiment_exposure"
    assert event["experiment"] == "read_aloud_call_offer_v1"
    assert event["arm"] == "offer"
    assert event["organization_id"] == OFFER_ORG
    assert event["execution_client"] == "claude_hosted"
    assert event["tool"] == "audio.synthesize"


async def test_holdout_arm_appends_nothing_but_still_logs_exposure(
    monkeypatch: pytest.MonkeyPatch, user_agent: Any, events: list[dict[str, Any]]
) -> None:
    user_agent(CLAUDE_UA)
    _sign_in(monkeypatch, HOLDOUT_ORG)

    out = await _synthesize()

    assert out.content[0].text == BASE_TEXT
    assert [(e["arm"], e["organization_id"]) for e in events] == [("holdout", HOLDOUT_ORG)]


@pytest.mark.parametrize("ua", ["claude-code/2.0.1", "openai-mcp/1.0.0", "Cursor/3.18.9", ""])
async def test_non_claude_hosted_client_gets_nothing(
    monkeypatch: pytest.MonkeyPatch, user_agent: Any, events: list[dict[str, Any]], ua: str
) -> None:
    user_agent(ua)
    _sign_in(monkeypatch, OFFER_ORG)

    out = await _synthesize()

    assert out.content[0].text == BASE_TEXT
    assert events == []


@pytest.mark.parametrize("value", [None, "", "false", "0", "off"])
async def test_flag_off_gets_nothing(
    monkeypatch: pytest.MonkeyPatch,
    user_agent: Any,
    events: list[dict[str, Any]],
    value: str | None,
) -> None:
    if value is None:
        monkeypatch.delenv(READ_ALOUD_OFFER_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(READ_ALOUD_OFFER_ENV_VAR, value)
    user_agent(CLAUDE_UA)
    _sign_in(monkeypatch, OFFER_ORG)

    out = await _synthesize()

    assert out.content[0].text == BASE_TEXT
    assert events == []


async def test_no_organization_gets_nothing(
    monkeypatch: pytest.MonkeyPatch, user_agent: Any, events: list[dict[str, Any]]
) -> None:
    user_agent(CLAUDE_UA)
    _sign_in(monkeypatch, None)

    out = await _synthesize()

    assert out.content[0].text == BASE_TEXT
    assert events == []


async def test_profile_without_phone_calls_gets_nothing(
    monkeypatch: pytest.MonkeyPatch, user_agent: Any, events: list[dict[str, Any]]
) -> None:
    """The line never names a tool this deployment does not serve."""
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, BUILDER_PROFILE)
    user_agent(CLAUDE_UA)
    _sign_in(monkeypatch, OFFER_ORG)

    out = await _synthesize()

    assert out.content[0].text == BASE_TEXT
    assert events == []


def test_bucketing_is_stable_and_roughly_even() -> None:
    assert experiment_arm(OFFER_ORG) == "offer"
    assert experiment_arm(HOLDOUT_ORG) == "holdout"
    org = str(uuid.uuid4())
    assert len({experiment_arm(org) for _ in range(50)}) == 1
    arms = [experiment_arm(str(uuid.UUID(int=i))) for i in range(10_000)]
    assert 0.47 < arms.count("offer") / len(arms) < 0.53


def test_a_failure_inside_the_offer_never_changes_the_result(
    monkeypatch: pytest.MonkeyPatch, user_agent: Any
) -> None:
    user_agent(CLAUDE_UA)

    def boom() -> None:
        raise RuntimeError("token lookup failed")

    monkeypatch.setattr(read_aloud_offer, "get_access_token", boom)
    original = action_tools._synthesis_result(MP3, content_type="audio/mpeg", sample_rate=None)

    assert with_read_aloud_offer(original) is original
