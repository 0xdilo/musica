"""Explicit discovery exclusions, shared by every client and playlist refresh."""

from __future__ import annotations

import fcntl
import json
from pathlib import Path

from musica_sync.navidrome import ROOT

PREFERENCES = ROOT / "state/curation/preferences.json"


def read_preferences(path: Path = PREFERENCES) -> dict:
    if not path.exists():
        return {"tracks": {}, "artists": {}}
    data = json.loads(path.read_text())
    if not all(isinstance(data.get(key), dict) for key in ("tracks", "artists")):
        raise ValueError("Invalid discovery preferences; restore the preferences backup.")
    return data


def update_preferences(
    kind: str, key: str, label: str, *, remove: bool = False, path: Path = PREFERENCES
) -> dict:
    if kind not in {"tracks", "artists"} or not key or len(key) > 300:
        raise ValueError("Invalid discovery exclusion.")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = read_preferences(path)
        if remove:
            data[kind].pop(key, None)
        else:
            data[kind][key] = label[:300]
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(path)
        return data
