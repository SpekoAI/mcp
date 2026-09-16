"""Keep Python interpretation aligned while exporting no original header bytes."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from spekoai_mcp.client_classification import (
    CLIENT_CLASSIFIER_VERSION,
    CLIENT_MATCHERS,
    classify_execution_client,
)


def test_matchers_and_version_match_the_shared_platform_contract() -> None:
    source_path = Path(__file__).resolve().parents[3] / "packages/analytics/src/lib/attribution.ts"
    if not source_path.is_file():
        pytest.skip("Shared TypeScript contract is absent in the standalone MCP mirror")
    source = source_path.read_text()
    expected = [
        (
            pattern.replace(r"\/", "/"),
            client,
            "observed_client_marker" if kind == "observed" else "generic_runtime",
        )
        for kind, pattern, client in re.findall(
            r"  (observed|runtime)\(/(.+)/i, '([^']+)'\),",
            source,
        )
    ]
    assert len(expected) > 20
    assert [
        (pattern.pattern, client, evidence) for pattern, client, evidence in CLIENT_MATCHERS
    ] == expected
    assert f"CLIENT_CLASSIFIER_VERSION = '{CLIENT_CLASSIFIER_VERSION}'" in source


@pytest.mark.parametrize(
    "header, client, evidence",
    [
        (None, "unknown:missing_observation", "unknown"),
        ("  ", "unknown:missing_observation", "unknown"),
        ("private-custom-tool/1", "unknown:generic_marker", "unknown"),
        ("OpenAI-MCP/1 (Codex)", "chatgpt_codex", "observed_client_marker"),
        ("OpenAI-MCP/1", "chatgpt", "observed_client_marker"),
        ("codex-mcp-client/1", "codex", "observed_client_marker"),
        ("claude-code/1", "claude_code", "observed_client_marker"),
        ("Claude-User/1", "claude_hosted", "observed_client_marker"),
        ("python-httpx/0.28", "python_httpx", "generic_runtime"),
        ("node", "node", "generic_runtime"),
        ("spekoai-python/1 python-httpx/0.28", "sdk_python", "observed_client_marker"),
        ("x" * 201 + " Codex-mcp-client/1", "unknown:generic_marker", "unknown"),
    ],
)
def test_only_fixed_values_leave_the_classifier(header, client, evidence) -> None:
    assert classify_execution_client(header) == {
        "execution_client": client,
        "client_evidence_class": evidence,
        "classifier_version": CLIENT_CLASSIFIER_VERSION,
    }


@pytest.mark.parametrize(
    "header",
    [
        "sk-synthetic-secret",
        "Bearer sk_synthetic_secret",
        "eyJsynthetic.secret.token",
        "cursor/1 token=sk-synthetic-secret email=private@example.com",
    ],
)
def test_credential_and_email_fragments_never_leave_the_classifier(header) -> None:
    event = classify_execution_client(header)
    serialized = json.dumps(event)
    assert header not in serialized
    assert "synthetic" not in serialized
    assert "private@example.com" not in serialized
    assert "client_ua" not in event
