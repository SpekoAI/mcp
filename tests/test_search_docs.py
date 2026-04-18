"""Tests for the `search_docs` tool / `search` module."""

from __future__ import annotations

import pytest

from spekoai_mcp.search import DocHit, search


@pytest.mark.parametrize(
    "query, expected_top_slug",
    [
        ("VoiceConversation", "client-skills"),
        ("createSpekoComponents", "adapter-livekit-skills"),
        ("AsyncSpeko", "sdk-python-skills"),
    ],
)
def test_seeded_queries_return_expected_top_slug(
    query: str, expected_top_slug: str
) -> None:
    hits = search(query, limit=5)
    assert hits, f"no hits for {query!r}"
    assert hits[0].slug == expected_top_slug, (
        f"expected {expected_top_slug!r} for {query!r}, got {hits[0].slug!r}"
    )


def test_empty_query_returns_no_hits() -> None:
    assert search("", limit=5) == []
    assert search("   ", limit=5) == []


def test_hit_shape() -> None:
    hits = search("transcribe", limit=3)
    assert hits
    for hit in hits:
        assert isinstance(hit, DocHit)
        assert hit.slug
        assert hit.title
        assert hit.score > 0
        assert hit.snippet


def test_limit_is_respected() -> None:
    hits = search("speko", limit=3)
    assert len(hits) <= 3
