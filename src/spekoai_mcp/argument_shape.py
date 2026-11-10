"""Repair tool arguments whose meaning is unambiguous but whose shape is wrong.

Prod 2026-10-19..21 logged 218 "Invalid arguments for tool" rejections before
any request left this server, plus 30 Platform 400s for the same mistakes on
manifest tools. Nearly all were one of a few shapes:

- the fields of a ``body`` tool sent at the top level (``audio.synthesize``
  with ``text``/``intent``, ``sessions.phone.create`` with ``to``/``agentId``:
  ~90),
- the neighbouring naming convention (``agentId`` on ``sessions.list``,
  ``sessionId`` where the tool says ``session_id``: ~60),
- a wrapper the tool does not have (``audio.transcribe`` given
  ``body: '{"audio_url": ...}'``, ``sessions.get`` given ``path: {id}``),
- a bare ``id`` for the tool's one required ``*_id`` argument.

Each repair only fills a DECLARED argument the caller did not send, from a key
the tool does NOT declare, so anything genuinely ambiguous or conflicting still
reaches validation unchanged and fails with its own message.
"""

from __future__ import annotations

import json
import re
from typing import Any

import mcp.types as mt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

BODY = "body"
# Wrappers models invent around a flat argument list.
_WRAPPER_KEYS = ("body", "path", "params", "arguments", "input", "query_params")

# Names models reach for instead of the declared one, per tool. camelCase /
# snake_case twins are handled generically and are not listed here.
_ALIASES: dict[str, dict[str, str]] = {
    "capabilities.describe": {"name": "actionId", "id": "actionId", "action": "actionId"},
    "operations.get": {"id": "operationId"},
    "operations.wait": {"id": "operationId"},
    "operations.cancel": {"id": "operationId"},
    "agents.duplicate": {"agent_id": "source_agent_id", "agentId": "source_agent_id"},
    "sessions.list": {
        "createdAfter": "from_",
        "created_after": "from_",
        "since": "from_",
        "from": "from_",
        "createdBefore": "to",
        "created_before": "to",
    },
    "phone_numbers.available.search": {"countryCode": "country", "country_code": "country"},
}

# `phone_numbers.available.search` takes 'US' | 'CA'; models send the name.
_COUNTRY_NAMES = {
    "US": "US",
    "USA": "US",
    "UNITED STATES": "US",
    "UNITED STATES OF AMERICA": "US",
    "CA": "CA",
    "CAN": "CA",
    "CANADA": "CA",
}


def _snake_case(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _camel_case(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def _as_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.lstrip().startswith("{"):
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _target_for(tool: str, key: str, declared: set[str], required: list[str]) -> str | None:
    target = _ALIASES.get(tool, {}).get(key)
    if target is None:
        twins = {_snake_case(key), _camel_case(key)} - {key}
        target = next((twin for twin in twins if twin in declared), None)
    if target is None and key == "id":
        ids = [name for name in required if name.endswith("_id") or name.endswith("Id")]
        target = ids[0] if len(ids) == 1 else None
    return target if target in declared else None


def normalize_arguments(
    tool: str, input_schema: dict[str, Any], arguments: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Return ``arguments`` reshaped to ``input_schema`` where that is unambiguous."""
    properties = input_schema.get("properties")
    if not isinstance(properties, dict) or not arguments:
        return arguments
    declared = set(properties)
    required = [name for name in input_schema.get("required", []) if isinstance(name, str)]
    args = dict(arguments)

    # 1. A wrapper the tool does not have: lift its fields to the top level.
    if BODY in declared:
        body = _as_object(args.get(BODY))
        if body is not None:
            args[BODY] = body
    else:
        for wrapper in _WRAPPER_KEYS:
            if wrapper in declared or wrapper not in args:
                continue
            inner = _as_object(args[wrapper])
            if inner is None:
                continue
            # A wrapper that disagrees with a top-level argument is ambiguous
            # (which agent should agents.delete remove?): leave it for
            # validation to reject rather than pick one.
            if any(key in args and args[key] != value for key, value in inner.items()):
                continue
            del args[wrapper]
            args.update(inner)

    # 2. A declared argument under another name.
    for key in list(args):
        if key in declared:
            continue
        target = _target_for(tool, key, declared, required)
        if target is not None and target not in args:
            args[target] = args.pop(key)

    # 3. Body fields sent at the top level: everything the tool still does not
    #    declare belongs in the body.
    if BODY in declared and BODY not in args:
        stray = {key: value for key, value in args.items() if key not in declared}
        if stray:
            args = {key: value for key, value in args.items() if key in declared}
            args[BODY] = stray

    if tool == "phone_numbers.available.search" and isinstance(args.get("country"), str):
        args["country"] = _COUNTRY_NAMES.get(args["country"].strip().upper(), args["country"])
    return args


class ArgumentShapeMiddleware(Middleware):
    """Apply :func:`normalize_arguments` before FastMCP validates a call."""

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, object],
    ) -> object:
        server = context.fastmcp_context.fastmcp if context.fastmcp_context else None
        arguments = context.message.arguments
        if server is None or not arguments:
            return await call_next(context)
        tool = await server.get_tool(context.message.name)
        if tool is None:
            return await call_next(context)
        reshaped = normalize_arguments(context.message.name, tool.parameters, arguments)
        if reshaped != arguments:
            message = context.message.model_copy(update={"arguments": reshaped})
            context = context.copy(message=message)
        return await call_next(context)
