"""Feedback returned for inline prompts sent to sessions.phone.create."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastmcp.exceptions import ToolError

from spekoai_mcp import attribution, http_client, voicemail_notice
from spekoai_mcp.action_tools import CREATE_PHONE_SESSION_BODY_DOC, phone_session_doc
from spekoai_mcp.phone_prompt_advice import SHAPE, advise
from spekoai_mcp.profiles import DEFAULT_PROFILE_ENV_VAR
from spekoai_mcp.server import create_server

STOCK_PROMPT = (
    "You are an AI assistant calling adidas Brand Center at 750 Burrard Street, Vancouver, on "
    "behalf of a customer. Introduce yourself truthfully as an AI assistant. This is an "
    "information-only stock and price inquiry. Confirm the branch is 750 Burrard. Ask staff to "
    "physically check current store stock for adidas Campus 00s, article HQ8708 (spell H Q 8 7 0 "
    "8 slowly if needed), Core Black / Cloud White / Off White, men's US size 10. Do not confuse "
    "with UK 10, women's 10, Campus 80s, another Campus model, or a different product code. Ask "
    "whether the exact pair is available to buy in this branch today, the in-store price in "
    "Canadian dollars before tax, and whether the store will match adidas.ca's current C$56 "
    "online price for this exact article. Official page "
    "https://www.adidas.ca/en/campus-00s-shoes/HQ8708.html currently displays C$56, down from "
    "C$140. Do not claim the store price is already confirmed. If in stock, confirm today's "
    "closing time. If unavailable, ask whether staff can check the same article and size at 848 "
    "Granville Street. Ask one brief question at a time. Allow staff time to check stock; remain "
    "on hold quietly and do not hang up during music or staff checking. Navigate phone menus if "
    "needed using available keypad tools. Do not buy, reserve, place an order, leave voicemail, "
    "provide personal data, or agree to charges. If you reach voicemail or the store is closed, "
    "end politely without leaving a message. Do not invent answers or treat online inventory as "
    "store inventory. Before ending, briefly read back the exact model, men's US 10, stock answer, "
    "price and matching answer so the staff can correct mistakes."
)
FIRST_QUESTION = (
    "Hi, I am an AI assistant calling for a customer. Could you check a shoe size in your store?"
)


def stock_call() -> dict[str, Any]:
    return {
        "to": "+16045550123",
        "intent": {"language": "en"},
        "systemPrompt": STOCK_PROMPT,
        "firstMessage": FIRST_QUESTION,
    }


def test_stock_inquiry_identifies_four_prompt_issues_in_priority_order() -> None:
    assert advise(stock_call()) == [
        "systemPrompt is long (1639 chars); phone calls go better with about 150 words: "
        "questions in order, facts to say only if asked, private notes.",
        "Move 'do not confuse ...' notes into a private-notes block the agent never says aloud; "
        "the agent can read them out.",
        "Ask the agent to say a code once and spell it only when asked; spelling instructions "
        "make it spell codes unprompted.",
        "Limit the read-back to one or two key facts at the end; a long read-back makes "
        "the callee confirm everything again.",
        SHAPE,
    ]


@pytest.mark.parametrize(
    "opener",
    [FIRST_QUESTION, "在庫はありますか？", "هل الحذاء متوفر؟"],
    ids=["english", "japanese", "arabic"],
)
def test_short_prompt_with_a_first_question_needs_no_advice(opener: str) -> None:
    body = {"systemPrompt": "Check whether the shoes are in stock.", "firstMessage": opener}
    assert advise(body) == []


def test_saved_agent_receives_no_inline_prompt_advice() -> None:
    assert advise({**stock_call(), "agentId": "agent_1"}) == []
    # A null agentId is still an inline prompt.
    assert advise({**stock_call(), "agentId": None})[-1] == SHAPE


def test_read_back_ignores_a_list_in_another_sentence() -> None:
    prompt = "Ask for the price, stock, and closing time. Read back the price."
    assert advise({"systemPrompt": prompt}) == []


def test_opener_without_a_question_gets_advice_when_there_is_room() -> None:
    opener = "Hello, I am an AI assistant calling for a customer."
    advice = advise({"systemPrompt": "Ask for the opening hours.", "firstMessage": opener})
    assert advice == [
        "firstMessage has no question; put your first question in it so a person who picks up "
        "hears why you called right away.",
        SHAPE,
    ]
    assert advise({**stock_call(), "firstMessage": opener}) == advise(stock_call())


@pytest.fixture
def phone_api(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(requests=[], reply=httpx.Response(201, json={"sessionId": "sess_1"}))

    def handler(request: httpx.Request) -> httpx.Response:
        state.requests.append((request.method, request.url.path, json.loads(request.content)))
        return state.reply

    monkeypatch.setattr(http_client, "get_access_token", lambda: SimpleNamespace(token="sk_test"))
    monkeypatch.setattr(http_client, "_TEST_TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.delenv(DEFAULT_PROFILE_ENV_VAR, raising=False)
    monkeypatch.delenv("SPEKO_MCP_PROMPT_ADVICE", raising=False)
    return state


@pytest.mark.parametrize("flag", [None, "off"], ids=["default", "off"])
async def test_advice_flag_only_changes_the_result(
    flag: str | None, monkeypatch: pytest.MonkeyPatch, phone_api: SimpleNamespace
) -> None:
    if flag is not None:
        monkeypatch.setenv("SPEKO_MCP_PROMPT_ADVICE", flag)
    monkeypatch.setenv(voicemail_notice.VOICEMAIL_NOTICE_ENV_VAR, "true")
    monkeypatch.setattr(voicemail_notice, "_SHOWN", voicemail_notice._ShownLog())
    token = SimpleNamespace(claims={"organization_id": "org_1"})
    monkeypatch.setattr(voicemail_notice, "get_access_token", lambda: token)
    monkeypatch.setattr(attribution, "submit_event", lambda event: None)
    free_call = {"maxDurationSeconds": 120}
    phone_api.reply = httpx.Response(201, json={"sessionId": "sess_1", "freeCall": free_call})

    out = await create_server().call_tool("sessions.phone.create", {"body": stock_call()})

    assert phone_api.requests == [("POST", "/v1/sessions/phone", stock_call())]
    payload = out.structured_content
    assert payload["sessionId"] == "sess_1"
    assert payload["freeCall"] == free_call
    assert "one free call abroad" in payload["note"]
    if flag is None:
        assert payload["promptAdvice"] == advise(stock_call())
    else:
        assert "promptAdvice" not in payload
    text = out.content[0].text
    rendered_payload, end = json.JSONDecoder().raw_decode(text)
    assert rendered_payload == payload
    assert voicemail_notice.VOICEMAIL_NOTICE_TEXT in text[end:]


async def test_connector_advice_uses_the_prompt_before_disclosure(
    monkeypatch: pytest.MonkeyPatch, phone_api: SimpleNamespace
) -> None:
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, "connector")
    body = {
        "to": "+16045550123",
        "intent": {"language": "en"},
        "systemPrompt": "Ask for the price.",
    }
    out = await create_server().call_tool("sessions.phone.create", {"body": body})

    sent = phone_api.requests[0][2]
    assert sent["firstMessage"] and "?" not in sent["firstMessage"]
    assert "promptAdvice" not in out.structured_content


async def test_rejected_call_preserves_the_api_error(phone_api: SimpleNamespace) -> None:
    phone_api.reply = httpx.Response(400, json={"error": "Call rejected", "code": "INVALID_CALL"})
    with pytest.raises(ToolError, match="Call rejected") as raised:
        await create_server().call_tool("sessions.phone.create", {"body": stock_call()})

    assert "promptAdvice" not in str(raised.value)
    assert phone_api.requests == [("POST", "/v1/sessions/phone", stock_call())]


def test_phone_tool_documents_prompt_writing_and_timezone() -> None:
    doc = phone_session_doc(None)
    assert "firstMessage = AI disclosure + first question" in doc
    assert "clear answers are final" in doc
    assert "timezone" in CREATE_PHONE_SESSION_BODY_DOC
