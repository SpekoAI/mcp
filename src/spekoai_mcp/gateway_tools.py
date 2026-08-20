"""MCP tools for organization-owned Runtime Gateway API keys."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp.types import ToolAnnotations
from pydantic import Field

from spekoai_mcp import gateway_client, http_client
from spekoai_mcp.action_tools import SPEKO_API_OUTPUT_SCHEMA, result, tool_title

GATEWAY_TOOL_NAME_BY_FUNCTION = {
    "list_gateway_keys": "gateway.keys.list",
    "create_gateway_key": "gateway.keys.create",
    "revoke_gateway_key": "gateway.keys.revoke",
}
GATEWAY_TOOL_NAMES = list(GATEWAY_TOOL_NAME_BY_FUNCTION.values())


def register_gateway_tools(mcp: FastMCP) -> None:
    for tool in [list_gateway_keys, create_gateway_key, revoke_gateway_key]:
        function_name = tool.__name__
        title = tool_title(function_name)
        mcp.tool(
            tool,
            name=GATEWAY_TOOL_NAME_BY_FUNCTION[function_name],
            title=title,
            output_schema=SPEKO_API_OUTPUT_SCHEMA,
            annotations=ToolAnnotations(
                title=title,
                read_only_hint=function_name == "list_gateway_keys",
                destructive_hint=function_name == "revoke_gateway_key",
                idempotent_hint=function_name == "list_gateway_keys",
                open_world_hint=True,
            ),
        )


def next_step_for_gateway_error(exc: Exception) -> str:
    if isinstance(exc, gateway_client.SpekoAuthError):
        return (
            "Ask an organization owner or admin to create a Platform API key "
            "with Manage Gateway API keys enabled, then reconnect MCP with it."
        )
    if not isinstance(exc, gateway_client.SpekoApiError):
        return "Retry the Speko Gateway request."
    if exc.status_code == 409:
        return "Revoke an unused key with gateway.keys.revoke, then retry."
    if exc.status_code in {401, 403}:
        return "Reconnect with a valid Platform API key carrying gateway.keys.manage."
    if exc.status_code == 404:
        return "Call gateway.keys.list and retry with a current key id."
    return "Inspect the Gateway response details, then retry."


async def gateway_call(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    text: str,
    payload_override: dict[str, Any] | None = None,
) -> ToolResult:
    try:
        payload = await gateway_client.call_gateway_api(method, path, body)
    except (gateway_client.SpekoApiError, gateway_client.SpekoAuthError) as exc:
        raise ToolError(
            http_client.tool_error_message(exc, next_step=next_step_for_gateway_error(exc))
        ) from exc
    return result(payload_override if payload_override is not None else payload, text=text)


async def list_gateway_keys() -> ToolResult:
    """List this organization’s Runtime Gateway API keys.

    Full secrets are never returned. Requires gateway.keys.manage on the
    Platform API key used to authenticate MCP.
    """
    return await gateway_call("GET", "/api/keys", text="Retrieved Gateway keys.")


async def create_gateway_key(
    name: Annotated[
        str,
        Field(
            min_length=1,
            max_length=100,
            description="Human-readable key name, such as 'pipecat-production'.",
        ),
    ],
) -> ToolResult:
    """Create a Runtime Gateway API key.

    The returned secret is shown exactly once. Store it immediately. Routing
    choices remain per request; the key carries no routing policy.
    """
    trimmed = name.strip()
    if not trimmed:
        raise ToolError("Invalid Gateway key name: name must not be blank.")
    return await gateway_call(
        "POST", "/api/keys", body={"name": trimmed}, text="Created Gateway key."
    )


async def revoke_gateway_key(
    key_id: Annotated[str, Field(description="Gateway key id from gateway.keys.list.")],
) -> ToolResult:
    """Revoke an organization Runtime Gateway API key permanently."""
    return await gateway_call(
        "DELETE",
        f"/api/keys/{http_client.path_segment(key_id)}",
        text="Revoked Gateway key.",
        payload_override={"id": key_id, "revoked": True},
    )
