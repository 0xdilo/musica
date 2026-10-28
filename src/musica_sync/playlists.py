from __future__ import annotations

import os
import random
import re
import sqlite3
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

GENRE_SPLIT_RE = re.compile(r"\s*(?:[;,/|]|\\?␀|\x00)\s*")
TOKEN_SPLIT_RE = re.compile(r"\s+")
YEAR_TOKEN_RE = re.compile(r"^(?P<year>\d{4})$")
DECADE_TOKEN_RE = re.compile(r"^(?P<decade>\d{2,4})s$")
SAFE_FILENAME_RE = re.compile(r"[^\w\-. ]+", re.UNICODE)

JUNK_GENRES: frozenset[str] = frozenset(
    {
        "music",
        "entertainment",
        "comedy",
        "gaming",
        "people & blogs",
        "howto & style",
        "film & animation",
        "nonprofits & activism",
        "education",
        "science & technology",
        "news & politics",
        "sports",
        "pets & animals",
        "travel & events",
        "autos & vehicles",
        "other",
    }
)


@dataclass(frozen=True)
class Track:
    path: Path
    artist: str
    title: str
    album: str
    year: int | None
    genres: tuple[str, ...]

    @property
    def search_blob(self) -> str:
        parts = [self.artist, self.title, self.album, *self.genres]
        return fold(" ".join(part for part in parts if part))


@dataclass(frozen=True)
class Query:
    raw: str
    genre_terms: tuple[str, ...] = ()
    free_terms: tuple[str, ...] = ()
    years: tuple[int, ...] = ()
    decades: tuple[int, ...] = ()


@dataclass
class PlaylistResult:
    name: str
    path: Path
    track_count: int


@dataclass
class AutoPlaylistReport:
    written: list[PlaylistResult] = field(default_factory=list)
    removed: list[Path] = field(default_factory=list)


def fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return stripped.casefold().strip()


def split_genres(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()

    seen: list[str] = []
    seen_folded: set[str] = set()
    for chunk in GENRE_SPLIT_RE.split(raw):
        cleaned = chunk.strip()
        if not cleaned:
            continue
        key = fold(cleaned)
        if not key or key in JUNK_GENRES or key in seen_folded:
            continue
        seen_folded.add(key)
        seen.append(cleaned)

    return tuple(seen)


def decode_path(raw: bytes | str) -> Path:
    if isinstance(raw, bytes):
        return Path(raw.decode("utf-8", errors="surrogateescape"))
    return Path(raw)


def load_tracks(db_path: Path) -> list[Track]:
    if not db_path.exists():
        return []

    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT path, artist, title, album, year, genres FROM items"
        ).fetchall()
    finally:
        connection.close()

    tracks: list[Track] = []
    for row in rows:
        tracks.append(
            Track(
                path=decode_path(row["path"]),
                artist=(row["artist"] or "").strip(),
                title=(row["title"] or "").strip(),
                album=(row["album"] or "").strip(),
                year=row["year"] if isinstance(row["year"], int) and row["year"] > 0 else None,
                genres=split_genres(row["genres"]),
            )
        )
    return tracks


def parse_query(raw: str) -> Query:
    cleaned = raw.strip()
    if not cleaned:
        return Query(raw=cleaned)

    genre_terms: list[str] = []
    free_terms: list[str] = []
    years: list[int] = []
    decades: list[int] = []

    for token in TOKEN_SPLIT_RE.split(cleaned):
        bare = token.strip()
        if not bare:
            continue

        prefix = ""
        body = bare
        if bare.startswith(("genre:", "g:")):
            prefix, _, body = bare.partition(":")

        decade_match = DECADE_TOKEN_RE.match(body)
        year_match = YEAR_TOKEN_RE.match(body)

        if not prefix and decade_match:
            decade = int(decade_match.group("decade"))
            if decade < 100:
                decade = 1900 + decade if decade >= 30 else 2000 + decade
            decades.append(decade - decade % 10)
            continue

        if not prefix and year_match:
            years.append(int(year_match.group("year")))
            continue

        folded = fold(body)
        if not folded:
            continue

        if prefix in ("genre", "g"):
            genre_terms.append(folded)
        else:
            free_terms.append(folded)

    return Query(
        raw=cleaned,
        genre_terms=tuple(genre_terms),
        free_terms=tuple(free_terms),
        years=tuple(years),
        decades=tuple(decades),
    )


def track_matches(track: Track, query: Query) -> bool:
    if query.years and track.year not in query.years:
        return False

    if query.decades:
        if track.year is None:
            return False
        if (track.year - track.year % 10) not in query.decades:
            return False

    folded_genres = tuple(fold(g) for g in track.genres)
    for term in query.genre_terms:
        if not any(term in genre for genre in folded_genres):
            return False

    if query.free_terms:
        blob = track.search_blob
        genre_blob = " ".join(folded_genres)
        for term in query.free_terms:
            if term in blob or term in genre_blob:
                continue
            return False

    return True


def filter_tracks(tracks: list[Track], query: Query) -> list[Track]:
    if not query.raw:
        return []

    matched = [track for track in tracks if track_matches(track, query)]
    matched.sort(key=lambda track: (fold(track.artist), fold(track.album), fold(track.title)))
    return matched


def count_genres(tracks: list[Track]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for track in tracks:
        unique: set[str] = set()
        for genre in track.genres:
            key = fold(genre)
            if not key or key in JUNK_GENRES or key in unique:
                continue
            unique.add(key)
            counter[genre] += 1
    return counter


def top_genres(tracks: list[Track], *, limit: int, min_tracks: int) -> list[tuple[str, int]]:
    if limit <= 0 or min_tracks <= 0:
        return []

    counter = count_genres(tracks)
    ranked = [(genre, count) for genre, count in counter.most_common() if count >= min_tracks]
    return ranked[:limit]


def tracks_for_genre(tracks: list[Track], genre: str) -> list[Track]:
    target = fold(genre)
    matched = [track for track in tracks if any(fold(g) == target for g in track.genres)]
    matched.sort(key=lambda track: (fold(track.artist), fold(track.album), fold(track.title)))
    return matched


def safe_filename(name: str) -> str:
    cleaned = SAFE_FILENAME_RE.sub(" ", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "playlist"


def render_m3u(tracks: list[Track], playlist_path: Path) -> str:
    lines = ["#EXTM3U"]
    base = playlist_path.parent
    for track in tracks:
        artist = track.artist or "Unknown Artist"
        title = track.title or track.path.stem
        lines.append(f"#EXTINF:-1,{artist} - {title}")
        try:
            relative = os.path.relpath(track.path, start=base)
        except ValueError:
            relative = str(track.path)
        lines.append(relative)
    return "\n".join(lines) + "\n"


def write_playlist(
    name: str,
    tracks: list[Track],
    *,
    playlists_dir: Path,
    prefix: str = "",
) -> PlaylistResult:
    playlists_dir.mkdir(parents=True, exist_ok=True)
    filename = safe_filename(f"{prefix}{name}") + ".m3u"
    playlist_path = playlists_dir / filename
    playlist_path.write_text(render_m3u(tracks, playlist_path), encoding="utf-8")
    return PlaylistResult(name=name, path=playlist_path, track_count=len(tracks))


def rebuild_auto_playlists(
    tracks: list[Track],
    *,
    playlists_dir: Path,
    limit: int,
    min_tracks: int,
    prefix: str = "Auto - ",
    day: date | None = None,
) -> AutoPlaylistReport:
    report = AutoPlaylistReport()

    if playlists_dir.exists():
        for existing in playlists_dir.glob(f"{prefix}*.m3u"):
            existing.unlink()
            report.removed.append(existing)

    if not tracks or limit <= 0 or min_tracks <= 0:
        return report

    for genre, _count in top_genres(tracks, limit=limit, min_tracks=min_tracks):
        genre_tracks = tracks_for_genre(tracks, genre)
        if len(genre_tracks) < min_tracks:
            continue
        random.Random(f"{day or date.today()}:{fold(genre)}").shuffle(genre_tracks)
        result = write_playlist(genre, genre_tracks, playlists_dir=playlists_dir, prefix=prefix)
        report.written.append(result)

    return report
