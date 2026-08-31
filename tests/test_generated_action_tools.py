from __future__ import annotations

from types import SimpleNamespace

from fastmcp.server.dependencies import fastmcp_request_ctx

from spekoai_mcp.generated_action_tools import _idempotency_key


def test_idempotency_key_is_stable_per_request_and_distinct_between_requests() -> None:
    arguments = {"operationId": "00000000-0000-4000-8000-000000000001"}
    first = fastmcp_request_ctx.set(SimpleNamespace(request_id="request-1"))  # type: ignore[arg-type]
    try:
        key = _idempotency_key("operations.cancel", arguments)
        assert _idempotency_key("operations.cancel", arguments) == key
    finally:
        fastmcp_request_ctx.reset(first)

    second = fastmcp_request_ctx.set(SimpleNamespace(request_id="request-2"))  # type: ignore[arg-type]
    try:
        assert _idempotency_key("operations.cancel", arguments) != key
    finally:
        fastmcp_request_ctx.reset(second)


def test_payload_text_serializes_the_full_payload() -> None:
    from spekoai_mcp.generated_action_tools import _payload_text
    import json

    payload = {"kind": "result", "data": [{"id": "agent_1", "name": "Front desk"}]}
    text = _payload_text("agents.list", payload)
    # Text-only MCP hosts (claude.ai web) see nothing but this block, so it
    # must carry the payload itself, not an acknowledgment.
    assert json.loads(text) == payload


def test_payload_text_truncates_oversized_payloads_with_a_marker() -> None:
    from spekoai_mcp.generated_action_tools import _TEXT_BYTE_CEILING, _payload_text

    payload = {"blob": "x" * (_TEXT_BYTE_CEILING + 500)}
    text = _payload_text("sessions.transcript.get", payload)
    assert len(text) < _TEXT_BYTE_CEILING + 200
    assert "truncated by the Speko MCP server" in text


def test_payload_text_survives_unserializable_payloads() -> None:
    from spekoai_mcp.generated_action_tools import _payload_text

    class Opaque:
        pass

    # default=str covers most objects; a truly unserializable payload
    # (circular) must not raise.
    circular: dict = {}
    circular["self"] = circular
    text = _payload_text("agents.get", circular)
    assert "agents.get" in text
