"""Prompt surface tests for `scaffold_project`.

The prompt must render valid messages for every supported scenario ×
language combination, reject unsupported ones with a clear error, and
each rendered scenario must reference at least one doc resource URI so
the downstream agent knows where to pull more context.
"""

from __future__ import annotations

import pytest
from fastmcp.exceptions import PromptError

from spekoai_mcp.server import create_server

_SUPPORTED: list[tuple[str, str]] = [
    ("voice_conversation", "typescript"),
    ("batch_transcribe", "typescript"),
    ("batch_transcribe", "python"),
    ("livekit_agent", "typescript"),
    ("quickstart", "typescript"),
    ("quickstart", "python"),
]


async def test_prompt_advertised() -> None:
    mcp = create_server()
    prompts = await mcp.list_prompts()
    assert any(p.name == "scaffold_project" for p in prompts)


@pytest.mark.parametrize("scenario, language", _SUPPORTED)
async def test_scenario_renders(scenario: str, language: str) -> None:
    mcp = create_server()
    result = await mcp.render_prompt(
        "scaffold_project",
        {"scenario": scenario, "language": language},
    )
    assert result.messages, f"empty message list for {scenario}/{language}"
    joined = "\n".join(
        m.content.text for m in result.messages if hasattr(m.content, "text")
    )
    assert "spekoai://docs/" in joined, (
        f"{scenario}/{language} doesn't reference any resource URI"
    )


async def test_install_command_names_right_package() -> None:
    mcp = create_server()
    result = await mcp.render_prompt(
        "scaffold_project",
        {"scenario": "voice_conversation"},
    )
    text = "\n".join(m.content.text for m in result.messages)
    assert "@spekoai/client" in text
    assert "@spekoai/sdk" in text  # backend needs this to mint sessions


async def test_livekit_agent_install_lists_peer_deps() -> None:
    mcp = create_server()
    result = await mcp.render_prompt(
        "scaffold_project",
        {"scenario": "livekit_agent"},
    )
    text = "\n".join(m.content.text for m in result.messages)
    assert "@spekoai/adapter-livekit" in text
    assert "@livekit/agents" in text
    assert "silero" in text


async def test_python_quickstart_uses_spekoai_package() -> None:
    mcp = create_server()
    result = await mcp.render_prompt(
        "scaffold_project",
        {"scenario": "quickstart", "language": "python"},
    )
    text = "\n".join(m.content.text for m in result.messages)
    assert "pip install spekoai" in text or "uv add spekoai" in text
    assert "from spekoai import" in text


async def test_voice_conversation_rejects_python() -> None:
    # FastMCP wraps the function's raised error; the explanatory message
    # is preserved on `__cause__`. That's what the MCP client sees as
    # error detail, and it's what must carry the actionable guidance.
    mcp = create_server()
    with pytest.raises(PromptError) as excinfo:
        await mcp.render_prompt(
            "scaffold_project",
            {"scenario": "voice_conversation", "language": "python"},
        )
    assert "TypeScript-only" in str(excinfo.value.__cause__)


async def test_livekit_agent_rejects_python() -> None:
    mcp = create_server()
    with pytest.raises(PromptError) as excinfo:
        await mcp.render_prompt(
            "scaffold_project",
            {"scenario": "livekit_agent", "language": "python"},
        )
    assert "TypeScript-only" in str(excinfo.value.__cause__)
