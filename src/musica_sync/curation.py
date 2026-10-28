"""Build rotating genre playlists from tags, sonic relevance and listening preferences."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import math
import random
import re
import sqlite3
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from musica_sync.navidrome import (
    ROOT,
    Subsonic,
    audiomuse_url,
    navidrome_url,
    request_json,
    upload_image,
)
from musica_sync.playlist_art import artwork
from musica_sync.playlists import JUNK_GENRES, fold
from musica_sync.preferences import read_preferences

LOGGER = logging.getLogger(__name__)
ALIASES = {
    "rap": "Hip Hop",
    "hip-hop": "Hip Hop",
    "hip hop": "Hip Hop",
    "hiphop": "Hip Hop",
    "electronic": "Electronic",
    "electronica": "Electronic",
    "electronic music": "Electronic",
    "idm": "IDM",
    "drum and bass": "Drum & Bass",
    "drum & bass": "Drum & Bass",
}
WHOLE_RELEASE = re.compile(
    r"\b(?:full album|full ep|full session|playlist|continuous mix|dj mix)\b", re.I
)
VIDEO_LABEL = re.compile(
    r"\([^)]*(?:official|lyrics?|audio|video)[^)]*\)|\[[^]]*(?:official|lyrics?|audio|video)[^]]*\]",
    re.I,
)


@dataclass(frozen=True)
class Song:
    id: str
    path: Path
    artist: str
    title: str
    album: str
    genres: tuple[str, ...]
    duration: float
    recording_id: str = ""
    favourite: bool = False
    bit_rate: int = 0
    play_count: int = 0
    last_played: str | None = None
    rating: int = 0
    codec: str = ""
    fingerprint: str = ""

    @property
    def artist_key(self) -> str:
        return fold(re.split(r",|\s+(?:feat\.?|ft\.?)\s+", self.artist, maxsplit=1, flags=re.I)[0])

    @property
    def title_identity(self) -> str:
        title = re.sub(r"\W+", " ", fold(VIDEO_LABEL.sub("", self.title))).strip()
        return self.artist_key + ":" + title

    @property
    def identity(self) -> str:
        return self.fingerprint or self.recording_id or self.title_identity


def canonical_genre(value: str) -> str:
    return ALIASES.get(fold(value), value.strip())


def eligible(song: Song) -> bool:
    return (
        song.path.is_file()
        and 60 <= song.duration <= 900
        and bool(song.artist_key)
        and not WHOLE_RELEASE.search(song.title)
        and not (
            song.duration <= 120
            and re.match(r"^(?:intro|outro|skit|interlude)\b", song.title, re.I)
        )
    )


def load_library(
    username: str, *, include_excluded: bool = False, include_damaged: bool = False
) -> dict[str, Song]:
    database = ROOT / "data/navidrome/navidrome.db"
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """SELECT m.*,coalesce(a.starred,0) AS favourite,
                      coalesce(a.play_count,0) AS user_plays,a.play_date AS last_played,
                      coalesce(a.rating,0) AS user_rating FROM media_file m
               LEFT JOIN user u ON u.user_name=?
               LEFT JOIN annotation a ON a.item_id=m.id AND a.user_id=u.id
                 AND a.item_type='media_file' WHERE m.missing=0""",
            (username,),
        ).fetchall()
    result = {}
    preferences = read_preferences()
    groups_path = ROOT / "state/library/fingerprint-groups.json"
    fingerprints = {}
    damaged = set()
    if groups_path.exists():
        quality = json.loads(groups_path.read_text())
        for entry in [*quality["entries"], *quality["errors"]]:
            path = Path(entry["path"])
            if not path.is_file():
                continue
            stat = path.stat()
            # Replacements are eligible again; a stale failure must not ban new audio.
            if stat.st_size != entry["size"] or stat.st_mtime_ns != entry["mtime_ns"]:
                continue
            if "group" in entry:
                fingerprints[entry["id"]] = entry["group"]
            else:
                damaged.add(entry["id"])
    for row in rows:
        song = Song(
            id=row["id"],
            path=ROOT / "music/library" / row["path"],
            artist=row["album_artist"] or row["artist"],
            title=row["title"],
            album=row["album"],
            duration=row["duration"],
            recording_id=row["mbz_recording_id"] or "",
            favourite=bool(row["favourite"]),
            bit_rate=row["bit_rate"],
            play_count=row["user_plays"],
            last_played=row["last_played"],
            rating=row["user_rating"],
            codec=row["codec"] or row["suffix"],
            fingerprint=fingerprints.get(row["id"], ""),
            genres=tuple(
                dict.fromkeys(
                    canonical_genre(g["value"])
                    for g in json.loads(row["tags"]).get("genre", [])
                    if fold(g["value"]) not in JUNK_GENRES
                )
            ),
        )
        excluded = (
            song.rating == 1
            or song.id in preferences["tracks"]
            or song.artist_key in preferences["artists"]
        )
        if (
            eligible(song)
            and (include_damaged or song.id not in damaged)
            and (include_excluded or not excluded)
        ):
            result[song.id] = song
    return result


def unique_recordings(songs: list[Song]) -> list[Song]:
    # Connect duplicates even when only one copy has a MusicBrainz identifier.
    parents = {s.id: s.id for s in songs}

    def root(key: str) -> str:
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    seen: dict[tuple[str, str], str] = {}
    for song in songs:
        keys = [("title", song.title_identity)]
        keys += [("recording", song.recording_id)] if song.recording_id else []
        keys += [("fingerprint", song.fingerprint)] if song.fingerprint else []
        for key in keys:
            if key in seen:
                parents[root(song.id)] = root(seen[key])
            seen[key] = song.id
    groups: dict[str, list[Song]] = defaultdict(list)
    for song in songs:
        groups[root(song.id)].append(song)
    result = []
    for group in groups.values():
        best = sorted(
            group,
            key=lambda s: (
                not s.favourite,
                -s.play_count,
                s.codec.casefold() not in {"flac", "alac", "wav", "pcm"},
                s.id,
            ),
        )[0]
        comparable = [
            s
            for s in group
            if s.favourite == best.favourite
            and s.play_count == best.play_count
            and s.codec.casefold() == best.codec.casefold()
        ]
        result.append(max(comparable, key=lambda s: (s.bit_rate, s.id)))
    return result


def played_age(song: Song, now: datetime | None = None) -> float:
    if not song.last_played:
        return math.inf
    parsed = datetime.fromisoformat(song.last_played.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0, ((now or datetime.now(UTC)) - parsed).total_seconds() / 86400)


def select_tracks(
    songs: list[Song],
    scores: dict[str, float],
    *,
    seed: str,
    count: int = 30,
    previous_ids: set[str] | None = None,
    now: datetime | None = None,
) -> list[Song]:
    candidates = unique_recordings(songs)
    rng = random.Random(seed)
    ranked = sorted(candidates, key=lambda s: (-scores.get(s.id, -math.inf), s.id))
    priorities = {
        song.id: 0.65 * (1 - index / max(1, len(ranked) - 1))
        + 0.25 * rng.random()
        + 0.1 * song.favourite
        - (0.65 if played_age(song, now) < 1 else 0.3 if played_age(song, now) < 7 else 0)
        - (0.3 if previous_ids and song.id in previous_ids else 0)
        + 0.08 / (1 + song.play_count)
        for index, song in enumerate(ranked)
    }
    artists: Counter[str] = Counter()
    albums: Counter[tuple[str, str]] = Counter()
    selected: list[Song] = []
    fresh = {
        s.id
        for s in candidates
        if previous_ids and s.id not in previous_ids and played_age(s, now) >= 7
    }
    fresh_target = min(math.ceil(min(count, len(candidates)) * 0.35), len(fresh))
    while candidates and len(selected) < count:
        allowed = [
            s
            for s in candidates
            if artists[s.artist_key] < 3
            and albums[(s.artist_key, fold(s.album))] < 2
            and (not selected or s.artist_key != selected[-1].artist_key)
        ]
        if not allowed:
            break
        still_needed = fresh_target - sum(s.id in fresh for s in selected)
        fresh_allowed = [s for s in allowed if s.id in fresh]
        if still_needed > 0 and count - len(selected) <= still_needed and fresh_allowed:
            allowed = fresh_allowed
        song = max(allowed, key=lambda s: priorities[s.id] - 0.35 * artists[s.artist_key])
        selected.append(song)
        artists[song.artist_key] += 1
        albums[(song.artist_key, fold(song.album))] += 1
        candidates.remove(song)
    return selected


def order_genre_tracks(songs: list[Song], scores: dict[str, float], *, seed: str) -> list[Song]:
    """Keep every recording, separating artists whenever the remaining pool allows it."""
    rng = random.Random(seed)
    groups: dict[str, list[Song]] = defaultdict(list)
    for song in sorted(unique_recordings(songs), key=lambda s: s.id):
        groups[song.artist_key].append(song)
    for tracks in groups.values():
        priorities = {s.id: scores.get(s.id, 0) + 0.1 * rng.random() for s in tracks}
        tracks.sort(key=lambda s: (played_age(s) < 7, -priorities[s.id], s.id))
    result: list[Song] = []
    while groups:
        alternatives = [
            artist for artist in groups if not result or artist != result[-1].artist_key
        ]
        artist = max(
            alternatives or list(groups),
            key=lambda key: (len(groups[key]), scores.get(groups[key][0].id, 0), key),
        )
        result.append(groups[artist].pop(0))
        if not groups[artist]:
            del groups[artist]
    return result


def genre_pools(library: dict[str, Song], minimum: int) -> dict[str, list[Song]]:
    groups: dict[str, list[Song]] = defaultdict(list)
    for song in library.values():
        for genre in song.genres:
            groups[genre].append(song)
    result = {}
    identities: list[set[str]] = []
    for genre, songs in sorted(groups.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        pool = unique_recordings(songs)
        ids = {s.identity for s in pool}
        if len(pool) < minimum:
            continue
        if any(len(ids & other) / len(ids | other) >= 0.85 for other in identities):
            continue
        result[genre] = pool
        identities.append(ids)
    return result


def sonic_scores(genre: str, songs: list[Song], token: str, total: int) -> dict[str, float]:
    query = f"A recording of {genre} music."
    fingerprint = hashlib.sha256(
        (query + str(total) + str(date.today()) + ",".join(sorted(s.id for s in songs))).encode()
    ).hexdigest()
    cache = ROOT / "state/curation/scores" / f"{fingerprint}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    response = request_json(
        audiomuse_url() + "/api/clap/search", {"query": query, "limit": total}, token
    )
    ids = {s.id for s in songs}
    scores = {
        r["item_id"]: float(r["similarity"])
        for r in response.get("results", [])
        if r["item_id"] in ids
    }
    if len(scores) < min(3, len(songs)):
        raise RuntimeError(f"AudioMuse returned too few analysed tracks for {genre}.")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(scores) + "\n")
    return scores


def choose_preference_seed(library: dict[str, Song], *, variation: str = "") -> Song | None:
    favourites = [s for s in library.values() if s.favourite]
    # Actual listening history is a useful fallback before the first heart.
    seeds = favourites or [s for s in library.values() if s.play_count]
    rested = [s for s in seeds if played_age(s) >= 1]
    seeds = sorted(rested or seeds, key=lambda s: s.id)
    if not seeds:
        return None
    rng = random.Random(f"{date.today()}:personal-radio:{variation}")
    return max(seeds, key=lambda s: rng.random() ** (1 / (1 + math.log1p(s.play_count))))


def similar_scores(client: Subsonic, library: dict[str, Song], seed: Song) -> dict[str, float]:
    response = client.call("getSimilarSongs2", [("id", seed.id), ("count", "150")])
    related = response.get("similarSongs2", {}).get("song", [])
    return {
        song["id"]: 1 / (rank + 2)
        for rank, song in enumerate(related)
        if song["id"] in library
        and library[song["id"]].identity != seed.identity
        and library[song["id"]].title_identity != seed.title_identity
    }


def preference_scores(client: Subsonic, library: dict[str, Song]) -> dict[str, float]:
    seed = choose_preference_seed(library)
    if seed is None:
        return {}
    favourites = [s for s in library.values() if s.favourite]
    recordings = {s.identity for s in favourites}
    titles = {s.title_identity for s in favourites}
    return {
        i: score
        for i, score in similar_scores(client, library, seed).items()
        if library[i].identity not in recordings and library[i].title_identity not in titles
    }


def build_plan(
    client: Subsonic, credentials: dict, *, limit: int = 40, minimum: int = 8, count: int = 30
) -> tuple[list[dict], dict[str, Song]]:
    if not 8 <= minimum <= count <= 100 or limit < 1:
        raise ValueError("Playlist policy requires 8 <= minimum <= count <= 100 and limit >= 1.")
    library = load_library(client.username)
    stats = request_json(audiomuse_url() + "/api/clap/stats", token=credentials["audiomuse_token"])
    if not stats["loaded"]:
        raise RuntimeError("AudioMuse index is not loaded; existing playlists remain unchanged.")
    pools = genre_pools(library, minimum)
    baseline_path = ROOT / "state/curation/baselines" / f"{date.today()}.json"
    if not baseline_path.exists():
        current = ROOT / "state/curation/catalogue.json"
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(current.read_text() if current.exists() else '{"playlists": []}\n')
    baseline = {
        p["name"]: set(p["song_ids"]) for p in json.loads(baseline_path.read_text())["playlists"]
    }

    def build(pair: tuple[str, list[Song]]) -> dict:
        genre, songs = pair
        scores = sonic_scores(genre, songs, credentials["audiomuse_token"], stats["song_count"])
        chosen = order_genre_tracks(
            songs,
            scores,
            seed=f"{date.today()}:{genre}",
        )
        return {
            "name": f"Generi / {genre}",
            "genre": genre,
            "song_ids": [s.id for s in chosen],
            "comment": f"Tutti i brani di {genre} idonei dalla tua libreria, senza doppioni. "
            "Ordine aggiornato automaticamente alternando gli artisti quando possibile.",
        }

    with ThreadPoolExecutor(max_workers=4) as executor:
        plan = [
            item for item in executor.map(build, pools.items()) if len(item["song_ids"]) >= minimum
        ][:limit]
    if not plan:
        raise RuntimeError("No coherent genre playlists met the policy; nothing will be replaced.")
    scores = preference_scores(client, library)
    related = select_tracks(
        [library[i] for i in scores],
        scores,
        seed=f"{date.today()}:favourites",
        count=count,
        previous_ids=baseline.get("Per te / Dai tuoi preferiti", set()),
    )
    if len(related) >= minimum:
        plan.append(
            {
                "name": "Per te / Dai tuoi preferiti",
                "genre": "Dai tuoi preferiti",
                "song_ids": [s.id for s in related],
                "comment": (
                    "Una radio attorno a un tuo preferito, con meno ripetizioni recenti. "
                    "Cambia ogni giorno."
                ),
            }
        )
    return plan, library


def publish(client: Subsonic, plan: list[dict]) -> list[dict]:
    manifest = ROOT / "state/curation/catalogue.json"
    previous = json.loads(manifest.read_text())["playlists"] if manifest.exists() else []
    pending = manifest.with_name("pending.json")
    recoverable = json.loads(pending.read_text())["playlists"] if pending.exists() else []
    live = client.call("getPlaylists").get("playlists", {}).get("playlist", [])
    owned = {p["id"]: p for p in live if p.get("owner") == client.username}
    managed = {p["name"]: p["id"] for p in [*previous, *recoverable] if p["id"] in owned}
    cover_hashes = {p["id"]: p.get("cover_hash") for p in previous}
    native = request_json(
        client.url + "/auth/login", {"username": client.username, "password": client.password}
    )
    # Render everything before publishing; missing dependencies must not leave partial updates.
    covers = {item["name"]: artwork(item["genre"]) for item in plan}
    manifest.parent.mkdir(parents=True, exist_ok=True)
    names = {p["name"] for p in plan}
    if any(p["name"] in names and p["id"] not in managed.values() for p in owned.values()):
        raise RuntimeError(
            "An unmanaged playlist has a generated name; resolve the collision first."
        )
    output = []
    for item in plan:
        playlist_id = managed.get(item["name"])
        if playlist_id:
            current = client.call("getPlaylist", [("id", playlist_id)])["playlist"]
            unchanged = [s["id"] for s in current.get("entry", [])] == item["song_ids"]
        else:
            unchanged = False
            if any(p["name"] == item["name"] for p in owned.values()):
                raise RuntimeError(f"Unmanaged playlist has the same name: {item['name']}")
        if not unchanged:
            values = [("playlistId", playlist_id)] if playlist_id else [("name", item["name"])]
            response = client.call(
                "createPlaylist", values + [("songId", i) for i in item["song_ids"]]
            )
            playlist_id = response["playlist"]["id"]
            managed[item["name"]] = playlist_id
            pending.write_text(
                json.dumps({"playlists": [{"name": n, "id": i} for n, i in managed.items()]}) + "\n"
            )
        client.call(
            "updatePlaylist",
            [("playlistId", playlist_id), ("public", "true"), ("comment", item["comment"])],
        )
        current = client.call("getPlaylist", [("id", playlist_id)])["playlist"]
        if [s["id"] for s in current.get("entry", [])] != item["song_ids"]:
            raise RuntimeError(f"Published playlist failed read-back verification: {item['name']}")
        cover, digest = covers[item["name"]]
        if cover_hashes.get(playlist_id) != digest:
            upload_image(client.url, native["token"], "playlist", playlist_id, cover.read_bytes())
        output.append({**item, "id": playlist_id, "cover_hash": digest})
        LOGGER.info("Verified %s: %d tracks", item["name"], len(item["song_ids"]))
    retained = {p["id"] for p in output}
    for old in previous:
        if old["id"] in owned and old["id"] not in retained:
            client.call("deletePlaylist", [("id", old["id"])])
    report = {
        "updated_at": datetime.now(UTC).isoformat(),
        "owner": client.username,
        "policy": {
            "min_seconds": 60,
            "max_seconds": 900,
            "genre_max_tracks": None,
            "genre_max_artist": None,
            "genre_max_album": None,
            "personal_max_artist": 3,
            "personal_max_album": 2,
        },
        "playlists": output,
    }
    manifest.parent.mkdir(parents=True, exist_ok=True)
    temp = manifest.with_suffix(".tmp")
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    temp.replace(manifest)
    pending.unlink(missing_ok=True)
    return output


def refresh_quality() -> None:
    from musica_sync.fingerprints import refresh as scan_fingerprints

    scan_fingerprints()


def refresh(
    *, limit: int = 40, minimum: int = 8, count: int = 30, dry_run: bool = False
) -> list[dict]:
    credentials = json.loads((ROOT / "state/sonic/credentials.json").read_text())
    client = Subsonic(
        navidrome_url(), credentials["navidrome_user"], credentials["navidrome_password"]
    )
    lock = ROOT / "state/curation/refresh.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another playlist rebuild is already running.") from None
        refresh_quality()
        plan, library = build_plan(client, credentials, limit=limit, minimum=minimum, count=count)
        for item in plan:
            LOGGER.info(
                "Prepared %s: %d tracks / %d artists",
                item["name"],
                len(item["song_ids"]),
                len({library[i].artist_key for i in item["song_ids"]}),
            )
        if dry_run:
            destination = ROOT / "state/curation/preview.json"
            destination.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
            return plan
        return publish(client, plan)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from musica_sync.cli import read_settings

    settings = read_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=settings.auto_playlist_top_genres)
    parser.add_argument("--min-tracks", type=int, default=max(8, settings.auto_playlist_min_tracks))
    parser.add_argument(
        "--tracks", type=int, default=30, help="Personal discovery size; genres are complete"
    )
    args = parser.parse_args()
    if not settings.curated_playlists or args.limit == 0 or args.min_tracks == 0:
        LOGGER.info("Curated playlist refresh is disabled in .env.")
        return
    refresh(limit=args.limit, minimum=args.min_tracks, count=args.tracks, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
