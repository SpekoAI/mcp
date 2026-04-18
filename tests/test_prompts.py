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


async def test_voice_conversation_prompt_lists_all_config_form_fields() -> None:
    """The scaffold must instruct the agent to build a config form that
    covers every user-tunable /v1/sessions field, so the demo actually
    exercises the API surface instead of just a Start button."""
    mcp = create_server()
    result = await mcp.render_prompt(
        "scaffold_project",
        {"scenario": "voice_conversation"},
    )
    text = "\n".join(m.content.text for m in result.messages)
    expected_fields = [
        "intent.language",
        "intent.vertical",
        "intent.optimizeFor",
        "systemPrompt",
        "voice",
        "llm.temperature",
        "llm.maxTokens",
        "ttsOptions.speed",
        "ttsOptions.sampleRate",
        # allowedProviders surface per-modality lists
        "constraints.allowedProviders.stt",
        "constraints.allowedProviders.llm",
        "constraints.allowedProviders.tts",
        "identity",
        "ttlSeconds",
        "metadata",
    ]
    for field in expected_fields:
        assert field in text, (
            f"voice_conversation scaffold is missing the config form "
            f"field {field!r}"
        )


async def test_voice_conversation_emits_correct_session_shape() -> None:
    """The /v1/sessions body the prompt emits must match the real API:
    `intent` is a NESTED object containing language + vertical; there
    is no `agent` / `agentId` / `SPEKO_AGENT_ID` concept. This regression
    test guards against the model inventing a flat or agent-keyed shape
    (as a previous scaffold did)."""
    mcp = create_server()
    result = await mcp.render_prompt(
        "scaffold_project",
        {"scenario": "voice_conversation"},
    )
    text = "\n".join(m.content.text for m in result.messages)
    # Must call the real endpoint path.
    assert "/v1/sessions" in text
    # Must use `intent: { language, vertical }` — nested, not flat.
    assert "intent:" in text
    assert "language:" in text and "vertical:" in text
    # Guard against the `agent`/`agentId` hallucination: check only
    # code-shape occurrences (`agentId:`, `"agent":`, `'agent':`, or a
    # `SPEKO_AGENT_ID` env reference) so the prompt's own prose warning
    # against these fields doesn't self-trip the assertion.
    forbidden_code_shapes = ["SPEKO_AGENT_ID", "agentId:", "'agent':", '"agent":']
    for needle in forbidden_code_shapes:
        assert needle not in text, (
            f"voice_conversation scaffold emits a forbidden code shape "
            f"{needle!r} — POST /v1/sessions has no such field"
        )


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
