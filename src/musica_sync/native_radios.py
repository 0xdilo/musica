"""Buffered discovery stations exposed as continuous MP3/ICY streams for native players."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from musica_sync import new_music, radio_relay
from musica_sync.discovery import DiscoveryError, credentials
from musica_sync.navidrome import ROOT, Subsonic, validate_local_url
from musica_sync.radio_sources import parse_context, playlist_info

LOGGER = logging.getLogger(__name__)
META_INTERVAL = 16384
BYTES_PER_SECOND = 20000
STREAM_SLOTS = threading.BoundedSemaphore(4)


def station_id(context: dict) -> str:
    mode, value = parse_context(context)
    return hashlib.sha256(json.dumps([mode, value]).encode()).hexdigest()[:16]


def stream_path(identifier: str) -> str:
    token = radio_relay.signature("discovery" + identifier, radio_relay.relay_key(create=True))
    return f"/discovery-radio/{identifier}/{token}"


def parse_path(path: str) -> str | None:
    match = re.fullmatch(r"/discovery-radio/([a-f0-9]{16})/([a-f0-9]{32})", path)
    if not match:
        return None
    identifier, token = match.groups()
    expected = radio_relay.signature("discovery" + identifier, radio_relay.relay_key())
    return identifier if hmac.compare_digest(token, expected) else None


def metadata_block(track: dict) -> bytes:
    title = f"{track['artist']} - {track['title']}"
    title = title.replace("'", "’").replace(";", ",").replace("\n", " ").replace("\r", " ")
    payload = ("StreamTitle='" + title + "';").encode()[:4080]
    payload = payload.decode(errors="ignore").encode()
    blocks = (len(payload) + 15) // 16
    return bytes([blocks]) + payload.ljust(blocks * 16, b"\0")


def prepare_track(username: str, context: dict, history: list[str], last_artist: str, cache: Path):
    request = {"context": context, "exclude": history[-80:], "last_artist": last_artist}
    try:
        result = new_music.radio_next(username, request)
    except DiscoveryError as exc:
        if exc.status != 422 or len(history) < 2:
            raise
        # Small recommendation pools may not contain 80 distinct tracks. Recycle
        # older offers while still excluding the immediately preceding recording.
        result = new_music.radio_next(username, {**request, "exclude": history[-1:]})
    source = new_music.preview_path(result["audio_url"])
    if source is None:
        raise RuntimeError("L'anteprima del brano non è più disponibile.")
    track = result["track"]
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / (track["id"] + ".mp3")
    if not target.is_file():
        temporary = target.with_suffix(".tmp.mp3")
        new_music.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-i",
                str(source),
                "-vn",
                "-map_metadata",
                "-1",
                "-codec:a",
                "libmp3lame",
                "-ar",
                "44100",
                "-ac",
                "2",
                "-b:a",
                "160k",
                "-write_xing",
                "0",
                "-id3v2_version",
                "0",
                str(temporary),
            ],
            timeout=180,
        )
        temporary.replace(target)
    return {"id": track["id"], "artist": track["artist"], "title": track["title"]}


@dataclass
class Station:
    id: str
    name: str
    context: dict
    queue: deque = field(default_factory=deque)
    history: deque = field(default_factory=lambda: deque(maxlen=80))
    last_artist: str = ""
    error: str = ""
    retry_at: float = 0
    listeners: int = 0
    published: bool = False


class RadioEngine:
    """One preparation worker and a shared durable buffer, independent of the phone's JS."""

    def __init__(self, username: str, base: str, directory: Path | None = None):
        self.username = username
        self.base = validate_local_url(base)
        self.directory = directory or ROOT / "state/native-radios"
        self.cache = self.directory / "audio"
        self.lock = threading.Condition()
        self.publication_lock = threading.Lock()
        self.stop = threading.Event()
        self.stations: dict[str, Station] = {}
        path = self.directory / "stations.json"
        if path.exists():
            for entry in json.loads(path.read_text())["stations"]:
                mode, value = parse_context(entry["context"])
                identifier = station_id({"mode": mode, "value": value})
                if identifier != entry["id"] or not new_music.valid_text(entry["name"]):
                    raise ValueError("Invalid native radio configuration: " + str(path))
                queue = deque(
                    t
                    for t in entry.get("queue", [])
                    if re.fullmatch(r"[a-f0-9]{32}", t["id"])
                    and (self.cache / (t["id"] + ".mp3")).is_file()
                )
                self.stations[identifier] = Station(
                    identifier,
                    entry["name"],
                    {"mode": mode, "value": value},
                    queue,
                    deque(entry.get("history", [])[-80:], maxlen=80),
                    entry.get("last_artist", ""),
                    published=entry.get("published", False),
                )

    def persist(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        entries = [
            {
                "id": s.id,
                "name": s.name,
                "context": s.context,
                "queue": list(s.queue),
                "history": list(s.history),
                "last_artist": s.last_artist,
                "published": s.published,
            }
            for s in self.stations.values()
        ]
        path = self.directory / "stations.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"stations": entries}, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(path)

    def add(self, data: dict) -> dict:
        mode, value = parse_context(data.get("context"))
        label = {
            "personal": "Per te",
            "favourites": "Dai tuoi preferiti",
            "genre": value.title(),
        }.get(mode, "")
        if mode == "playlist":
            label = playlist_info(self.username, value)["name"]
        name = data.get("name", "Scopri · " + label)
        if not new_music.valid_text(name) or len(name) > 100:
            raise DiscoveryError(400, "Dai alla radio un nome di massimo 100 caratteri.")
        context = {"mode": mode, "value": value}
        identifier = station_id(context)
        with self.lock:
            if identifier not in self.stations:
                if len(self.stations) >= 16:
                    raise DiscoveryError(409, "Sono già configurate 16 radio personalizzate.")
                self.stations[identifier] = Station(identifier, name.strip(), context)
                self.persist()
                self.lock.notify_all()
            return self.describe(self.stations[identifier])

    def describe(self, station: Station) -> dict:
        return {
            "id": station.id,
            "name": station.name,
            "context": station.context,
            "status": "ready" if station.queue else "failed" if station.error else "preparing",
            "buffered": len(station.queue),
            "error": station.error,
            "published": station.published,
        }

    def overview(self) -> list[dict]:
        with self.lock:
            return [self.describe(s) for s in self.stations.values()]

    def publish(self, station: Station) -> None:
        with self.publication_lock:
            with self.lock:
                if self.stations.get(station.id) is not station:
                    return
            account = credentials()
            client = Subsonic(
                os.environ.get("NAVIDROME_URL", "http://127.0.0.1:4533"),
                self.username,
                account["navidrome_password"],
            )
            home = self.base + "/#radio=" + station.id
            stations = client.call("getInternetRadioStations").get("internetRadioStations", {})
            previous = next(
                (
                    s
                    for s in stations.get("internetRadioStation", [])
                    if s.get("homePageUrl") == home
                ),
                None,
            )
            values = [
                ("name", station.name),
                ("streamUrl", self.base + stream_path(station.id)),
                ("homepageUrl", home),
            ]
            if previous:
                client.call("updateInternetRadioStation", [("id", previous["id"]), *values])
            else:
                client.call("createInternetRadioStation", values)
            with self.lock:
                station.published = True
                station.error = ""
                station.retry_at = 0
                self.persist()

    def remove(self, identifier: str) -> None:
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{16}", identifier):
            raise DiscoveryError(400, "Radio non valida.")
        with self.publication_lock:
            with self.lock:
                if identifier not in self.stations:
                    raise DiscoveryError(404, "Radio non trovata.")
            account = credentials()
            client = Subsonic(
                os.environ.get("NAVIDROME_URL", "http://127.0.0.1:4533"),
                self.username,
                account["navidrome_password"],
            )
            home = self.base + "/#radio=" + identifier
            stations = client.call("getInternetRadioStations").get("internetRadioStations", {})
            previous = next(
                (
                    s
                    for s in stations.get("internetRadioStation", [])
                    if s.get("homePageUrl") == home
                ),
                None,
            )
            if previous:
                client.call("deleteInternetRadioStation", [("id", previous["id"])])
            with self.lock:
                del self.stations[identifier]
                self.persist()
                self.lock.notify_all()

    def fill_one(self) -> bool:
        with self.lock:
            eligible = [
                s
                for s in self.stations.values()
                if (len(s.queue) < 2 or (s.queue and not s.published)) and s.retry_at < time.time()
            ]
            if not eligible:
                return False
            eligible.sort(key=lambda s: (-s.listeners, len(s.queue)))
            station = eligible[0]
            history = list(station.history) + [t["id"] for t in station.queue]
            last_artist = station.queue[-1]["artist"] if station.queue else station.last_artist
        try:
            if station.queue and not station.published:
                self.publish(station)
                return True
            track = prepare_track(self.username, station.context, history, last_artist, self.cache)
            with self.lock:
                if self.stations.get(station.id) is not station:
                    return True
                station.queue.append(track)
                station.error = ""
                self.persist()
                self.lock.notify_all()
            if not station.published:
                self.publish(station)
            self.cleanup_cache()
            LOGGER.info("Discovery radio buffered: %s · %s", station.name, track["title"])
        except (
            OSError,
            RuntimeError,
            ValueError,
            DiscoveryError,
            sqlite3.Error,
            subprocess.SubprocessError,
        ):
            LOGGER.exception("Could not prepare discovery radio %s", station.name)
            with self.lock:
                station.error = "Preparazione non riuscita. Riprovo automaticamente tra un minuto."
                station.retry_at = time.time() + 60
                self.lock.notify_all()
        return True

    def cleanup_cache(self) -> None:
        files = sorted(self.cache.glob("*.mp3"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        with self.lock:
            protected = {t["id"] for s in self.stations.values() for t in s.queue}
        for path in files:
            if path.stat().st_mtime > time.time() - 3600:
                continue
            if path.stem in protected:
                continue
            if total > 512 * 1024 * 1024 or path.stat().st_mtime < time.time() - 86400:
                total -= path.stat().st_size
                path.unlink()

    def run(self) -> None:
        while not self.stop.is_set():
            if not self.fill_one():
                self.stop.wait(2)
        with self.lock:
            self.lock.notify_all()

    def take(self, identifier: str, timeout: float = 20) -> dict:
        deadline = time.monotonic() + timeout
        with self.lock:
            station = self.stations.get(identifier)
            if station is None:
                raise DiscoveryError(404, "Radio non trovata.")
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self.stop.is_set():
                    raise DiscoveryError(503, "La radio sta preparando il prossimo brano. Riprova.")
                if not station.queue:
                    self.lock.wait(min(remaining, 2))
                    continue
                track = station.queue.popleft()
                station.history.append(track["id"])
                station.last_artist = track["artist"]
                self.persist()
                self.lock.notify_all()
                # A buffered recommendation may have been saved since preparation.
                owned = new_music.owned_tracks()
                if new_music.identity(track["artist"], track["title"]) not in owned:
                    (self.cache / (track["id"] + ".mp3")).touch()
                    return track

    def chunks(self, identifier: str, first: dict):
        remaining = META_INTERVAL
        track = first
        while not self.stop.is_set():
            with (self.cache / (track["id"] + ".mp3")).open("rb") as audio:
                while chunk := audio.read(min(4096, remaining)):
                    yield chunk, False
                    remaining -= len(chunk)
                    if remaining == 0:
                        yield metadata_block(track), True
                        remaining = META_INTERVAL
            track = self.take(identifier)


def serve_stream(engine: RadioEngine, handler, *, send_body: bool) -> None:
    identifier = parse_path(urlsplit(handler.path).path)
    if identifier not in engine.stations:
        handler.send_error(404, "Radio not found")
        return
    if not STREAM_SLOTS.acquire(blocking=False):
        handler.send_error(503, "Radio connection limit reached")
        return
    station = engine.stations[identifier]
    started = False
    with engine.lock:
        station.listeners += 1
    try:
        first = engine.take(identifier) if send_body else None
        handler.send_response(200)
        handler.send_header("Content-Type", "audio/mpeg")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Accept-Ranges", "none")
        handler.send_header("icy-name", station.name.encode("ascii", errors="replace").decode())
        handler.send_header("icy-br", "160")
        handler.send_header("icy-pub", "0")
        wants_metadata = handler.headers.get("Icy-MetaData") == "1"
        if wants_metadata:
            handler.send_header("icy-metaint", str(META_INTERVAL))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.close_connection = True
        started = True
        if send_body:
            start, sent = time.monotonic(), 0
            for chunk, is_metadata in engine.chunks(identifier, first):
                if is_metadata and not wants_metadata:
                    continue
                handler.wfile.write(chunk)
                handler.wfile.flush()
                if not is_metadata:
                    sent += len(chunk)
                    delay = sent / BYTES_PER_SECOND - (time.monotonic() - start) - 2
                    if delay > 0 and engine.stop.wait(delay):
                        break
    except (BrokenPipeError, ConnectionResetError):
        LOGGER.info("Discovery radio listener disconnected")
    except (OSError, DiscoveryError) as exc:
        LOGGER.warning("Discovery stream interrupted: %s", exc)
        if not started:
            handler.send_error(getattr(exc, "status", 503), "Radio is preparing. Retry shortly.")
    finally:
        with engine.lock:
            station.listeners -= 1
        STREAM_SLOTS.release()
