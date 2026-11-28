"""Deterministic feedback for inline outbound phone prompts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_CONFUSION = re.compile(r"\bdo not confuse\b|\bdon'?t confuse\b|\bnot to be confused\b", re.I)
_SPELLING = re.compile(r"\bspell\b[^.]{0,40}\b(slowly|letter by letter|twice)\b", re.I)
_READ_BACK = re.compile(r"\bread ?back\b|\brepeat back\b", re.I)
_QUESTION_MARK = re.compile(r"[?？؟]")

SHAPE = (
    "Shape for phone tasks: firstMessage = AI disclosure + first question; systemPrompt = "
    "questions in order, facts to say only if asked, private checks never said aloud; at "
    "most one short read-back at the end."
)


def advise(body: Mapping[str, Any]) -> list[str]:
    """Up to four findings in priority order, then the target shape; [] when clean."""
    try:
        prompt = body.get("systemPrompt")
        if body.get("agentId") or not isinstance(prompt, str) or not prompt:
            return []

        advice = []
        if len(prompt) > 1200:
            advice.append(
                f"systemPrompt is long ({len(prompt)} chars); phone calls go better with about "
                "150 words: questions in order, facts to say only if asked, private notes."
            )
        if _CONFUSION.search(prompt):
            advice.append(
                "Move 'do not confuse ...' notes into a private-notes block the agent never says "
                "aloud; the agent can read them out."
            )
        if _SPELLING.search(prompt):
            advice.append(
                "Ask the agent to say a code once and spell it only when asked; spelling "
                "instructions make it spell codes unprompted."
            )
        if any(
            _READ_BACK.search(sentence)
            and sum(bool(item.strip()) for item in sentence.split(",")) >= 3
            for sentence in re.split(r"[.!?]", prompt)
        ):
            advice.append(
                "Limit the read-back to one or two key facts at the end; a long read-back makes "
                "the callee confirm everything again."
            )
        first_message = body.get("firstMessage")
        if isinstance(first_message, str) and not _QUESTION_MARK.search(first_message):
            advice.append(
                "firstMessage has no question; put your first question in it so a person who "
                "picks up hears why you called right away."
            )
        if not advice:
            return []
        # The shape to aim for next time, after the specific findings.
        return [*advice[:4], SHAPE]
    except Exception:
        # Advice must never interfere with placing the call.
        return []
