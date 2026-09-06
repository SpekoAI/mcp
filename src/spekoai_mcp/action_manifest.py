"""Load the generated, sanitized action registry manifest."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import Any


@lru_cache(maxsize=1)
def action_manifest() -> dict[str, Any]:
    raw = (files("spekoai_mcp") / "_data" / "action-manifest.json").read_text(
        encoding="utf-8"
    )
    manifest = json.loads(raw)
    if manifest.get("schemaVersion") != 1 or not isinstance(manifest.get("actions"), list):
        raise RuntimeError("Unsupported or invalid Speko action manifest")
    return manifest


def action_entries() -> list[dict[str, Any]]:
    return list(action_manifest()["actions"])


def manifest_tool_names(profile: str) -> frozenset[str]:
    return frozenset(
        entry["id"]
        for entry in action_entries()
        if profile in entry.get("profiles", [])
    )
