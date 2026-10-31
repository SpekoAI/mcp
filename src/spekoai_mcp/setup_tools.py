"""`setup.connect`: how to connect an MCP client to Speko.

Served by the unauthenticated docs endpoint, where someone who has not
connected yet lands. The result renders in the `ui://speko/connect.html` card
(`apps.py`); a host without MCP Apps gets the same steps as text.

The steps mirror the "Install in your tool" section of the MCP quickstart,
`apps/docs/content/docs/(platform)/quickstart/mcp.mdx`. Update both together.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from mcp.types import TextContent, ToolAnnotations

from spekoai_mcp.apps import CONNECT_APP, CONNECT_META

SETUP_TOOL_NAMES = ["setup.connect"]

MCP_ENDPOINT = "https://mcp.speko.ai/mcp"
INSTALLER = "npx @spekoai/mcp@latest init"
SETUP_DOCS_URL = "https://docs.speko.ai/quickstart/mcp"
AUTH_NOTE = (
    # Plain text: the card shows it verbatim, so no Markdown backticks.
    "OAuth browser sign-in. For automation, send a Speko Platform API key in "
    "the Authorization header as Bearer <key> instead."
)


def client_steps(endpoint: str) -> list[dict[str, str]]:
    """Setup for every supported client, pointed at ``endpoint``.

    `setup.connect` uses the hosted endpoint; the root setup page
    (`server.py`) uses the address of the host that serves it.
    """
    return [
        {
            "id": "claude-code",
            "name": "Claude Code",
            "instructions": "Run in a terminal:",
            "snippet": f"claude mcp add --transport http speko {endpoint}",
            "snippetLanguage": "bash",
        },
        {
            "id": "codex",
            "name": "Codex",
            "instructions": "Add to ~/.codex/config.toml:",
            "snippet": f'[mcp_servers.speko]\nurl = "{endpoint}"',
            "snippetLanguage": "toml",
        },
        {
            "id": "opencode",
            "name": "OpenCode",
            "instructions": "Add to opencode.json:",
            "snippet": (
                "{\n"
                '  "$schema": "https://opencode.ai/config.json",\n'
                '  "mcp": {\n'
                '    "speko": {\n'
                '      "type": "remote",\n'
                f'      "url": "{endpoint}",\n'
                '      "enabled": true\n'
                "    }\n"
                "  }\n"
                "}"
            ),
            "snippetLanguage": "json",
        },
        {
            "id": "cursor",
            "name": "Cursor",
            "instructions": ("Open Cursor Settings > MCP > Add new global MCP server, then add:"),
            "snippet": (
                "{\n"
                '  "mcpServers": {\n'
                '    "speko": {\n'
                f'      "url": "{endpoint}"\n'
                "    }\n"
                "  }\n"
                "}"
            ),
            "snippetLanguage": "json",
        },
        {
            "id": "other",
            "name": "Other clients",
            "instructions": (
                "Configure a remote HTTP MCP server named speko at this URL, with "
                "OAuth browser sign-in:"
            ),
            "snippet": endpoint,
            "snippetLanguage": "text",
        },
    ]


CLIENTS: list[dict[str, str]] = client_steps(MCP_ENDPOINT)


_CLIENT_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Stable client id."},
        "name": {"type": "string", "description": "Client display name."},
        "instructions": {
            "type": "string",
            "description": "Where the snippet goes in this client.",
        },
        "snippet": {
            "type": "string",
            "description": "Command or config to paste, already filled in for Speko.",
        },
        "snippetLanguage": {
            "type": "string",
            "description": "Snippet syntax for display: bash, toml, json or text.",
        },
    },
    "required": ["id", "name", "instructions", "snippet", "snippetLanguage"],
    "additionalProperties": False,
}

CONNECT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "How to connect an MCP client to Speko.",
    "properties": {
        "endpoint": {
            "type": "string",
            "description": "Speko's hosted MCP endpoint, for clients configured by URL.",
        },
        "installer": {
            "type": "string",
            "description": "Guided installer that configures supported clients.",
        },
        "auth": {"type": "string", "description": "How the client signs in."},
        "docsUrl": {"type": "string", "description": "Full setup guide."},
        "clients": {
            "type": "array",
            "description": "Setup for every supported client, in display order.",
            "items": _CLIENT_SCHEMA,
        },
    },
    "required": ["endpoint", "installer", "auth", "docsUrl", "clients"],
    "additionalProperties": False,
}


def _steps_text(payload: dict[str, Any]) -> str:
    """The same steps as plain text, for hosts that do not render the card."""
    lines = [
        f"Speko MCP endpoint: {payload['endpoint']}",
        f"Guided installer: {payload['installer']}",
        f"Sign-in: {payload['auth']}",
    ]
    for client in payload["clients"]:
        lines += ["", f"{client['name']}: {client['instructions']}", client["snippet"]]
    lines += ["", f"Full guide: {payload['docsUrl']}"]
    return "\n".join(lines)


async def connect_speko() -> ToolResult:
    """Show how to connect an MCP client to Speko.

    Use this when the user asks how to install, add or connect Speko in an
    MCP client such as Claude Code, Codex, OpenCode or Cursor. Returns the
    hosted endpoint, a guided installer command, how sign-in works, and a
    ready-to-paste setup snippet per client. Connecting gives the client the
    full Speko toolset: calls, agents, phone numbers and speech.
    """
    payload: dict[str, Any] = {
        "endpoint": MCP_ENDPOINT,
        "installer": INSTALLER,
        "auth": AUTH_NOTE,
        "docsUrl": SETUP_DOCS_URL,
        "clients": [dict(entry) for entry in CLIENTS],
    }
    return ToolResult(
        content=[TextContent(type="text", text=_steps_text(payload))],
        structured_content=payload,
    )


def register_setup_tools(mcp: FastMCP) -> None:
    mcp.tool(
        connect_speko,
        name="setup.connect",
        title="Connect Speko",
        output_schema=CONNECT_OUTPUT_SCHEMA,
        app=CONNECT_APP,
        meta=CONNECT_META,
        annotations=ToolAnnotations(
            title="Connect Speko",
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
