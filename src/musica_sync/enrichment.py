"""Conservative, provenance-recorded metadata repair using existing tags and Last.fm."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from beets.library import Library
from beetsplug import lastgenre
from mediafile import MediaFile

from musica_sync.metadata import clean_title
from musica_sync.navidrome import ROOT
from musica_sync.playlists import fold, split_genres

LOGGER = logging.getLogger(__name__)
VERSION = re.compile(
    r"\b(remix|live|acoustic|instrumental|strumentale|remaster\w*|nightcore|sped up|slowed)\b", re.I
)
AMBIGUOUS_ARTIST = re.compile(
    r"(?:many|multiple|several|\d+)\s+(?:different\s+)?(?:bands|artists|acts)|"
    r"(?:bands|artists).{0,70}(?:same name|this name|called)",
    re.I,
)


def identity(value: str) -> str:
    return re.sub(r"[^\w]", "", fold(value))


def query_artist(value: str) -> str:
    return (
        re.sub(
            r"\s*(?:-\s*Topic|Official(?:\s+Music)?|Music|VEVO)\s*$", "", value, flags=re.I
        ).strip()
        or value
    )


def verified_track(
    track: dict, artist: str, title: str, *, correcting_identity: bool = False
) -> bool:
    returned_artist = track.get("artist", {}).get("name", "")
    returned_title = track.get("name", "")
    listeners = int(track.get("listeners", 0) or 0)
    identified = bool(track.get("mbid"))
    if correcting_identity:
        supported = identified or (listeners >= 1000 and bool(track.get("album", {}).get("title")))
    else:
        supported = identified or listeners >= 100
    return supported and (
        SequenceMatcher(None, identity(artist), identity(returned_artist)).ratio() >= 0.92
        and SequenceMatcher(None, identity(title), identity(returned_title)).ratio() >= 0.94
    )


class LastFM:
    def __init__(self, key: str):
        if not key:
            raise ValueError("LASTFM_API_KEY is required for verified metadata enrichment.")
        self.key = key
        self.lock = threading.Lock()
        self.next_request = 0.0
        self.cache_path = ROOT / "state/improvements/lastfm.sqlite"
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.cache_path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS response (key TEXT PRIMARY KEY, value TEXT)")

    def call(self, method: str, **params: str) -> dict:
        cache_key = json.dumps([method, params], sort_keys=True)
        with sqlite3.connect(self.cache_path) as db:
            cached = db.execute("SELECT value FROM response WHERE key=?", (cache_key,)).fetchone()
        if cached:
            return json.loads(cached[0])
        with self.lock:
            delay = self.next_request - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.next_request = time.monotonic() + 0.26
        body = urlencode(
            {"method": method, "api_key": self.key, "format": "json", "autocorrect": "1", **params}
        ).encode()
        for attempt in range(3):
            try:
                with urlopen(
                    Request("https://ws.audioscrobbler.com/2.0/", data=body), timeout=25
                ) as response:
                    value = json.load(response)
                error = value.get("error")
                if error not in {None, 6}:
                    raise RuntimeError(f"Last.fm metadata API returned error {error}.")
                break
            except HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise RuntimeError(
                        f"Last.fm metadata API unavailable (HTTP {exc.code})."
                    ) from None
                time.sleep(2**attempt)
        with sqlite3.connect(self.cache_path) as db:
            db.execute(
                "INSERT OR REPLACE INTO response VALUES (?,?)", (cache_key, json.dumps(value))
            )
        return value


def genre_whitelist() -> set[str]:
    path = Path(lastgenre.__file__).parent / "genres.txt"
    return {
        fold(s.strip())
        for s in path.read_text().splitlines()
        if s.strip() and not s.startswith("#")
    }


def tags_from(data: dict, whitelist: set[str]) -> list[str]:
    tags = data.get("toptags", data.get("tags", {})).get("tag", [])
    if isinstance(tags, dict):
        tags = [tags]
    names = [tag["name"].title() for tag in tags if fold(tag["name"]) in whitelist]
    return list(split_genres(";".join(names)))[:3]


def propose(item: dict, api: LastFM, whitelist: set[str], consensus: dict) -> dict | None:
    changes = {}
    title = clean_title(item["title"])
    if title and title != item["title"]:
        changes["title"] = title
    if item["year"] and not 1900 <= item["year"] <= datetime.now().year:
        changes["year"] = 0
    evidence = []
    if not item["genres"] and item["length"] <= 900:
        artist = item["artist"]
        candidates = [(artist, title)] if artist and title else []
        alias = query_artist(artist)
        if alias != artist and identity(alias) != identity(title):
            candidates.append((alias, title))
        if item["album"] and title != item["album"] and len(title) < 90:
            candidates.append((title, clean_title(item["album"])))
            # Legacy "Artist - Track - Album" was split at the first dash.
            # Verify the shorter track candidate externally before repairing it.
            album_parts = clean_title(item["album"]).split(" - ", 1)
            if len(album_parts) == 2:
                candidates.append((title, album_parts[0]))
        matched = None
        for candidate_artist, candidate_title in candidates:
            track = api.call("track.getInfo", artist=candidate_artist, track=candidate_title).get(
                "track"
            )
            correcting = (candidate_artist, candidate_title) != (item["artist"], title)
            if track and verified_track(
                track, candidate_artist, candidate_title, correcting_identity=correcting
            ):
                matched = track
                artist = track["artist"]["name"]
                if (candidate_artist, candidate_title) != candidates[0] or artist != item["artist"]:
                    changes["artist"] = artist
                    if item["albumartist"] in {"", item["artist"]}:
                        changes["albumartist"] = artist
                if not VERSION.search(candidate_title) or VERSION.search(track["name"]):
                    changes["title"] = track["name"]
                if (
                    candidate_title != title
                    and isinstance(track.get("album"), dict)
                    and track["album"].get("title")
                ):
                    changes["album"] = track["album"]["title"]
                evidence.append("lastfm_verified_track")
                break
        genres = tags_from(matched, whitelist) if matched else []
        if not genres and artist:
            inherited = consensus.get(identity(artist), [])
            if inherited:
                genres = inherited
                evidence.append("library_artist_consensus")
            else:
                info = api.call("artist.getInfo", artist=query_artist(artist)).get("artist", {})
                listeners = int(info.get("stats", {}).get("listeners", 0) or 0)
                established = listeners >= 5000 or bool(info.get("mbid")) and listeners >= 200
                ambiguous = AMBIGUOUS_ARTIST.search(info.get("bio", {}).get("summary", ""))
                if (
                    established
                    and not ambiguous
                    and identity(info.get("name", "")) == identity(query_artist(artist))
                ):
                    genres = tags_from(info, whitelist)
                    if genres:
                        evidence.append("lastfm_verified_artist")
        if genres:
            changes["genres"] = genres
    changes = {key: value for key, value in changes.items() if value != item[key]}
    if not changes:
        return None
    return {
        "id": item["id"],
        "path": item["path"],
        "before": item,
        "changes": changes,
        "evidence": evidence or ["display_cleanup"],
    }


def build_proposals(key: str) -> list[dict]:
    library = Library(ROOT / "data/beets/library.db")
    try:
        items = [
            {
                "id": item.id,
                "path": str(Path(item.path.decode(errors="surrogateescape"))),
                "title": item.title,
                "artist": item.artist,
                "albumartist": item.albumartist,
                "album": item.album,
                "year": item.year,
                "genres": item.genres,
                "length": item.length,
            }
            for item in library.items()
            if Path(item.path.decode(errors="surrogateescape")).is_file()
        ]
    finally:
        library._close()
    artists: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        if item["genres"]:
            artists[identity(item["artist"])].append(item)
    consensus = {}
    for artist, known in artists.items():
        counts = Counter(g for item in known for g in set(item["genres"]))
        if len(known) >= 3:
            consensus[artist] = [g for g, n in counts.most_common(2) if n / len(known) >= 0.65]
    api = LastFM(key)
    whitelist = genre_whitelist()
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(lambda item: propose(item, api, whitelist, consensus), items))
    return [r for r in result if r]


def apply_proposals(proposals: list[dict]) -> int:
    library = Library(ROOT / "data/beets/library.db")
    try:
        for proposal in proposals:
            item = library.get_item(proposal["id"])
            if item is None:
                raise RuntimeError(
                    f"Beets item {proposal['id']} disappeared; rebuild the proposal."
                )
            media = MediaFile(item.path)
            for field, value in proposal["changes"].items():
                setattr(media, field, value or None if field == "year" else value)
                setattr(item, field, value)
            media.save()
            item.mtime = item.current_mtime()
            item.store()
    finally:
        library._close()
    return len(proposals)
