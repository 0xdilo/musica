"""Remove video categories from music genre metadata, preserving real genres."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from beets.library import Library
from mediafile import MediaFile

from musica_sync.playlists import JUNK_GENRES, fold


def music_genres(values: list[str]) -> list[str]:
    return [value for value in values if fold(value) not in JUNK_GENRES]


def clean_library_genres(database: Path, backups: Path) -> int:
    library = Library(database)
    try:
        changed = [item for item in library.items() if music_genres(item.genres) != item.genres]
        if not changed:
            return 0
        backups.mkdir(parents=True, exist_ok=True)
        manifest = [
            {"id": item.id, "path": str(item.path), "genres": item.genres} for item in changed
        ]
        (backups / f"video-category-genres-{time.time_ns()}.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        )
        for item in changed:
            media = MediaFile(item.path)
            media.genres = music_genres(media.genres)
            media.save()
            item.genres = music_genres(item.genres)
            item.mtime = item.current_mtime()
            item.store()
        logging.info("Removed video categories from %d tracks' genre tags.", len(changed))
        return len(changed)
    finally:
        library._close()
