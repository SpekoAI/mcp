"""Tests for hosted MCP workflow prompts."""

from __future__ import annotations

from spekoai_mcp.server import create_server


async def test_prompts_are_advertised() -> None:
    mcp = create_server()
    names = {prompt.name for prompt in await mcp.list_prompts()}

    assert names == {"scaffold_project", "migrate_voice_agent"}


async def test_migrate_voice_agent_renders_vapi_guide_and_parse_tool() -> None:
    mcp = create_server()
    rendered = await mcp.render_prompt(
        "migrate_voice_agent",
        {"from_platform": "vapi"},
    )
    text = "\n".join(message.content.text for message in rendered.messages)

    assert "spekoai://docs/migration-vapi" in text
    assert "migration.external_config.parse" in text
