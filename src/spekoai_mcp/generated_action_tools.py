"""FastMCP Tool instances generated from the canonical TypeScript manifest."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import uuid4

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from fastmcp.tools import Tool, ToolResult
from mcp.types import TextContent, ToolAnnotations
from pydantic import Field

from spekoai_mcp import http_client
from spekoai_mcp.action_manifest import action_entries
from spekoai_mcp.credit_exhaustion import BALANCE_ACTION_ID, balance_result_text
from spekoai_mcp.profiles import current_profile, profile_serves_tool
from spekoai_mcp.tool_text import payload_text

DESCRIBE_ACTION_ID = "capabilities.describe"


class ManifestActionTool(Tool):
    action_id: str = Field(exclude=True)
    idempotency: str = Field(exclude=True)

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        if self.action_id == DESCRIBE_ACTION_ID:
            described = await _describe_mcp_tool(arguments.get("actionId"))
            if described is not None:
                return described
        idempotency_key = None
        if self.idempotency == "required":
            idempotency_key = _idempotency_key(self.action_id, arguments)
        payload = await http_client.call_action(
            self.action_id,
            arguments,
            idempotency_key=idempotency_key,
        )
        text = payload_text(payload, action=self.action_id)
        if self.action_id == BALANCE_ACTION_ID:
            text = balance_result_text(payload, text)
        return ToolResult(
            content=[TextContent(type="text", text=text)],
            structured_content=payload,
        )


def _idempotency_key(action_id: str, arguments: dict[str, Any]) -> str:
    """One key per tool invocation, never shared with a later invocation.

    The key used to hash the JSON-RPC request id. That id is only unique within
    one client connection, and this server is stateless HTTP: every new chat
    starts numbering again, so the same user calling `billing.portal.create`
    (no arguments) as request 3 twice produced the same key. Platform scopes the
    key by organization, principal and action, so the second call replayed the
    first: 409 ONE_TIME_SECRET_NOT_REPLAYABLE for an action with a secret
    output (14 in prod 2026-10-19..21 across billing.portal.create,
    billing.checkout.create and integrations.connect), and a silently replayed
    old result for any other required-idempotency action.
    """
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(f"{action_id}:{uuid4().hex}:{canonical}".encode()).hexdigest()
    return f"mcp-{digest}"


async def _describe_mcp_tool(target: Any) -> ToolResult | None:
    """Answer `capabilities.describe` for a tool this server already lists.

    Platform's registry holds only manifest actions, so describing a
    hand-written tool (`sessions.phone.create`, `audio.synthesize`,
    `agents.update`) answered 404 ACTION_NOT_FOUND: 20 times in prod
    2026-10-19..21, every one a model asking for the schema it needed. The tool
    is in this server's tools/list, so return its schema here instead.
    Manifest actions, and anything this profile does not serve, still go to
    Platform, which owns their contract and authorization.
    """
    if not isinstance(target, str) or target in _MANIFEST_ACTION_IDS:
        return None
    if not profile_serves_tool(target, current_profile()):
        return None
    tool = await get_context().fastmcp.get_tool(target)
    if tool is None:
        return None
    annotations = tool.annotations
    effect = "read" if annotations is not None and annotations.read_only_hint else "write"
    if annotations is not None and annotations.destructive_hint:
        effect = "destructive"
    warning = (
        f"{target} is an MCP tool in your tool list, not a registry action. Call it "
        "directly with arguments matching inputSchema; do not route it through "
        "another tool."
    )
    payload = {
        "kind": "result",
        "data": {
            "action": {
                "id": target,
                "version": 1,
                "description": tool.description or "",
                "inputSchema": tool.parameters,
                "outputSchema": tool.output_schema or {},
                "effect": effect,
                "requiredScopes": [],
                "allowedPrincipals": ["oauth-user", "api-key"],
                "idempotency": "none",
                "execution": "sync",
                "profiles": [],
                "secretInputPaths": [],
                "secretOutputPaths": [],
            }
        },
        "warnings": [{"code": "MCP_TOOL_NOT_REGISTRY_ACTION", "message": warning}],
        "resourceLinks": [],
    }
    text = f"{warning}\n\ninputSchema: {json.dumps(tool.parameters, separators=(',', ':'))}"
    return ToolResult(content=[TextContent(type="text", text=text)], structured_content=payload)


def _open_output_schema(schema: Any) -> Any:
    """Allow fields the manifest does not list yet, at every depth.

    Clients validate a tool's structured content against its output schema,
    and Platform deploys ahead of this server. With `additionalProperties:
    false`, one new response field fails the whole call in the client while the
    action itself succeeded: on 2026-10-12 `support.ticket.create` began
    returning `ticketUrl`, and every agent was told its ticket failed although it
    was stored. Additive fields must never break a released server.
    """
    if isinstance(schema, dict):
        return {
            key: _open_output_schema(value)
            for key, value in schema.items()
            if not (key == "additionalProperties" and value is False)
        }
    if isinstance(schema, list):
        return [_open_output_schema(item) for item in schema]
    return schema


def _title(action_id: str) -> str:
    return " ".join(part.replace("_", " ").title() for part in action_id.split("."))


_MANIFEST_ACTION_IDS: frozenset[str] = frozenset(entry["id"] for entry in action_entries())


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
                output_schema=_open_output_schema(entry["outputSchema"]),
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
