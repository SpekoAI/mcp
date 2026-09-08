"""Package metadata stays aligned with the importable runtime version."""

from __future__ import annotations

from importlib.metadata import version

import spekoai_mcp


def test_runtime_version_matches_package_metadata() -> None:
    assert spekoai_mcp.__version__ == version("spekoai-mcp")
