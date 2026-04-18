"""Shared pytest fixtures.

Guarantees `src/spekoai_mcp/_docs/manifest.json` exists before tests run.
The sync script is normally invoked by Nx (`sync-docs` target) or the
Dockerfile build stage, but pytest must not fail merely because a
developer hasn't run it manually yet.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _docs_dir() -> Path:
    return _project_root() / "src" / "spekoai_mcp" / "_docs"


def _needs_regen() -> bool:
    manifest = _docs_dir() / "manifest.json"
    if not manifest.exists():
        return True
    manifest_mtime = manifest.stat().st_mtime
    # Only walk directories that actually contain bundled sources.
    # `CLAUDE.md` and `ROADMAP.md` are deliberately excluded from the
    # bundle (see `scripts/sync_docs.py` policy comment), so we
    # intentionally skip them here too — otherwise editing a roadmap
    # would trigger a no-op regen every pytest session.
    monorepo_root = _project_root().parents[1]
    packages_dir = monorepo_root / "packages"
    if not packages_dir.is_dir():
        return False
    bundled_filenames = {"README.md", "SKILLS.md", "index.ts"}
    for path in packages_dir.rglob("*"):
        if (
            path.is_file()
            and path.name in bundled_filenames
            and path.stat().st_mtime > manifest_mtime
        ):
            return True
    return False


def _import_sync_docs():
    script = _project_root() / "scripts" / "sync_docs.py"
    spec = importlib.util.spec_from_file_location("sync_docs", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["sync_docs"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True, scope="session")
def ensure_docs_synced() -> None:
    """Regenerate `_docs/` if missing or stale relative to sources."""
    if not _needs_regen():
        return
    sync_docs = _import_sync_docs()
    sync_docs.sync()
    # Bust any lru_cache on the manifest so tests see fresh data.
    try:
        from spekoai_mcp.docs import load_manifest
        load_manifest.cache_clear()
    except ImportError:
        pass
