"""Package metadata stays aligned with the importable runtime version."""

from __future__ import annotations

from importlib.metadata import version
from pathlib import Path

import pytest

import spekoai_mcp


def test_runtime_version_matches_package_metadata() -> None:
    assert spekoai_mcp.__version__ == version("spekoai-mcp")


def test_shallow_repo_root_does_not_raise_index_error() -> None:
    """_default_repo_root handles shallow directory depth without IndexError."""
    import scripts.sync_docs as sync_docs

    root = sync_docs._default_repo_root()
    assert root.exists()


def test_default_repo_root_resolution_scenarios(monkeypatch: pytest.MonkeyPatch) -> None:
    """_default_repo_root anchors on packages/ or falls back to parent correctly."""
    import scripts.sync_docs as sync_docs

    # Scenario 1: monorepo layout (/work/platform/packages/mcp-server/scripts/sync_docs.py)
    fake_mono = Path("/work/platform/packages/mcp-server/scripts/sync_docs.py")
    monkeypatch.setattr(sync_docs, "__file__", str(fake_mono))
    assert sync_docs._default_repo_root() == Path("/work/platform").resolve()

    # Scenario 2: deep standalone mirror (~/dev/work/mcp/scripts/sync_docs.py)
    fake_deep_mirror = Path("/home/user/dev/work/mcp/scripts/sync_docs.py")
    monkeypatch.setattr(sync_docs, "__file__", str(fake_deep_mirror))
    assert sync_docs._default_repo_root() == Path("/home/user/dev/work/mcp").resolve()

    # Scenario 3: shallow standalone mirror (e.g. D:/mcp/scripts/sync_docs.py)
    fake_shallow_mirror = Path("D:/mcp/scripts/sync_docs.py")
    monkeypatch.setattr(sync_docs, "__file__", str(fake_shallow_mirror))
    assert sync_docs._default_repo_root() == Path("D:/mcp").resolve()


def test_conftest_needs_regen_handles_standalone_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    """_needs_regen handles standalone repos without IndexError and returns False."""
    import tests.conftest as conftest

    # Current checkout
    result = conftest._needs_regen()
    assert isinstance(result, bool)

    # Simulated root-level shallow checkout with no parents[1]
    monkeypatch.setattr(conftest, "_project_root", lambda: Path("D:/mcp"))
    assert conftest._needs_regen() is False
