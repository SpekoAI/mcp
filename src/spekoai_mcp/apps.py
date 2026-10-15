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

The resource deliberately sets no `_meta.ui.domain` (so no
`AppConfig(domain=...)`, which serializes to it). Hosts read that one field
differently: ChatGPT wants an HTTPS origin, while Claude accepts only
`{sha256(connector URL)[:32]}.claudemcpcontent.com` and shows "Invalid
ui.domain format" instead of the app for anything else. ChatGPT gets its
origin from the `openai/widgetDomain` alias instead, which Claude ignores.

Parallel to `components.py`, which serves source files for agents to paste
into a project. These resources are executable UI for the host itself, so
they live in their own module and their own `ui://` namespace.
"""

from __future__ import annotations

import os
from importlib.resources import files
from typing import Any
from urllib.parse import urlsplit

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

# ChatGPT's status line while the tool runs and after it returns. Its Apps SDK
# reference caps each at 64 characters and spells them as flat keys. Manufact's
# publishing checklist hint describes a nested `openai/toolInvocation` object
# instead, and the only server publicly shown passing that check sent the
# nested form, so both shapes carry the same two strings.
OPENAI_INVOKING_META_KEY = "openai/toolInvocation/invoking"
OPENAI_INVOKED_META_KEY = "openai/toolInvocation/invoked"
OPENAI_TOOL_INVOCATION_META_KEY = "openai/toolInvocation"


def _tool_view_meta(uri: str, *, invoking: str, invoked: str) -> dict[str, Any]:
    """Tool-side `_meta` for a tool whose result renders in a view."""
    return {
        OPENAI_TEMPLATE_META_KEY: uri,
        OPENAI_INVOKING_META_KEY: invoking,
        OPENAI_INVOKED_META_KEY: invoked,
        OPENAI_TOOL_INVOCATION_META_KEY: {"invoking": invoking, "invoked": invoked},
    }


AUDIO_PLAYER_META = _tool_view_meta(
    AUDIO_PLAYER_URI, invoking="Synthesizing speech…", invoked="Synthesized speech"
)

# The public docs endpoint's one view: setup steps for connecting a client to
# Speko, bound to `setup.connect` (`setup_tools.py`). Static content, so it
# needs no CSP grant, permission or account data.
CONNECT_URI = "ui://speko/connect.html"
CONNECT_APP = AppConfig(resource_uri=CONNECT_URI)
CONNECT_META = _tool_view_meta(
    CONNECT_URI, invoking="Preparing setup steps…", invoked="Setup steps ready"
)

# Resource-side ChatGPT keys. `openai/widgetDomain` is the alias for
# `_meta.ui.domain` (see the module docstring for why the standard key stays
# unset); ChatGPT requires a domain to submit an app with UI, unique per app.
# `openai/widgetDescription` is what the model reads when the widget loads.
OPENAI_WIDGET_DOMAIN_META_KEY = "openai/widgetDomain"
OPENAI_WIDGET_DESCRIPTION_META_KEY = "openai/widgetDescription"

# The deployment's public URL, already required whenever OAuth is on
# (`auth.py`). Each profile is its own service with its own host, so its
# origin is a domain unique to that app.
BASE_URL_ENV_VAR = "SPEKOAI_MCP_BASE_URL"

AUDIO_PLAYER_WIDGET_DESCRIPTION = (
    "Shows the user a player for the synthesized speech, with a download "
    "button and the provider and model. A clip too long to embed appears as "
    "a link that opens the audio. The player presents the audio, so do not "
    "describe or repeat it."
)

CONNECT_WIDGET_DESCRIPTION = (
    "Shows the user how to connect an MCP client to Speko: the endpoint, a "
    "one-line installer, and a copyable setup snippet per client. The card "
    "presents the steps, so do not repeat them in full."
)


def _widget_domain() -> str | None:
    """The deployment's origin, or None when no public URL is configured."""
    parts = urlsplit((os.environ.get(BASE_URL_ENV_VAR) or "").strip())
    if parts.scheme != "https" or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def _view_resource_meta(widget_description: str) -> dict[str, str]:
    """Resource-side `_meta` for a view: its description and, when the
    deployment has a public origin, its ChatGPT widget domain."""
    meta = {OPENAI_WIDGET_DESCRIPTION_META_KEY: widget_description}
    if domain := _widget_domain():
        meta[OPENAI_WIDGET_DOMAIN_META_KEY] = domain
    return meta


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
            "controls with the resolved provider, model and duration. "
            "A clip too long to embed is offered as its download link."
        ),
        meta=_view_resource_meta(AUDIO_PLAYER_WIDGET_DESCRIPTION),
    )
    def audio_player() -> str:
        return _read_app("audio_player.html")


def register_public_apps(mcp: FastMCP) -> None:
    """Views served by the unauthenticated docs endpoint."""

    @mcp.resource(
        CONNECT_URI,
        name="app_connect",
        title="Connect Speko",
        description=(
            "Setup card for `setup.connect`. Shows the Speko MCP endpoint, the "
            "installer command, and per-client setup snippets with copy buttons."
        ),
        meta=_view_resource_meta(CONNECT_WIDGET_DESCRIPTION),
    )
    def connect_card() -> str:
        return _read_app("connect.html")
