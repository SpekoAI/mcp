"""Tiny full-text search over the bundled docs.

No external deps — a case-insensitive substring match with a simple
token-overlap score, plus a snippet around the first hit. Good enough
for the ~20 short markdown files we ship; an agent that wants more
precision can just read the index resource and follow slugs directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, Field

from spekoai_mcp.docs import DocEntry, all_slugs, get_entry, read_doc

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def _tokenize(s: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(s)]


class DocHit(BaseModel):
    """One search result. Serialized verbatim to MCP clients."""

    slug: str = Field(description="Doc slug — read with spekoai://docs/{slug}.")
    title: str
    package_name: str
    kind: str
    score: float = Field(description="Higher = better match.")
    snippet: str = Field(
        description="~240 char excerpt around the first match, with ellipses."
    )


@dataclass
class _Scored:
    slug: str
    score: float
    first_hit: int | None


def _snippet_around(body: str, idx: int, width: int = 240) -> str:
    start = max(0, idx - width // 2)
    end = min(len(body), start + width)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(body) else ""
    return prefix + body[start:end].strip() + suffix


def search(query: str, limit: int = 5) -> list[DocHit]:
    query = (query or "").strip()
    if not query:
        return []

    query_tokens = set(_tokenize(query))
    # Substring hits carry the most signal; use the raw query lowercased.
    q_lower = query.lower()

    scored: list[_Scored] = []
    # Carry (entry, body) forward so the result-building loop doesn't
    # re-read the top-N docs — `importlib.resources` isn't memoized, so
    # every `read_doc()` hits the filesystem. Bundle is small enough
    # that holding all bodies in memory during a single search is fine.
    cache: dict[str, tuple[DocEntry, str]] = {}
    for slug in all_slugs():
        body = read_doc(slug)
        entry = get_entry(slug)
        cache[slug] = (entry, body)

        body_lower = body.lower()

        first_sub_hit = body_lower.find(q_lower)
        substring_hits = 0
        if first_sub_hit >= 0:
            substring_hits = body_lower.count(q_lower)

        # Token overlap — Jaccard-ish, bounded.
        body_tokens = set(_tokenize(body))
        overlap = len(query_tokens & body_tokens) / max(len(query_tokens), 1)

        # Title bonus: if the raw query lands in the title or
        # package_name, weight it — cheapest strong signal we have.
        title_lower = (entry["title"] + " " + entry["package_name"]).lower()
        title_bonus = 1.0 if q_lower in title_lower else 0.0

        score = 2.0 * substring_hits + 1.0 * overlap + 3.0 * title_bonus
        if score > 0:
            scored.append(_Scored(
                slug=slug,
                score=score,
                first_hit=first_sub_hit if first_sub_hit >= 0 else None,
            ))

    scored.sort(key=lambda s: s.score, reverse=True)
    top = scored[:limit]

    hits: list[DocHit] = []
    for s in top:
        entry, body = cache[s.slug]
        if s.first_hit is not None:
            snippet = _snippet_around(body, s.first_hit)
        else:
            # No substring hit but token overlap matched — show the
            # first non-blank paragraph so the result is still useful.
            snippet = _snippet_around(body, 0)
        hits.append(DocHit(
            slug=s.slug,
            title=entry["title"],
            package_name=entry["package_name"],
            kind=entry["kind"],
            score=round(s.score, 3),
            snippet=snippet,
        ))
    return hits
