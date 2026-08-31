"""FastMCP Tool instances generated from the canonical TypeScript manifest."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import uuid4

from fastmcp import FastMCP
from fastmcp.server.dependencies import fastmcp_request_ctx
from fastmcp.tools import Tool, ToolResult
from mcp.types import TextContent, ToolAnnotations
from pydantic import Field

from spekoai_mcp import http_client
from spekoai_mcp.action_manifest import action_entries


class ManifestActionTool(Tool):
    action_id: str = Field(exclude=True)
    idempotency: str = Field(exclude=True)

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        idempotency_key = None
        if self.idempotency == "required":
            idempotency_key = _idempotency_key(self.action_id, arguments)
        payload = await http_client.call_action(
            self.action_id,
            arguments,
            idempotency_key=idempotency_key,
        )
        return ToolResult(
            content=[TextContent(type="text", text=_payload_text(self.action_id, payload))],
            structured_content=payload,
        )


_TEXT_BYTE_CEILING = 100_000


def _payload_text(action_id: str, payload: Any) -> str:
    """Serialize the result payload into the text block.

    Hosts that do not surface structuredContent to the model (claude.ai web,
    among others) only see text content. A bare "Executed X." acknowledgment
    made every account tool look like it returned nothing, so the text block
    must carry a functionally equivalent JSON rendering of the payload
    (MCP spec: servers SHOULD return both). Oversized payloads are truncated
    with an explicit marker instead of silently flooding the context.
    """
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return f"Executed {action_id}. (result not serializable as JSON)"
    if len(text) > _TEXT_BYTE_CEILING:
        return (
            text[:_TEXT_BYTE_CEILING]
            + f'... [truncated by the Speko MCP server: {len(text)} chars total; '
            f"narrow the query or use pagination arguments]"
        )
    return text


def _idempotency_key(action_id: str, arguments: dict[str, Any]) -> str:
    """Stay stable for one MCP request without merging later invocations."""
    request_context = fastmcp_request_ctx.get()
    request_id = request_context.request_id if request_context is not None else uuid4().hex
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(f"{action_id}:{request_id}:{canonical}".encode()).hexdigest()
    return f"mcp-{digest}"


def _title(action_id: str) -> str:
    return " ".join(part.replace("_", " ").title() for part in action_id.split("."))


def register_generated_action_tools(mcp: FastMCP) -> None:
    for entry in action_entries():
        effect = entry["effect"]
        action_id = entry["id"]
        title = _title(action_id)
        mcp.add_tool(
            ManifestActionTool(
                name=action_id,
                title=title,
                description=entry["description"],
                parameters=entry["inputSchema"],
                output_schema=entry["outputSchema"],
                annotations=ToolAnnotations(
                    title=title,
                    read_only_hint=effect == "read",
                    destructive_hint=effect == "destructive",
                    idempotent_hint=entry["idempotency"] != "none" or effect == "read",
                    open_world_hint=True,
                ),
                meta={
                    "speko": {
                        "actionVersion": entry["version"],
                        "effect": effect,
                        "requiredScopes": entry["requiredScopes"],
                        "profiles": entry["profiles"],
                    }
                },
                action_id=action_id,
                idempotency=entry["idempotency"],
            )
        )
