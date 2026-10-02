"""MCP Apps UI resources — HTML a host renders in a sandboxed iframe.

One app today: `ui://speko/audio-player.html`, bound to `audio.synthesize`.

Without it the tool is mute in a chat host. Its payload is base64 audio on
`structuredContent` plus a one-line acknowledgment (see
`action_tools._synthesis_result`), because rendering megabytes of base64 as
text would flood the model's context — so the audio arrives but nobody can
hear it. The app reads the same `structuredContent` off
`ui/notifications/tool-result` and gives the user a player.

The HTML carries no external script or stylesheet, so the resource needs no
`csp.resource_domains` grant, and playback needs no `permissions` entry —
those gate camera/microphone/geolocation/clipboard, not `<audio>`. A host
that does not implement the extension ignores `_meta.ui` and still gets the
acknowledgment text, so nothing regresses on a text-only client.

Parallel to `components.py`, which serves source files for agents to paste
into a project. These resources are executable UI for the host itself, so
they live in their own module and their own `ui://` namespace.
"""

from __future__ import annotations

from importlib.resources import files

from fastmcp import FastMCP
from fastmcp.apps import AppConfig

_APPS_PACKAGE = "spekoai_mcp._apps"

AUDIO_PLAYER_URI = "ui://speko/audio-player.html"

# Attached to `audio.synthesize` at registration. `visibility` is left unset:
# the tool stays model-callable, and the UI is the presentation of its result
# rather than a separate app-only surface.
AUDIO_PLAYER_APP = AppConfig(resource_uri=AUDIO_PLAYER_URI)

# ChatGPT's own spelling of `_meta.ui.resourceUri`. Its docs call the key an
# alias and claim the standard one is read too, but the live client disagrees:
# on 2026-09-16 `openai-mcp/1.0.0 (Codex)` called `audio.synthesize` against
# `chatgpt.speko.ai`, printed its "Rendered inline audio in chat" card, and
# never issued the `resources/read` that rendering the widget requires. The
# Apps SDK's own example tool sets both keys, so we set both. Harmless
# elsewhere: a host that does not know the key ignores it.
OPENAI_TEMPLATE_META_KEY = "openai/outputTemplate"

AUDIO_PLAYER_META: dict[str, str] = {OPENAI_TEMPLATE_META_KEY: AUDIO_PLAYER_URI}


def _read_app(filename: str) -> str:
    return (files(_APPS_PACKAGE) / filename).read_text(encoding="utf-8")


def register_apps(mcp: FastMCP) -> None:
    @mcp.resource(
        AUDIO_PLAYER_URI,
        name="app_audio_player",
        title="Speech player",
        description=(
            "Audio player for `audio.synthesize`. Decodes the tool's "
            "base64 payload, wraps headerless PCM in a WAV container so a "
            "browser can play it, and renders playback and download "
            "controls with the resolved provider, model and duration."
        ),
    )
    def audio_player() -> str:
        return _read_app("audio_player.html")
