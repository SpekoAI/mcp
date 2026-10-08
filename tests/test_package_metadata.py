"""Package metadata stays aligned with the importable runtime version."""

from __future__ import annotations

from importlib.metadata import version

import spekoai_mcp


def test_runtime_version_matches_package_metadata() -> None:
    assert spekoai_mcp.__version__ == version("spekoai-mcp")


def test_shallow_repo_root_does_not_raise_index_error() -> None:
    """_default_repo_root handles shallow directory depth without IndexError."""
    import scripts.sync_docs as sync_docs

    root = sync_docs._default_repo_root()
    assert root.exists()


def test_conftest_needs_regen_handles_standalone_repo() -> None:
    """_needs_regen handles standalone repos without IndexError."""
    import tests.conftest as conftest

    # Must return boolean and not raise IndexError on shallow root
    result = conftest._needs_regen()
    assert isinstance(result, bool)

