"""MCP resources exposing drop-in client-side components.

One resource today:

- `spekoai://components/react/voice-session` — the `<SpekoVoiceSession>`
  React component wrapping `@spekoai/client`. Agents paste this file
  directly into a React/Next.js project; the `scaffold_voice_app` tool
  inlines the same source into its manifest.

Lives in its own URI namespace (parallel to `spekoai://docs/…`) so the
docs manifest and `list_packages` output don't have to grow a
`component` kind. Parallel paths stay clean when we later add
`vue/…` or `svelte/…` variants.
"""

from __future__ import annotations

from importlib.resources import files

from fastmcp import FastMCP

_COMPONENTS_PACKAGE = "spekoai_mcp._components"


def _read_component(filename: str) -> str:
    return (files(_COMPONENTS_PACKAGE) / filename).read_text(encoding="utf-8")


def register_components(mcp: FastMCP) -> None:
    @mcp.resource(
        "spekoai://components/react/voice-session",
        name="component_react_voice_session",
        title="<SpekoVoiceSession> — React voice session component",
        description=(
            "Drop-in React component that wraps @spekoai/client's "
            "VoiceConversation.create(). Marked 'use client' for Next.js "
            "App Router; loads @spekoai/client via dynamic import so the "
            "SDK stays out of the SSR bundle. Props: sessionEndpoint, "
            "sessionBody?, onError?, onTranscript?, className?. Paste "
            "verbatim into components/VoiceSession.tsx."
        ),
        mime_type="text/plain",
    )
    def react_voice_session() -> str:
        return _read_component("react_voice_session.tsx")
