"""Private hosted MCP tools that relay authenticated calls to Speko API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import ToolResult
from mcp.types import ResourceLink, TextContent
from pydantic import Field

from spekoai_mcp import http_client

ExternalPlatform = Literal["livekit", "pipecat", "retell", "vapi"]

ACTION_TOOL_NAMES = [
    "get_balance",
    "speko_inspect",
    "speko_build",
    "speko_migrate",
    "speko_deploy",
    "speko_rollback",
    "speko_test",
    "speko_logs",
    "speko_calls_get",
    "speko_evals_list",
    "speko_evals_run",
    "speko_evals_add_from_call",
    "speko_briefing",
    "speko_share",
    "speko_build_and_test",
    "speko_migrate_and_deploy",
]


def register_action_tools(mcp: FastMCP) -> None:
    mcp.tool(speko_inspect)
    mcp.tool(speko_build)
    mcp.tool(speko_migrate)
    mcp.tool(speko_deploy)
    mcp.tool(speko_rollback)
    mcp.tool(speko_test)
    mcp.tool(speko_logs)
    mcp.tool(speko_calls_get)
    mcp.tool(speko_evals_list)
    mcp.tool(speko_evals_run)
    mcp.tool(speko_evals_add_from_call)
    mcp.tool(speko_briefing)
    mcp.tool(speko_share)
    mcp.tool(speko_build_and_test)
    mcp.tool(speko_migrate_and_deploy)


def canonical_result(
    payload: dict[str, Any],
    *,
    text: str | None = None,
    links: list[ResourceLink] | None = None,
) -> ToolResult:
    content = [TextContent(type="text", text=text or json.dumps(payload, indent=2, sort_keys=True))]
    content.extend(links or [])
    return ToolResult(content=content, structured_content=payload)


def link(
    uri: str, *, name: str, description: str, mime_type: str = "application/json"
) -> ResourceLink:
    return ResourceLink(
        type="resource_link",
        uri=uri,
        name=name,
        description=description,
        mimeType=mime_type,
    )


def first_str(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def first_int(payload: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def as_list(payload: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def speko_tool_error(exc: Exception, *, next_step: str) -> ToolError:
    return ToolError(http_client.tool_error_message(exc, next_step=next_step))


def session_config_from(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("session_config")
    if not isinstance(value, dict):
        raise ToolError(
            "Speko response did not include session_config; "
            "trace_id=unavailable; next_step=Check /v1/inference output."
        )
    return value


def agent_payload_from(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("agent_create_payload")
    if isinstance(value, dict):
        return value
    return {
        "name": first_str(session_config_from(payload), "name") or "Speko voice agent",
        "systemPrompt": first_str(session_config_from(payload), "systemPrompt")
        or "You are a concise voice agent.",
        "intent": session_config_from(payload).get("intent") or {"language": "en"},
    }


def unmappable_tools(payload: dict[str, Any]) -> list[Any]:
    value = payload.get("unmappable_tools")
    if isinstance(value, list):
        return value
    diff = payload.get("diff")
    if isinstance(diff, dict) and isinstance(diff.get("unmappable_tools"), list):
        return diff["unmappable_tools"]
    return []


def version_resource_uri(agent_id: str, version_number: int | None) -> str:
    return f"speko://agents/{agent_id}/version/{version_number or 'latest'}"


async def deploy_payload(payload: dict[str, Any]) -> dict[str, Any]:
    agent_id = first_str(payload, "agent_id")
    if not agent_id:
        created = await http_client.create_agent(agent_payload_from(payload))
        agent_id = first_str(created, "id", "agent_id")
        if not agent_id:
            raise ToolError("Speko create-agent response did not include an id.")
        payload["agent_id"] = agent_id
    version = await http_client.deploy_agent(
        agent_id,
        session_config_from(payload),
        briefing_markdown=first_str(payload, "briefing_markdown", "briefing_md"),
    )
    payload["version"] = version
    payload["version_number"] = first_int(version, "version_number")
    return payload


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
        for path in list(root.rglob("*.ts"))[:30] + list(root.rglob("*.py"))[:30]:
            if "node_modules" not in path.parts and path.is_file():
                files[str(path.relative_to(root))] = path.read_text(
                    encoding="utf-8",
                    errors="ignore",
                )[:50_000]
    return {"workspace_root": str(root), "files": files, "deep": deep}


async def speko_inspect(
    workspace_root: Annotated[str, Field(description="Workspace root to summarize.")] = ".",
    deep: Annotated[bool, Field(description="Include a shallow sample of source files.")] = False,
    metadata: Annotated[
        dict[str, Any] | None, Field(description="Optional client-supplied metadata.")
    ] = None,
) -> ToolResult:
    """Inspect a voice-agent codebase and return migration recommendations."""
    body = collect_workspace_metadata(workspace_root, deep=deep)
    if metadata:
        body["metadata"] = metadata
    try:
        payload = await http_client.inspect_workspace(body)
    except (http_client.SpekoApiError, http_client.SpekoAuthError) as exc:
        raise speko_tool_error(exc, next_step="Reconnect to /mcp-auth and retry inspect.") from exc
    return canonical_result(payload)


async def speko_build(
    prose: Annotated[str, Field(description="Plain-English description of the voice agent.")],
    deploy: Annotated[
        bool, Field(description="Create/deploy the generated agent version.")
    ] = False,
    workspace_root: Annotated[str, Field(description="Optional workspace root for hints.")] = ".",
) -> ToolResult:
    """Build a Speko SessionConfig from prose."""
    if not prose.strip():
        raise ToolError("prose is required")
    body = {
        "prose": prose,
        "workspace_context": collect_workspace_metadata(workspace_root, deep=False),
    }
    try:
        payload = await http_client.build_session_config(body)
        if deploy:
            payload = await deploy_payload(payload)
    except (http_client.SpekoApiError, http_client.SpekoAuthError) as exc:
        raise speko_tool_error(exc, next_step="Fix the prompt/config or re-authenticate.") from exc
    return canonical_result(payload, text=first_str(payload, "briefing_markdown"))


async def speko_migrate(
    from_platform: Annotated[ExternalPlatform, Field(description="Source platform.")],
    config_path: Annotated[str, Field(description="Path to the source config file.")],
    deploy: Annotated[
        bool, Field(description="Deploy after conversion if no tools need review.")
    ] = False,
) -> ToolResult:
    """Convert a LiveKit, Pipecat, Retell, or Vapi config into Speko."""
    path = Path(config_path).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise ToolError(f"config_path is not a readable file: {path}")
    raw = path.read_text(encoding="utf-8")
    try:
        payload = await http_client.parse_config(from_platform, raw)
        review_required = bool(unmappable_tools(payload))
        payload["review_required"] = review_required
        if deploy and not review_required:
            payload = await deploy_payload(payload)
    except (http_client.SpekoApiError, http_client.SpekoAuthError) as exc:
        raise speko_tool_error(exc, next_step="Check the config path and source format.") from exc
    return canonical_result(payload, text=first_str(payload, "briefing_markdown"))


async def speko_deploy(
    agent_id: Annotated[str, Field(description="Agent id to deploy to.")],
    session_config: Annotated[dict[str, Any], Field(description="Speko SessionConfig to deploy.")],
    briefing_markdown: Annotated[
        str | None, Field(description="Optional briefing markdown.")
    ] = None,
) -> ToolResult:
    """Deploy a SessionConfig as a new immutable AgentVersion."""
    try:
        version = await http_client.deploy_agent(
            agent_id,
            session_config,
            briefing_markdown=briefing_markdown,
        )
    except (http_client.SpekoApiError, http_client.SpekoAuthError) as exc:
        raise speko_tool_error(exc, next_step="Check the agent id and session_config.") from exc
    version_number = first_int(version, "version_number")
    uri = version_resource_uri(agent_id, version_number)
    return canonical_result(
        {"agent_id": agent_id, "version": version, "resource_uri": uri},
        links=[link(uri, name=f"v{version_number or 'latest'}", description="Agent version")],
    )


async def speko_rollback(
    agent_id: Annotated[str, Field(description="Agent id to roll back.")],
    target_version_number: Annotated[int, Field(description="Historical version number.")],
) -> ToolResult:
    """Roll an agent back by copying a historical version into a new live version."""
    try:
        version = await http_client.rollback_agent(agent_id, target_version_number)
    except (http_client.SpekoApiError, http_client.SpekoAuthError) as exc:
        raise speko_tool_error(exc, next_step="Check the agent id and version number.") from exc
    version_number = first_int(version, "version_number")
    uri = version_resource_uri(agent_id, version_number)
    return canonical_result(
        {"agent_id": agent_id, "version": version, "resource_uri": uri},
        links=[link(uri, name=f"v{version_number or 'latest'}", description="Rollback version")],
    )


async def speko_test(
    agent_id: Annotated[str | None, Field(description="Optional deployed agent id.")] = None,
    session_config: Annotated[dict[str, Any] | None, Field(description="Draft config.")] = None,
) -> ToolResult:
    """Start a test browser/voice session for an agent or draft config."""
    try:
        session = await http_client.create_test_session(
            agent_id=agent_id, session_config=session_config
        )
    except (http_client.SpekoApiError, http_client.SpekoAuthError) as exc:
        raise speko_tool_error(exc, next_step="Check credits and session config.") from exc
    session_id = first_str(session, "sessionId", "session_id", "id") or "unknown"
    uri = f"speko://calls/{session_id}"
    return canonical_result(
        {"agent_id": agent_id, "test_session": session, "call_resource_uri": uri},
        links=[link(uri, name=session_id, description="Test call")],
    )


async def speko_logs(
    agent_id: Annotated[str, Field(description="Agent id to list calls for.")],
    since: Annotated[str | None, Field(description="Optional ISO timestamp lower bound.")] = None,
    limit: Annotated[int, Field(description="Maximum calls to return.", ge=1, le=100)] = 50,
) -> ToolResult:
    """List recent calls/logs for an agent."""
    try:
        payload = await http_client.list_agent_calls(agent_id, since=since, limit=limit)
    except (http_client.SpekoApiError, http_client.SpekoAuthError) as exc:
        raise speko_tool_error(exc, next_step="Check the agent id.") from exc
    calls = as_list(payload, "calls", "entries")
    return canonical_result(
        {"agent_id": agent_id, "calls": calls, "count": len(calls)},
        links=[
            link(f"speko://calls/{call_id}", name=call_id, description="Call trace")
            for call in calls
            if (call_id := first_str(call, "id", "call_id", "session_id"))
        ],
    )


async def speko_calls_get(
    call_id: Annotated[str, Field(description="Call id to fetch.")],
) -> ToolResult:
    """Fetch full call detail including transcript and span tree."""
    try:
        call = await http_client.get_call(call_id)
    except (http_client.SpekoApiError, http_client.SpekoAuthError) as exc:
        raise speko_tool_error(exc, next_step="Check the call id.") from exc
    call_uri = f"speko://calls/{call_id}"
    recording_uri = f"{call_uri}/recording"
    return canonical_result(
        {"call_id": call_id, "call": call, "call_resource_uri": call_uri},
        links=[
            link(call_uri, name=call_id, description="Call transcript"),
            link(
                recording_uri,
                name=f"{call_id} recording",
                description="Recording",
                mime_type="audio/wav",
            ),
        ],
    )


async def speko_evals_list(
    agent_id: Annotated[str, Field(description="Agent id to list evals for.")],
) -> ToolResult:
    """List regression evals for an agent."""
    try:
        payload = await http_client.list_agent_evals(agent_id)
    except (http_client.SpekoApiError, http_client.SpekoAuthError) as exc:
        raise speko_tool_error(exc, next_step="Check the agent id.") from exc
    evals = as_list(payload, "evals", "entries")
    return canonical_result({"agent_id": agent_id, "evals": evals, "count": len(evals)})


async def speko_evals_run(
    agent_id: Annotated[str, Field(description="Agent id.")],
    eval_id: Annotated[str, Field(description="Eval id to run.")],
) -> ToolResult:
    """Run one regression eval."""
    try:
        run = await http_client.run_agent_eval(agent_id, eval_id)
    except (http_client.SpekoApiError, http_client.SpekoAuthError) as exc:
        raise speko_tool_error(exc, next_step="Check the agent/eval ids.") from exc
    return canonical_result({"agent_id": agent_id, "eval_id": eval_id, "run": run})


async def speko_evals_add_from_call(
    agent_id: Annotated[str, Field(description="Agent that owns the call.")],
    call_id: Annotated[str, Field(description="Call id to promote.")],
    name: Annotated[str | None, Field(description="Optional eval name.")] = None,
    expected_behavior: Annotated[
        str,
        Field(description="Expected future behavior."),
    ] = "The agent should resolve the failed behavior observed in the source call.",
    assertion_kind: Annotated[str, Field(description="Assertion kind.")] = "custom",
    block_deploy_on_fail: Annotated[bool, Field(description="Block deploys on fail.")] = True,
) -> ToolResult:
    """Promote a call into a regression eval."""
    body = {
        "name": name or f"Regression from call {call_id}",
        "description": f"Created from MCP after reviewing call {call_id}.",
        "expected_behavior": expected_behavior,
        "assertion_kind": assertion_kind,
        "assertion_config": {},
        "input_kind": "transcript",
        "input_payload": {"call_id": call_id},
        "source_call_id": call_id,
        "block_deploy_on_fail": block_deploy_on_fail,
    }
    try:
        eval_item = await http_client.add_agent_eval(agent_id, body)
    except (http_client.SpekoApiError, http_client.SpekoAuthError) as exc:
        raise speko_tool_error(exc, next_step="Check the call id and eval name.") from exc
    eval_id = first_str(eval_item, "id", "eval_id")
    return canonical_result(
        {"agent_id": agent_id, "call_id": call_id, "eval": eval_item, "eval_id": eval_id}
    )


async def speko_briefing(
    agent_id: Annotated[str, Field(description="Agent id.")],
    template_id: Annotated[str, Field(description="Briefing template id.")] = "web-in-app",
    version_id: Annotated[str | None, Field(description="Optional AgentVersion id.")] = None,
) -> ToolResult:
    """Render briefing markdown for an agent/version."""
    try:
        briefing = await http_client.render_agent_briefing(
            agent_id=agent_id,
            template_id=template_id,
            version_id=version_id,
        )
    except (http_client.SpekoApiError, http_client.SpekoAuthError) as exc:
        raise speko_tool_error(exc, next_step="Check the agent id and template id.") from exc
    markdown = first_str(briefing, "rendered_markdown", "briefing_markdown")
    return canonical_result(briefing, text=markdown)


async def speko_share(
    build_id: Annotated[str, Field(description="AgentVersion id or agent id.")],
    title: Annotated[str | None, Field(description="Optional share-card title.")] = None,
) -> ToolResult:
    """Create a public share card for an agent build."""
    try:
        raw = await http_client.create_share_card(build_id, title=title)
    except (http_client.SpekoApiError, http_client.SpekoAuthError) as exc:
        raise speko_tool_error(exc, next_step="Check the build id.") from exc
    payload: dict[str, Any] = {}
    if "application/json" in raw.content_type:
        payload = json.loads(raw.content.decode("utf-8"))
    png_url = (
        first_str(payload, "png_url", "share_url", "url")
        or f"https://api.speko.dev/v1/share/build/{build_id}.png"
    )
    return canonical_result(
        {"build_id": build_id, "png_url": png_url, "share": payload},
        links=[
            link(
                png_url,
                name=f"{build_id} share card",
                description="Share card PNG",
                mime_type="image/png",
            )
        ],
    )


async def speko_build_and_test(
    prose: Annotated[str, Field(description="Plain-English voice agent description.")],
    workspace_root: Annotated[str, Field(description="Optional workspace root.")] = ".",
) -> ToolResult:
    """Build a draft SessionConfig and start a test session."""
    build = await speko_build(prose, deploy=False, workspace_root=workspace_root)
    payload = dict(build.structured_content or {})
    test = await speko_test(
        agent_id=first_str(payload, "agent_id"), session_config=session_config_from(payload)
    )
    return canonical_result({"build": payload, "test": test.structured_content or {}})


async def speko_migrate_and_deploy(
    from_platform: Annotated[ExternalPlatform, Field(description="Source platform.")],
    config_path: Annotated[str, Field(description="Path to source config.")],
) -> ToolResult:
    """Migrate and deploy only when all tools are mapped."""
    result = await speko_migrate(from_platform=from_platform, config_path=config_path, deploy=True)
    return canonical_result(dict(result.structured_content or {}), text=None)
