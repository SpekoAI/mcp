"""Runtime loader for the bundled SpekoAI docs.

The `_docs/` directory is populated at build time by
`scripts/sync_docs.py` — at runtime we only read from the wheel via
`importlib.resources`. Never fall back to reading sibling-package
directories directly; that path works in editable dev installs but
fails silently in the deployed wheel.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import Literal, TypedDict

Status = Literal["stable", "alpha", "scaffold", "internal", "platform"]
Kind = Literal["readme", "skills", "roadmap", "platform", "quickstart"]


class DocEntry(TypedDict):
    """One row of the manifest. Mirrors `scripts/sync_docs.SourceDoc`
    plus the derived `title` and `summary`."""

    slug: str
    source: str
    package_name: str
    npm_or_pypi: str | None
    status: Status
    kind: Kind
    title: str
    summary: str


class DocsNotBuiltError(RuntimeError):
    """Raised when `_docs/manifest.json` is missing at runtime.

    In a wheel install this can't happen (the file is bundled). In an
    editable dev install it means `scripts/sync_docs.py` hasn't been run
    yet; the conftest regenerates on demand, but production code paths
    shouldn't depend on that.
    """


_DOCS_PACKAGE = "spekoai_mcp._docs"


@lru_cache(maxsize=1)
def load_manifest() -> list[DocEntry]:
    try:
        manifest_res = files(_DOCS_PACKAGE) / "manifest.json"
        raw = manifest_res.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise DocsNotBuiltError(
            "spekoai_mcp/_docs/manifest.json is missing. Run "
            "`python scripts/sync_docs.py` before building or running tests."
        ) from exc
    return json.loads(raw)


def all_slugs() -> list[str]:
    return [entry["slug"] for entry in load_manifest()]


def get_entry(slug: str) -> DocEntry:
    for entry in load_manifest():
        if entry["slug"] == slug:
            return entry
    raise KeyError(f"unknown doc slug: {slug!r}")


def read_doc(slug: str) -> str:
    """Return the markdown body for `slug`. Raises `KeyError` if unknown."""
    entry = get_entry(slug)  # raises KeyError if slug is bogus
    file_res = files(_DOCS_PACKAGE) / f"{entry['slug']}.md"
    return file_res.read_text(encoding="utf-8")


def format_index() -> str:
    """Render the manifest as a markdown index for `spekoai://docs/index`."""
    manifest = load_manifest()
    by_kind: dict[Kind, list[DocEntry]] = {
        "readme": [],
        "skills": [],
        "roadmap": [],
        "platform": [],
        "quickstart": [],
    }
    for entry in manifest:
        by_kind[entry["kind"]].append(entry)

    sections = [
        ("## Skill sheets (dense agent-oriented references)", "skills"),
        ("## READMEs (prose walkthroughs)", "readme"),
        ("## Platform", "platform"),
        ("## Quickstart example", "quickstart"),
        ("## Roadmaps (deferred work)", "roadmap"),
    ]
    lines: list[str] = [
        "# SpekoAI documentation index",
        "",
        (
            "Every URI below resolves via "
            "`resources/read` on this MCP server. Prefer the skill sheets "
            "first — they're written for LLMs and name the exact types "
            "and gotchas. The READMEs are longer prose walkthroughs."
        ),
        "",
    ]
    for heading, kind_key in sections:
        entries = by_kind.get(kind_key, [])  # type: ignore[arg-type]
        if not entries:
            continue
        lines.append(heading)
        lines.append("")
        for entry in entries:
            uri = f"spekoai://docs/{entry['slug']}"
            pkg = entry["package_name"]
            status = entry["status"]
            summary = entry["summary"] or ""
            if summary:
                lines.append(f"- `{uri}` — **{pkg}** ({status}) — {summary}")
            else:
                lines.append(f"- `{uri}` — **{pkg}** ({status})")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
