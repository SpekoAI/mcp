"""Hosted MCP tools for Speko Router API keys.

The Router is the OpenAI-compatible gateway at `api.speko.ai`: one key
routes speech-to-text, language-model, and text-to-speech traffic across
providers. The routing policy — language, use case, objective, max price,
and an ordered chain per stage — lives ON the key, so a caller who
configured it sends nothing per request.

These four tools give an agent the same provisioning surface the console
has, against the same control plane (`control.speko.ai`) and the same
validator (`apps/control-plane/src/key-policy.ts`). Shapes inlined into the
descriptions below are derived from that file plus the route handlers in
`apps/control-plane/src/index.ts`; keep them in sync when those change,
because an LLM client only sees these descriptions.

Two deliberate asymmetries with `action_tools`:

- Auth. Every tool here refuses a Speko API key and requires OAuth. See
  `router_client` for why.
- Policy defaults. `parseKeyPolicy` is strict in both directions: unknown
  keys are rejected AND all seven keys must be present, so a partial policy
  400s. These tools accept a partial policy and fill the defaults before
  sending, which is the difference between one call and an LLM looping on
  `invalid_policy`.
"""

from __future__ import annotations

import math
import re
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import ToolResult
from mcp.types import ToolAnnotations
from pydantic import Field

from spekoai_mcp import http_client, router_client
from spekoai_mcp.action_tools import SPEKO_API_OUTPUT_SCHEMA, result, tool_title

ROUTER_TOOL_NAME_BY_FUNCTION = {
    "list_router_keys": "router.keys.list",
    "create_router_key": "router.keys.create",
    "update_router_key": "router.keys.update",
    "revoke_router_key": "router.keys.revoke",
}

ROUTER_TOOL_NAMES = list(ROUTER_TOOL_NAME_BY_FUNCTION.values())

READ_ONLY_ROUTER_TOOL_NAMES = {"list_router_keys"}

# `revoke` sets `revoked_at`; the row is never deleted. Still destructive
# from the caller's side: the secret stops routing immediately and cannot
# be un-revoked.
DESTRUCTIVE_ROUTER_TOOL_NAMES = {"revoke_router_key"}

POLICY_STAGES = ("stt", "llm", "tts")
POLICY_OBJECTIVES = ("latency", "cost", "quality", "balanced")
POLICY_USE_CASES = (
    "realtime_agent",
    "phone_agent",
    "transcription",
    "voice_content",
    "translation",
    "other",
)
POLICY_KEYS = ("language", "useCase", "objective", "maxPricePerMinUsd", *POLICY_STAGES)
MAX_CHAIN_LENGTH = 4
MAX_KEY_NAME_LENGTH = 64

_LANGUAGE_RE = re.compile(r"^[A-Za-z0-9-]{1,35}$")
# Candidate ids are `provider:model` exactly as GET /v1/models returns them.
_CANDIDATE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*:.+$", re.IGNORECASE)
_CONTROL_CHARS = frozenset(chr(code) for code in (*range(0x20), 0x7F))

POLICY_SHAPE = (
    "{language:'en', useCase:null, objective:'balanced', maxPricePerMinUsd:null, "
    "stt:{chain:[]}, llm:{chain:[]}, tts:{chain:[]}}"
)

POLICY_NEXT_STEP = (
    "Pass only the policy fields you want to change; omitted fields take their "
    f"defaults, which are {POLICY_SHAPE}. objective is one of "
    f"{', '.join(POLICY_OBJECTIVES)}; useCase is null or one of "
    f"{', '.join(POLICY_USE_CASES)}; a chain holds up to {MAX_CHAIN_LENGTH} "
    "candidate ids from GET https://api.speko.ai/v1/models where routable is true."
)

CATALOG_NEXT_STEP = (
    "Read GET https://api.speko.ai/v1/models (public, no auth) and use only ids "
    "whose routable field is true, spelled exactly as that response spells them, "
    "for example 'deepgram:nova-3'."
)


def register_router_tools(mcp: FastMCP) -> None:
    """Register the Router key tools.

    MUST run after the default-surface registrations and BEFORE
    `register_builder_tools`, so builder-only tools stay last (see
    `profiles.py`). These tools are absent from
    `BUILDER_PROFILE_TOOL_NAMES`, so `ToolProfileMiddleware` hides them
    from the builder profile with no edit there.
    """
    for tool in [
        list_router_keys,
        create_router_key,
        update_router_key,
        revoke_router_key,
    ]:
        name = tool.__name__
        public_name = ROUTER_TOOL_NAME_BY_FUNCTION[name]
        title = tool_title(name)
        mcp.tool(
            tool,
            name=public_name,
            title=title,
            output_schema=SPEKO_API_OUTPUT_SCHEMA,
            annotations=ToolAnnotations(
                title=title,
                readOnlyHint=name in READ_ONLY_ROUTER_TOOL_NAMES,
                destructiveHint=name in DESTRUCTIVE_ROUTER_TOOL_NAMES,
                idempotentHint=name in READ_ONLY_ROUTER_TOOL_NAMES,
                openWorldHint=True,
            ),
        )


def next_step_for_router_error(exc: Exception) -> str:
    """Turn a control-plane error code into the one action that fixes it."""
    if isinstance(exc, router_client.SpekoAuthError):
        return (
            "Reconnect Speko MCP with OAuth (browser sign-in). Router keys cannot "
            "be provisioned with a Speko API key."
        )
    if not isinstance(exc, router_client.SpekoApiError):
        return "Retry the Speko router control-plane request."
    code = exc.message.strip()
    if code == "key_limit":
        return (
            "Ten active router keys is the limit. Call router.keys.revoke "
            "on a key you no longer need, then retry."
        )
    if code == "unroutable_model":
        return f"A chain element is not routable. {CATALOG_NEXT_STEP}"
    if code == "catalog_unavailable":
        return (
            "The router model catalog is momentarily unavailable, so the chain "
            "could not be validated. Retry, or create the key without a policy "
            "and set the chain later with router.keys.update."
        )
    if code == "invalid_policy":
        return POLICY_NEXT_STEP
    if code == "name_too_long":
        return f"Shorten name to {MAX_KEY_NAME_LENGTH} characters or fewer."
    if code == "invalid_name":
        return "name must be text without control characters."
    if code == "not_found":
        return (
            "No active router key with that id belongs to the signed-in user. "
            "Call router.keys.list for current ids."
        )
    if exc.status_code in {401, 403}:
        return (
            "Reconnect Speko MCP with OAuth (browser sign-in) and confirm the "
            "account's email is verified."
        )
    if exc.status_code == 429:
        return "Rate limited by the router control plane. Wait, then retry."
    if exc.status_code == 503:
        return "The router control plane or its auth authority is unavailable. Retry shortly."
    return "Inspect the router control-plane response details, then retry."


async def router_call(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    text: str,
    payload_override: dict[str, Any] | None = None,
) -> ToolResult:
    try:
        payload = await router_client.call_control_api(method, path, body)
    except (router_client.SpekoApiError, router_client.SpekoAuthError) as exc:
        raise ToolError(
            http_client.tool_error_message(exc, next_step=next_step_for_router_error(exc))
        ) from exc
    return result(payload_override if payload_override is not None else payload, text=text)


def _policy_error(detail: str) -> ToolError:
    return ToolError(f"Invalid router key policy: {detail}; next_step={POLICY_NEXT_STEP}")


def default_router_policy() -> dict[str, Any]:
    """Mirror of `defaultKeyPolicy()` in the control plane."""
    return {
        "language": "en",
        "useCase": None,
        "objective": "balanced",
        "maxPricePerMinUsd": None,
        "stt": {"chain": []},
        "llm": {"chain": []},
        "tts": {"chain": []},
    }


def _normalize_chain(stage: str, value: Any) -> list[str]:
    """Accept `{'chain': [...]}` (the contract shape) or a bare list."""
    if isinstance(value, dict):
        unknown = sorted(key for key in value if key != "chain")
        if unknown:
            raise _policy_error(f"{stage} accepts only a chain field, got {', '.join(unknown)}")
        raw = value.get("chain", [])
    else:
        raw = value
    if not isinstance(raw, list):
        raise _policy_error(f"{stage}.chain must be an array of candidate ids")
    if len(raw) > MAX_CHAIN_LENGTH:
        raise _policy_error(
            f"{stage}.chain holds at most {MAX_CHAIN_LENGTH} candidate ids, got {len(raw)}"
        )
    chain: list[str] = []
    for candidate in raw:
        if not isinstance(candidate, str) or not _CANDIDATE_ID_RE.match(candidate):
            raise _policy_error(
                f"{stage}.chain entries must be 'provider:model' candidate ids, "
                f"got {candidate!r}. {CATALOG_NEXT_STEP}"
            )
        if not 3 <= len(candidate) <= 160:
            raise _policy_error(f"{stage}.chain entry {candidate!r} has an unusable length")
        if candidate in chain:
            raise _policy_error(f"{stage}.chain repeats {candidate!r}")
        chain.append(candidate)
    return chain


def normalize_policy(policy: Any) -> dict[str, Any]:
    """Validate a partial policy locally and fill every default.

    The control plane requires all seven keys to be present, so sending a
    partial object 400s with `invalid_policy`. Filling the defaults here is
    what makes a one-field policy work.
    """
    if not isinstance(policy, dict):
        raise _policy_error(f"policy must be an object shaped like {POLICY_SHAPE}")
    unknown = sorted(key for key in policy if key not in POLICY_KEYS)
    if unknown:
        raise _policy_error(
            f"unknown field(s) {', '.join(unknown)}; allowed fields are {', '.join(POLICY_KEYS)}"
        )

    normalized = default_router_policy()

    if "language" in policy:
        language = policy["language"]
        if not isinstance(language, str) or not _LANGUAGE_RE.match(language):
            raise _policy_error(
                "language must be a BCP-47 tag of up to 35 characters, for example 'en' or 'es-MX'"
            )
        normalized["language"] = language

    if "objective" in policy:
        objective = policy["objective"]
        if objective not in POLICY_OBJECTIVES:
            raise _policy_error(f"objective must be one of {', '.join(POLICY_OBJECTIVES)}")
        normalized["objective"] = objective

    if "useCase" in policy:
        use_case = policy["useCase"]
        if use_case is not None and use_case not in POLICY_USE_CASES:
            raise _policy_error(f"useCase must be null or one of {', '.join(POLICY_USE_CASES)}")
        normalized["useCase"] = use_case

    if "maxPricePerMinUsd" in policy:
        max_price = policy["maxPricePerMinUsd"]
        if max_price is not None and (
            isinstance(max_price, bool)
            or not isinstance(max_price, int | float)
            or not math.isfinite(max_price)
            or max_price < 0
        ):
            raise _policy_error("maxPricePerMinUsd must be null or a number >= 0")
        normalized["maxPricePerMinUsd"] = max_price

    for stage in POLICY_STAGES:
        if stage in policy:
            normalized[stage] = {"chain": _normalize_chain(stage, policy[stage])}

    return normalized


def validate_key_name(name: Any) -> str:
    """Mirror of `parseKeyName` — checked here so the round trip is not wasted."""
    if not isinstance(name, str) or not name.strip():
        raise ToolError(
            "Invalid router key name: pass a short label such as 'pipecat-prod'; "
            "next_step=Retry with a non-empty name."
        )
    trimmed = name.strip()
    if len(trimmed) > MAX_KEY_NAME_LENGTH:
        raise ToolError(
            f"Invalid router key name: at most {MAX_KEY_NAME_LENGTH} characters; "
            f"next_step=Shorten name to {MAX_KEY_NAME_LENGTH} characters or fewer."
        )
    if any(char in _CONTROL_CHARS for char in trimmed):
        raise ToolError(
            "Invalid router key name: control characters are not allowed; "
            "next_step=Retry with plain text."
        )
    return trimmed


PolicyArgument = Annotated[
    dict[str, Any] | None,
    Field(
        description=(
            "Routing policy carried BY the key, so callers send no per-request "
            "routing headers. Every field is optional and omitted fields take "
            f"their defaults: {POLICY_SHAPE}. language is BCP-47. objective is "
            f"{' | '.join(POLICY_OBJECTIVES)}. useCase is null or "
            f"{' | '.join(POLICY_USE_CASES)}. maxPricePerMinUsd is null or a "
            "number >= 0. Each stage takes an ORDERED chain: element 0 is the "
            "pin, elements 1..3 are the failover order, max 4. An EMPTY chain "
            "means the router picks by objective and is the right default — only "
            "pass a chain when you already have exact candidate ids from GET "
            "https://api.speko.ai/v1/models with routable true. Note the policy "
            "is the DEFAULT for the key, not a cap: a request header still wins."
        )
    ),
]


async def list_router_keys() -> ToolResult:
    """List the signed-in user's Speko Router API keys.

    Returns each key's id, name, prefix, timestamps, and the routing policy
    it carries. Secrets are never returned — the full key is shown once, at
    creation. Use the returned id with router.keys.update or
    router.keys.revoke."""
    return await router_call("GET", "/api/keys", text="Retrieved router keys.")


async def create_router_key(
    name: Annotated[
        str,
        Field(
            description=(
                "Short human label for the key, up to 64 characters, "
                "for example 'pipecat-prod' or 'livekit-staging'."
            )
        ),
    ],
    policy: PolicyArgument = None,
) -> ToolResult:
    """Create a Speko Router API key with its routing policy.

    The returned `key` is the full secret and is shown EXACTLY ONCE — it
    cannot be retrieved later, so hand it to the user or write it into their
    environment (SPEKO_API_KEY) in the same turn. It authenticates the
    OpenAI-compatible router at https://api.speko.ai/v1.

    Omit `policy` unless the user asked for specific routing: the default
    routes every stage by objective across the whole routable catalog. When
    they do want a pinned model, element 0 of that stage's chain is the pin
    and the rest are the failover order. Requires OAuth; a Speko API key
    cannot mint router keys."""
    body: dict[str, Any] = {"name": validate_key_name(name)}
    if policy is not None:
        body["policy"] = normalize_policy(policy)
    return await router_call("POST", "/api/keys", body=body, text="Created router key.")


async def update_router_key(
    key_id: Annotated[str, Field(description="Router key id from router.keys.list.")],
    name: Annotated[
        str | None,
        Field(description="New label for the key. Omit to leave it unchanged."),
    ] = None,
    policy: PolicyArgument = None,
) -> ToolResult:
    """Rename a Speko Router API key or replace its routing policy.

    The policy is REPLACED, not merged: fields you omit are reset to their
    defaults, so read the current policy with router.keys.list first and
    resend what should survive. The key's secret does not change, so callers
    already using it pick up the new routing on the next request. Pass at
    least one of name or policy."""
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = validate_key_name(name)
    if policy is not None:
        body["policy"] = normalize_policy(policy)
    if not body:
        raise ToolError(
            "Invalid router key update: pass name, policy, or both; "
            "next_step=Retry with the field you want to change."
        )
    return await router_call(
        "PATCH",
        f"/api/keys/{http_client.path_segment(key_id)}",
        body=body,
        text="Updated router key.",
    )


async def revoke_router_key(
    key_id: Annotated[str, Field(description="Router key id from router.keys.list.")],
) -> ToolResult:
    """Revoke a Speko Router API key.

    The secret stops routing within seconds and cannot be restored; issue a
    new key with router.keys.create instead. Usage history for the revoked
    key is retained."""
    return await router_call(
        "DELETE",
        f"/api/keys/{http_client.path_segment(key_id)}",
        text="Revoked router key.",
        payload_override={"id": key_id, "revoked": True},
    )
