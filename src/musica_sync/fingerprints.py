"""Local Chromaprint duplicate groups. Files are never removed or transcoded."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import chromaprint

from musica_sync.curation import load_library
from musica_sync.navidrome import ROOT

LOGGER = logging.getLogger(__name__)
VERSION = re.compile(
    r"\b(remix|live|acoustic|instrumental|strumentale|remaster\w*|nightcore|sped up|slowed)\b", re.I
)
CACHE = ROOT / "state/library/fingerprints.sqlite"
GROUPS = ROOT / "state/library/fingerprint-groups.json"


def fingerprint(song) -> tuple[dict | None, dict | None]:
    stat = song.path.stat()
    path = str(song.path)
    with sqlite3.connect(CACHE) as db:
        saved = db.execute(
            "SELECT duration,fp FROM fingerprint_full WHERE path=? AND size=? AND mtime_ns=?",
            (path, stat.st_size, stat.st_mtime_ns),
        ).fetchone()
    if saved:
        duration, value = saved
    else:
        result = subprocess.run(
            ["fpcalc", "-length", "120", "-json", path], capture_output=True, text=True, timeout=60
        )
        if result.returncode:
            return None, {
                "id": song.id,
                "path": path,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "error": "Audio decoding failed",
                "exit_code": result.returncode,
            }
        data = json.loads(result.stdout)
        duration, value = data["duration"], data["fingerprint"]
        with sqlite3.connect(CACHE, timeout=30) as db:
            db.execute(
                "INSERT OR REPLACE INTO fingerprint_full VALUES (?,?,?,?,?)",
                (path, stat.st_size, stat.st_mtime_ns, duration, value),
            )
    return {
        "id": song.id,
        "path": path,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "duration": duration,
        "fp": value,
        "title": song.title,
        "artist": song.artist,
    }, None


def compatible_versions(a: str, b: str) -> bool:
    return {v.casefold() for v in VERSION.findall(a)} == {v.casefold() for v in VERSION.findall(b)}


def match_score(a: list[int], b: list[int]) -> float:
    """Conservative Chromaprint word matches with a small encoder-padding window."""
    shortest = min(len(a), len(b))
    if shortest < 100:
        return 0.0
    best = 0.0
    for offset in range(-16, 17):
        left = a[max(offset, 0) :]
        right = b[max(-offset, 0) :]
        size = min(len(left), len(right))
        sample = range(0, size, max(1, size // 32))
        if sum((left[i] ^ right[i]).bit_count() <= 2 for i in sample) / len(sample) < 0.9:
            continue
        score = sum((x ^ y).bit_count() <= 2 for x, y in zip(left, right)) / shortest
        best = max(best, score)
    return best


def refresh() -> dict:
    config = json.loads((ROOT / "state/sonic/credentials.json").read_text())
    songs = load_library(config["navidrome_user"], include_excluded=True, include_damaged=True)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(CACHE) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS fingerprint_full "
            "(path TEXT PRIMARY KEY,size INTEGER,mtime_ns INTEGER,duration REAL,fp TEXT)"
        )
    with ThreadPoolExecutor(max_workers=4) as pool:
        scanned = list(pool.map(fingerprint, songs.values()))
    entries = [r for r, _ in scanned if r]
    decoded = {r["id"]: list(chromaprint.decode_fingerprint(r["fp"].encode())[0]) for r in entries}
    errors = [e for _, e in scanned if e]
    parents = {r["id"]: r["id"] for r in entries}

    def root(key: str) -> str:
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    buckets = defaultdict(list)
    matches = []
    for current in sorted(entries, key=lambda r: r["duration"]):
        duration = int(current["duration"])
        candidates = [r for n in range(duration - 2, duration + 3) for r in buckets[n]]
        for other in candidates:
            if abs(current["duration"] - other["duration"]) > 2:
                continue
            if not compatible_versions(current["title"], other["title"]):
                continue
            similarity = match_score(decoded[current["id"]], decoded[other["id"]])
            if similarity >= 0.99:
                parents[root(current["id"])] = root(other["id"])
                matches.append(
                    {
                        "a": current["id"],
                        "b": other["id"],
                        "similarity": similarity,
                        "labels": [current["title"], other["title"]],
                    }
                )
        buckets[duration].append(current)
    groups = defaultdict(list)
    for entry in entries:
        groups[root(entry["id"])].append(entry)
    mapping = []
    for group in groups.values():
        if len(group) < 2:
            continue
        group_id = min(r["id"] for r in group)
        for row in group:
            mapping.append(
                {k: row[k] for k in ("id", "path", "size", "mtime_ns")} | {"group": group_id}
            )
    report = {
        "checked_at": datetime.now(UTC).isoformat(),
        "analysed": len(entries),
        "errors": errors,
        "duplicate_groups": sum(len(g) > 1 for g in groups.values()),
        "matches": matches,
        "entries": mapping,
    }
    temporary = GROUPS.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(GROUPS)
    LOGGER.info(
        "Chromaprint: %d analysed, %d duplicate groups, %d failures",
        len(entries),
        report["duplicate_groups"],
        len(errors),
    )
    if errors:
        LOGGER.warning(
            "Excluded %d damaged recordings from discovery; originals preserved. See %s",
            len(errors),
            GROUPS,
        )
    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    refresh()
