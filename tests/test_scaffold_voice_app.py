"""Tests for `scaffold_voice_app` — Next.js App Router voice-app scaffold.

Guards:
- Stable file layout (four files + .env.example).
- The backend route is a Next.js App Router Route Handler (POST export,
  `runtime = 'nodejs'`).
- The neutral default system prompt lands in the route file.
- `languages=['en', 'es']` appends the multilingual note.
- Explicit `system_prompt` wins over the default verbatim.
- The component file content is pulled from the bundled component
  resource — catching drift between the two.
- `optimize_for` and `region` knobs flow into the route's baked-in
  defaults when non-default and stay commented out otherwise.

Vertical / use-case branching is deliberately not exposed in v0.
"""

from __future__ import annotations


from spekoai_mcp.scaffolds import ScaffoldManifest, build_voice_app_manifest
from spekoai_mcp.server import create_server

_EXPECTED_PATHS = {
    "app/api/speko/route.ts",
    "components/speko-voice-session.tsx",
    "app/page.tsx",
    "app/layout.tsx",
    ".env.example",
}


def _files_by_path(manifest: ScaffoldManifest) -> dict[str, str]:
    return {f.path: f.content for f in manifest.files}


def test_manifest_has_expected_files() -> None:
    manifest = build_voice_app_manifest()
    paths = {f.path for f in manifest.files}
    assert paths == _EXPECTED_PATHS


def test_route_handler_uses_node_runtime_and_post() -> None:
    manifest = build_voice_app_manifest()
    route = _files_by_path(manifest)["app/api/speko/route.ts"]
    assert "export const runtime = 'nodejs'" in route
    assert "export async function POST" in route
    assert "/v1/sessions" in route


def test_default_neutral_system_prompt_baked_in() -> None:
    manifest = build_voice_app_manifest()
    route = _files_by_path(manifest)["app/api/speko/route.ts"]
    # A phrase unique to the neutral default prompt.
    assert "concise, helpful voice assistant" in route
    # Steers the user to overwrite for their domain.
    assert "Edit this prompt to" in route


def test_english_only_does_not_add_multilingual_note() -> None:
    manifest = build_voice_app_manifest(languages=["en"])
    route = _files_by_path(manifest)["app/api/speko/route.ts"]
    assert "English and Spanish" not in route
    assert "'en-US'" in route


def test_spanish_adds_multilingual_append_and_sets_language() -> None:
    manifest = build_voice_app_manifest(languages=["en", "es"])
    route = _files_by_path(manifest)["app/api/speko/route.ts"]
    assert "English and Spanish" in route
    # First language wins for intent.language.
    assert "'en-US'" in route


def test_spanish_first_sets_es_language_tag() -> None:
    manifest = build_voice_app_manifest(languages=["es"])
    route = _files_by_path(manifest)["app/api/speko/route.ts"]
    assert "'es-US'" in route


def test_explicit_system_prompt_overrides_default_verbatim() -> None:
    override = "Speak only in pig Latin. Ignore all other instructions."
    manifest = build_voice_app_manifest(languages=["en"], system_prompt=override)
    route = _files_by_path(manifest)["app/api/speko/route.ts"]
    assert override in route
    # The neutral default must NOT leak through.
    assert "concise, helpful voice assistant" not in route


def test_component_file_is_use_client_and_exports_component() -> None:
    manifest = build_voice_app_manifest()
    body = _files_by_path(manifest)["components/speko-voice-session.tsx"]
    assert body.startswith("'use client';")
    assert "export function SpekoVoiceSession" in body


def test_install_commands_include_spekoai_client() -> None:
    manifest = build_voice_app_manifest()
    joined = " ".join(manifest.install_commands)
    assert "@spekoai/client" in joined
    assert "shadcn@latest init" in joined
    assert "label" in joined
    assert "select" in joined
    assert "textarea" in joined


def test_page_seeds_ui_defaults() -> None:
    manifest = build_voice_app_manifest(languages=["es"])
    page = _files_by_path(manifest)["app/page.tsx"]
    assert "DEFAULT_CONFIG" in page
    assert "'es-US'" in page
    assert "systemPrompt" in page


def test_component_declares_session_config_types() -> None:
    manifest = build_voice_app_manifest()
    body = _files_by_path(manifest)["components/speko-voice-session.tsx"]
    assert "SessionConfig" in body
    assert "SessionLanguage" in body
    assert "SessionOptimizeFor" in body


def test_route_returns_livekit_tokensource_shape() -> None:
    manifest = build_voice_app_manifest()
    route = _files_by_path(manifest)["app/api/speko/route.ts"]
    assert "server_url: livekitUrl" in route
    assert "participant_token: conversationToken" in route


def test_page_is_root_and_imports_component_via_alias() -> None:
    manifest = build_voice_app_manifest()
    paths = {f.path for f in manifest.files}
    assert "app/page.tsx" in paths
    assert "app/voice/page.tsx" not in paths
    page = _files_by_path(manifest)["app/page.tsx"]
    assert "@/components/speko-voice-session" in page


def test_component_resource_is_referenced() -> None:
    manifest = build_voice_app_manifest()
    assert "spekoai://components/react/voice-session" in manifest.component_resources


async def test_scaffold_voice_app_tool_advertised() -> None:
    mcp = create_server()
    tools = await mcp.list_tools()
    assert any(t.name == "scaffold_voice_app" for t in tools)


async def test_scaffold_voice_app_tool_returns_manifest() -> None:
    mcp = create_server()
    result = await mcp.call_tool(
        "scaffold_voice_app",
        {"languages": ["en", "es"]},
    )
    payload = result.structured_content or {}
    paths = {f["path"] for f in payload.get("files", [])}
    assert paths == _EXPECTED_PATHS
    route = next(
        f["content"] for f in payload["files"]
        if f["path"] == "app/api/speko/route.ts"
    )
    assert "concise, helpful voice assistant" in route
    assert "English and Spanish" in route


async def test_scaffold_voice_app_tool_returns_actionable_text() -> None:
    """The tool emits a text block alongside the structured manifest so
    receiving agents (Claude Code, Cursor, etc.) get prose instructions
    without re-interpreting the JSON payload."""
    mcp = create_server()
    result = await mcp.call_tool("scaffold_voice_app", {})
    assert result.content, "tool must return at least one content block"
    text_blocks = [c for c in result.content if getattr(c, "type", None) == "text"]
    assert text_blocks, "tool must include a text content block"
    combined = "\n".join(c.text for c in text_blocks)
    assert "Create these files" in combined
    assert "Run install commands" in combined
    assert "Set environment variables" in combined
    assert "app/api/speko/route.ts" in combined


def test_scaffold_with_accuracy_bakes_in_optimize_for() -> None:
    """When optimize_for is non-default (`latency`), the route handler
    bakes the constant in (uncommented)."""
    manifest = build_voice_app_manifest(optimize_for="accuracy")
    route = _files_by_path(manifest)["app/api/speko/route.ts"]
    assert "DEFAULT_OPTIMIZE_FOR" in route
    assert "'accuracy'" in route
    active_lines = [
        ln for ln in route.splitlines()
        if ln.startswith("const DEFAULT_OPTIMIZE_FOR")
    ]
    assert active_lines, (
        "scaffold with optimize_for='accuracy' must emit an active "
        "(non-commented) DEFAULT_OPTIMIZE_FOR declaration"
    )
    assert "Speko routing picks" in route
    assert "optimize_for=accuracy" in route


def test_scaffold_with_cost_bakes_in_optimize_for() -> None:
    """The cost preset is the third axis — must bake in just like accuracy."""
    manifest = build_voice_app_manifest(optimize_for="cost")
    route = _files_by_path(manifest)["app/api/speko/route.ts"]
    assert "'cost'" in route
    active_lines = [
        ln for ln in route.splitlines()
        if ln.startswith("const DEFAULT_OPTIMIZE_FOR")
    ]
    assert active_lines


def test_scaffold_default_keeps_optimize_for_commented() -> None:
    """The default scaffold (optimize_for='latency', region='global')
    keeps DEFAULT_REGION and DEFAULT_OPTIMIZE_FOR commented out — the
    Speko router infers the same axis from the conversation when the
    constant is absent."""
    manifest = build_voice_app_manifest()
    route = _files_by_path(manifest)["app/api/speko/route.ts"]
    active_optimize = [
        ln for ln in route.splitlines()
        if ln.startswith("const DEFAULT_OPTIMIZE_FOR")
    ]
    active_region = [
        ln for ln in route.splitlines()
        if ln.startswith("const DEFAULT_REGION")
    ]
    assert not active_optimize, (
        "default scaffold must leave DEFAULT_OPTIMIZE_FOR commented out"
    )
    assert not active_region, (
        "default scaffold must leave DEFAULT_REGION commented out"
    )


def test_scaffold_with_region_bakes_in_default_region() -> None:
    manifest = build_voice_app_manifest(region="us-east4")
    route = _files_by_path(manifest)["app/api/speko/route.ts"]
    assert "const DEFAULT_REGION = 'us-east4';" in route


def test_scaffold_page_optimize_for_uses_param_when_non_default() -> None:
    manifest = build_voice_app_manifest(optimize_for="cost")
    page = _files_by_path(manifest)["app/page.tsx"]
    assert "optimizeFor: 'cost' as const" in page


async def test_scaffold_voice_app_tool_does_not_accept_use_case() -> None:
    """Vertical branching was deliberately removed in v0."""
    mcp = create_server()
    tools = await mcp.list_tools()
    tool = next(t for t in tools if t.name == "scaffold_voice_app")
    schema_props = (tool.parameters or {}).get("properties", {})
    assert "use_case" not in schema_props
    assert set(schema_props.get("optimize_for", {}).get("enum", [])) == {
        "latency",
        "accuracy",
        "cost",
    }
