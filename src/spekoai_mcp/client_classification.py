"""Bounded client classification mirrored from packages/analytics/src/lib/attribution.ts.

The parity test requires coordinated matcher and version changes across runtimes.
Never export the original User-Agent or unmatched fragments.
"""

from __future__ import annotations

import re

CLIENT_CLASSIFIER_VERSION = "execution-client-v1"

# First match wins. Product markers precede generic runtimes.
CLIENT_MATCHERS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(r"(?:^|[\s;(])claude-user(?=[/\s;()]|$)", re.IGNORECASE),
        "claude_hosted",
        "observed_client_marker",
    ),
    (
        re.compile(r"(?:^|[\s;(])openai-mcp(?:/[^\s()]*)?[^\r\n]*\(Codex\)", re.IGNORECASE),
        "chatgpt_codex",
        "observed_client_marker",
    ),
    (
        re.compile(r"(?:^|[\s;(])openai-mcp(?=[/\s;()]|$)", re.IGNORECASE),
        "chatgpt",
        "observed_client_marker",
    ),
    (
        re.compile(r"(?:^|[\s;(])codex-mcp-client(?=[/\s;()]|$)", re.IGNORECASE),
        "codex",
        "observed_client_marker",
    ),
    (
        re.compile(r"(?:^|[\s;(])claude-?code(?=[/\s;()]|$)", re.IGNORECASE),
        "claude_code",
        "observed_client_marker",
    ),
    (
        re.compile(r"(?:^|[\s;(])cursor(?=[/\s;()]|$)", re.IGNORECASE),
        "cursor",
        "observed_client_marker",
    ),
    (
        re.compile(r"(?:^|[\s;(])hermes(?:-agent|-mcp)?(?=[/\s;()]|$)", re.IGNORECASE),
        "other_oauth:hermes",
        "observed_client_marker",
    ),
    (
        re.compile(r"(?:^|[\s;(])replit-agent-mcp-client(?=[/\s;()]|$)", re.IGNORECASE),
        "other_oauth:replit",
        "observed_client_marker",
    ),
    (re.compile(r"^zapier$", re.IGNORECASE), "zapier", "observed_client_marker"),
    (
        re.compile(r"(?:^|[\s;(])(?:n8n-nodes-speko|n8n)(?=[/\s;()]|$)", re.IGNORECASE),
        "n8n",
        "observed_client_marker",
    ),
    (re.compile(r"(?:^|[\s;(])make/", re.IGNORECASE), "make", "observed_client_marker"),
    (
        re.compile(r"(?:^|[\s;(])(?:leadconnector|highlevel)(?=[/\s;()]|$)", re.IGNORECASE),
        "gohighlevel",
        "observed_client_marker",
    ),
    (
        re.compile(r"(?:^|[\s;(])spekoai-python/", re.IGNORECASE),
        "sdk_python",
        "observed_client_marker",
    ),
    (re.compile(r"(?:^|[\s;(])@spekoai/sdk/", re.IGNORECASE), "sdk_ts", "observed_client_marker"),
    (
        re.compile(r"(?:^|[\s;(])@spekoai/ai-sdk-provider/", re.IGNORECASE),
        "ai_sdk_provider",
        "observed_client_marker",
    ),
    (re.compile(r"(?:^|[\s;(])pipecat-speko/", re.IGNORECASE), "pipecat", "observed_client_marker"),
    (
        re.compile(r"(?:^|[\s;(])@spekoai/mastra-voice/", re.IGNORECASE),
        "mastra",
        "observed_client_marker",
    ),
    (
        re.compile(r"(?:^|[\s;(])openclaw-speko/", re.IGNORECASE),
        "openclaw",
        "observed_client_marker",
    ),
    (
        re.compile(r"(?:^|[\s;(])@spekoai/adapter-livekit/", re.IGNORECASE),
        "adapter_livekit",
        "observed_client_marker",
    ),
    (
        re.compile(r"(?:^|[\s;(])spekoai-mcp(?=[/\s;()]|$)", re.IGNORECASE),
        "mcp_relay",
        "observed_client_marker",
    ),
    (
        re.compile(r"(?:^|[\s;(])windsurf(?=[/\s;()]|$)", re.IGNORECASE),
        "windsurf",
        "observed_client_marker",
    ),
    (re.compile(r"(?:^|[\s;(])zed(?=[/\s;()]|$)", re.IGNORECASE), "zed", "observed_client_marker"),
    (
        re.compile(r"(?:^|[\s;(])opencode(?=[/\s;()]|$)", re.IGNORECASE),
        "opencode",
        "observed_client_marker",
    ),
    (
        re.compile(r"(?:^|[\s;(])cline(?=[/\s;()]|$)", re.IGNORECASE),
        "cline",
        "observed_client_marker",
    ),
    (
        re.compile(
            r"(?:^|[\s;(])(?:vscode|visual-?studio-?code|code)(?=[/\s;()]|$)", re.IGNORECASE
        ),
        "vscode",
        "observed_client_marker",
    ),
    (
        re.compile(r"(?:^|[\s;(])gemini-cli(?=[/\s;()]|$)", re.IGNORECASE),
        "gemini_cli",
        "observed_client_marker",
    ),
    (
        re.compile(r"(?:^|[\s;(])(?:python-httpx|httpx)/", re.IGNORECASE),
        "python_httpx",
        "generic_runtime",
    ),
    (
        re.compile(r"(?:^|[\s;(])(?:python-urllib|python-requests)/", re.IGNORECASE),
        "python_other",
        "generic_runtime",
    ),
    (re.compile(r"(?:^|[\s;(])curl/", re.IGNORECASE), "curl", "generic_runtime"),
    (
        re.compile(
            r"^(?:node)(?:/\S+)?$|(?:^|[\s;(])(?:undici|node-fetch|axios)(?=[/\s;()]|$)",
            re.IGNORECASE,
        ),
        "node",
        "generic_runtime",
    ),
    (re.compile(r"(?:^|[\s;(])bun/", re.IGNORECASE), "bun", "generic_runtime"),
    (
        re.compile(r"(?:^|[\s;(])go-http-client(?=[/\s;()]|$)", re.IGNORECASE),
        "go",
        "generic_runtime",
    ),
    (re.compile(r"^mozilla/", re.IGNORECASE), "browser", "generic_runtime"),
)


def classify_execution_client(user_agent: str | None) -> dict[str, str]:
    """Interpret at most 200 characters; output only fixed allowlisted values."""
    marker = (user_agent or "").strip()[:200]
    client = "unknown:generic_marker" if marker else "unknown:missing_observation"
    evidence = "unknown"
    for pattern, candidate, candidate_evidence in CLIENT_MATCHERS:
        if marker and pattern.search(marker):
            client, evidence = candidate, candidate_evidence
            break
    return {
        "execution_client": client,
        "client_evidence_class": evidence,
        "classifier_version": CLIENT_CLASSIFIER_VERSION,
    }
