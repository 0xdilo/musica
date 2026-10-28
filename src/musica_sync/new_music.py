"""External recommendations and a durable, opt-in automatic acquisition queue."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import logging
import os
import random
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections import defaultdict, deque
from contextlib import closing, contextmanager
from datetime import date
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen

import chromaprint
from mediafile import MediaFile

from musica_sync.cli import read_settings
from musica_sync.curation import load_library
from musica_sync.discovery import DiscoveryError, credentials
from musica_sync.fingerprints import compatible_versions, match_score
from musica_sync.metadata import clean_title
from musica_sync.navidrome import ROOT, Subsonic, navidrome_url
from musica_sync.playlists import fold
from musica_sync.preferences import read_preferences

LOGGER = logging.getLogger(__name__)
ACTIVE = {"queued", "matching", "downloading", "importing"}
LASTFM_GATE = threading.Lock()
LASTFM_NEXT_CALL = 0.0


def identity(artist: str, title: str) -> str:
    def normalize(value: str) -> str:
        return re.sub(r"\W+", " ", fold(clean_title(value))).strip()

    return normalize(artist) + ":" + normalize(title)


def key_for(artist: str, title: str) -> str:
    return hashlib.sha256(identity(artist, title).encode()).hexdigest()[:32]


@contextmanager
def database():
    directory = ROOT / "state/new-music"
    directory.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(directory / "queue.sqlite", timeout=30)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(
            "CREATE TABLE IF NOT EXISTS recommendations ("
            "id TEXT, owner TEXT, artist TEXT, title TEXT, reason TEXT, "
            "score REAL, updated REAL, dismissed INTEGER DEFAULT 0, PRIMARY KEY(id,owner))"
        )
        if "personal" not in {r[1] for r in db.execute("PRAGMA table_info(recommendations)")}:
            db.execute("BEGIN IMMEDIATE")
            if "personal" not in {r[1] for r in db.execute("PRAGMA table_info(recommendations)")}:
                db.execute("ALTER TABLE recommendations ADD COLUMN personal INTEGER DEFAULT 1")
            db.commit()
        db.execute(
            "CREATE TABLE IF NOT EXISTS discovery_pools ("
            "owner TEXT,scope TEXT,id TEXT,score REAL,reason TEXT,updated REAL,"
            "PRIMARY KEY(owner,scope,id))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS discovery_contexts ("
            "owner TEXT,scope TEXT,label TEXT,updated REAL,PRIMARY KEY(owner,scope))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS jobs ("
            "id TEXT, owner TEXT, status TEXT, error TEXT DEFAULT '', "
            "video_id TEXT DEFAULT '', song_id TEXT DEFAULT '', updated REAL, "
            "PRIMARY KEY(id,owner))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS radio_offers ("
            "id TEXT, owner TEXT, offered REAL, PRIMARY KEY(id,owner))"
        )
        yield db


def lastfm(method: str, **values: str) -> dict:
    global LASTFM_NEXT_CALL

    api_key = read_settings().lastfm_api_key
    if not api_key:
        raise RuntimeError("Configura LASTFM_API_KEY per scoprire nuovi brani.")
    query = urlencode({"method": method, "api_key": api_key, "format": "json", **values})
    with LASTFM_GATE:
        time.sleep(max(0, LASTFM_NEXT_CALL - time.monotonic()))
        LASTFM_NEXT_CALL = time.monotonic() + 0.25
    try:
        with urlopen("https://ws.audioscrobbler.com/2.0/?" + query, timeout=20) as response:
            result = json.load(response)
    except (OSError, URLError, ValueError):
        raise RuntimeError("Last.fm non disponibile. Riprova tra poco.") from None
    if "error" in result:
        raise RuntimeError("Last.fm non ha restituito consigli validi. Riprova tra poco.")
    return result


def owned_tracks() -> dict[str, str]:
    connection = sqlite3.connect(f"file:{ROOT}/data/navidrome/navidrome.db?mode=ro", uri=True)
    with closing(connection) as db:
        rows = db.execute("SELECT id,artist,title,path FROM media_file WHERE missing=0").fetchall()
    return {
        identity(artist, title): song_id
        for song_id, artist, title, path in rows
        if (ROOT / "music/library" / path).is_file()
    }


def recommendations(
    username: str, *, refresh: bool = False, exclude_ids: set[str] | None = None
) -> dict:
    with database() as db:
        newest = db.execute(
            "SELECT max(updated) FROM recommendations WHERE owner=? AND personal=1", (username,)
        ).fetchone()[0]
    if refresh or not newest or time.time() - newest > 21600:
        library = load_library(username)
        seeds = [s for s in library.values() if s.favourite]
        seeds = seeds or [s for s in library.values() if s.play_count]
        rng = random.Random(f"{date.today()}:{username}")
        rng.shuffle(seeds)
        seeds = sorted(seeds, key=lambda s: -s.play_count)
        by_artist = {}
        for seed in seeds:
            by_artist.setdefault(seed.artist_key, seed)
        seeds = list(by_artist.values())[:16]
        if not seeds:
            return {"tracks": [], "message": "Metti un cuore ai brani che ami per iniziare."}
        found: dict[str, dict] = {}
        failures = 0
        for seed in seeds:
            try:
                response = lastfm(
                    "track.getSimilar",
                    artist=seed.artist,
                    track=seed.title,
                    autocorrect="1",
                    limit="40",
                )
            except RuntimeError:
                failures += 1
                continue
            for item in response.get("similartracks", {}).get("track", []):
                artist, title = item.get("artist", {}).get("name", ""), item.get("name", "")
                if not valid_text(artist) or not valid_text(title):
                    continue
                key = key_for(artist, title)
                score = float(item.get("match", 0))
                if key not in found or score > found[key]["score"]:
                    found[key] = {
                        "id": key,
                        "artist": artist,
                        "title": title,
                        "score": score,
                        "reason": f"Perché ascolti {seed.title} · {seed.artist}",
                    }
            time.sleep(0.25)
            if len(found) >= 120:
                break
        if not found and failures:
            raise DiscoveryError(503, "Consigli temporaneamente non disponibili. Riprova tra poco.")
        with database() as db:
            for item in found.values():
                db.execute(
                    "INSERT INTO recommendations(id,owner,artist,title,reason,score,updated) "
                    "VALUES (?,?,?,?,?,?,?) ON CONFLICT(id,owner) DO UPDATE SET "
                    "reason=excluded.reason,score=excluded.score,updated=excluded.updated,personal=1",
                    (
                        item["id"],
                        username,
                        item["artist"],
                        item["title"],
                        item["reason"],
                        item["score"],
                        time.time(),
                    ),
                )
    owned = owned_tracks()
    excluded = read_preferences()["artists"]
    with database() as db:
        rows = db.execute(
            "SELECT r.*,j.status,j.error,j.song_id FROM recommendations r LEFT JOIN jobs j "
            "ON r.id=j.id AND r.owner=j.owner WHERE r.owner=? AND r.dismissed=0 AND r.personal=1 "
            "ORDER BY r.score DESC,r.id",
            (username,),
        ).fetchall()
    groups = defaultdict(deque)
    for row in rows:
        groups[row["reason"]].append(row)
    ordered = []
    while groups:
        for reason in list(groups):
            ordered.append(groups[reason].popleft())
            if not groups[reason]:
                del groups[reason]
    result, artists = [], {}
    for row in ordered:
        if exclude_ids and row["id"] in exclude_ids:
            continue
        if identity(row["artist"], row["title"]) in owned or fold(row["artist"]) in excluded:
            continue
        artist_key = fold(row["artist"])
        if artists.get(artist_key, 0) >= 2:
            continue
        artists[artist_key] = artists.get(artist_key, 0) + 1
        result.append(public_track(row))
        if len(result) == 24:
            break
    return {
        "tracks": result,
        "message": "Consigli dai tuoi ascolti. Salva per scaricare in libreria.",
    }


def valid_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value.strip()) <= 240
        and not re.search(r"[\x00-\x1f]", value)
    )


def public_track(row) -> dict:
    return {
        "id": row["id"],
        "artist": row["artist"],
        "title": row["title"],
        "reason": row["reason"],
        "status": row["status"] or "new",
        "error": row["error"] or "",
        "song_id": row["song_id"] or "",
    }


def get_track(username: str, data: dict) -> dict:
    key = data.get("id", "")
    if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{32}", key):
        raise DiscoveryError(400, "Brano consigliato non valido.")
    with database() as db:
        row = db.execute(
            "SELECT * FROM recommendations WHERE id=? AND owner=?", (key, username)
        ).fetchone()
    if not row:
        raise DiscoveryError(404, "Consiglio non trovato. Aggiorna le scoperte.")
    return dict(row)


def save(username: str, data: dict) -> dict:
    track = get_track(username, data)
    existing = owned_tracks().get(identity(track["artist"], track["title"]))
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        queued = db.execute(
            "SELECT count(*) FROM jobs "
            "WHERE status IN ('queued','matching','downloading','importing')"
        ).fetchone()[0]
        current = db.execute(
            "SELECT status,song_id FROM jobs WHERE id=? AND owner=?", (track["id"], username)
        ).fetchone()
        if current and (
            current[0] in ACTIVE or (current[0] == "ready" and existing and current[1] == existing)
        ):
            return {"accepted": True}
        if queued >= 50:
            raise DiscoveryError(
                429, "Ci sono già 50 salvataggi in coda. Attendi qualche download."
            )
        db.execute(
            "INSERT INTO jobs(id,owner,status,song_id,updated) VALUES (?,?,?,?,?) "
            "ON CONFLICT(id,owner) DO UPDATE SET status=excluded.status,error='',"
            "song_id=excluded.song_id,updated=excluded.updated",
            (track["id"], username, "ready" if existing else "queued", existing or "", time.time()),
        )
    return {"accepted": True}


def dismiss(username: str, data: dict) -> dict:
    track = get_track(username, data)
    with database() as db:
        db.execute(
            "UPDATE recommendations SET dismissed=1 WHERE id=? AND owner=?", (track["id"], username)
        )
    return {"accepted": True}


def jobs(username: str) -> dict:
    with database() as db:
        rows = db.execute(
            "SELECT r.*,j.status,j.error,j.song_id FROM jobs j JOIN recommendations r "
            "ON r.id=j.id AND r.owner=j.owner WHERE j.owner=? ORDER BY j.updated DESC LIMIT 100",
            (username,),
        ).fetchall()
    return {"tracks": [public_track(row) for row in rows]}


def update_job(track: dict, status: str, *, error: str = "", song_id: str = "", video_id: str = ""):
    with database() as db:
        db.execute(
            "UPDATE jobs SET status=?,error=?,song_id=?,updated=?,"
            "video_id=CASE WHEN ?='' THEN video_id ELSE ? END WHERE id=? AND owner=?",
            (status, error, song_id, time.time(), video_id, video_id, track["id"], track["owner"]),
        )


def run(command: list[str], *, timeout: int = 180) -> str:
    result = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        # yt-dlp output may include URLs or cookies; keep raw logs local and protected.
        log = ROOT / "state/new-music/last-command-error.log"
        log.write_text(result.stderr[-16000:])
        log.chmod(0o600)
        raise RuntimeError("La fonte non è disponibile. Premi Riprova più tardi.")
    return result.stdout


def yt_command() -> list[str]:
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-config",
        "--no-warnings",
        "--socket-timeout",
        "20",
        "--retries",
        "2",
        "--no-playlist",
    ]
    settings = read_settings()
    if settings.cookies_file:
        command += ["--cookies", str(settings.cookies_file)]
    elif settings.cookies_from_browser:
        command += ["--cookies-from-browser", settings.cookies_from_browser]
    return command


def candidate_score(track: dict, candidate: dict) -> float:
    title = candidate.get("title", "")
    if not isinstance(title, str) or not compatible_versions(track["title"], title):
        return -1
    duration = candidate.get("duration")
    if not isinstance(duration, (float, int)) or not 30 <= duration <= 1200:
        return -1
    if re.search(
        r"\b(cover|karaoke|tutorial|reaction|1 hour|full album|extended loop)\b", title, re.I
    ):
        return -1
    text = set(re.findall(r"\w+", fold(title + " " + candidate.get("channel", ""))))
    for value, minimum in ((track["artist"], 0.8), (track["title"], 0.9)):
        words = set(re.findall(r"\w+", fold(value)))
        if not words or len(words & text) / len(words) < minimum:
            return -1
    expected = set(re.findall(r"\w+", fold(track["artist"] + " " + track["title"])))
    coverage = len(text & expected) / max(1, len(expected))
    if coverage < 0.85:
        return -1
    official = candidate.get("channel_is_verified") or " - Topic" in candidate.get("channel", "")
    return coverage + 0.15 * bool(official)


def resolve_video(track: dict) -> str:
    response = json.loads(
        run(
            [
                *yt_command(),
                "--flat-playlist",
                "--dump-single-json",
                "--",
                f"ytsearch8:{track['artist']} {track['title']} audio",
            ]
        )
    )
    candidates = [c for c in response.get("entries", []) if isinstance(c, dict)]
    candidates = [c for c in candidates if re.fullmatch(r"[\w-]{11}", c.get("id", ""))]
    chosen = max(candidates, key=lambda c: candidate_score(track, c), default=None)
    if chosen is None or candidate_score(track, chosen) < 0:
        raise RuntimeError("Non ho trovato una versione affidabile del brano. Riprova più tardi.")
    return chosen["id"]


def preview_secret() -> bytes:
    path = ROOT / "state/new-music/preview.key"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_bytes()
    with os.fdopen(descriptor, "wb") as handle:
        value = secrets.token_bytes(32)
        handle.write(value)
    return value


def preview_path(url: str) -> Path | None:
    match = re.fullmatch(r"/preview/([a-f0-9]{32})/(\d{10})/([a-f0-9]{64})\.mp3", url)
    if not match:
        return None
    key, expires, signature = match.groups()
    if int(expires) < time.time():
        return None
    expected = hmac.new(preview_secret(), f"{key}:{expires}".encode(), "sha256").hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None
    path = ROOT / "state/new-music/downloads" / key / "track.mp3"
    return path if path.is_file() else None


def preview(username: str, data: dict) -> dict:
    track = get_track(username, data)
    download_audio(track)
    expires = str(int(time.time()) + 3600)
    signature = hmac.new(
        preview_secret(), f"{track['id']}:{expires}".encode(), "sha256"
    ).hexdigest()
    return {"audio_url": f"/preview/{track['id']}/{expires}/{signature}.mp3"}


def radio_next(username: str, data: dict) -> dict:
    from musica_sync.radio_sources import parse_context, scoped_candidates

    mode, value = parse_context(data.get("context", {}))
    excluded = data.get("exclude", [])
    last_artist = data.get("last_artist", "")
    if (
        not isinstance(excluded, list)
        or len(excluded) > 80
        or any(not isinstance(i, str) or not re.fullmatch(r"[a-f0-9]{32}", i) for i in excluded)
        or not isinstance(last_artist, str)
        or len(last_artist) > 240
    ):
        raise DiscoveryError(400, "Richiesta radio non valida.")
    label = "Per te"
    if mode == "personal":
        candidates = recommendations(username, exclude_ids=set(excluded))["tracks"]
        if not candidates:
            candidates = recommendations(username, refresh=True, exclude_ids=set(excluded))[
                "tracks"
            ]
    else:
        candidates, label = scoped_candidates(username, mode, value, set(excluded))
    with database() as db:
        offered = {
            row[0]: row[1]
            for row in db.execute("SELECT id,offered FROM radio_offers WHERE owner=?", (username,))
        }
    candidates.sort(key=lambda t: (t["artist"] == last_artist, offered.get(t["id"], 0)))
    for track in candidates[:8]:
        with database() as db:
            db.execute(
                "INSERT INTO radio_offers VALUES (?,?,?) ON CONFLICT(id,owner) "
                "DO UPDATE SET offered=excluded.offered",
                (track["id"], username, time.time()),
            )
        try:
            resolved = preview(username, {"id": track["id"]})
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError):
            LOGGER.warning("Skipping unavailable discovery recording %s", track["id"])
            continue
        path = preview_path(resolved["audio_url"])
        if path is None:
            continue
        if fingerprint_duplicate(path):
            LOGGER.info("Skipping discovery recording already present under another title")
            continue
        return {
            "track": track,
            "audio_url": resolved["audio_url"],
            "duration": MediaFile(path).length,
            "context_label": label,
        }
    raise DiscoveryError(503, "Non riesco a preparare altri brani nuovi. Premi Riprova.")


def fingerprint_duplicate(path: Path) -> str | None:
    cache = ROOT / "state/library/fingerprints.sqlite"
    if not cache.exists():
        return None
    fp = json.loads(run(["fpcalc", "-length", "120", "-json", str(path)], timeout=60))
    decoded = list(chromaprint.decode_fingerprint(fp["fingerprint"].encode())[0])
    with closing(sqlite3.connect(cache)) as db:
        rows = db.execute(
            "SELECT path,fp,size,mtime_ns FROM fingerprint_full WHERE abs(duration-?)<=2",
            (fp["duration"],),
        ).fetchall()
    for other_path, other_fp, size, mtime_ns in rows:
        if not Path(other_path).is_file():
            continue
        stat = Path(other_path).stat()
        if stat.st_size != size or stat.st_mtime_ns != mtime_ns:
            continue
        other = list(chromaprint.decode_fingerprint(other_fp.encode())[0])
        if match_score(decoded, other) >= 0.99:
            relative = str(Path(other_path).relative_to(ROOT / "music/library"))
            connection = sqlite3.connect(
                f"file:{ROOT}/data/navidrome/navidrome.db?mode=ro", uri=True
            )
            with closing(connection) as db:
                row = db.execute(
                    "SELECT id FROM media_file WHERE path=? AND missing=0", (relative,)
                ).fetchone()
            if row:
                return row[0]
    return None


def scan_library() -> None:
    config = credentials()
    client = Subsonic(navidrome_url(), config["navidrome_user"], config["navidrome_password"])
    client.call("startScan")
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if not client.call("getScanStatus").get("scanStatus", {}).get("scanning", False):
            return
        time.sleep(0.5)
    raise RuntimeError("Navidrome scan did not finish within five minutes. Check server logs.")


def already_imported(track: dict) -> bool:
    path = ROOT / "data/beets/library.db"
    if not path.exists():
        return False
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT artist,title,path FROM items").fetchall()
    return any(
        identity(artist, title) == identity(track["artist"], track["title"])
        and Path(os.fsdecode(path)).is_file()
        for artist, title, path in rows
    )


def download_audio(track: dict, *, update_status: bool = False) -> Path:
    stage = ROOT / "state/new-music/downloads" / track["id"]
    stage.mkdir(parents=True, exist_ok=True)
    audio = stage / "track.mp3"
    with (stage / "download.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if audio.is_file() and (stage / "verified").exists():
            (stage / "verified").touch()
            return audio
        video = track.get("video_id") or resolve_video(track)
        if update_status:
            update_job(track, "downloading", video_id=video)
        run(
            [
                *yt_command(),
                "--format",
                "bestaudio/best",
                "--max-filesize",
                "100M",
                "--extract-audio",
                "--audio-format",
                "mp3",
                "--audio-quality",
                "0",
                "--embed-thumbnail",
                "--convert-thumbnails",
                "jpg",
                "--write-info-json",
                "--output",
                str(stage / "track.%(ext)s"),
                "--",
                f"https://www.youtube.com/watch?v={video}",
            ],
            timeout=600,
        )
        if not audio.is_file():
            raise RuntimeError("Il download non ha prodotto un file audio. Riprova.")
        try:
            run(["ffmpeg", "-v", "error", "-i", str(audio), "-f", "null", "-"], timeout=120)
        except RuntimeError:
            audio.rename(stage / f"invalid-{int(time.time())}.mp3")
            raise
        (stage / "verified").touch()
    return audio


def acquire(track: dict) -> str:
    existing = owned_tracks().get(identity(track["artist"], track["title"]))
    if existing:
        return existing
    if already_imported(track):
        update_job(track, "importing")
        scan_library()
        existing = owned_tracks().get(identity(track["artist"], track["title"]))
        if not existing:
            raise RuntimeError("File già importato, ma non ancora indicizzato. Premi Riprova.")
        return existing
    update_job(track, "matching")
    downloaded = download_audio(track, update_status=True)
    with downloaded.with_suffix(".info.json").open() as handle:
        video = json.load(handle)["id"]
    audio = downloaded.with_name("import.mp3")
    shutil.copy2(downloaded, audio)
    existing = fingerprint_duplicate(audio)
    if existing:
        return existing
    tags = MediaFile(audio)
    tags.artist = track["artist"]
    tags.albumartist = track["artist"]
    tags.title = track["title"]
    tags.album = "Singles"
    tags.comments = f"https://www.youtube.com/watch?v={video}"
    tags.save()
    update_job(track, "importing")
    from musica_sync.cli import BEETS_RUNTIME_CONFIG

    if not BEETS_RUNTIME_CONFIG.exists():
        raise RuntimeError("Configurazione importazione mancante. Esegui musica-sync doctor.")
    run(
        ["beet", "-c", str(BEETS_RUNTIME_CONFIG), "import", "-A", "-s", "-q", str(audio)],
        timeout=300,
    )
    scan_library()
    existing = owned_tracks().get(identity(track["artist"], track["title"]))
    if not existing:
        raise RuntimeError("File scaricato, ma non ancora indicizzato. Premi Riprova.")
    return existing


def work_once() -> bool:
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT r.*,j.video_id FROM jobs j JOIN recommendations r "
            "ON j.id=r.id AND j.owner=r.owner WHERE j.status='queued' ORDER BY j.updated LIMIT 1"
        ).fetchone()
        if not row:
            return False
        track = dict(row)
        db.execute(
            "UPDATE jobs SET status='matching' WHERE id=? AND owner=?",
            (track["id"], track["owner"]),
        )
    try:
        song_id = acquire(track)
        update_job(track, "ready", song_id=song_id)
        LOGGER.info("Saved %s · %s", track["artist"], track["title"])
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        LOGGER.exception("Automatic music import failed for %s", track["id"])
        message = (
            str(exc) if isinstance(exc, RuntimeError) else "Importazione non riuscita. Riprova."
        )
        update_job(track, "failed", error=message)
    return True


def cleanup_previews() -> None:
    with database() as db:
        active = {
            row[0]
            for row in db.execute(
                "SELECT id FROM jobs "
                "WHERE status IN ('queued','matching','downloading','importing')"
            )
        }
    directory = ROOT / "state/new-music/downloads"
    if not directory.exists():
        return
    for stage in directory.iterdir():
        if not stage.is_dir() or not re.fullmatch(r"[a-f0-9]{32}", stage.name):
            continue
        stamp = stage / "verified"
        age = time.time() - (stamp if stamp.exists() else stage).stat().st_mtime
        if stage.name not in active and age > 86400:
            with (stage / "download.lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                shutil.rmtree(stage)


def main() -> None:
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with database():
        pass
    with (ROOT / "state/new-music/worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another automatic music worker is running.") from None
        with database() as db:
            db.execute(
                "UPDATE jobs SET status='queued' "
                "WHERE status IN ('matching','downloading','importing')"
            )
        cleaned_at = 0
        while True:
            if time.time() - cleaned_at > 1800:
                cleanup_previews()
                cleaned_at = time.time()
            if not work_once():
                time.sleep(3)


if __name__ == "__main__":
    main()
