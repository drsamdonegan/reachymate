"""Pinned, local-only community recordings. Dataset content is inert, untrusted JSON."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

MAX_RECORDING_BYTES = 16 * 1024 * 1024
COMMUNITY_TAG = "reachy_mini_community_moves"


def recording_id(repo: str, filename: str) -> str:
    label = re.sub(r"[^a-z0-9_-]+", "_", f"{repo.split('/')[0]}_{Path(filename).stem}".lower())
    suffix = hashlib.sha256(f"{repo}/{filename}".encode()).hexdigest()[:10]
    return f"move_{label[:47]}_{suffix}"


def load_index(directory: Path) -> dict:
    path = directory / "community-manifest.json"
    if not path.is_file():
        return {}
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("Community manifest is too large")
    data = json.loads(path.read_text())
    result = {}
    for name, entry in data.get("moves", {}).items():
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name) or not isinstance(entry, dict):
            continue
        if not re.fullmatch(r"[a-f0-9]{40}", entry.get("revision", "")):
            continue
        if not re.fullmatch(r"[a-f0-9]{64}", entry.get("sha256", "")):
            continue
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", entry.get("repo", "")):
            continue
        if not isinstance(entry.get("title"), str) or len(entry["title"]) > 120:
            continue
        if (directory / "community" / f"{name}.json").is_file():
            result[name] = entry
    return result


def read_recording(directory: Path, name: str, entry: dict) -> dict:
    path = directory / "community" / f"{name}.json"
    if path.stat().st_size > MAX_RECORDING_BYTES:
        raise ValueError("Recording is too large")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != entry["sha256"]:
        raise ValueError("Recording integrity check failed; provision the library again")
    return json.loads(content)
