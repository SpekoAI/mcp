"""Which hosts serve the SMS tools.

Sending a text reaches a real person's phone, so no directory host serves it.
Reading threads stays wherever reads of calls and numbers do.
"""

from __future__ import annotations

import pytest

from spekoai_mcp.profiles import (
    BUILDER_PROFILE,
    CONNECTOR_PROFILE,
    CUSTOMER_PROFILE,
    DEFAULT_PROFILE_ENV_VAR,
    DIRECTORY_PROFILES,
    MUSE_PROFILE,
    REPLIT_PROFILE,
)
from spekoai_mcp.server import create_server

SMS_SEND_TOOLS = {"sms.messages.send", "sms.conversations.reply"}
SMS_READ_TOOLS = {
    "sms.conversations.list",
    "sms.conversations.get",
    "sms.conversations.messages.list",
    "sms.messages.list",
    "sms.messages.get",
}


async def _served(monkeypatch: pytest.MonkeyPatch, profile: str) -> set[str]:
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, profile)
    return {tool.name for tool in await create_server().list_tools()}


@pytest.mark.parametrize("profile", sorted(DIRECTORY_PROFILES | {BUILDER_PROFILE, REPLIT_PROFILE}))
async def test_no_curated_host_can_send_sms(monkeypatch: pytest.MonkeyPatch, profile: str) -> None:
    assert (await _served(monkeypatch, profile)) & SMS_SEND_TOOLS == set()


async def test_customer_host_serves_every_sms_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    served = await _served(monkeypatch, CUSTOMER_PROFILE)
    assert SMS_SEND_TOOLS | SMS_READ_TOOLS | {"sms.conversations.update"} <= served


@pytest.mark.parametrize("profile", [CONNECTOR_PROFILE, MUSE_PROFILE])
async def test_directory_hosts_read_sms_threads(
    monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    assert SMS_READ_TOOLS <= await _served(monkeypatch, profile)
