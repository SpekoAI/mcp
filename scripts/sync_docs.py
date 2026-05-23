#!/usr/bin/env python3
"""Copy public docs into this wheel's `_docs/` at build time.

The MCP server exposes SpekoAI's public READMEs, hosted llms.txt exports,
and a handful of migration / quickstart files as MCP resources. At runtime
the server reads them via `importlib.resources` — meaning they must live
inside the package directory when the wheel is built. The source of truth
is either a sibling package doc (`packages/sdk/README.md`, …) or the
hosted docs export (`https://docs.speko.dev/llms-full.txt`), so we copy
or fetch them here as a pre-build step and write a `manifest.json` with
slug metadata.

Invocation:
    python packages/mcp-server/scripts/sync_docs.py [--repo-root PATH]

Idempotent: rewrites `_docs/*.md` + `_docs/manifest.json` every run. Safe
to call from Nx (via the `sync-docs` target), the Dockerfile, and pytest
conftest.
"""

from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal
from urllib.error import URLError
from urllib.request import urlopen

Status = Literal["stable", "alpha", "scaffold", "internal", "platform"]
Kind = Literal["readme", "llms", "roadmap", "platform", "quickstart", "guide"]


@dataclass(frozen=True)
class SourceDoc:
    slug: str
    source: str  # repo-root-relative path or https URL
    package_name: str  # human label, matches npm/PyPI name where applicable
    npm_or_pypi: str | None  # canonical install name; None for internal packages
    status: Status
    kind: Kind


# Bundle policy: ONLY public, user-facing docs for PUBLIC packages and
# docs.speko.dev llms exports.
#
# Excluded deliberately:
# - `@spekoai/core`, `@spekoai/providers` — `"private": true` internal
#   monorepo packages that external users don't need.
# - Root `CLAUDE.md` — describes `apps/*` and monorepo internals.
# - ROADMAP.md files — forward-looking product direction /
#   infra-engineering notes we don't want leaking publicly.
# - `apps/*/SKILLS.md` and `packages/*/SKILLS.md` — replaced by the
#   hosted llms.txt exports, which are generated from the public docs site
#   and are more useful to agents.
#
# Order here becomes the order in the generated `index` resource.
# Change with intent.
SOURCE_DOCS: list[SourceDoc] = [
    # --- Hosted docs exports ----------------------------------------------
    SourceDoc(
        "llms", "https://docs.speko.dev/llms.txt", "docs.speko.dev", None, "platform", "llms"
    ),
    SourceDoc(
        "llms-full",
        "https://docs.speko.dev/llms-full.txt",
        "docs.speko.dev",
        None,
        "platform",
        "llms",
    ),
    # --- READMEs -----------------------------------------------------------
    SourceDoc(
        "sdk-readme", "packages/sdk/README.md", "@spekoai/sdk", "@spekoai/sdk", "stable", "readme"
    ),
    SourceDoc(
        "client-readme",
        "packages/client/README.md",
        "@spekoai/client",
        "@spekoai/client",
        "alpha",
        "readme",
    ),
    SourceDoc(
        "sdk-python-readme",
        "packages/sdk-python/README.md",
        "spekoai (Python)",
        "spekoai",
        "alpha",
        "readme",
    ),
    SourceDoc(
        "adapter-livekit-readme",
        "packages/adapter-livekit/README.md",
        "@spekoai/adapter-livekit",
        "@spekoai/adapter-livekit",
        "stable",
        "readme",
    ),
    SourceDoc(
        "adapter-vapi-readme",
        "packages/adapter-vapi/README.md",
        "@spekoai/adapter-vapi",
        "@spekoai/adapter-vapi",
        "scaffold",
        "readme",
    ),
    SourceDoc(
        "adapter-retell-readme",
        "packages/adapter-retell/README.md",
        "@spekoai/adapter-retell",
        "@spekoai/adapter-retell",
        "scaffold",
        "readme",
    ),
    SourceDoc(
        "mcp-server-readme",
        "packages/mcp-server/README.md",
        "spekoai-mcp",
        "spekoai-mcp",
        "alpha",
        "readme",
    ),
    # --- Quickstart example ------------------------------------------------
    SourceDoc(
        "quickstart-node-readme",
        "packages/sdk/examples/quickstart-node/README.md",
        "quickstart-node",
        None,
        "platform",
        "quickstart",
    ),
    SourceDoc(
        "quickstart-node-index-ts",
        "packages/sdk/examples/quickstart-node/src/index.ts",
        "quickstart-node",
        None,
        "platform",
        "quickstart",
    ),
    # --- Migration guides --------------------------------------------------
    SourceDoc(
        "migration-livekit",
        "packages/mcp-server/docs/migrations/livekit.md",
        "migration guides",
        None,
        "platform",
        "guide",
    ),
    SourceDoc(
        "migration-pipecat",
        "packages/mcp-server/docs/migrations/pipecat.md",
        "migration guides",
        None,
        "platform",
        "guide",
    ),
    SourceDoc(
        "migration-retell",
        "packages/mcp-server/docs/migrations/retell.md",
        "migration guides",
        None,
        "platform",
        "guide",
    ),
    SourceDoc(
        "migration-vapi",
        "packages/mcp-server/docs/migrations/vapi.md",
        "migration guides",
        None,
        "platform",
        "guide",
    ),
]


_TITLE_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)


def _extract_title(body: str, fallback: str) -> str:
    m = _TITLE_RE.search(body)
    if m:
        return m.group(1).strip().strip("`")
    return fallback


def _extract_summary(body: str) -> str:
    """Pull the first paragraph after the H1, stripping blockquotes.

    For .ts source files (the quickstart index), the first comment block
    serves as the summary; if there's none, fall back to the filename.
    """
    lines = body.splitlines()
    # Skip blank lines + the first heading, then find the first non-blank
    # paragraph that isn't a blockquote marker or a code fence.
    i = 0
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    if i < len(lines) and lines[i].startswith("#"):
        i += 1
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    collected: list[str] = []
    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.lstrip("> ").strip()
        if not stripped:
            if collected:
                break
            i += 1
            continue
        if stripped.startswith("```"):
            break
        collected.append(stripped)
        i += 1
    if not collected:
        return ""
    summary = " ".join(collected)
    # Trim to one sentence-ish — keep it compact for the index listing.
    if len(summary) > 240:
        cut = summary.rfind(". ", 0, 240)
        summary = summary[: cut + 1] if cut > 80 else summary[:240].rstrip() + "…"
    return summary


def _default_repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _is_remote_source(source: str) -> bool:
    return source.startswith(("https://", "http://"))


def _ssl_context() -> ssl.SSLContext | None:
    """Prefer certifi when available; local Python installs often lack CA setup."""
    try:
        import certifi  # type: ignore[import-not-found]
    except ImportError:
        return None
    return ssl.create_default_context(cafile=certifi.where())


def _read_source(root: Path, doc: SourceDoc) -> tuple[str, str]:
    """Read a local source file or hosted docs export.

    Returns `(body, suffix)` where suffix is used only for TS wrapping.
    """
    if _is_remote_source(doc.source):
        try:
            with urlopen(doc.source, timeout=15, context=_ssl_context()) as response:
                content_type = response.headers.get("content-type", "")
                if "text/" not in content_type and "markdown" not in content_type:
                    raise RuntimeError(
                        f"{doc.source} returned unexpected content-type {content_type!r}"
                    )
                return response.read().decode("utf-8"), Path(doc.source).suffix
        except URLError as exc:
            raise RuntimeError(f"failed to fetch {doc.source}: {exc}") from exc

    src = root / doc.source
    if not src.exists():
        raise FileNotFoundError(doc.source)
    return src.read_text(encoding="utf-8"), src.suffix


def sync(repo_root: Path | None = None) -> Path:
    """Copy all SOURCE_DOCS into `_docs/`, write manifest.json. Returns dest dir."""
    root = repo_root or _default_repo_root()
    dest = Path(__file__).resolve().parents[1] / "src" / "spekoai_mcp" / "_docs"
    dest.mkdir(parents=True, exist_ok=True)

    # Clean out stale generated files so a removed slug doesn't linger.
    for existing in dest.glob("*.md"):
        existing.unlink()
    manifest_path = dest / "manifest.json"
    if manifest_path.exists():
        manifest_path.unlink()

    manifest: list[dict[str, object]] = []
    missing: list[str] = []

    for doc in SOURCE_DOCS:
        try:
            body, suffix = _read_source(root, doc)
        except FileNotFoundError:
            missing.append(doc.source)
            continue
        # For .ts files, wrap in a fenced code block so downstream MCP
        # clients render it as code, not prose. Also prepend a title line.
        if suffix == ".ts":
            title = f"{doc.package_name} — {Path(doc.source).name}"
            wrapped = f"# {title}\n\nSource file at `{doc.source}`.\n\n```ts\n{body}\n```\n"
            body_for_write = wrapped
            summary = (
                f"TypeScript source for {Path(doc.source).name} in the {doc.package_name} example."
            )
        else:
            title = _extract_title(body, doc.package_name)
            body_for_write = body
            summary = _extract_summary(body)

        (dest / f"{doc.slug}.md").write_text(body_for_write, encoding="utf-8")
        entry = {
            **asdict(doc),
            "title": title,
            "summary": summary,
        }
        manifest.append(entry)

    if missing:
        # Fail loud — missing source files would silently produce a shorter
        # index, which is worse than a broken build.
        raise SystemExit("sync_docs: missing source files:\n  " + "\n  ".join(missing))

    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return dest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Path to the monorepo root. Defaults to three levels above this script.",
    )
    args = parser.parse_args()
    dest = sync(args.repo_root)
    print(f"sync_docs: wrote {len(SOURCE_DOCS)} docs + manifest.json to {dest}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # pragma: no cover — surfaced to the build logs
        print(f"sync_docs: {exc}", file=sys.stderr)
        raise
