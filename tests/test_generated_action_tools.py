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
