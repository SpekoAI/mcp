"""Hosted MCP tools that relay authenticated calls to the Speko API.

Body shapes inlined into tool descriptions below are derived from the
zod validators in `apps/server/src/routes/` (sessions.ts,
sessions-phone.ts, agents.ts, phone-numbers.ts, knowledge-bases.ts,
agent-evals.ts, inference.ts). Keep them in sync when the route
schemas change; an LLM client only sees these descriptions.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import re
import socket
from functools import wraps
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp.types import TextContent, ToolAnnotations
from pydantic import Field

from spekoai_mcp import http_client
from spekoai_mcp.profiles import DIRECTORY_PROFILES, current_profile
from spekoai_mcp.tool_text import payload_text

ExternalPlatform = Literal["livekit", "pipecat", "retell", "vapi"]

# --- AI disclosure -----------------------------------------------------------
#
# Agents created or dialled through a PUBLISHED DIRECTORY HOST disclose that
# they are an AI. Enforced here, in the
# relay, rather than left to whatever system prompt a caller happens to pass:
# assistant-directory policy requires the platform to inject the disclosure,
# not to trust configuration.
#
# Two paths are covered because a reviewer hit both:
#   - the first thing spoken on the call (DISCLOSURE_OPENER)
#   - the answer when someone asks "am I talking to a person?" (DISCLOSURE_RULE)
#
# Scope: directory deployments only. Direct MCP clients keep their existing
# behaviour on the customer host, as do the dashboard and the platform API.
DISCLOSURE_OPENER = "Before we start, I should say I am an AI assistant."

DISCLOSURE_RULE = (
    "AI DISCLOSURE (mandatory, enforced by the Speko MCP server and not "
    "overridable): you are an AI assistant. Say so plainly at the start of "
    "the call, and confirm it whenever anyone asks whether they are speaking "
    "to a person, a bot, or an AI. Never claim or imply that you are human. "
    "This instruction overrides any other persona guidance in this prompt."
)


def apply_ai_disclosure(body: dict[str, Any]) -> dict[str, Any]:
    """Force AI disclosure into an agent or phone-session body.

    Mutates and returns ``body``. Idempotent: re-applying leaves it alone, so
    a caller who already discloses is not made to say it twice.
    """
    prompt = body.get("systemPrompt")
    if isinstance(prompt, str):
        if DISCLOSURE_RULE not in prompt:
            body["systemPrompt"] = f"{prompt.rstrip()}\n\n{DISCLOSURE_RULE}"
    elif prompt is None:
        body["systemPrompt"] = DISCLOSURE_RULE

    first = body.get("firstMessage")
    if isinstance(first, str) and first.strip():
        if DISCLOSURE_OPENER not in first:
            body["firstMessage"] = f"{DISCLOSURE_OPENER} {first.lstrip()}"
    else:
        body["firstMessage"] = DISCLOSURE_OPENER
    return body


def apply_directory_disclosure(body: dict[str, Any]) -> dict[str, Any]:
    """Apply AI disclosure on every published directory surface.

    Fires for any profile in ``DIRECTORY_PROFILES`` — Anthropic's MCP
    Directory (`connector`) and OpenAI's Plugin Directory (`chatgpt`) both
    require that a person picking up the phone is told they are speaking to
    an AI. Deployments without a configured profile are never rewritten.
    """
    if current_profile() in DIRECTORY_PROFILES:
        apply_ai_disclosure(body)
    return body


CREATE_AGENT_NEXT_STEP = (
    "For create_agent, pass a body like "
    "{'name':'Support','systemPrompt':'...','intent':{'language':'en'}}. "
    "For migrations, call parse_external_config first and pass its "
    "agent_create_payload as create_agent.body."
)

CREATE_SESSION_NEXT_STEP = (
    "For create_session, pass a body like {'agentId':'<agent id>'} or "
    "{'intent':{'language':'en'}}. Add mode:'s2s' for speech-to-speech."
)

CREATE_PHONE_SESSION_NEXT_STEP = (
    "For create_phone_session, pass a body like "
    "{'to':'+12015551234','agentId':'<agent id>'} or "
    "{'to':'+12015551234','intent':{'language':'en'}}."
)

UPDATE_AGENT_NEXT_STEP = (
    "For update_agent, pass only the fields to change, for example "
    "{'systemPrompt':'...'} or {'intent':{'language':'es'}}."
)

CREATE_AGENT_TOOL_NEXT_STEP = (
    "For create_agent_tool, pass a body like {'name':'lookup_order',"
    "'description':'Look up an order by id.',"
    "'parameters':{'type':'object','properties':{}},"
    "'source':{'kind':'webhook','url':'https://...','secret':'<min 8 chars>'}}."
)

TOOL_SOURCE_KINDS = ("inline", "webhook", "builtin", "integration")

_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")

ACTION_TOOL_NAME_BY_FUNCTION = {
    "get_organization": "organization.get",
    "get_credit_balance": "credits.balance.get",
    "list_credit_ledger": "credits.ledger.list",
    "get_usage_summary": "usage.summary.get",
    "list_agents": "agents.list",
    "preview_stacks": "agents.preview_stacks",
    "create_agent": "agents.create",
    "get_agent": "agents.get",
    "update_agent": "agents.update",
    "delete_agent": "agents.delete",
    "list_agent_tools": "agents.tools.list",
    "create_agent_tool": "agents.tools.create",
    "get_agent_tool": "agents.tools.get",
    "update_agent_tool": "agents.tools.update",
    "delete_agent_tool": "agents.tools.delete",
    "deploy_agent": "agents.deploy",
    "rollback_agent": "agents.rollback",
    "list_agent_versions": "agents.versions.list",
    "test_call_agent": "agents.test_call",
    "create_session": "sessions.create",
    "create_phone_session": "sessions.phone.create",
    "list_sessions": "sessions.list",
    "get_session": "sessions.get",
    "get_session_transcript": "sessions.transcript.get",
    "get_session_recording": "sessions.recording.get",
    "list_agent_calls": "agents.calls.list",
    "get_call": "calls.get",
    "get_call_recording": "calls.recording.get",
    "list_phone_numbers": "phone_numbers.list",
    "search_available_phone_numbers": "phone_numbers.available.search",
    "create_phone_number": "phone_numbers.create",
    "get_phone_number": "phone_numbers.get",
    "update_phone_number": "phone_numbers.update",
    "delete_phone_number": "phone_numbers.delete",
    "create_knowledge_base": "knowledge_bases.create",
    "list_knowledge_bases": "knowledge_bases.list",
    "get_knowledge_base": "knowledge_bases.get",
    "delete_knowledge_base": "knowledge_bases.delete",
    "list_knowledge_documents": "knowledge_bases.documents.list",
    "create_knowledge_document": "knowledge_bases.documents.create",
    "get_knowledge_document": "knowledge_bases.documents.get",
    "delete_knowledge_document": "knowledge_bases.documents.delete",
    "finalize_knowledge_document": "knowledge_bases.documents.finalize",
    "list_agent_evals": "agents.evals.list",
    "create_agent_eval": "agents.evals.create",
    "run_agent_eval": "agents.evals.run",
    "get_eval": "evals.get",
    "list_monitors": "agents.monitors.list",
    "create_monitor": "agents.monitors.create",
    "update_monitor": "agents.monitors.update",
    "delete_monitor": "agents.monitors.delete",
    "list_monitor_events": "agents.monitors.events.list",
    "list_online_eval_results": "agents.monitoring.results.list",
    "inspect_workspace": "migration.workspace.inspect",
    "build_session_config": "migration.session_config.build",
    "parse_external_config": "migration.external_config.parse",
    "render_briefing": "migration.briefing.render",
    "create_share_card": "share_cards.create",
    "synthesize_speech": "audio.synthesize",
    "transcribe_audio": "audio.transcribe",
}

ACTION_TOOL_NAMES = list(ACTION_TOOL_NAME_BY_FUNCTION.values())

SPEKO_API_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Structured JSON payload returned by the Speko API or a Speko MCP helper. "
        "Fields vary by tool and endpoint."
    ),
    "additionalProperties": True,
}

READ_ONLY_ACTION_TOOL_NAMES = {
    "get_organization",
    "get_credit_balance",
    "list_credit_ledger",
    "get_usage_summary",
    "list_agents",
    "preview_stacks",
    "get_agent",
    "list_agent_tools",
    "get_agent_tool",
    "list_agent_versions",
    "list_sessions",
    "get_session",
    "get_session_transcript",
    "get_session_recording",
    "list_agent_calls",
    "get_call",
    "get_call_recording",
    "list_phone_numbers",
    "search_available_phone_numbers",
    "get_phone_number",
    "list_knowledge_bases",
    "get_knowledge_base",
    "list_knowledge_documents",
    "get_knowledge_document",
    "list_agent_evals",
    "get_eval",
    "list_monitors",
    "list_monitor_events",
    "list_online_eval_results",
    "inspect_workspace",
    "build_session_config",
    "parse_external_config",
    "render_briefing",
}

DESTRUCTIVE_ACTION_TOOL_NAMES = {
    "delete_agent",
    "delete_agent_tool",
    "rollback_agent",
    "delete_phone_number",
    "delete_knowledge_base",
    "delete_knowledge_document",
    "delete_monitor",
}


async def synthesize_speech(
    body: Annotated[
        dict[str, Any],
        Field(
            description=(
                "JSON body for POST /v1/synthesize. Required: text (1-50000 "
                "chars, must contain a speakable character) and intent "
                "({language: BCP-47 tag, region?: string, optimizeFor?: "
                "'balanced'|'accuracy'|'latency'|'cost'}). Optional: voice "
                "(string), model (upstream model such as "
                "'eleven_multilingual_v2' or 'sonic-2'), speed (0.5-2), "
                "instructions (speaking-style text, applied only when the "
                "resolved model is instruction-capable), spokenForm (bool; "
                "normalizes markdown, URLs and numbers before synthesis), "
                "sampleRate (16000|24000|44100|48000), constraints "
                "({allowedProviders?: {tts?: string[]}})."
            )
        ),
    ],
) -> ToolResult:
    """Synthesize speech from text, returning base64 audio.

    Routed across TTS providers by the intent. The audio is returned as base64
    with its content type and sample rate so a client can save or play it.
    """
    raw = await http_client.call_speko_api_raw("POST", "/v1/synthesize", body=body)
    if not raw.content:
        raise ToolError("Speko synthesize returned an empty body.")
    return result(
        {
            "audio_base64": base64.b64encode(raw.content).decode("ascii"),
            "content_type": raw.content_type,
            "size_bytes": len(raw.content),
            "sample_rate": body.get("sampleRate"),
        },
        # The payload is base64 audio; rendering it as text would flood the
        # model's context with megabytes of useless characters.
        text=f"Synthesized {len(raw.content)} bytes of {raw.content_type}.",
        summary_only=True,
    )


# Router refusals the Platform endpoint can still serve. Everything else — a
# provider error, a timeout, a rate limit — is a real failure and is raised
# rather than retried on a second backend, which would transcribe and bill the
# same audio twice.
_ROUTER_FALLBACK_CODES = frozenset(
    {
        "unsupported_media",
        "capability_unsupported",
        "authentication_failed",
        "invalid_request",
        "no_eligible_route",
    }
)

# Platform routing DEMOTES candidates that cannot serve a canonical parameter
# rather than SELECTING one that can, so an unpinned `wordTimestamps` request
# answers 422 no_capable_provider even though a capable model exists. This is
# the only model carrying the mapping today; the Router needs no equivalent,
# because its automatic routing filters on the capability itself.
_WORD_TIMESTAMP_STT_PIN = "gemini:gemini-3.5-transcribe"


def _is_wav(audio: bytes) -> bool:
    """Whether the bytes are a RIFF/WAVE container."""
    return len(audio) >= 12 and audio[:4] == b"RIFF" and audio[8:12] == b"WAVE"


async def transcribe_audio(
    audio_url: Annotated[
        str,
        Field(
            description=(
                "HTTPS URL of the audio to transcribe. Signed recording URLs "
                "from sessions.recording.get and calls.recording.get work "
                "directly. The server fetches the bytes and forwards them."
            )
        ),
    ],
    language: Annotated[
        str,
        Field(description="BCP-47 language tag such as 'en' or 'es-MX'."),
    ] = "en",
    keywords: Annotated[
        list[str] | None,
        Field(description="Domain terms to bias recognition, up to 200."),
    ] = None,
    word_timestamps: Annotated[
        bool,
        Field(
            description=(
                "Return per-word start/end timings alongside the transcript, "
                "for subtitles and alignment. Adds a `words` array of "
                "{text, start_ms, end_ms}."
            )
        ),
    ] = False,
) -> ToolResult:
    """Transcribe audio to text.

    Speech to text only: no audio is generated and none is returned.
    """
    audio, content_type = await _fetch_audio(audio_url)
    router_token = http_client.router_bearer_token()

    # The Router is the current transcription product, and its automatic
    # routing picks a model that can serve what was asked for. Two things keep
    # a request on the Platform endpoint instead, and both are properties of
    # the request rather than preferences: the Router takes WAV/PCM only, so a
    # call recording (Ogg/Opus) answers 415, and it authenticates Speko API
    # keys only, so an OAuth-delegated MCP session has no credential for it.
    if router_token and _is_wav(audio):
        try:
            return await _transcribe_via_router(
                audio,
                token=router_token,
                language=language,
                keywords=keywords,
                word_timestamps=word_timestamps,
            )
        except http_client.SpekoApiError as exc:
            if exc.code not in _ROUTER_FALLBACK_CODES:
                raise

    return await _transcribe_via_platform(
        audio,
        content_type=content_type,
        language=language,
        keywords=keywords,
        word_timestamps=word_timestamps,
    )


async def _transcribe_via_router(
    audio: bytes,
    *,
    token: str,
    language: str,
    keywords: list[str] | None,
    word_timestamps: bool,
) -> ToolResult:
    """Transcribe through the Router's batch endpoint."""
    options: dict[str, Any] = {}
    if keywords:
        options["keywords"] = keywords[:100]
    if word_timestamps:
        options["word_timestamps"] = True

    request_payload: dict[str, Any] = {
        "routing": {"mode": "auto", "objective": "balanced"},
        "language": language,
    }
    if options:
        request_payload["options"] = options

    response = await http_client.post_router_transcription(
        audio, request_payload=request_payload, token=token
    )
    text = (response.get("text") or "").strip()
    route = response.get("route") if isinstance(response.get("route"), dict) else {}
    payload: dict[str, Any] = {
        "text": text,
        "language": language,
        "provider": route.get("provider"),
        "model": route.get("model"),
    }
    words = response.get("words")
    if isinstance(words, list) and words:
        payload["words"] = words
    return result(payload, text=text or "No speech detected.")


async def _transcribe_via_platform(
    audio: bytes,
    *,
    content_type: str,
    language: str,
    keywords: list[str] | None,
    word_timestamps: bool,
) -> ToolResult:
    """Transcribe through the Platform one-shot endpoint.

    Reached when the Router cannot serve the request: a container it does not
    decode, or a session with no Speko API key to authenticate with.
    """
    intent: dict[str, Any] = {"language": language}
    headers = {"X-Speko-Intent": json.dumps(intent)}

    stt_options: dict[str, Any] = {}
    if keywords:
        stt_options["keywords"] = keywords[:200]
    if word_timestamps:
        stt_options["wordTimestamps"] = True
        headers["X-Speko-Constraints"] = json.dumps(
            {"allowedProviders": {"stt": [_WORD_TIMESTAMP_STT_PIN]}}
        )
    if stt_options:
        headers["X-Speko-Stt-Options"] = json.dumps(stt_options)

    raw = await http_client.post_speko_api_bytes(
        "/v1/transcribe",
        audio,
        content_type=content_type,
        extra_headers=headers,
    )
    stream = raw.content.decode("utf-8", errors="replace")
    text = _transcript_from_sse(stream)
    payload: dict[str, Any] = {"text": text, "language": language}
    done = _done_frame_from_sse(stream)
    provider, model = done.get("provider"), done.get("model")
    if isinstance(provider, str) and provider:
        payload["provider"] = provider
    if isinstance(model, str) and model:
        payload["model"] = model
    words = done.get("words")
    if isinstance(words, list) and words:
        # Platform publishes word timings in SECONDS; the Router and this tool
        # publish milliseconds, so one shape reaches the caller either way.
        payload["words"] = [
            {
                "text": word.get("text"),
                "start_ms": round(float(word["start"]) * 1000),
                "end_ms": round(float(word["end"]) * 1000),
                **({"speaker": word["speaker"]} if word.get("speaker") else {}),
            }
            for word in words
            if isinstance(word, dict)
            and word.get("start") is not None
            and word.get("end") is not None
        ]
    return result(payload, text=text or "No speech detected.")


# The caller chooses this URL, and the fetch runs from inside our network, so
# an unrestricted GET is a server-side request forgery primitive: a redirect to
# 169.254.169.254 would hand back the Cloud Run service account's token. Every
# hop is validated, and the body is capped so one large URL cannot exhaust
# memory in a process that serves every other tool.
_MAX_AUDIO_BYTES = 25 * 1024 * 1024
_MAX_REDIRECTS = 3


def _assert_public_address(address: str, *, source: str) -> None:
    """Refuse an address that is not globally routable."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:  # not an address we can judge; treat as untrusted
        raise ToolError(
            f"{source} is not an IP address that can be validated: {address!r}"
        ) from None
    if not parsed.is_global or parsed.is_multicast:
        raise ToolError(
            f"{source} is the non-public address {parsed}. "
            "Pass a publicly reachable URL, such as a signed recording URL."
        )


def _assert_fetchable(url: str) -> None:
    """Reject anything that is not a public https endpoint."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ToolError(f"audio_url must be an https:// URL, got {parsed.scheme or 'no'} scheme.")
    host = parsed.hostname
    if not host:
        raise ToolError("audio_url has no host.")
    try:
        resolved = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise ToolError(f"Unable to resolve {host}: {exc}") from exc
    for info in resolved:
        _assert_public_address(info[4][0], source="audio_url")


def _assert_connected_peer_is_public(response: httpx.Response) -> None:
    """Check the address actually connected to, not the one we resolved.

    `_assert_fetchable` resolves the hostname, then httpx resolves it again when
    it opens the connection. A short-TTL name can answer publicly for the first
    lookup and privately for the second, so validating only the first is
    check-then-use: the connection would land on the metadata service anyway.

    This runs before any of the body is read, so a rebound connection is refused
    rather than buffered and forwarded to the transcription API.
    """
    stream = response.extensions.get("network_stream")
    if stream is None:  # no live socket to inspect (mocked or already closed)
        return
    peer = stream.get_extra_info("server_addr")
    if not peer:
        return
    _assert_public_address(str(peer[0]), source="the address audio_url connected to")


async def _fetch_audio(url: str) -> tuple[bytes, str]:
    """Fetch audio over https, validating every redirect hop and capping size."""
    current = url
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as client:
        for _ in range(_MAX_REDIRECTS + 1):
            _assert_fetchable(current)
            try:
                async with client.stream("GET", current) as response:
                    _assert_connected_peer_is_public(response)
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise ToolError(f"{current} redirected without a Location header.")
                        # Re-validated at the top of the next iteration.
                        current = str(httpx.URL(current).join(location))
                        continue
                    response.raise_for_status()
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > _MAX_AUDIO_BYTES:
                            raise ToolError(
                                f"Audio at {current} exceeds the "
                                f"{_MAX_AUDIO_BYTES // (1024 * 1024)} MB limit."
                            )
                        chunks.append(chunk)
                    if not size:
                        raise ToolError(f"Audio at {current} is empty.")
                    return (
                        b"".join(chunks),
                        response.headers.get("content-type") or "application/octet-stream",
                    )
            except httpx.HTTPError as exc:
                raise ToolError(f"Unable to fetch audio from {current}: {exc}") from exc
    raise ToolError(f"audio_url exceeded {_MAX_REDIRECTS} redirects.")


def _transcript_from_sse(stream: str) -> str:
    """Read the transcript out of the `/v1/transcribe` event stream.

    The route emits named frames: `meta` (routing decision), `transcript`
    (incremental, `isFinal` marking the ones that count), `done` (carrying the
    authoritative assembled `text`), and `error`.

    So `done.text` wins when present — accumulating the `transcript` finals AND
    then appending `done.text` would return the transcript twice. The finals are
    only a fallback for a stream that ends without a `done` frame. An `error`
    frame arrives with HTTP 200, so it has to be raised from here or a failed
    transcription reads as silence.
    """
    finals: list[str] = []
    for name, payload in _parse_sse(stream):
        if name == "error":
            detail = payload.get("error") or payload.get("code") or "unknown error"
            raise ToolError(f"Transcription failed: {detail}")
        if name == "done":
            text = payload.get("text")
            if isinstance(text, str):
                return text.strip()
        elif name == "transcript" and payload.get("isFinal") is True:
            piece = payload.get("text")
            if isinstance(piece, str) and piece.strip():
                finals.append(piece.strip())
    return " ".join(finals).strip()


def _done_frame_from_sse(stream: str) -> dict[str, Any]:
    """The `done` frame's payload, or an empty dict when the stream had none.

    Read separately from {@link _transcript_from_sse} so the transcript's
    assembly rules — done.text wins, trailing finals are the fallback — stay in
    one place and are not re-derived here.
    """
    for name, payload in _parse_sse(stream):
        if name == "done":
            return payload
    return {}


def _parse_sse(stream: str) -> list[tuple[str, dict[str, Any]]]:
    """Split an SSE body into (event name, JSON payload) pairs.

    Frames are separated by a blank line. An unnamed frame is `message` per the
    spec. Frames whose data is absent, unparseable, or not an object are
    skipped rather than raised on: a partial flush must not fail the whole read.
    """
    events: list[tuple[str, dict[str, Any]]] = []
    for frame in re.split(r"\n\s*\n", stream):
        if not frame.strip():
            continue
        name = "message"
        data_lines: list[str] = []
        for line in frame.splitlines():
            if line.startswith("event:"):
                name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        if not data_lines:
            continue
        body = "\n".join(data_lines)
        if not body or body == "[DONE]":
            continue
        try:
            payload = json.loads(body)
        except ValueError:
            continue
        if isinstance(payload, dict):
            events.append((name, payload))
    return events


def register_action_tools(mcp: FastMCP) -> None:
    for tool in [
        get_organization,
        get_credit_balance,
        list_credit_ledger,
        get_usage_summary,
        list_agents,
        preview_stacks,
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
        test_call_agent,
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
        list_monitors,
        create_monitor,
        update_monitor,
        delete_monitor,
        list_monitor_events,
        list_online_eval_results,
        inspect_workspace,
        build_session_config,
        parse_external_config,
        render_briefing,
        create_share_card,
        synthesize_speech,
        transcribe_audio,
    ]:
        name = tool.__name__
        public_name = ACTION_TOOL_NAME_BY_FUNCTION[name]
        title = tool_title(name)
        attributed_tool = _with_action_attribution(tool, public_name)
        mcp.tool(
            attributed_tool,
            name=public_name,
            title=title,
            output_schema=SPEKO_API_OUTPUT_SCHEMA,
            annotations=ToolAnnotations(
                title=title,
                read_only_hint=name in READ_ONLY_ACTION_TOOL_NAMES,
                destructive_hint=name in DESTRUCTIVE_ACTION_TOOL_NAMES,
                idempotent_hint=name in READ_ONLY_ACTION_TOOL_NAMES,
                open_world_hint=True,
            ),
        )


def _with_action_attribution(tool: Any, action_id: str) -> Any:
    """Bind the public dotted tool name while a handwritten relay executes."""

    @wraps(tool)
    async def attributed(*args: Any, **kwargs: Any) -> Any:
        token = http_client.set_current_action_id(action_id)
        try:
            return await tool(*args, **kwargs)
        finally:
            http_client.reset_current_action_id(token)

    return attributed


def tool_title(name: str) -> str:
    """Turn snake_case tool names into compact UI titles."""
    replacements = {
        "id": "ID",
        "api": "API",
        "mcp": "MCP",
        "s2s": "S2S",
        "url": "URL",
    }
    return " ".join(replacements.get(part, part.capitalize()) for part in name.split("_"))


def result(
    payload: dict[str, Any],
    text: str = "Speko API request completed.",
    *,
    summary_only: bool = False,
) -> ToolResult:
    """Return `payload` in both blocks.

    `text` is an acknowledgment, not an answer — on a text-only host it is the
    *whole* result the model sees, so the payload has to go there too. Pass
    `summary_only=True` for payloads that must never be rendered as text
    (base64 audio, for one); those keep the acknowledgment.
    """
    body = text if summary_only else payload_text(payload)
    return ToolResult(
        content=[TextContent(type="text", text=body)],
        structured_content=payload,
    )


def list_result(payload: list[Any], text: str = "Speko API request completed.") -> ToolResult:
    structured = {"result": payload}
    return ToolResult(
        content=[TextContent(type="text", text=payload_text(structured))],
        structured_content=structured,
    )


def tool_error(exc: Exception, *, next_step: str) -> ToolError:
    return ToolError(http_client.tool_error_message(exc, next_step=next_step))


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
        raise tool_error(exc, next_step=next_step_for_error(exc, path=path)) from exc
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
        raise tool_error(exc, next_step=next_step_for_error(exc, path=path)) from exc
    if isinstance(payload, list):
        return list_result(payload, text=text)
    if isinstance(payload, dict):
        return result(payload, text=text)
    return result({"result": payload}, text=text)


def next_step_for_error(exc: Exception, *, path: str) -> str:
    if isinstance(exc, http_client.SpekoAuthError):
        return "Check authentication and retry the Speko MCP request."
    if isinstance(exc, http_client.SpekoApiError) and exc.status_code == 400:
        if path == "/v1/agents":
            return CREATE_AGENT_NEXT_STEP
        if path == "/v1/sessions":
            return CREATE_SESSION_NEXT_STEP
        if path == "/v1/sessions/phone":
            return CREATE_PHONE_SESSION_NEXT_STEP
        if path.endswith("/tools"):
            return CREATE_AGENT_TOOL_NEXT_STEP
        return (
            "Fix the request body using the validation details, then retry the Speko MCP request."
        )
    if isinstance(exc, http_client.SpekoApiError) and exc.status_code in {401, 403}:
        return "Check authentication and retry the Speko MCP request."
    return "Retry the Speko MCP request or inspect the Speko API response details."


def validate_create_agent_body(body: dict[str, Any]) -> None:
    missing = [
        key
        for key in ("name", "systemPrompt", "intent")
        if key not in body or body[key] in (None, "")
    ]
    if missing:
        raise ToolError(
            "Invalid create_agent body: missing required field(s) "
            f"{', '.join(missing)}; next_step={CREATE_AGENT_NEXT_STEP}"
        )

    if not isinstance(body.get("name"), str):
        raise ToolError(
            "Invalid create_agent body: body.name must be a string; "
            f"next_step={CREATE_AGENT_NEXT_STEP}"
        )
    if not isinstance(body.get("systemPrompt"), str):
        raise ToolError(
            "Invalid create_agent body: body.systemPrompt must be a string; "
            f"next_step={CREATE_AGENT_NEXT_STEP}"
        )

    intent = body.get("intent")
    if not isinstance(intent, dict):
        raise ToolError(
            "Invalid create_agent body: body.intent must be an object with a routing "
            "language, for example {'language':'en'}. It is not a use-case string "
            f"like 'customer_support'; next_step={CREATE_AGENT_NEXT_STEP}"
        )

    language = intent.get("language")
    if not isinstance(language, str) or len(language.strip()) < 2:
        raise ToolError(
            "Invalid create_agent body: body.intent.language must be a BCP-47 "
            f"language string such as 'en' or 'en-US'; next_step={CREATE_AGENT_NEXT_STEP}"
        )


def validate_intent_field(intent: Any, *, tool: str, next_step: str) -> None:
    """Validate an optional routing-intent object on a session/agent body."""
    if not isinstance(intent, dict):
        raise ToolError(
            f"Invalid {tool} body: body.intent must be an object with a routing "
            "language, for example {'language':'en'}. It is not a use-case "
            f"string like 'customer_support'; next_step={next_step}"
        )
    language = intent.get("language")
    if not isinstance(language, str) or len(language.strip()) < 2:
        raise ToolError(
            f"Invalid {tool} body: body.intent.language must be a BCP-47 "
            f"language string such as 'en' or 'en-US'; next_step={next_step}"
        )


def validate_session_target(body: dict[str, Any], *, tool: str, next_step: str) -> None:
    """Sessions need a persisted agent or an inline routing intent."""
    if not body.get("agentId") and not body.get("intent"):
        raise ToolError(
            f"Invalid {tool} body: either agentId or intent is required; next_step={next_step}"
        )
    if body.get("intent") is not None:
        validate_intent_field(body["intent"], tool=tool, next_step=next_step)


def validate_create_session_body(body: dict[str, Any]) -> None:
    # Mirrors apps/server/src/routes/sessions.ts createS2sSession: an
    # explicit s2s.provider + s2s.model pin needs neither agentId nor
    # intent — intent is only required for automatic provider selection.
    if body.get("mode") == "s2s":
        raw_spec = body.get("s2s")
        spec = raw_spec if isinstance(raw_spec, dict) else {}
        has_provider = spec.get("provider") not in (None, "")
        has_model = spec.get("model") not in (None, "")
        if has_provider != has_model:
            raise ToolError(
                "Invalid create_session body: s2s.provider and s2s.model must "
                f"be supplied together; next_step={CREATE_SESSION_NEXT_STEP}"
            )
        if has_provider and has_model:
            if body.get("intent") is not None:
                validate_intent_field(
                    body["intent"],
                    tool="create_session",
                    next_step=CREATE_SESSION_NEXT_STEP,
                )
            return
    validate_session_target(body, tool="create_session", next_step=CREATE_SESSION_NEXT_STEP)


def validate_create_phone_session_body(body: dict[str, Any]) -> None:
    to = body.get("to")
    if not isinstance(to, str) or not _E164_RE.match(to):
        raise ToolError(
            "Invalid create_phone_session body: body.to must be an E.164 "
            "phone number such as '+12015551234'; "
            f"next_step={CREATE_PHONE_SESSION_NEXT_STEP}"
        )
    validate_session_target(
        body, tool="create_phone_session", next_step=CREATE_PHONE_SESSION_NEXT_STEP
    )


def validate_update_agent_body(body: dict[str, Any]) -> None:
    if not body:
        raise ToolError(
            "Invalid update_agent body: pass at least one field to change; "
            f"next_step={UPDATE_AGENT_NEXT_STEP}"
        )
    if body.get("intent") is not None:
        validate_intent_field(body["intent"], tool="update_agent", next_step=UPDATE_AGENT_NEXT_STEP)


def validate_create_agent_tool_body(body: dict[str, Any]) -> None:
    missing = [
        key
        for key in ("name", "description", "parameters", "source")
        if key not in body or body[key] in (None, "")
    ]
    if missing:
        raise ToolError(
            "Invalid create_agent_tool body: missing required field(s) "
            f"{', '.join(missing)}; next_step={CREATE_AGENT_TOOL_NEXT_STEP}"
        )
    source = body.get("source")
    if not isinstance(source, dict) or source.get("kind") not in TOOL_SOURCE_KINDS:
        raise ToolError(
            "Invalid create_agent_tool body: body.source.kind must be one of "
            f"{', '.join(TOOL_SOURCE_KINDS)}; next_step={CREATE_AGENT_TOOL_NEXT_STEP}"
        )
    if source["kind"] == "webhook" and not (source.get("url") and source.get("secret")):
        raise ToolError(
            "Invalid create_agent_tool body: a webhook source requires url and "
            f"secret (>=8 chars); next_step={CREATE_AGENT_TOOL_NEXT_STEP}"
        )


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


async def preview_stacks(
    description: Annotated[
        str,
        Field(
            description=(
                "One line on what the agent does (e.g. 'a dental clinic phone "
                "receptionist that books appointments'). Used to tailor the picks."
            )
        ),
    ],
    region: Annotated[
        str,
        Field(
            description=(
                "Region for latency-aware picks. Default 'usa' (United States). "
                "Only 'usa' is supported today; more regions later."
            )
        ),
    ] = "usa",
) -> ToolResult:
    """Preview the THREE voice-stack options before creating an agent — so the user picks.

    Returns the same recommendation the dashboard's agent-create shows, as three tiers.
    Present them to the user with these labels and each tier's STT / LLM / TTS:
        premium        -> "Quality"
        balanced       -> "Fastest"
        cost_optimized -> "Cheapest"
    Ask which one they want (and confirm the region — default USA). Then call create_agent
    with the chosen objective mapped to intent.optimizeFor:
        Quality -> 'quality',  Fastest -> 'latency',  Cheapest -> 'cost'
    plus intent.region. The server then pins that tier's failover stack automatically, so
    the created agent matches exactly what you previewed.
    """
    return await call(
        "POST",
        "/v1/recommend-stack/from-description",
        body={
            "description": description,
            "constraints": {"language": "en", "region": region},
        },
        text=(
            "Stack options. Tier names map to objectives: premium=Quality, "
            "balanced=Fastest, cost_optimized=Cheapest. Each tier lists its "
            "stt, llm and tts components."
        ),
    )


async def create_agent(
    body: Annotated[
        dict[str, Any],
        Field(
            description=(
                "JSON body for POST /v1/agents. Required shape: "
                "{name: string, systemPrompt: string, intent: {language: string, "
                "optimizeFor?: 'latency'|'quality'|'cost', region?: string}}. "
                "intent.optimizeFor selects which failover stack the server pins: "
                "'quality' is the premium tier, 'latency' the fastest tier, 'cost' "
                "the cheapest. intent.region defaults to 'usa'. The intent field is "
                "routing metadata describing how to route the agent's audio, not a "
                "use-case string. The stack tiers available for a given description, "
                "and their stt/llm/tts components, are reported by preview_stacks. "
                "Migrated agents supply agent_create_payload from "
                "parse_external_config, which already carries an intent."
            )
        ),
    ],
) -> ToolResult:
    """Create a Speko agent.

    The agent is pinned to the failover stack matching intent.optimizeFor
    (quality / latency / cost) and intent.region. Stack tiers and their
    components for a given description are reported by preview_stacks."""
    validate_create_agent_body(body)
    apply_directory_disclosure(body)
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
    body: Annotated[
        dict[str, Any],
        Field(
            description=(
                "JSON body for PATCH /v1/agents/{id}. All fields optional; "
                "only supplied fields change, and null clears a nullable "
                "field. Fields: name (string, <=120 chars), systemPrompt "
                "(string), voice (string|null), intent ({language: BCP-47 "
                "string, optimizeFor?: 'latency'|'quality'|'cost'}), "
                "llmOptions ({temperature?: 0-2, maxTokens?: int, model?: "
                "string}|null), stackPreferences ({allowedProviders?: "
                "{stt?|llm?|tts?|s2s?: string[]}}|null), sttOptions "
                "({keywords?: string[] <=200 entries, language?: string, "
                "workload?: 'conversation'|'transcription'|'narration', "
                "diarization?: bool, speakersExpected?: 1-10, smartFormat?: "
                "bool, fillerWords?: bool, profanityFilter?: bool, "
                "wordTimestamps?: bool, "
                "providerOptions?: {provider: {param: value}}, <=2KB "
                "serialized}|null), ttsOptions ({speed?: 0.5-2, model?: "
                "string, workload?: same enum, providerOptions?: same "
                "shape}|null), runMode "
                "('cascade'|'s2s'), backgroundAudio ({ambient?: {clip: "
                "'office-ambience'|'city-ambience'|'forest-ambience'|"
                "'crowded-room'|'keyboard-typing'|'keyboard-typing2', "
                "volume?: 0-16, linear gain where 1 = the clip's own level; "
                "office-ambience needs ~5-10 to be audible, crowded-room "
                "distorts past ~1.6}}|null), speechNormalization "
                "({pronunciationDictionary?: {term: spoken}, "
                "textReplacements?: {from: to}}|null), webhooks "
                "({preCall?|postCall?|status?|analysis?|recording?: {url: "
                "string, headers?: object, timeoutMs?: 100-8000}|null}|null)."
            )
        ),
    ],
) -> ToolResult:
    """Update one Speko agent."""
    validate_update_agent_body(body)
    apply_directory_disclosure(body)
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
        dict[str, Any],
        Field(
            description=(
                "JSON body for POST /v1/agents/{agentId}/tools. Required "
                "shape: {name: identifier string (<=64 chars, "
                "[a-zA-Z_][a-zA-Z0-9_]*), description: string (1-1024 "
                "chars), parameters: JSON Schema object for the tool's "
                "arguments, source: one of {kind:'inline'} | "
                "{kind:'webhook', url: string URL, secret: string (>=8 "
                "chars), headers?: object, timeoutMs?: 100-4000, "
                "responseMode?: 'sync'|'async', asyncAck?: string} | "
                "{kind:'builtin', name: string, config?: any} | "
                "{kind:'integration', installationId: uuid, appKey: "
                "string, actionKey: string, config?: any}}."
            )
        ),
    ],
) -> ToolResult:
    """Create a tool on an agent."""
    validate_create_agent_tool_body(body)
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
        Field(
            description=(
                "JSON body for PATCH /v1/agents/{agentId}/tools/{toolId}. "
                "All fields optional: description (1-1024 chars), "
                "parameters (JSON Schema object), source (same shapes as "
                "create_agent_tool; for kind 'webhook', secret is optional "
                "on update; omit it to keep the existing secret)."
            )
        ),
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


async def test_call_agent(
    agent_id: Annotated[
        str,
        Field(
            description=(
                "Agent id to test (the agent under test). It answers first, using its saved config."
            )
        ),
    ],
    objective: Annotated[
        str | None,
        Field(
            description=(
                "Plain-language goal for a synthesized caller, e.g. 'Ask the all-you-can-eat "
                "price and whether there's a vegan broth, then book a table for 4 on Friday at "
                "7pm under Alex.' Provide this, OR caller_agent_id, OR caller_system_prompt."
            )
        ),
    ] = None,
    caller_agent_id: Annotated[
        str | None,
        Field(
            description=(
                "Use another persisted agent as the caller instead of a synthesized persona."
            )
        ),
    ] = None,
    caller_system_prompt: Annotated[
        str | None,
        Field(description="Full system prompt for the caller persona; overrides objective."),
    ] = None,
    caller_first_message: Annotated[
        str | None,
        Field(
            description=(
                "Caller's opening line. Defaults to listening first so the two agents don't "
                "greet over each other (only the agent under test greets)."
            )
        ),
    ] = None,
    ttl_seconds: Annotated[
        int | None,
        Field(description="Hard wall-clock cap in seconds (30-1800, default 180)."),
    ] = None,
    record: Annotated[
        bool | None,
        Field(
            description="Record the conversation. Default true (subject to org recording settings)."
        ),
    ] = None,
) -> ToolResult:
    """Start an agent-to-agent test call.

    Dispatches the agent under test plus a caller (a persona synthesized from
    `objective`, or another agent via `caller_agent_id`) into ONE LiveKit room with
    NO phone/SIP leg — so it CANNOT hairpin the way dialing the agent's own number
    does. Returns immediately with session ids; the conversation runs in the
    background. To review it: poll calls.get(agentSessionId) until it ends, then
    read sessions.transcript.get(agentSessionId) and calls.recording.get(agentSessionId).
    Provide exactly one of objective / caller_agent_id / caller_system_prompt.
    """
    body: dict[str, Any] = {}
    if objective is not None:
        body["objective"] = objective
    if caller_agent_id is not None:
        body["callerAgentId"] = caller_agent_id
    if caller_system_prompt is not None:
        body["callerSystemPrompt"] = caller_system_prompt
    if caller_first_message is not None:
        body["callerFirstMessage"] = caller_first_message
    if ttl_seconds is not None:
        body["ttlSeconds"] = ttl_seconds
    if record is not None:
        body["record"] = record
    return await call(
        "POST",
        f"/v1/agents/{http_client.path_segment(agent_id)}/test-call",
        body=body,
        text="Started agent-to-agent test call.",
    )


async def create_session(
    body: Annotated[
        dict[str, Any],
        Field(
            description=(
                "JSON body for POST /v1/sessions. Required: either agentId "
                "(string, persisted agent whose fields seed the session) or "
                "intent ({language: BCP-47 string such as 'en', region?: "
                "string (default 'global'), optimizeFor?: "
                "'balanced'|'accuracy'|'latency'|'cost'}); exception: mode "
                "'s2s' with both s2s.provider and s2s.model pinned needs "
                "neither. Optional: mode ('cascade' | 's2s'; when omitted, "
                "defaults to 's2s' if the referenced agent's run mode is "
                "'s2s', else 'cascade'), voice (string), "
                "systemPrompt (string), firstMessage (string <=2000 chars; "
                "null or '' opens the session listening), llm "
                "({temperature?: 0-2, maxTokens?: int}), ttsOptions "
                "({sampleRate?: int, speed?: number, workload?: "
                "'conversation'|'transcription'|'narration', "
                "providerOptions?: {provider: {param: value}}, <=2KB "
                "serialized}), sttOptions ({keywords?: string[] <=200 "
                "entries, language?: string, workload?: same enum, "
                "diarization?: bool, speakersExpected?: 1-10, smartFormat?: "
                "bool, fillerWords?: bool, profanityFilter?: bool, "
                "wordTimestamps?: bool, "
                "providerOptions?: same shape}), backgroundAudio "
                "({ambient?: {clip: 'office-ambience'|'city-ambience'|"
                "'forest-ambience'|'crowded-room'|'keyboard-typing'|"
                "'keyboard-typing2', volume?: 0-16, linear gain where 1 = the "
                "clip's own level; office-ambience needs ~5-10 to be audible, "
                "crowded-room distorts past ~1.6}}), constraints "
                "({allowedProviders?: {stt?|llm?|tts?|s2s?: string[]}}), "
                "metadata (object), ttlSeconds (int, <=86400, default 900), "
                "identity (string <=128). For mode 's2s' add s2s "
                "({provider?: 'openai'|'google'|'xai'|'inworld'|'alibaba', "
                "model?: string, voice?: string, systemPrompt?: string, "
                "temperature?: 0-2, inputSampleRate?/outputSampleRate?: "
                "16000|24000, tools?: [{name, description, parameters}]}); "
                "s2s ttlSeconds caps at 3600 (default 1800). Per-call "
                "fields win over agent defaults."
            )
        ),
    ],
) -> ToolResult:
    """Create a browser/WebRTC or server-to-server voice session."""
    validate_create_session_body(body)
    apply_directory_disclosure(body)
    return await call("POST", "/v1/sessions", body=body, text="Created session.")


async def create_phone_session(
    body: Annotated[
        dict[str, Any],
        Field(
            description=(
                "JSON body for POST /v1/sessions/phone. Required: to (E.164 "
                "string such as '+12015551234') plus either agentId "
                "(string) or intent ({language: BCP-47 string, optimizeFor?: "
                "'balanced'|'accuracy'|'latency'|'cost'}). Optional: from "
                "(E.164 string; defaults to an owned phone number), voice "
                "(string), systemPrompt (string), firstMessage (string), "
                "llm ({temperature?: 0-2, maxTokens?: int}), ttsOptions "
                "({sampleRate?: int, speed?: number, workload?: "
                "'conversation'|'transcription'|'narration', "
                "providerOptions?: {provider: {param: value}}, <=2KB "
                "serialized}), sttOptions ({keywords?: string[] <=200 "
                "entries, language?: string, workload?: same enum, "
                "diarization?: bool, speakersExpected?: 1-10, smartFormat?: "
                "bool, fillerWords?: bool, profanityFilter?: bool, "
                "wordTimestamps?: bool, "
                "providerOptions?: same shape}), telephony "
                "({region?: string, amd?: {mode: "
                "'agent'|'carrier'|'disabled' (default 'agent'), "
                "timeoutSeconds?: int <=60}}), constraints "
                "({allowedProviders?: {stt?|llm?|tts?: string[]}}), "
                "metadata (object). Per-call fields win over agent defaults."
            )
        ),
    ],
) -> ToolResult:
    """Create an outbound phone session."""
    validate_create_phone_session_body(body)
    apply_directory_disclosure(body)
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
    """Search phone numbers available to buy.

    Searching is read-only and always works. BUYING one does not follow from it.
    `phone_numbers.create` debits the workspace's prepaid credits (setup plus the
    first month; it is not a card payment or a checkout) and is gated on a
    business declaration. A brand-new workspace has none, so credits alone are
    never enough. Read `phone_numbers.kyb.get` and file it with
    `phone_numbers.kyb.submit` — two fields and an attestation, right here — and
    the purchase works immediately; approval is not required. The purchase tool
    itself is absent from some surfaces, in which case the declaration still
    applies and the number is bought in the dashboard.
    """
    return await call_list(
        "GET",
        http_client.with_query(
            "/v1/phone-numbers/available",
            {"areaCode": area_code, "locality": locality, "limit": limit},
        ),
        text="Retrieved available phone numbers.",
    )


async def create_phone_number(
    body: Annotated[
        dict[str, Any],
        Field(
            description=(
                "JSON body for POST /v1/phone-numbers. Required: e164 "
                "(E.164 string such as '+12015551234'; pick one from "
                "search_available_phone_numbers). Optional: direction "
                "('inbound'|'outbound'|'both', default 'outbound'), label "
                "(string <=120), agentId (string; agent that answers "
                "inbound calls on this number), dispatchMetadataTemplate "
                "(object)."
            )
        ),
    ],
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
        dict[str, Any],
        Field(
            description=(
                "JSON body for PATCH /v1/phone-numbers/{id}. All fields "
                "optional: direction ('inbound'|'outbound'|'both'), label "
                "(string <=120 | null), agentId (string to relink, null to "
                "unlink), dispatchMetadataTemplate (object | null)."
            )
        ),
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
    body: Annotated[
        dict[str, Any],
        Field(
            description=(
                "JSON body for POST /v1/knowledge-bases. Required shape: "
                "{agentId: string, name: string (1-120 chars)}. Optional: "
                "description (string <=2000)."
            )
        ),
    ],
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
        Field(
            description=(
                "JSON body for POST /v1/knowledge-bases/{kbId}/documents. "
                "Required shape: {filename: string (1-512 chars), "
                "contentType: MIME string such as 'text/markdown' (<=120 "
                "chars), sizeBytes: non-negative int}. Optional: metadata "
                "(object). The response includes an upload URL that accepts "
                "the file bytes by PUT. The document becomes searchable once "
                "finalize_knowledge_document records the upload."
            )
        ),
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
    body: Annotated[
        dict[str, Any],
        Field(
            description=(
                "JSON body for POST /v1/agents/{id}/evals. Required shape: "
                "{name: string (1-160 chars), expected_behavior: string}. "
                "Optional: description (string <=1024), assertion_kind "
                "('contains_phrase'|'tool_called'|'language_switched'|"
                "'within_latency'|'no_hallucination'|'custom', default "
                "'custom'), assertion_config (object, default {}), "
                "input_kind ('transcript'|'audio_url'|'assertion_only', "
                "default 'transcript'), input_payload (object, default {}), "
                "source_call_id (uuid of the call to promote), "
                "block_deploy_on_fail (bool, default true)."
            )
        ),
    ],
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


async def list_monitors(
    agent_id: Annotated[str, Field(description="Agent id.")],
) -> ToolResult:
    """List an agent's alert monitors — rules that watch an eval metric on scored
    production calls and notify when it crosses a threshold."""
    return await call_list(
        "GET",
        f"/v1/agents/{http_client.path_segment(agent_id)}/monitors",
        text="Retrieved monitors.",
    )


async def create_monitor(
    agent_id: Annotated[str, Field(description="Agent id.")],
    body: Annotated[
        dict[str, Any],
        Field(
            description=(
                "JSON body for POST /v1/agents/{agentId}/monitors. Required: name "
                "(string); metric_ref (e.g. 'pass_rate' | 'latency.p95_ms' | 'verdict'); "
                "aggregation ('single' = evaluate the latest scored call [recommended], "
                "'rolling_window', or 'on_run_complete'); operator ('lt'|'lte'|'gt'|'gte'"
                "|'eq'|'neq'); and a threshold — threshold_float (number) for numeric "
                "metrics OR threshold_string for the 'verdict' metric. Optional: "
                "description; window_size_runs (int); channels (object) selecting where "
                "the alert lands — {slack: {channel: '#alerts' | webhookUrl: 'https://"
                "hooks.slack.com/…'}, email: {recipients: 'a@b.com, c@d.com'}, webhook: "
                "{url, secret}}. Empty channels just tracks breaches in the dashboard."
            )
        ),
    ],
) -> ToolResult:
    """Create an alert monitor on an agent (fires when a metric crosses its threshold)."""
    return await call(
        "POST",
        f"/v1/agents/{http_client.path_segment(agent_id)}/monitors",
        body=body,
        text="Created monitor.",
    )


async def update_monitor(
    agent_id: Annotated[str, Field(description="Agent id.")],
    monitor_id: Annotated[str, Field(description="Monitor id.")],
    body: Annotated[
        dict[str, Any],
        Field(
            description=(
                "JSON body for PATCH /v1/agents/{agentId}/monitors/{monitorId}. All "
                "fields optional: name, description, metric_ref, aggregation, operator, "
                "threshold_float, threshold_string, window_size_runs, channels, status "
                "('active'|'deleted')."
            )
        ),
    ],
) -> ToolResult:
    """Update an alert monitor (threshold, channels, status, etc.)."""
    return await call(
        "PATCH",
        f"/v1/agents/{http_client.path_segment(agent_id)}/monitors/{http_client.path_segment(monitor_id)}",
        body=body,
        text="Updated monitor.",
    )


async def delete_monitor(
    agent_id: Annotated[str, Field(description="Agent id.")],
    monitor_id: Annotated[str, Field(description="Monitor id.")],
) -> ToolResult:
    """Delete an alert monitor."""
    return await call(
        "DELETE",
        f"/v1/agents/{http_client.path_segment(agent_id)}/monitors/{http_client.path_segment(monitor_id)}",
        text="Deleted monitor.",
    )


async def list_monitor_events(
    agent_id: Annotated[str, Field(description="Agent id.")],
    monitor_id: Annotated[str, Field(description="Monitor id.")],
) -> ToolResult:
    """List a monitor's firing history (breach events + observed values)."""
    return await call_list(
        "GET",
        f"/v1/agents/{http_client.path_segment(agent_id)}/monitors/{http_client.path_segment(monitor_id)}/events",
        text="Retrieved monitor events.",
    )


async def list_online_eval_results(
    agent_id: Annotated[str, Field(description="Agent id.")],
) -> ToolResult:
    """List production calls scored by online monitoring (verdict + scores per call)."""
    return await call_list(
        "GET",
        f"/v1/agents/{http_client.path_segment(agent_id)}/online-eval-results",
        text="Retrieved scored production calls.",
    )


async def inspect_workspace(
    files: Annotated[
        dict[str, str],
        Field(
            description=(
                "Client-supplied relative file names and text content. Send only files the "
                "user selected for migration analysis; the MCP server never reads its filesystem."
            ),
            max_length=60,
        ),
    ],
    metadata: Annotated[
        dict[str, Any] | None,
        Field(description="Optional client-supplied languages, frameworks, and migration hints."),
    ] = None,
) -> ToolResult:
    """Inspect a voice-agent codebase and return migration recommendations."""
    if any(len(content) > 200_000 for content in files.values()):
        raise ToolError("Each workspace file must be 200,000 characters or smaller.")
    if sum(len(content) for content in files.values()) > 500_000:
        raise ToolError("Workspace metadata must be 500,000 characters or smaller in total.")
    body: dict[str, Any] = {"files": files}
    if metadata:
        body["metadata"] = metadata
    return await call("POST", "/v1/inference/inspect", body=body, text="Inspected workspace.")


async def build_session_config(
    body: Annotated[
        dict[str, Any],
        Field(
            description=(
                "JSON body for POST /v1/inference/sessionconfig. All fields "
                "optional: prose (natural-language description of the agent "
                "to build), intent (routing-intent object such as "
                "{'language':'en'}), workspace_context ({repo_languages?: "
                "string[], framework_hints?: string[]}). Unknown extra keys "
                "are passed through."
            )
        ),
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
    """Parse an external voice-agent config into a Speko SessionConfig draft.

    Output is a scaffold: verify it against the raw config and check `warnings`
    and `unmappable_tools` before creating anything.
    """
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
