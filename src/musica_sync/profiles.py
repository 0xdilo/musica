"""Merge local listening data without losing play counts, ratings or favourites."""

from __future__ import annotations

import sqlite3


def latest(*values: str | None) -> str | None:
    return max((v for v in values if v), default=None)


def merged_annotation(target: dict, source: dict) -> dict:
    result = dict(target)
    result["play_count"] = (target.get("play_count") or 0) + (source.get("play_count") or 0)
    result["play_date"] = latest(target.get("play_date"), source.get("play_date"))
    result["starred"] = bool(target.get("starred") or source.get("starred"))
    dates = [r["starred_at"] for r in (target, source) if r.get("starred") and r.get("starred_at")]
    result["starred_at"] = min(dates) if dates else None
    newest = max((target, source), key=lambda r: (r.get("rated_at") or "", bool(r.get("rating"))))
    result["rating"] = newest.get("rating") or 0
    result["rated_at"] = newest.get("rated_at")
    return result


def merge_annotations(db: sqlite3.Connection, source_id: str, target_id: str) -> int:
    db.row_factory = sqlite3.Row
    sources = db.execute("SELECT * FROM annotation WHERE user_id=?", (source_id,)).fetchall()
    for source in sources:
        target = db.execute(
            "SELECT * FROM annotation WHERE user_id=? AND item_id=? AND item_type=?",
            (target_id, source["item_id"], source["item_type"]),
        ).fetchone()
        values = dict(source)
        if target:
            values = merged_annotation(dict(target), values)
        values["user_id"] = target_id
        columns = list(values)
        db.execute(
            f"INSERT OR REPLACE INTO annotation ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            list(values.values()),
        )
    db.execute("DELETE FROM annotation WHERE user_id=?", (source_id,))
    return len(sources)


def merge_users(db: sqlite3.Connection, source: str, target: str) -> dict:
    db.row_factory = sqlite3.Row
    users = {r["user_name"]: r["id"] for r in db.execute("SELECT id,user_name FROM user")}
    if source not in users or target not in users or source == target:
        raise ValueError("Both distinct profile names must exist before merging.")
    source_id, target_id = users[source], users[target]
    # This migration deliberately refuses unknown nonempty conflict-prone tables.
    # The current installation has none; a future migration must handle their semantics.
    for table in ("bookmark", "user_props", "playqueue", "scrobble_buffer"):
        if db.execute(f"SELECT count(*) FROM {table} WHERE user_id=?", (source_id,)).fetchone()[0]:
            raise ValueError(
                f"Source has {table} data; add a conflict-aware merge before proceeding."
            )
    counts = {"annotations_merged": merge_annotations(db, source_id, target_id)}
    for table in ("player", "share", "scrobbles"):
        counts[table] = db.execute(
            f"UPDATE {table} SET user_id=? WHERE user_id=?", (target_id, source_id)
        ).rowcount
    counts["playlists_transferred"] = db.execute(
        "UPDATE playlist SET owner_id=? WHERE owner_id=?", (target_id, source_id)
    ).rowcount
    db.execute(
        "INSERT OR IGNORE INTO user_library(user_id,library_id) "
        "SELECT ?,library_id FROM user_library WHERE user_id=?",
        (target_id, source_id),
    )
    db.execute("DELETE FROM user_library WHERE user_id=?", (source_id,))
    return {"source": source, "target": target, **counts}
