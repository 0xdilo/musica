"""Keep external discovery tied to a genre, favourites or a specific playlist."""

from __future__ import annotations

import json
import logging
import random
import re
import time
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from musica_sync.curation import load_library
from musica_sync.discovery import DiscoveryError, credentials
from musica_sync.navidrome import Subsonic, navidrome_url
from musica_sync.new_music import (
    database,
    identity,
    key_for,
    lastfm,
    owned_tracks,
    public_track,
    valid_text,
)
from musica_sync.playlists import fold
from musica_sync.preferences import read_preferences

LOGGER = logging.getLogger(__name__)


def parse_context(value: object) -> tuple[str, str]:
    if not isinstance(value, dict):
        raise DiscoveryError(400, "Scegli un punto di partenza valido per la radio.")
    mode, target = value.get("mode", "personal"), value.get("value", "")
    if not isinstance(mode, str) or mode not in {"personal", "favourites", "genre", "playlist"}:
        raise DiscoveryError(400, "Tipo di radio non valido.")
    if not isinstance(target, str) or len(target) > 120 or re.search(r"[\x00-\x1f]", target):
        raise DiscoveryError(400, "Genere o playlist non validi.")
    target = target.strip()
    if mode == "playlist" and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", target):
        raise DiscoveryError(400, "Playlist non valida.")
    if mode == "genre" and len(target) < 2:
        raise DiscoveryError(400, "Scrivi un genere, ad esempio punk.")
    if mode in {"personal", "favourites"} and target:
        raise DiscoveryError(400, "Questa radio non richiede un genere o una playlist.")
    return mode, fold(target) if mode == "genre" else target


def playlist_info(username: str, playlist_id: str) -> dict:
    config = credentials()
    client = Subsonic(navidrome_url(), username, config["navidrome_password"])
    try:
        return client.call("getPlaylist", [("id", playlist_id)])["playlist"]
    except RuntimeError:
        raise DiscoveryError(404, "Playlist non disponibile per questo account.") from None


def radio_options(username: str) -> dict:
    config = credentials()
    client = Subsonic(navidrome_url(), username, config["navidrome_password"])
    playlists = client.call("getPlaylists").get("playlists", {}).get("playlist", [])
    items = [{"id": p["id"], "name": p["name"], "count": p.get("songCount", 0)} for p in playlists]
    items.sort(key=lambda p: (p["name"].startswith(("Generi /", "Per te /")), p["name"].casefold()))
    return {"playlists": items}


def seed_tracks(username: str, mode: str, value: str) -> tuple[list[dict], str, str]:
    if mode == "playlist":
        playlist = playlist_info(username, value)
        label = playlist["name"]
        entries = playlist.get("entry", [])
        tracks = [
            {"artist": t.get("artist", ""), "title": t.get("title", "")}
            for t in entries
            if valid_text(t.get("artist")) and valid_text(t.get("title"))
        ]
        signature = ",".join(t["id"] for t in entries)
    else:
        songs = [s for s in load_library(username).values() if s.favourite]
        label = "Preferiti"
        tracks = [{"artist": s.artist, "title": s.title} for s in songs]
        signature = ",".join(sorted(s.id for s in songs))
    if not tracks:
        raise DiscoveryError(
            422, f"{label}: aggiungi almeno un brano per avviare una radio simile."
        )
    rng = random.Random(f"{date.today()}:{mode}:{value}")
    rng.shuffle(tracks)
    by_artist = {}
    for track in tracks:
        by_artist.setdefault(fold(track["artist"]), track)
    return list(by_artist.values())[:12], label, signature


def similar_seed(seed: dict) -> tuple[list[dict], str]:
    reason = f"Simile a {seed['title']} · {seed['artist']}"
    try:
        response = lastfm(
            "track.getSimilar",
            artist=seed["artist"],
            track=seed["title"],
            limit="60",
            autocorrect="1",
        )
    except RuntimeError as exc:
        LOGGER.warning("Similar-track lookup failed for %s: %s", reason, exc)
        return [], reason
    return response.get("similartracks", {}).get("track", []), reason


def fetch_candidates(mode: str, value: str, seeds: list[dict]) -> list[dict]:
    if mode == "genre":
        aliases = {"elettronica": "electronic", "post punk": "post-punk", "hip hop": "hip-hop"}
        response = lastfm("tag.getTopTracks", tag=aliases.get(value, value), limit="200")
        batches = [(response.get("tracks", {}).get("track", []), f"Nuova musica · {value}")]
    else:
        with ThreadPoolExecutor(max_workers=3) as executor:
            batches = list(executor.map(similar_seed, seeds))
    found = {}
    for tracks, reason in batches:
        for rank, entry in enumerate(tracks):
            artist = entry.get("artist", {}).get("name", "")
            title = entry.get("name", "")
            if not valid_text(artist) or not valid_text(title):
                continue
            key = key_for(artist, title)
            score = float(entry.get("match", 1 / (rank + 1)))
            if key not in found or score > found[key]["score"]:
                found[key] = {
                    "id": key,
                    "artist": artist,
                    "title": title,
                    "score": score,
                    "reason": reason,
                }
    return list(found.values())


def scoped_candidates(
    username: str, mode: str, value: str, exclude: set[str]
) -> tuple[list[dict], str]:
    seeds, label, signature = (
        ([], value, "") if mode == "genre" else seed_tracks(username, mode, value)
    )
    scope = json.dumps([mode, value, signature], ensure_ascii=False)
    with database() as db:
        current = db.execute(
            "SELECT updated FROM discovery_contexts WHERE owner=? AND scope=?", (username, scope)
        ).fetchone()
    if not current or time.time() - current[0] > 21600:
        try:
            candidates = fetch_candidates(mode, value, seeds)
        except RuntimeError:
            raise DiscoveryError(
                503, f"Radio {label} temporaneamente non disponibile. Riprova."
            ) from None
        if not candidates:
            instruction = (
                "Prova un altro nome di genere."
                if mode == "genre"
                else "Prova ad aggiungere altri brani alla raccolta."
            )
            raise DiscoveryError(
                422,
                f"Non ho trovato consigli per {label}. {instruction}",
            )
        with database() as db:
            db.execute("DELETE FROM discovery_pools WHERE owner=? AND scope=?", (username, scope))
            for track in candidates:
                db.execute(
                    "INSERT INTO recommendations"
                    "(id,owner,artist,title,reason,score,updated,personal) "
                    "VALUES (?,?,?,?,?,?,?,0) ON CONFLICT(id,owner) DO NOTHING",
                    (
                        track["id"],
                        username,
                        track["artist"],
                        track["title"],
                        track["reason"],
                        track["score"],
                        time.time(),
                    ),
                )
                db.execute(
                    "INSERT INTO discovery_pools VALUES (?,?,?,?,?,?)",
                    (username, scope, track["id"], track["score"], track["reason"], time.time()),
                )
            db.execute(
                "INSERT INTO discovery_contexts VALUES (?,?,?,?) ON CONFLICT(owner,scope) "
                "DO UPDATE SET label=excluded.label,updated=excluded.updated",
                (username, scope, label, time.time()),
            )
    owned, preferences = owned_tracks(), read_preferences()["artists"]
    with database() as db:
        rows = db.execute(
            "SELECT r.id,r.artist,r.title,p.reason,j.status,j.error,j.song_id "
            "FROM discovery_pools p JOIN recommendations r ON r.id=p.id AND r.owner=p.owner "
            "LEFT JOIN jobs j ON r.id=j.id AND r.owner=j.owner "
            "WHERE p.owner=? AND p.scope=? AND r.dismissed=0 ORDER BY p.score DESC,p.id",
            (username, scope),
        ).fetchall()
    groups = defaultdict(deque)
    for row in rows:
        if (
            row["id"] not in exclude
            and identity(row["artist"], row["title"]) not in owned
            and fold(row["artist"]) not in preferences
        ):
            groups[row["reason"]].append(row)
    selected, artists = [], Counter()
    while groups and len(selected) < 40:
        for reason in list(groups):
            row = groups[reason].popleft()
            if artists[fold(row["artist"])] < 3:
                selected.append(public_track(row))
                artists[fold(row["artist"])] += 1
            if not groups[reason]:
                del groups[reason]
    if not selected:
        raise DiscoveryError(
            422, f"Non restano brani nuovi per {label}. Scegli un altro punto di partenza."
        )
    return selected, label
