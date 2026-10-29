"""Small private companion for Amperfy and the Shazamma e salva shortcut."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from musica_sync import capture, native_radios, radio_relay
from musica_sync.cli import read_settings
from musica_sync.discovery import DiscoveryError, credentials
from musica_sync.navidrome import ROOT, Subsonic, validate_local_url

LOGGER = logging.getLogger(__name__)
ATTEMPTS: deque[float] = deque()
PAIR_LOCK = threading.Lock()
RADIOS: native_radios.RadioEngine | None = None
ASSETS = ROOT / "web/capture"
FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/Shazamma-e-salva.shortcut": ("Shazamma-e-salva.shortcut", "application/octet-stream"),
}


def public_url() -> str:
    value = os.environ.get("MUSICA_CAPTURE_PUBLIC_URL")
    return validate_local_url(value) if value else radio_relay.public_base()


def allow_pair() -> bool:
    with PAIR_LOCK:
        now = time.monotonic()
        while ATTEMPTS and ATTEMPTS[0] < now - 60:
            ATTEMPTS.popleft()
        if len(ATTEMPTS) >= 10:
            return False
        ATTEMPTS.append(now)
        return True


class Handler(BaseHTTPRequestHandler):
    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, format, *args):
        # Request paths can carry station signatures. Never log tokens or payloads.
        LOGGER.info("Capture HTTP %s", args[1] if len(args) > 1 else "request")

    def respond(self, status: int, body: bytes, mime: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json_response(self, status: int, body: dict) -> None:
        self.respond(status, json.dumps(body, ensure_ascii=False).encode())

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        try:
            if path.startswith("/discovery-radio/") and RADIOS is not None:
                native_radios.serve_stream(RADIOS, self, send_body=self.command != "HEAD")
            elif path.startswith("/radio/"):
                radio_relay.serve_stream(self, send_body=self.command != "HEAD")
            elif path in FILES:
                name, mime = FILES[path]
                self.respond(200, (ASSETS / name).read_bytes(), mime)
            elif path == "/api/capture/health":
                self.json_response(200, {"status": "ok"})
            elif path == "/api/radios":
                capture.authenticate(self.headers.get("Authorization", ""))
                from musica_sync.radio_sources import radio_options

                self.json_response(
                    200,
                    {
                        "stations": RADIOS.overview() if RADIOS else [],
                        "enabled": RADIOS is not None,
                        **radio_options(credentials()["navidrome_user"]),
                    },
                )
            elif path in {"/api/capture/jobs", "/api/capture/config"}:
                username = capture.authenticate(self.headers.get("Authorization", ""))
                if path.endswith("jobs"):
                    self.json_response(200, {"tracks": capture.saved_tracks(username)[:100]})
                else:
                    self.json_response(
                        200,
                        {
                            "connection": json.dumps(
                                {
                                    "url": public_url() + "/api/capture",
                                    "token": capture.device_token(),
                                }
                            ),
                            "username": username,
                            "library_url": os.environ.get("MUSICA_LIBRARY_PUBLIC_URL", ""),
                        },
                    )
            else:
                self.json_response(404, {"error": "Pagina non trovata."})
        except DiscoveryError as exc:
            self.json_response(exc.status, {"error": str(exc)})
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            LOGGER.exception("Capture request failed")
            self.json_response(
                503, {"error": "Servizio non disponibile. Controlla i log del server."}
            )

    def do_POST(self) -> None:
        self.close_connection = True
        path = urlsplit(self.path).path
        try:
            if path not in {"/api/capture", "/api/capture/pair", "/api/radios"}:
                raise DiscoveryError(404, "Richiesta non trovata.")
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                raise DiscoveryError(403, "Origine della richiesta non valida.")
            if self.headers.get("Transfer-Encoding"):
                raise DiscoveryError(400, "Invia una richiesta JSON con Content-Length.")
            if self.headers.get_content_type() != "application/json":
                raise DiscoveryError(415, "Invia titolo e artista in formato JSON.")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if not 0 < length <= 4096:
                raise DiscoveryError(400, "Richiesta troppo grande o vuota.")
            if path.endswith("pair"):
                if not allow_pair():
                    raise DiscoveryError(429, "Troppi tentativi. Attendi un minuto e riprova.")
                username = None
            else:
                username = capture.authenticate(self.headers.get("Authorization", ""))
            try:
                data = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeError):
                raise DiscoveryError(400, "Richiesta JSON non valida.") from None
            if not isinstance(data, dict):
                raise DiscoveryError(400, "Richiesta JSON non valida.")
            if username is None:
                self.json_response(200, {"token": capture.pair(data)})
            elif path == "/api/radios":
                if RADIOS is None:
                    raise DiscoveryError(503, "Le radio personalizzate non sono abilitate.")
                if "remove" in data:
                    RADIOS.remove(data["remove"])
                    self.json_response(200, {"accepted": True})
                    return
                if not read_settings().lastfm_api_key:
                    raise DiscoveryError(
                        503, "Configura LASTFM_API_KEY sul server per creare radio."
                    )
                self.json_response(202, {"station": RADIOS.add(data)})
            else:
                self.json_response(202, capture.submit(username, data))
        except DiscoveryError as exc:
            self.json_response(exc.status, {"error": str(exc)})
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            LOGGER.exception("Capture save failed")
            self.json_response(503, {"error": "Salvataggio non riuscito. Riprova tra poco."})


def playlist_worker(stop: threading.Event) -> None:
    last_synced: list[str] = []
    while not stop.is_set():
        try:
            account = credentials()
            username = account["navidrome_user"]
            tracks = capture.saved_tracks(username)
            song_ids = list(
                dict.fromkeys(t["song_id"] for t in reversed(tracks) if t["status"] == "ready")
            )
            if song_ids != last_synced:
                client = Subsonic(
                    os.environ.get("NAVIDROME_URL", "http://127.0.0.1:4533"),
                    username,
                    account["navidrome_password"],
                )
                capture.sync_playlist(username, song_ids, client)
                last_synced = song_ids
                LOGGER.info("Da Shazam playlist synchronized: %d tracks", len(song_ids))
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            LOGGER.exception("Could not update Da Shazam; retrying in 15 seconds")
        stop.wait(15)


def main() -> None:
    global RADIOS

    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    capture.device_token()
    public_url()
    if os.environ.get("MUSICA_NATIVE_RADIOS", "false").lower() == "true":
        RADIOS = native_radios.RadioEngine(credentials()["navidrome_user"], public_url())
        threading.Thread(target=RADIOS.run, daemon=True, name="native-radio-buffer").start()
    for name, _ in FILES.values():
        if not (ASSETS / name).is_file():
            raise SystemExit(f"Missing capture asset: {ASSETS / name}")
    server = ThreadingHTTPServer(
        ("127.0.0.1", int(os.environ.get("MUSICA_CAPTURE_PORT", "9181"))), Handler
    )
    server.timeout = 20
    stop = threading.Event()
    worker = threading.Thread(target=playlist_worker, args=(stop,), daemon=True)
    worker.start()
    LOGGER.info("Musica Capture listening on loopback port %d", server.server_port)
    try:
        server.serve_forever()
    finally:
        stop.set()
        if RADIOS is not None:
            RADIOS.stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
