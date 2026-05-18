"""Authenticated Speko API relay for hosted MCP action tools."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from fastmcp.server.dependencies import get_access_token

DEFAULT_API_BASE = "https://api.speko.dev"

_TEST_TRANSPORT: httpx.AsyncBaseTransport | None = None


class SpekoAuthError(RuntimeError):
    """Raised when a private MCP tool is called without MCP auth."""


class SpekoApiError(RuntimeError):
    """Clean exception for upstream API failures."""

    def __init__(self, status_code: int, message: str, *, trace_id: str | None = None) -> None:
        super().__init__(f"Speko API returned {status_code}: {message}")
        self.status_code = status_code
        self.message = message
        self.trace_id = trace_id


@dataclass(frozen=True)
class SpekoRawResponse:
    content: bytes
    content_type: str


def get_api_base() -> str:
    return (
        os.environ.get("SPEKOAI_API_URL")
        or os.environ.get("SPEKO_API_BASE")
        or os.environ.get("SPEKOAI_BASE_URL")
        or DEFAULT_API_BASE
    ).rstrip("/")


def _path_segment(value: str | int) -> str:
    return quote(str(value), safe="")


def _with_query(path: str, query: dict[str, Any | None]) -> str:
    clean = {key: value for key, value in query.items() if value not in (None, "")}
    return f"{path}?{urlencode(clean)}" if clean else path


def _bearer_token() -> str:
    access_token = get_access_token()
    if access_token is None:
        raise SpekoAuthError(
            "This tool requires the authenticated SpekoAI MCP endpoint. "
            "Connect /mcp-auth with OAuth or Authorization: Bearer <Speko API key>."
        )
    token = getattr(access_token, "token", access_token)
    if not isinstance(token, str) or not token:
        raise SpekoAuthError("Authenticated MCP token is missing or invalid.")
    return token


def _error_details(resp: httpx.Response) -> tuple[str, str | None]:
    trace_id = resp.headers.get("x-request-id") or resp.headers.get("x-trace-id")
    try:
        payload = resp.json()
    except ValueError:
        return (resp.text.strip() or resp.reason_phrase)[:500], trace_id
    if isinstance(payload, dict):
        trace = payload.get("trace_id") or payload.get("traceId")
        if isinstance(trace, str) and trace:
            trace_id = trace
        detail = payload.get("error") or payload.get("message") or payload.get("detail")
        if isinstance(detail, str) and detail:
            return detail[:500], trace_id
    return json.dumps(payload)[:500], trace_id


async def _call_speko_api(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    api_base = get_api_base()
    url = f"{api_base}/{path.lstrip('/')}"
    try:
        async with httpx.AsyncClient(
            timeout=60.0,
            follow_redirects=True,
            transport=_TEST_TRANSPORT,
        ) as client:
            resp = await client.request(
                method.upper(),
                url,
                headers={"Authorization": f"Bearer {_bearer_token()}"},
                json=body,
            )
    except httpx.HTTPError as exc:
        raise SpekoApiError(0, f"Unable to reach SpekoAI API at {api_base}: {exc}") from exc
    if resp.status_code >= 400:
        message, trace_id = _error_details(resp)
        raise SpekoApiError(resp.status_code, message, trace_id=trace_id)
    if not resp.content:
        return {}
    try:
        payload = resp.json()
    except ValueError as exc:
        raise SpekoApiError(resp.status_code, "Speko API returned a non-JSON response.") from exc
    if not isinstance(payload, dict):
        raise SpekoApiError(resp.status_code, "Speko API returned an unexpected JSON response.")
    return payload


async def _call_speko_api_raw(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> SpekoRawResponse:
    api_base = get_api_base()
    url = f"{api_base}/{path.lstrip('/')}"
    try:
        async with httpx.AsyncClient(
            timeout=60.0,
            follow_redirects=True,
            transport=_TEST_TRANSPORT,
        ) as client:
            resp = await client.request(
                method.upper(),
                url,
                headers={"Authorization": f"Bearer {_bearer_token()}"},
                json=body,
            )
    except httpx.HTTPError as exc:
        raise SpekoApiError(0, f"Unable to reach SpekoAI API at {api_base}: {exc}") from exc
    if resp.status_code >= 400:
        message, trace_id = _error_details(resp)
        raise SpekoApiError(resp.status_code, message, trace_id=trace_id)
    return SpekoRawResponse(
        content=resp.content,
        content_type=resp.headers.get("content-type", "application/octet-stream"),
    )


def tool_error_message(exc: Exception, *, next_step: str) -> str:
    trace_id = getattr(exc, "trace_id", None) or "unavailable"
    return f"{exc}; trace_id={trace_id}; next_step={next_step}"


async def get_balance() -> dict[str, Any]:
    return await _call_speko_api("GET", "/v1/credits/balance")


async def create_agent(payload: dict[str, Any]) -> dict[str, Any]:
    return await _call_speko_api("POST", "/v1/agents", payload)


async def build_session_config(body: dict[str, Any]) -> dict[str, Any]:
    return await _call_speko_api("POST", "/v1/inference/sessionconfig", body)


async def parse_config(format_: str, raw: str) -> dict[str, Any]:
    return await _call_speko_api(
        "POST", "/v1/inference/parse-config", {"format": format_, "raw": raw}
    )


async def inspect_workspace(body: dict[str, Any]) -> dict[str, Any]:
    return await _call_speko_api("POST", "/v1/inference/inspect", body)


async def deploy_agent(
    agent_id: str,
    session_config: dict[str, Any],
    *,
    briefing_markdown: str | None = None,
) -> dict[str, Any]:
    return await _call_speko_api(
        "POST",
        f"/v1/agents/{_path_segment(agent_id)}/deploy",
        {"session_config": session_config, "briefing_markdown": briefing_markdown},
    )


async def rollback_agent(agent_id: str, target_version_number: int) -> dict[str, Any]:
    return await _call_speko_api(
        "POST",
        f"/v1/agents/{_path_segment(agent_id)}/rollback",
        {"target_version_number": target_version_number},
    )


async def create_test_session(
    *,
    agent_id: str | None,
    session_config: dict[str, Any] | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"mode": "cascade", "metadata": {"source": "mcp"}}
    if agent_id:
        body["agentId"] = agent_id
    if session_config:
        intent = session_config.get("intent")
        if isinstance(intent, dict):
            body["intent"] = intent
        for source_key, target_key in [
            ("voice", "voice"),
            ("systemPrompt", "systemPrompt"),
            ("sttOptions", "sttOptions"),
        ]:
            if source_key in session_config and session_config[source_key] is not None:
                body[target_key] = session_config[source_key]
        llm = session_config.get("llmOptions")
        if isinstance(llm, dict):
            body["llm"] = llm
        stack = session_config.get("stackPreferences")
        if isinstance(stack, dict):
            body["constraints"] = stack
    return await _call_speko_api("POST", "/v1/sessions", body)


async def list_agent_calls(agent_id: str, *, since: str | None, limit: int) -> dict[str, Any]:
    path = _with_query(
        f"/v1/agents/{_path_segment(agent_id)}/calls",
        {"since": since, "limit": limit},
    )
    return await _call_speko_api("GET", path)


async def get_call(call_id: str) -> dict[str, Any]:
    return await _call_speko_api("GET", f"/v1/calls/{_path_segment(call_id)}")


async def list_agent_evals(agent_id: str) -> dict[str, Any]:
    return await _call_speko_api("GET", f"/v1/agents/{_path_segment(agent_id)}/evals")


async def add_agent_eval(agent_id: str, body: dict[str, Any]) -> dict[str, Any]:
    return await _call_speko_api("POST", f"/v1/agents/{_path_segment(agent_id)}/evals", body)


async def run_agent_eval(agent_id: str, eval_id: str) -> dict[str, Any]:
    return await _call_speko_api(
        "POST",
        f"/v1/agents/{_path_segment(agent_id)}/evals/{_path_segment(eval_id)}/run",
        {},
    )


async def render_agent_briefing(
    *,
    agent_id: str,
    template_id: str,
    version_id: str | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"agent_id": agent_id, "template_id": template_id}
    if version_id:
        body["version_id"] = version_id
    return await _call_speko_api("POST", "/v1/inference/briefing", body)


async def create_share_card(build_id: str, *, title: str | None = None) -> SpekoRawResponse:
    body = {"title": title} if title else {}
    return await _call_speko_api_raw(
        "POST", f"/v1/share/build/{_path_segment(build_id)}/card.png", body
    )
