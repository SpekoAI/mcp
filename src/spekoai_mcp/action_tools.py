"""Hosted MCP tools that relay authenticated calls to the Speko API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent
from pydantic import Field

from spekoai_mcp import http_client

ExternalPlatform = Literal["livekit", "pipecat", "retell", "vapi"]

ACTION_TOOL_NAMES = [
    "get_organization",
    "get_credit_balance",
    "list_credit_ledger",
    "get_usage_summary",
    "list_agents",
    "create_agent",
    "get_agent",
    "update_agent",
    "delete_agent",
    "list_agent_tools",
    "create_agent_tool",
    "get_agent_tool",
    "update_agent_tool",
    "delete_agent_tool",
    "deploy_agent",
    "rollback_agent",
    "list_agent_versions",
    "create_session",
    "create_phone_session",
    "list_sessions",
    "get_session",
    "get_session_transcript",
    "get_session_recording",
    "list_agent_calls",
    "get_call",
    "get_call_recording",
    "list_phone_numbers",
    "search_available_phone_numbers",
    "create_phone_number",
    "get_phone_number",
    "update_phone_number",
    "delete_phone_number",
    "create_knowledge_base",
    "list_knowledge_bases",
    "get_knowledge_base",
    "delete_knowledge_base",
    "list_knowledge_documents",
    "create_knowledge_document",
    "get_knowledge_document",
    "delete_knowledge_document",
    "finalize_knowledge_document",
    "list_agent_evals",
    "create_agent_eval",
    "run_agent_eval",
    "get_eval",
    "inspect_workspace",
    "build_session_config",
    "parse_external_config",
    "render_briefing",
    "create_share_card",
]


def register_action_tools(mcp: FastMCP) -> None:
    for tool in [
        get_organization,
        get_credit_balance,
        list_credit_ledger,
        get_usage_summary,
        list_agents,
        create_agent,
        get_agent,
        update_agent,
        delete_agent,
        list_agent_tools,
        create_agent_tool,
        get_agent_tool,
        update_agent_tool,
        delete_agent_tool,
        deploy_agent,
        rollback_agent,
        list_agent_versions,
        create_session,
        create_phone_session,
        list_sessions,
        get_session,
        get_session_transcript,
        get_session_recording,
        list_agent_calls,
        get_call,
        get_call_recording,
        list_phone_numbers,
        search_available_phone_numbers,
        create_phone_number,
        get_phone_number,
        update_phone_number,
        delete_phone_number,
        create_knowledge_base,
        list_knowledge_bases,
        get_knowledge_base,
        delete_knowledge_base,
        list_knowledge_documents,
        create_knowledge_document,
        get_knowledge_document,
        delete_knowledge_document,
        finalize_knowledge_document,
        list_agent_evals,
        create_agent_eval,
        run_agent_eval,
        get_eval,
        inspect_workspace,
        build_session_config,
        parse_external_config,
        render_briefing,
        create_share_card,
    ]:
        mcp.tool(tool)


def result(payload: dict[str, Any], text: str = "Speko API request completed.") -> ToolResult:
    return ToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=payload,
    )


def list_result(payload: list[Any], text: str = "Speko API request completed.") -> ToolResult:
    return ToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content={"result": payload},
    )


def tool_error(exc: Exception, *, next_step: str) -> ToolError:
    return ToolError(http_client.tool_error_message(exc, next_step=next_step))


def collect_workspace_metadata(workspace_root: str, *, deep: bool) -> dict[str, Any]:
    root = Path(workspace_root).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        return {"workspace_root": str(root), "missing": True}
    candidates = ["package.json", "pyproject.toml", "requirements.txt", "pnpm-lock.yaml"]
    files: dict[str, str] = {}
    for name in candidates:
        path = root / name
        if path.exists() and path.is_file():
            files[name] = path.read_text(encoding="utf-8", errors="ignore")[:200_000]
    if deep:
        source_files: list[Path] = []
        for pattern in ["*.ts", "*.tsx", "*.js", "*.jsx", "*.py"]:
            source_files.extend(root.rglob(pattern))
        sampled = [
            path
            for path in source_files
            if "node_modules" not in path.parts and ".venv" not in path.parts and path.is_file()
        ][:60]
        for path in sampled:
            files[str(path.relative_to(root))] = path.read_text(
                encoding="utf-8",
                errors="ignore",
            )[:50_000]
    return {"workspace_root": str(root), "files": files, "deep": deep}


async def call(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    text: str = "Speko API request completed.",
) -> ToolResult:
    try:
        payload = await http_client.call_speko_api(method, path, body)
    except (http_client.SpekoApiError, http_client.SpekoAuthError) as exc:
        raise tool_error(
            exc, next_step="Check authentication and retry the Speko MCP request."
        ) from exc
    return result(payload, text=text)


async def call_list(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    text: str = "Speko API request completed.",
) -> ToolResult:
    try:
        payload = await http_client.call_speko_api_any(method, path, body)
    except (http_client.SpekoApiError, http_client.SpekoAuthError) as exc:
        raise tool_error(
            exc, next_step="Check authentication and retry the Speko MCP request."
        ) from exc
    if isinstance(payload, list):
        return list_result(payload, text=text)
    if isinstance(payload, dict):
        return result(payload, text=text)
    return result({"result": payload}, text=text)


async def get_organization() -> ToolResult:
    """Get the authenticated caller's Speko organization."""
    return await call("GET", "/v1/organization", text="Retrieved organization.")


async def get_credit_balance() -> ToolResult:
    """Get the authenticated organization's prepaid credit balance."""
    return await call("GET", "/v1/credits/balance", text="Retrieved credit balance.")


async def list_credit_ledger(
    limit: Annotated[int | None, Field(description="Maximum ledger entries to return.")] = None,
    cursor: Annotated[str | None, Field(description="ISO cursor from the previous page.")] = None,
    kind: Annotated[
        str | None,
        Field(
            description=(
                "Optional comma-separated ledger kinds: grant,debit,topup,refund,adjustment."
            )
        ),
    ] = None,
) -> ToolResult:
    """List credit ledger entries for the authenticated organization."""
    return await call(
        "GET",
        http_client.with_query(
            "/v1/credits/ledger", {"limit": limit, "cursor": cursor, "kind": kind}
        ),
        text="Retrieved credit ledger.",
    )


async def get_usage_summary(
    from_: Annotated[str | None, Field(description="Optional ISO start timestamp.")] = None,
    to: Annotated[str | None, Field(description="Optional ISO end timestamp.")] = None,
) -> ToolResult:
    """Get usage summary for the authenticated organization."""
    return await call(
        "GET",
        http_client.with_query("/v1/usage", {"from": from_, "to": to}),
        text="Retrieved usage summary.",
    )


async def list_agents() -> ToolResult:
    """List agents in the authenticated organization."""
    return await call_list("GET", "/v1/agents", text="Retrieved agents.")


async def create_agent(
    body: Annotated[
        dict[str, Any],
        Field(
            description="JSON body for POST /v1/agents, including name, systemPrompt, and intent."
        ),
    ],
) -> ToolResult:
    """Create a Speko agent."""
    return await call("POST", "/v1/agents", body=body, text="Created agent.")


async def get_agent(
    agent_id: Annotated[str, Field(description="Agent id.")],
) -> ToolResult:
    """Get one Speko agent."""
    return await call(
        "GET", f"/v1/agents/{http_client.path_segment(agent_id)}", text="Retrieved agent."
    )


async def update_agent(
    agent_id: Annotated[str, Field(description="Agent id.")],
    body: Annotated[dict[str, Any], Field(description="JSON body for PATCH /v1/agents/{id}.")],
) -> ToolResult:
    """Update one Speko agent."""
    return await call(
        "PATCH",
        f"/v1/agents/{http_client.path_segment(agent_id)}",
        body=body,
        text="Updated agent.",
    )


async def delete_agent(
    agent_id: Annotated[str, Field(description="Agent id.")],
) -> ToolResult:
    """Delete one Speko agent."""
    return await call(
        "DELETE", f"/v1/agents/{http_client.path_segment(agent_id)}", text="Deleted agent."
    )


async def list_agent_tools(
    agent_id: Annotated[str, Field(description="Agent id.")],
) -> ToolResult:
    """List tools registered on an agent."""
    return await call_list(
        "GET",
        f"/v1/agents/{http_client.path_segment(agent_id)}/tools",
        text="Retrieved agent tools.",
    )


async def create_agent_tool(
    agent_id: Annotated[str, Field(description="Agent id.")],
    body: Annotated[
        dict[str, Any], Field(description="JSON body for POST /v1/agents/{agentId}/tools.")
    ],
) -> ToolResult:
    """Create a tool on an agent."""
    return await call(
        "POST",
        f"/v1/agents/{http_client.path_segment(agent_id)}/tools",
        body=body,
        text="Created agent tool.",
    )


async def get_agent_tool(
    agent_id: Annotated[str, Field(description="Agent id.")],
    tool_id: Annotated[str, Field(description="Tool id.")],
) -> ToolResult:
    """Get one agent tool."""
    return await call(
        "GET",
        f"/v1/agents/{http_client.path_segment(agent_id)}/tools/{http_client.path_segment(tool_id)}",
        text="Retrieved agent tool.",
    )


async def update_agent_tool(
    agent_id: Annotated[str, Field(description="Agent id.")],
    tool_id: Annotated[str, Field(description="Tool id.")],
    body: Annotated[
        dict[str, Any],
        Field(description="JSON body for PATCH /v1/agents/{agentId}/tools/{toolId}."),
    ],
) -> ToolResult:
    """Update one agent tool."""
    return await call(
        "PATCH",
        f"/v1/agents/{http_client.path_segment(agent_id)}/tools/{http_client.path_segment(tool_id)}",
        body=body,
        text="Updated agent tool.",
    )


async def delete_agent_tool(
    agent_id: Annotated[str, Field(description="Agent id.")],
    tool_id: Annotated[str, Field(description="Tool id.")],
) -> ToolResult:
    """Delete one agent tool."""
    return await call(
        "DELETE",
        f"/v1/agents/{http_client.path_segment(agent_id)}/tools/{http_client.path_segment(tool_id)}",
        text="Deleted agent tool.",
    )


async def deploy_agent(
    agent_id: Annotated[str, Field(description="Agent id.")],
    session_config: Annotated[dict[str, Any], Field(description="Speko SessionConfig to deploy.")],
    briefing_markdown: Annotated[
        str | None, Field(description="Optional briefing markdown.")
    ] = None,
    source: Annotated[
        str | None, Field(description="Optional source label. Defaults to mcp upstream.")
    ] = None,
) -> ToolResult:
    """Deploy a SessionConfig as a new immutable agent version."""
    body: dict[str, Any] = {"session_config": session_config}
    if briefing_markdown is not None:
        body["briefing_markdown"] = briefing_markdown
    if source is not None:
        body["source"] = source
    return await call(
        "POST",
        f"/v1/agents/{http_client.path_segment(agent_id)}/deploy",
        body=body,
        text="Deployed agent.",
    )


async def rollback_agent(
    agent_id: Annotated[str, Field(description="Agent id.")],
    target_version_number: Annotated[
        int, Field(description="Historical version number to roll back to.")
    ],
) -> ToolResult:
    """Roll an agent back to a historical version."""
    return await call(
        "POST",
        f"/v1/agents/{http_client.path_segment(agent_id)}/rollback",
        body={"target_version_number": target_version_number},
        text="Rolled back agent.",
    )


async def list_agent_versions(
    agent_id: Annotated[str, Field(description="Agent id.")],
) -> ToolResult:
    """List versions for an agent."""
    return await call_list(
        "GET",
        f"/v1/agents/{http_client.path_segment(agent_id)}/versions",
        text="Retrieved agent versions.",
    )


async def create_session(
    body: Annotated[dict[str, Any], Field(description="JSON body for POST /v1/sessions.")],
) -> ToolResult:
    """Create a browser/WebRTC or server-to-server voice session."""
    return await call("POST", "/v1/sessions", body=body, text="Created session.")


async def create_phone_session(
    body: Annotated[dict[str, Any], Field(description="JSON body for POST /v1/sessions/phone.")],
) -> ToolResult:
    """Create an outbound phone session."""
    return await call("POST", "/v1/sessions/phone", body=body, text="Created phone session.")


async def list_sessions(
    limit: Annotated[int | None, Field(description="Maximum sessions to return.")] = None,
    cursor: Annotated[str | None, Field(description="ISO cursor from the previous page.")] = None,
    status: Annotated[str | None, Field(description="Optional status filter.")] = None,
    kind: Annotated[str | None, Field(description="Optional kind filter: cascade or s2s.")] = None,
    from_: Annotated[str | None, Field(description="Optional ISO start timestamp.")] = None,
    to: Annotated[str | None, Field(description="Optional ISO end timestamp.")] = None,
    agent: Annotated[str | None, Field(description="Optional agent id filter.")] = None,
) -> ToolResult:
    """List sessions for the authenticated organization."""
    return await call(
        "GET",
        http_client.with_query(
            "/v1/sessions",
            {
                "limit": limit,
                "cursor": cursor,
                "status": status,
                "kind": kind,
                "from": from_,
                "to": to,
                "agent": agent,
            },
        ),
        text="Retrieved sessions.",
    )


async def get_session(
    session_id: Annotated[str, Field(description="Session id.")],
) -> ToolResult:
    """Get one session."""
    return await call(
        "GET",
        f"/v1/sessions/{http_client.path_segment(session_id)}",
        text="Retrieved session.",
    )


async def get_session_transcript(
    session_id: Annotated[str, Field(description="Session id.")],
) -> ToolResult:
    """Get one session transcript."""
    return await call(
        "GET",
        f"/v1/sessions/{http_client.path_segment(session_id)}/transcript",
        text="Retrieved session transcript.",
    )


async def get_session_recording(
    session_id: Annotated[str, Field(description="Session id.")],
) -> ToolResult:
    """Get a signed recording URL for one session."""
    return await call(
        "GET",
        f"/v1/sessions/{http_client.path_segment(session_id)}/recording",
        text="Retrieved session recording URL.",
    )


async def list_agent_calls(
    agent_id: Annotated[str, Field(description="Agent id.")],
    limit: Annotated[int | None, Field(description="Maximum calls to return.")] = None,
    cursor: Annotated[str | None, Field(description="ISO cursor from the previous page.")] = None,
    since: Annotated[str | None, Field(description="Optional ISO lower-bound timestamp.")] = None,
) -> ToolResult:
    """List recent calls for an agent."""
    return await call(
        "GET",
        http_client.with_query(
            f"/v1/agents/{http_client.path_segment(agent_id)}/calls",
            {"limit": limit, "cursor": cursor, "since": since},
        ),
        text="Retrieved agent calls.",
    )


async def get_call(
    call_id: Annotated[str, Field(description="Call/session id.")],
) -> ToolResult:
    """Get call detail including transcript."""
    return await call(
        "GET", f"/v1/calls/{http_client.path_segment(call_id)}", text="Retrieved call."
    )


async def get_call_recording(
    call_id: Annotated[str, Field(description="Call/session id.")],
) -> ToolResult:
    """Get a signed recording URL for one call."""
    return await call(
        "GET",
        f"/v1/calls/{http_client.path_segment(call_id)}/recording",
        text="Retrieved call recording URL.",
    )


async def list_phone_numbers() -> ToolResult:
    """List phone numbers in the authenticated organization."""
    return await call_list("GET", "/v1/phone-numbers", text="Retrieved phone numbers.")


async def search_available_phone_numbers(
    area_code: Annotated[str | None, Field(description="Optional 3-digit US area code.")] = None,
    locality: Annotated[str | None, Field(description="Optional locality/city filter.")] = None,
    limit: Annotated[int | None, Field(description="Maximum available numbers to return.")] = None,
) -> ToolResult:
    """Search available phone numbers."""
    return await call_list(
        "GET",
        http_client.with_query(
            "/v1/phone-numbers/available",
            {"areaCode": area_code, "locality": locality, "limit": limit},
        ),
        text="Retrieved available phone numbers.",
    )


async def create_phone_number(
    body: Annotated[dict[str, Any], Field(description="JSON body for POST /v1/phone-numbers.")],
) -> ToolResult:
    """Provision a phone number."""
    return await call("POST", "/v1/phone-numbers", body=body, text="Created phone number.")


async def get_phone_number(
    phone_number_id: Annotated[str, Field(description="Phone-number row id.")],
) -> ToolResult:
    """Get one phone number."""
    return await call(
        "GET",
        f"/v1/phone-numbers/{http_client.path_segment(phone_number_id)}",
        text="Retrieved phone number.",
    )


async def update_phone_number(
    phone_number_id: Annotated[str, Field(description="Phone-number row id.")],
    body: Annotated[
        dict[str, Any], Field(description="JSON body for PATCH /v1/phone-numbers/{id}.")
    ],
) -> ToolResult:
    """Update one phone number."""
    return await call(
        "PATCH",
        f"/v1/phone-numbers/{http_client.path_segment(phone_number_id)}",
        body=body,
        text="Updated phone number.",
    )


async def delete_phone_number(
    phone_number_id: Annotated[str, Field(description="Phone-number row id.")],
) -> ToolResult:
    """Release and delete one phone number."""
    return await call(
        "DELETE",
        f"/v1/phone-numbers/{http_client.path_segment(phone_number_id)}",
        text="Deleted phone number.",
    )


async def create_knowledge_base(
    body: Annotated[dict[str, Any], Field(description="JSON body for POST /v1/knowledge-bases.")],
) -> ToolResult:
    """Create a knowledge base."""
    return await call("POST", "/v1/knowledge-bases", body=body, text="Created knowledge base.")


async def list_knowledge_bases(
    agent_id: Annotated[str | None, Field(description="Optional agent id filter.")] = None,
) -> ToolResult:
    """List knowledge bases."""
    return await call_list(
        "GET",
        http_client.with_query("/v1/knowledge-bases", {"agentId": agent_id}),
        text="Retrieved knowledge bases.",
    )


async def get_knowledge_base(
    knowledge_base_id: Annotated[str, Field(description="Knowledge-base id.")],
) -> ToolResult:
    """Get one knowledge base."""
    return await call(
        "GET",
        f"/v1/knowledge-bases/{http_client.path_segment(knowledge_base_id)}",
        text="Retrieved knowledge base.",
    )


async def delete_knowledge_base(
    knowledge_base_id: Annotated[str, Field(description="Knowledge-base id.")],
) -> ToolResult:
    """Delete one knowledge base."""
    return await call(
        "DELETE",
        f"/v1/knowledge-bases/{http_client.path_segment(knowledge_base_id)}",
        text="Deleted knowledge base.",
    )


async def list_knowledge_documents(
    knowledge_base_id: Annotated[str, Field(description="Knowledge-base id.")],
) -> ToolResult:
    """List documents in a knowledge base."""
    return await call_list(
        "GET",
        f"/v1/knowledge-bases/{http_client.path_segment(knowledge_base_id)}/documents",
        text="Retrieved knowledge documents.",
    )


async def create_knowledge_document(
    knowledge_base_id: Annotated[str, Field(description="Knowledge-base id.")],
    body: Annotated[
        dict[str, Any],
        Field(description="JSON body for POST /v1/knowledge-bases/{kbId}/documents."),
    ],
) -> ToolResult:
    """Create a knowledge document row and upload URL."""
    return await call(
        "POST",
        f"/v1/knowledge-bases/{http_client.path_segment(knowledge_base_id)}/documents",
        body=body,
        text="Created knowledge document.",
    )


async def get_knowledge_document(
    knowledge_base_id: Annotated[str, Field(description="Knowledge-base id.")],
    document_id: Annotated[str, Field(description="Knowledge-document id.")],
) -> ToolResult:
    """Get one knowledge document."""
    return await call(
        "GET",
        (
            f"/v1/knowledge-bases/{http_client.path_segment(knowledge_base_id)}"
            f"/documents/{http_client.path_segment(document_id)}"
        ),
        text="Retrieved knowledge document.",
    )


async def delete_knowledge_document(
    knowledge_base_id: Annotated[str, Field(description="Knowledge-base id.")],
    document_id: Annotated[str, Field(description="Knowledge-document id.")],
) -> ToolResult:
    """Delete one knowledge document."""
    return await call(
        "DELETE",
        (
            f"/v1/knowledge-bases/{http_client.path_segment(knowledge_base_id)}"
            f"/documents/{http_client.path_segment(document_id)}"
        ),
        text="Deleted knowledge document.",
    )


async def finalize_knowledge_document(
    knowledge_base_id: Annotated[str, Field(description="Knowledge-base id.")],
    document_id: Annotated[str, Field(description="Knowledge-document id.")],
) -> ToolResult:
    """Finalize a knowledge document and enqueue ingestion."""
    return await call(
        "POST",
        (
            f"/v1/knowledge-bases/{http_client.path_segment(knowledge_base_id)}"
            f"/documents/{http_client.path_segment(document_id)}/finalize"
        ),
        body={},
        text="Finalized knowledge document.",
    )


async def list_agent_evals(
    agent_id: Annotated[str, Field(description="Agent id.")],
) -> ToolResult:
    """List evals for an agent."""
    return await call(
        "GET",
        f"/v1/agents/{http_client.path_segment(agent_id)}/evals",
        text="Retrieved agent evals.",
    )


async def create_agent_eval(
    agent_id: Annotated[str, Field(description="Agent id.")],
    body: Annotated[dict[str, Any], Field(description="JSON body for POST /v1/agents/{id}/evals.")],
) -> ToolResult:
    """Create an eval for an agent."""
    return await call(
        "POST",
        f"/v1/agents/{http_client.path_segment(agent_id)}/evals",
        body=body,
        text="Created agent eval.",
    )


async def run_agent_eval(
    agent_id: Annotated[str, Field(description="Agent id.")],
    eval_id: Annotated[str, Field(description="Eval id.")],
) -> ToolResult:
    """Run one agent eval."""
    return await call(
        "POST",
        f"/v1/agents/{http_client.path_segment(agent_id)}/evals/{http_client.path_segment(eval_id)}/run",
        body={},
        text="Queued agent eval run.",
    )


async def get_eval(
    eval_id: Annotated[str, Field(description="Eval id.")],
) -> ToolResult:
    """Get eval detail and recent runs."""
    return await call(
        "GET", f"/v1/evals/{http_client.path_segment(eval_id)}", text="Retrieved eval."
    )


async def inspect_workspace(
    workspace_root: Annotated[str, Field(description="Workspace root to summarize.")] = ".",
    deep: Annotated[bool, Field(description="Include a shallow sample of source files.")] = False,
    metadata: Annotated[
        dict[str, Any] | None,
        Field(description="Optional client-supplied metadata to pass through."),
    ] = None,
) -> ToolResult:
    """Inspect a voice-agent codebase and return migration recommendations."""
    body = collect_workspace_metadata(workspace_root, deep=deep)
    if metadata:
        body["metadata"] = metadata
    return await call("POST", "/v1/inference/inspect", body=body, text="Inspected workspace.")


async def build_session_config(
    body: Annotated[
        dict[str, Any],
        Field(description="JSON body for POST /v1/inference/sessionconfig."),
    ],
) -> ToolResult:
    """Build a Speko SessionConfig draft from prose and hints."""
    return await call(
        "POST",
        "/v1/inference/sessionconfig",
        body=body,
        text="Built session config draft.",
    )


async def parse_external_config(
    format: Annotated[ExternalPlatform, Field(description="External config format.")],
    raw: Annotated[str, Field(description="Raw external configuration text or JSON.")],
) -> ToolResult:
    """Parse an external voice-agent config into a Speko SessionConfig draft."""
    return await call(
        "POST",
        "/v1/inference/parse-config",
        body={"format": format, "raw": raw},
        text="Parsed external config.",
    )


async def render_briefing(
    agent_id: Annotated[str, Field(description="Agent id.")],
    template_id: Annotated[str, Field(description="Briefing template id.")] = "web-in-app",
    version_id: Annotated[str | None, Field(description="Optional AgentVersion id.")] = None,
) -> ToolResult:
    """Render briefing markdown for an agent/version."""
    body: dict[str, Any] = {"agent_id": agent_id, "template_id": template_id}
    if version_id is not None:
        body["version_id"] = version_id
    return await call("POST", "/v1/inference/briefing", body=body, text="Rendered briefing.")


async def create_share_card(
    build_id: Annotated[str, Field(description="AgentVersion id or agent id.")],
    title: Annotated[str | None, Field(description="Optional share-card title.")] = None,
) -> ToolResult:
    """Create a public share card for an agent build."""
    body = {"title": title} if title else {}
    try:
        raw = await http_client.call_speko_api_raw(
            "POST",
            f"/v1/share/build/{http_client.path_segment(build_id)}/card.png",
            body,
        )
    except (http_client.SpekoApiError, http_client.SpekoAuthError) as exc:
        raise tool_error(
            exc, next_step="Check the build id and retry share-card creation."
        ) from exc
    if "application/json" in raw.content_type:
        try:
            payload = json.loads(raw.content.decode("utf-8") or "{}")
        except ValueError as exc:
            raise ToolError("Speko share-card endpoint returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise ToolError("Speko share-card endpoint returned an unexpected JSON payload.")
        return result(payload, text="Created share card.")
    return result(
        {
            "content_type": raw.content_type,
            "size_bytes": len(raw.content),
        },
        text="Created share card.",
    )
