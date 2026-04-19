"""Tests for `scaffold_voice_app` — Next.js App Router voice-app scaffold.

Guards:
- Every vertical emits the same four file paths so downstream tooling
  can rely on a stable layout.
- The backend route is a Next.js App Router Route Handler (POST export,
  `runtime = 'nodejs'`).
- The vertical-specific default system prompt lands in the route file.
- `languages=['en', 'es']` appends the multilingual note UNLESS the
  vertical's default prompt already covers it (support_agent does).
- Explicit `system_prompt` wins over the default verbatim.
- The component file content is pulled from the bundled component
  resource — catching drift between the two.
"""

from __future__ import annotations

import pytest

from spekoai_mcp.scaffolds import ScaffoldManifest, build_voice_app_manifest
from spekoai_mcp.server import create_server

_VERTICALS = ["healthcare", "insurance", "financial_services", "support_agent"]

_EXPECTED_PATHS = {
    "app/api/speko/route.ts",
    "components/speko-voice-session.tsx",
    "app/page.tsx",
    "app/layout.tsx",
    ".env.example",
}


def _files_by_path(manifest: ScaffoldManifest) -> dict[str, str]:
    return {f.path: f.content for f in manifest.files}


@pytest.mark.parametrize("use_case", _VERTICALS)
def test_manifest_has_expected_files(use_case: str) -> None:
    manifest = build_voice_app_manifest(use_case)  # type: ignore[arg-type]
    paths = {f.path for f in manifest.files}
    assert paths == _EXPECTED_PATHS


@pytest.mark.parametrize("use_case", _VERTICALS)
def test_route_handler_uses_node_runtime_and_post(use_case: str) -> None:
    manifest = build_voice_app_manifest(use_case)  # type: ignore[arg-type]
    route = _files_by_path(manifest)["app/api/speko/route.ts"]
    assert "export const runtime = 'nodejs'" in route
    assert "export async function POST" in route
    assert "/v1/sessions" in route


def test_healthcare_system_prompt_baked_in() -> None:
    manifest = build_voice_app_manifest("healthcare")
    route = _files_by_path(manifest)["app/api/speko/route.ts"]
    # A phrase unique to the healthcare default prompt.
    assert "licensed clinician" in route


def test_insurance_system_prompt_baked_in() -> None:
    manifest = build_voice_app_manifest("insurance")
    route = _files_by_path(manifest)["app/api/speko/route.ts"]
    assert "underwriter" in route


def test_financial_services_system_prompt_baked_in() -> None:
    manifest = build_voice_app_manifest("financial_services")
    route = _files_by_path(manifest)["app/api/speko/route.ts"]
    assert "Do not give investment advice" in route


def test_support_agent_system_prompt_baked_in() -> None:
    manifest = build_voice_app_manifest("support_agent")
    route = _files_by_path(manifest)["app/api/speko/route.ts"]
    assert "global customer support voice assistant" in route.lower()


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


def test_support_agent_skips_multilingual_append() -> None:
    """support_agent's default prompt already addresses multilingual
    behavior — adding a second note would be redundant. Make sure we
    don't double up."""
    manifest = build_voice_app_manifest("support_agent", languages=["en", "es"])
    route = _files_by_path(manifest)["app/api/speko/route.ts"]
    # The generic append phrase uses "both English and Spanish"; the
    # support_agent default uses "in whichever language they use".
    assert "both English and Spanish" not in route


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


def test_install_commands_include_livekit_and_agents_ui() -> None:
    manifest = build_voice_app_manifest("healthcare")
    joined = " ".join(manifest.install_commands)
    assert "@livekit/components-react" in joined
    assert "livekit-client" in joined
    assert "shadcn@latest init" in joined
    assert "@agents-ui" in joined
    # Config panel depends on these shadcn primitives.
    assert "label" in joined
    assert "select" in joined
    assert "textarea" in joined


def test_page_seeds_ui_defaults_from_vertical() -> None:
    manifest = build_voice_app_manifest("healthcare", languages=["es"])
    page = _files_by_path(manifest)["app/page.tsx"]
    # Default config object is declared server-side and passed to the
    # client island so the pre-call form shows the right initial values.
    assert "DEFAULT_CONFIG" in page
    assert "'es-US'" in page
    assert "'healthcare'" in page
    assert "systemPrompt" in page


def test_component_declares_session_config_types() -> None:
    manifest = build_voice_app_manifest("healthcare")
    body = _files_by_path(manifest)["components/speko-voice-session.tsx"]
    assert "SessionConfig" in body
    assert "SessionLanguage" in body
    assert "SessionVertical" in body
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
        {"use_case": "insurance", "languages": ["en", "es"]},
    )
    payload = result.structured_content or {}
    paths = {f["path"] for f in payload.get("files", [])}
    assert paths == _EXPECTED_PATHS
    route = next(
        f["content"] for f in payload["files"]
        if f["path"] == "app/api/speko/route.ts"
    )
    assert "underwriter" in route
    assert "English and Spanish" in route
