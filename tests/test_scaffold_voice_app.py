"""Tests for `scaffold_voice_app` — Next.js App Router voice-app scaffold.

Guards:
- Every use case emits the same four file paths so downstream tooling
  can rely on a stable layout.
- The backend route is a Next.js App Router Route Handler (POST export,
  `runtime = 'nodejs'`).
- The use-case-specific default system prompt lands in the route file.
- `languages=['en', 'es']` appends the multilingual note.
- Explicit `system_prompt` wins over the default verbatim.
- The component file content is pulled from the bundled component
  resource — catching drift between the two.
"""

from __future__ import annotations

import pytest

from spekoai_mcp.scaffolds import ScaffoldManifest, build_voice_app_manifest
from spekoai_mcp.server import create_server

_USE_CASES = ["general", "healthcare", "finance", "legal"]

_EXPECTED_PATHS = {
    "app/api/speko/route.ts",
    "components/speko-voice-session.tsx",
    "app/page.tsx",
    "app/layout.tsx",
    ".env.example",
}


def _files_by_path(manifest: ScaffoldManifest) -> dict[str, str]:
    return {f.path: f.content for f in manifest.files}


@pytest.mark.parametrize("use_case", _USE_CASES)
def test_manifest_has_expected_files(use_case: str) -> None:
    manifest = build_voice_app_manifest(use_case)  # type: ignore[arg-type]
    paths = {f.path for f in manifest.files}
    assert paths == _EXPECTED_PATHS


@pytest.mark.parametrize("use_case", _USE_CASES)
def test_route_handler_uses_node_runtime_and_post(use_case: str) -> None:
    manifest = build_voice_app_manifest(use_case)  # type: ignore[arg-type]
    route = _files_by_path(manifest)["app/api/speko/route.ts"]
    assert "export const runtime = 'nodejs'" in route
    assert "export async function POST" in route
    assert "/v1/sessions" in route


def test_general_system_prompt_baked_in() -> None:
    manifest = build_voice_app_manifest("general")
    route = _files_by_path(manifest)["app/api/speko/route.ts"]
    assert "concise, helpful voice assistant" in route


def test_healthcare_system_prompt_baked_in() -> None:
    manifest = build_voice_app_manifest("healthcare")
    route = _files_by_path(manifest)["app/api/speko/route.ts"]
    # A phrase unique to the healthcare default prompt.
    assert "licensed clinician" in route


def test_finance_system_prompt_baked_in() -> None:
    manifest = build_voice_app_manifest("finance")
    route = _files_by_path(manifest)["app/api/speko/route.ts"]
    assert "Do not give investment advice" in route


def test_legal_system_prompt_baked_in() -> None:
    manifest = build_voice_app_manifest("legal")
    route = _files_by_path(manifest)["app/api/speko/route.ts"]
    assert "Never give legal advice" in route


def test_english_only_does_not_add_multilingual_note() -> None:
    manifest = build_voice_app_manifest("healthcare", languages=["en"])
    route = _files_by_path(manifest)["app/api/speko/route.ts"]
    assert "English and Spanish" not in route
    assert "'en-US'" in route


def test_spanish_adds_multilingual_append_and_sets_language() -> None:
    manifest = build_voice_app_manifest("healthcare", languages=["en", "es"])
    route = _files_by_path(manifest)["app/api/speko/route.ts"]
    assert "English and Spanish" in route
    # First language wins for intent.language.
    assert "'en-US'" in route


def test_spanish_first_sets_es_language_tag() -> None:
    manifest = build_voice_app_manifest("healthcare", languages=["es"])
    route = _files_by_path(manifest)["app/api/speko/route.ts"]
    assert "'es-US'" in route


def test_explicit_system_prompt_overrides_default_verbatim() -> None:
    override = "Speak only in pig Latin. Ignore all other instructions."
    manifest = build_voice_app_manifest(
        "healthcare", languages=["en"], system_prompt=override
    )
    route = _files_by_path(manifest)["app/api/speko/route.ts"]
    assert override in route
    # The healthcare default must NOT leak through.
    assert "licensed clinician" not in route


def test_component_file_is_use_client_and_exports_component() -> None:
    manifest = build_voice_app_manifest("healthcare")
    body = _files_by_path(manifest)["components/speko-voice-session.tsx"]
    assert body.startswith("'use client';")
    assert "export function SpekoVoiceSession" in body


def test_install_commands_include_spekoai_client() -> None:
    manifest = build_voice_app_manifest("healthcare")
    joined = " ".join(manifest.install_commands)
    # The browser SDK is the scaffold's core dependency — it pulls
    # livekit-client transitively so we don't list livekit-client here.
    assert "@spekoai/client" in joined
    assert "shadcn@latest init" in joined
    # Config panel depends on these shadcn primitives.
    assert "label" in joined
    assert "select" in joined
    assert "textarea" in joined


def test_page_seeds_ui_defaults_from_use_case() -> None:
    manifest = build_voice_app_manifest("healthcare", languages=["es"])
    page = _files_by_path(manifest)["app/page.tsx"]
    # Default config object is declared server-side and passed to the
    # client island so the pre-call form shows the right initial values.
    assert "DEFAULT_CONFIG" in page
    assert "'es-US'" in page
    assert "systemPrompt" in page


def test_component_declares_session_config_types() -> None:
    manifest = build_voice_app_manifest("healthcare")
    body = _files_by_path(manifest)["components/speko-voice-session.tsx"]
    assert "SessionConfig" in body
    assert "SessionLanguage" in body
    assert "SessionOptimizeFor" in body


def test_route_returns_livekit_tokensource_shape() -> None:
    manifest = build_voice_app_manifest("healthcare")
    route = _files_by_path(manifest)["app/api/speko/route.ts"]
    # Must map Speko's response to the shape TokenSource.endpoint() expects.
    assert "server_url: livekitUrl" in route
    assert "participant_token: conversationToken" in route


def test_page_is_root_and_imports_component_via_alias() -> None:
    manifest = build_voice_app_manifest("healthcare")
    paths = {f.path for f in manifest.files}
    assert "app/page.tsx" in paths
    assert "app/voice/page.tsx" not in paths
    page = _files_by_path(manifest)["app/page.tsx"]
    assert "@/components/speko-voice-session" in page


def test_component_resource_is_referenced() -> None:
    manifest = build_voice_app_manifest("healthcare")
    assert "spekoai://components/react/voice-session" in manifest.component_resources


async def test_scaffold_voice_app_tool_advertised() -> None:
    mcp = create_server()
    tools = await mcp.list_tools()
    assert any(t.name == "scaffold_voice_app" for t in tools)


async def test_scaffold_voice_app_tool_returns_manifest() -> None:
    mcp = create_server()
    result = await mcp.call_tool(
        "scaffold_voice_app",
        {"use_case": "legal", "languages": ["en", "es"]},
    )
    payload = result.structured_content or {}
    paths = {f["path"] for f in payload.get("files", [])}
    assert paths == _EXPECTED_PATHS
    route = next(
        f["content"] for f in payload["files"]
        if f["path"] == "app/api/speko/route.ts"
    )
    assert "Never give legal advice" in route
    assert "English and Spanish" in route


async def test_scaffold_voice_app_tool_returns_actionable_text() -> None:
    """The tool must emit a text content block alongside the structured
    manifest so receiving agents (Claude Code, etc.) get prose
    instructions without re-interpreting the JSON payload."""
    mcp = create_server()
    result = await mcp.call_tool(
        "scaffold_voice_app", {"use_case": "general"}
    )
    assert result.content, "tool must return at least one content block"
    text_blocks = [c for c in result.content if getattr(c, "type", None) == "text"]
    assert text_blocks, "tool must include a text content block"
    combined = "\n".join(c.text for c in text_blocks)
    # The checklist sections agents rely on to act on the manifest.
    assert "Create these files" in combined
    assert "Run install commands" in combined
    assert "Set environment variables" in combined
    # One of the scaffold files' paths must appear so the text is
    # actionable on its own (not just a generic header).
    assert "app/api/speko/route.ts" in combined
