"""The text block every tool result must carry.

MCP lets a tool answer in two places: the `content` text blocks and
`structuredContent`. Hosts are not required to feed both to the model, and
several do not — claude.ai web reads text blocks only, while Claude Code
surfaces structured content. A server that puts the payload solely in
`structuredContent` and an acknowledgment in the text block therefore looks
like it returns nothing at all on a text-only host, even though every call
succeeded. That is exactly what shipped: `Executed agents.list.` reached the
model and the agent list did not.

The spec's guidance is that a server SHOULD return text that is functionally
equivalent to its structured content. `payload_text` is the single place that
renders it, so the two tool families (manifest-generated and handwritten)
cannot drift apart again.
"""

from __future__ import annotations

import json
from typing import Any

# Big enough for any realistic list page, small enough that one oversized
# response cannot evict the conversation from the model's context window.
TEXT_CHAR_CEILING = 100_000


def payload_text(payload: Any, *, action: str | None = None) -> str:
    """Render `payload` as the text a text-only host will show the model.

    Falls back to a named acknowledgment only when the payload genuinely
    cannot be serialized — losing the text block is better than raising and
    turning a successful API call into a tool error.
    """
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        label = f"Executed {action}." if action else "Speko API request completed."
        return f"{label} (result not serializable as JSON)"

    if len(text) > TEXT_CHAR_CEILING:
        return (
            text[:TEXT_CHAR_CEILING]
            + f"... [truncated by the Speko MCP server: {len(text)} chars total; "
            "narrow the query or use pagination arguments]"
        )
    return text
