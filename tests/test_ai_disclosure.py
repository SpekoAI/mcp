"""AI disclosure is enforced by the relay, not by caller configuration."""

from spekoai_mcp import action_tools
from spekoai_mcp.action_tools import (
    DISCLOSURE_OPENER,
    DISCLOSURE_RULE,
    apply_ai_disclosure,
)
from spekoai_mcp.profiles import (
    CONNECTOR_EXCLUDED_PREFIXES,
    CONNECTOR_PROFILE,
    DIRECTORY_REQUIRED_ABSENT_TOOL_NAMES,
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


def test_connector_profile_excludes_speech_and_arming() -> None:
    """Was `test_connector_profile_keeps_outbound_calling` until 2026-08-27.

    It asserted the outbound-calling wedge survived the directory cut. The MCP
    Directory team refuted the premise — "configuring is arming" — so every
    name it protected now has to be excluded instead.
    """
    for name in DIRECTORY_REQUIRED_ABSENT_TOOL_NAMES:
        assert _is_connector_excluded(name), name

    # Same category, reached by the directory's own reasoning rather than by
    # their enumeration.
    for name in (
        "agents.rollback",
        "agents.tools.create",
        "agents.tools.update",
        "knowledge_bases.documents.finalize",
    ):
        assert _is_connector_excluded(name), name


def test_connector_profile_keeps_reads_and_transcription() -> None:
    """The cut must not overshoot what the directory explicitly blessed."""
    for name in (
        "audio.transcribe",
        "sessions.transcript.get",
        "agents.list",
        "agents.tools.list",
        "agents.tools.get",
        "phone_numbers.list",
        "credits.balance.get",
    ):
        assert not _is_connector_excluded(name), name


def test_excluded_prefixes_are_call_free() -> None:
    assert CONNECTOR_EXCLUDED_PREFIXES == ("agents.evals.", "agents.monitors.")


def test_gate_applies_on_connector_profile(monkeypatch) -> None:
    """The published directory surface always discloses."""
    monkeypatch.setattr(action_tools, "current_profile", lambda: CONNECTOR_PROFILE)
    body = action_tools.apply_directory_disclosure({"firstMessage": "Hi, this is Ava."})
    assert body["firstMessage"].startswith(DISCLOSURE_OPENER)
    assert DISCLOSURE_RULE in body["systemPrompt"]


def test_gate_leaves_direct_mcp_clients_alone(monkeypatch) -> None:
    """Claude Code, Codex and Cursor keep their existing behaviour."""
    for profile in (None, "builder"):
        monkeypatch.setattr(action_tools, "current_profile", lambda p=profile: p)
        body = action_tools.apply_directory_disclosure({"firstMessage": "Hi, this is Ava."})
        assert body == {"firstMessage": "Hi, this is Ava."}, profile


def test_gate_is_inert_without_an_http_request() -> None:
    """stdio and in-process callers resolve to no profile, so nothing is rewritten."""
    body = action_tools.apply_directory_disclosure({"firstMessage": "Hi."})
    assert body == {"firstMessage": "Hi."}
