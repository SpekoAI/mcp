"""AI disclosure is enforced by the relay, not by caller configuration."""

from spekoai_mcp.action_tools import (
    DISCLOSURE_OPENER,
    DISCLOSURE_RULE,
    apply_ai_disclosure,
)
from spekoai_mcp.profiles import (
    CONNECTOR_EXCLUDED_PREFIXES,
    _is_connector_excluded,
)


def test_disclosure_added_to_bare_body() -> None:
    body = apply_ai_disclosure({"to": "+12015551234"})
    assert body["firstMessage"] == DISCLOSURE_OPENER
    assert body["systemPrompt"] == DISCLOSURE_RULE


def test_disclosure_prepends_and_keeps_caller_text() -> None:
    body = apply_ai_disclosure(
        {"systemPrompt": "You are Ava from Northside Clinic.", "firstMessage": "Hi, this is Ava."}
    )
    assert body["firstMessage"].startswith(DISCLOSURE_OPENER)
    assert "Hi, this is Ava." in body["firstMessage"]
    assert "You are Ava from Northside Clinic." in body["systemPrompt"]
    assert DISCLOSURE_RULE in body["systemPrompt"]


def test_disclosure_is_idempotent() -> None:
    once = apply_ai_disclosure({"firstMessage": "Hello.", "systemPrompt": "Be brief."})
    twice = apply_ai_disclosure(dict(once))
    assert twice == once
    assert twice["firstMessage"].count(DISCLOSURE_OPENER) == 1
    assert twice["systemPrompt"].count(DISCLOSURE_RULE) == 1


def test_blank_first_message_still_discloses() -> None:
    assert apply_ai_disclosure({"firstMessage": "   "})["firstMessage"] == DISCLOSURE_OPENER


def test_connector_profile_hides_bulk_and_scheduled_tools() -> None:
    for name in (
        "agents.evals.run",
        "agents.evals.create",
        "agents.evals.list",
        "agents.monitors.create",
        "agents.monitors.events.list",
    ):
        assert _is_connector_excluded(name), name


def test_connector_profile_keeps_outbound_calling() -> None:
    for name in (
        "sessions.phone.create",
        "agents.create",
        "agents.test_call",
        "phone_numbers.create",
        "sessions.transcript.get",
    ):
        assert not _is_connector_excluded(name), name


def test_excluded_prefixes_are_call_free() -> None:
    assert CONNECTOR_EXCLUDED_PREFIXES == ("agents.evals.", "agents.monitors.")
