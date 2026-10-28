from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

BAD_ALBUM_VALUES = {"", "script", "Singles"}
PROMO_SUFFIX_RE = re.compile(
    r"""
    (?:
        \s*[-–]\s*
        |
        \s*
    )
    [\(\[]?
    (
        official(?:\s+music)?\s+video |
        official\s+audio |
        audio\s+oficial |
        lyric(?:s)?\s+video
    )
    [\)\]]?
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)
TRACK_NUMBER_RE = re.compile(r"^\s*\d{1,2}[.)-]\s+")
PROMO_BRACKET_RE = re.compile(
    r"[\[(](?:official(?:\s+(?:music|hd|4k))?\s*(?:video|audio|visualizer)?|"
    r"(?:music|lyric[s]?)\s+video|clip officiel|audio oficial|(?:hd|hq|4k))[\])]",
    re.IGNORECASE,
)


def load_info_json(audio_path: Path) -> dict[str, Any] | None:
    info_path = audio_path.with_suffix(".info.json")
    if not info_path.exists():
        return None

    return json.loads(info_path.read_text(encoding="utf-8"))


def first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str):
            text = value.strip()
            if text:
                return text
    return None


def clean_title(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", PROMO_BRACKET_RE.sub("", text)).strip()
    while True:
        updated = PROMO_SUFFIX_RE.sub("", cleaned).strip()
        if updated == cleaned:
            return updated
        cleaned = updated


def strip_artist_prefix(title: str, artist: str | None) -> str:
    if not artist:
        return title

    artist_folded = artist.casefold()
    for separator in (" - ", " | ", " ｜ ", ": "):
        prefix = f"{artist}{separator}"
        if title.casefold().startswith(prefix.casefold()):
            return title[len(prefix) :].strip()

        left, _, right = title.partition(separator)
        if left.casefold() == artist_folded and right:
            return right.strip()

    return title


def strip_track_number(title: str) -> str:
    return TRACK_NUMBER_RE.sub("", title).strip()


def derive_title_and_album(raw_title: str, artist: str | None) -> tuple[str, str | None]:
    without_artist = strip_artist_prefix(raw_title, artist)
    cleaned = clean_title(without_artist)
    candidate = strip_track_number(cleaned)

    # A channel/uploader is not necessarily the artist. An unverified "Artist - Song"
    # must not become title="Artist", album="Song".
    if " - " not in candidate or without_artist == raw_title:
        return candidate, None

    left, right = candidate.split(" - ", 1)
    left = strip_track_number(left)
    right = clean_title(right)
    if left and right:
        return left, right
    return candidate, None


def derive_year(info: dict[str, Any] | None, current: dict[str, Any]) -> int | None:
    if info:
        for key in ("release_year",):
            value = info.get(key)
            if isinstance(value, int) and value > 0:
                return value

        for key in ("release_date", "upload_date"):
            value = info.get(key)
            if isinstance(value, str) and len(value) >= 4 and value[:4].isdigit():
                return int(value[:4])

    current_year = current.get("year")
    if isinstance(current_year, int) and current_year > 0:
        return current_year
    return None


def derive_tags(
    *,
    path_stem: str,
    current: dict[str, Any],
    info: dict[str, Any] | None,
    repair_legacy_playlist_album: str | None = None,
) -> dict[str, Any]:
    info = info or {}

    artist = first_text(
        info.get("artist"),
        info.get("creator"),
        current.get("artist"),
        info.get("uploader"),
        info.get("channel"),
    )

    raw_title = (
        first_text(
            info.get("track"),
            current.get("title"),
            info.get("title"),
            path_stem,
        )
        or path_stem
    )

    if info.get("track"):
        title = clean_title(str(info["track"]))
        parsed_album = None
    else:
        title, parsed_album = derive_title_and_album(raw_title, artist)

    title = title or path_stem

    existing_album = first_text(current.get("album"))
    if existing_album in BAD_ALBUM_VALUES or existing_album == repair_legacy_playlist_album:
        existing_album = None

    album = first_text(info.get("album"), parsed_album, existing_album)
    if not album:
        album = title

    albumartist = first_text(
        info.get("artist"),
        info.get("creator"),
        current.get("albumartist"),
        artist,
    )

    return {
        "title": title,
        "artist": artist,
        "album": album,
        "albumartist": albumartist,
        "year": derive_year(info, current),
    }
