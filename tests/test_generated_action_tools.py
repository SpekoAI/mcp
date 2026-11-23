from __future__ import annotations

from types import SimpleNamespace

from fastmcp.server.dependencies import fastmcp_request_ctx

from spekoai_mcp.action_manifest import action_entries
from spekoai_mcp.generated_action_tools import _idempotency_key, _open_output_schema


def test_idempotency_key_is_never_shared_between_invocations() -> None:
    """JSON-RPC ids restart with every stateless connection, so two chats that
    each send request 3 with the same arguments must still get distinct keys;
    a shared key replays the first result (409 ONE_TIME_SECRET_NOT_REPLAYABLE
    on billing.portal.create in prod)."""
    arguments: dict[str, object] = {}
    first = fastmcp_request_ctx.set(SimpleNamespace(request_id=3))  # type: ignore[arg-type]
    try:
        key = _idempotency_key("billing.portal.create", arguments)
    finally:
        fastmcp_request_ctx.reset(first)
    second = fastmcp_request_ctx.set(SimpleNamespace(request_id=3))  # type: ignore[arg-type]
    try:
        assert _idempotency_key("billing.portal.create", arguments) != key
    finally:
        fastmcp_request_ctx.reset(second)
    assert key.startswith("mcp-")


def test_open_output_schema_drops_closed_objects_at_every_depth() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "data": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "items": {"type": "array", "items": {"additionalProperties": False}}
                },
            },
            "extra": {"type": "object", "additionalProperties": {"type": "string"}},
        },
    }
    opened = _open_output_schema(schema)
    assert "additionalProperties" not in opened
    assert "additionalProperties" not in opened["properties"]["data"]
    assert (
        "additionalProperties" not in opened["properties"]["data"]["properties"]["items"]["items"]
    )
    # A typed map is a real constraint, not a closed object; it stays.
    assert opened["properties"]["extra"]["additionalProperties"] == {"type": "string"}
    # The manifest entry itself is not mutated.
    assert schema["additionalProperties"] is False


def test_no_registered_output_schema_rejects_a_new_response_field() -> None:
    def closed(node: object) -> bool:
        if isinstance(node, dict):
            if node.get("additionalProperties") is False:
                return True
            return any(closed(value) for value in node.values())
        if isinstance(node, list):
            return any(closed(item) for item in node)
        return False

    for entry in action_entries():
        assert not closed(_open_output_schema(entry["outputSchema"])), entry["id"]
