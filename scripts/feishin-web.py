"""Serve the pinned official Feishin web assets and its supported runtime settings."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from musica_sync import new_music
from musica_sync.discovery import DiscoveryError, authenticate, feedback, overview, search
from musica_sync.radio_relay import serve_stream

ROOT = Path(__file__).resolve().parents[1]
ASSETS = Path(os.environ.get("FEISHIN_ASSETS", str(ROOT / "bin/feishin-musica")))
SEARCH_SLOTS = threading.BoundedSemaphore(2)


class MusicHTTPServer(ThreadingHTTPServer):
    # HTTP/2 at the tailnet proxy can fan out a page's chunks simultaneously.
    request_queue_size = 256


SERVER_KEYS = {
    "SERVER_URL",
    "REMOTE_URL",
    "SERVER_NAME",
    "SERVER_TYPE",
    "SERVER_LOCK",
    "LEGACY_AUTHENTICATION",
    "ANALYTICS_DISABLED",
}


def settings_script(environment: dict[str, str]) -> bytes:
    values = {
        key: value
        for key, value in environment.items()
        if key in SERVER_KEYS or key.startswith("FS_")
    }
    return (
        '"use strict";\n'
        + "\n".join(
            f"window.{key} = {json.dumps(value)};"
            for key, value in sorted(values.items())
            if key.replace("_", "").isalnum()
        )
    ).encode()


def accepts_gzip(value: str) -> bool:
    for encoding in value.lower().split(","):
        parts = [part.strip() for part in encoding.split(";")]
        if parts[0] != "gzip":
            continue
        quality = next((part[2:] for part in parts[1:] if part.startswith("q=")), "1")
        try:
            return float(quality) > 0
        except ValueError:
            return False
    return False


class Handler(SimpleHTTPRequestHandler):
    def serve_preview(self, *, send_body: bool) -> None:
        path = new_music.preview_path(urlsplit(self.path).path)
        if path is None:
            self.send_error(404, "Anteprima scaduta. Premi di nuovo Anteprima.")
            return
        with path.open("rb") as handle:
            size = path.stat().st_size
            start, end = 0, size - 1
            byte_range = self.headers.get("Range", "")
            if byte_range:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", byte_range)
                if not match or not any(match.groups()):
                    self.send_error(416, "Invalid audio range")
                    return
                left, right = match.groups()
                if left:
                    start, end = int(left), min(int(right), size - 1) if right else size - 1
                else:
                    start = max(0, size - int(right))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            self.send_response(206 if byte_range else 200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "private, no-store")
            if byte_range:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if send_body:
                handle.seek(start)
                remaining = end - start + 1
                try:
                    while remaining and (chunk := handle.read(min(65536, remaining))):
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    logging.info("Audio preview client disconnected")

    def serve_radio(self, *, send_body: bool) -> None:
        serve_stream(self, send_body=send_body)

    def send_head(self):
        path = Path(self.translate_path(self.path))
        compressed = path.with_suffix(path.suffix + ".gz")
        if (
            accepts_gzip(self.headers.get("Accept-Encoding", ""))
            and path.is_file()
            and compressed.is_file()
        ):
            handle = compressed.open("rb")
            self.send_response(200)
            self.send_header("Content-Type", self.guess_type(str(path)))
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(compressed.stat().st_size))
            self.send_header("Vary", "Accept-Encoding")
            self.end_headers()
            return handle
        return super().send_head()

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path not in {
            "/api/discovery/search",
            "/api/discovery/overview",
            "/api/discovery/feedback",
            "/api/discovery/new-music",
            "/api/discovery/save",
            "/api/discovery/saves",
            "/api/discovery/dismiss",
            "/api/discovery/preview",
            "/api/discovery/new-radio",
            "/api/discovery/radio-options",
        }:
            self.json_response(404, {"error": "Richiesta non trovata."})
            return
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            self.json_response(403, {"error": "Origine della richiesta non valida."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 0 < length <= 4096:
            self.json_response(400, {"error": "Richiesta troppo grande o vuota."})
            return
        needs_slot = path.rsplit("/", 1)[-1] in {"search", "new-music", "preview", "new-radio"}
        if needs_slot and not SEARCH_SLOTS.acquire(blocking=False):
            self.json_response(429, {"error": "Due ricerche sono già in corso. Riprova tra poco."})
            return
        try:
            user = authenticate(self.headers.get("Authorization", ""))
            try:
                data = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeError):
                raise DiscoveryError(400, "Richiesta non valida.") from None
            if not isinstance(data, dict):
                raise DiscoveryError(400, "Richiesta non valida.")
            if path.endswith("/search"):
                result = search(user, data)
            elif path.endswith("/feedback"):
                result = feedback(user, data)
            elif path.endswith("/new-music"):
                if not isinstance(data.get("refresh", False), bool):
                    raise DiscoveryError(400, "Opzione aggiornamento non valida.")
                result = new_music.recommendations(user, refresh=data.get("refresh", False))
            elif path.endswith("/save"):
                result = new_music.save(user, data)
            elif path.endswith("/saves"):
                result = new_music.jobs(user)
            elif path.endswith("/dismiss"):
                result = new_music.dismiss(user, data)
            elif path.endswith("/preview"):
                result = new_music.preview(user, data)
            elif path.endswith("/new-radio"):
                result = new_music.radio_next(user, data)
            elif path.endswith("/radio-options"):
                from musica_sync.radio_sources import radio_options

                result = radio_options(user)
            else:
                result = overview(user)
            self.json_response(200, result)
        except DiscoveryError as exc:
            self.json_response(exc.status, {"error": str(exc)})
        except (RuntimeError, OSError, ValueError, KeyError):
            logging.exception("Discovery service failed")
            self.json_response(
                503, {"error": "Ricerca musicale non disponibile. Riprova tra poco."}
            )
        finally:
            if needs_slot:
                SEARCH_SLOTS.release()

    def json_response(self, status: int, data: dict) -> None:
        content = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            # A cancelled search must not crash the server thread.
            logging.info("Discovery client disconnected")

    def do_GET(self) -> None:
        self.serve(send_body=True)

    def do_HEAD(self) -> None:
        self.serve(send_body=False)

    def serve(self, *, send_body: bool) -> None:
        if urlsplit(self.path).path.startswith("/preview/"):
            self.serve_preview(send_body=send_body)
            return
        if urlsplit(self.path).path.startswith("/radio/"):
            self.serve_radio(send_body=send_body)
            return
        if urlsplit(self.path).path == "/settings.js":
            content = settings_script(dict(os.environ))
            mime = "application/javascript; charset=utf-8"
        elif urlsplit(self.path).path == "/musica.css":
            content = (ROOT / "config/feishin.css").read_bytes()
            mime = "text/css; charset=utf-8"
        elif urlsplit(self.path).path in {"/", "/index.html"}:
            content = (
                (ASSETS / "index.html")
                .read_text()
                .replace(
                    "<head>",
                    "<head><script>"
                    + settings_script(dict(os.environ)).decode().replace("<", "\\u003c")
                    + "</script>",
                )
                .replace("</head>", '<link rel="stylesheet" href="/musica.css"></head>')
                .encode()
            )
            mime = "text/html; charset=utf-8"
        elif urlsplit(self.path).path == "/assets/manifest.webmanifest":
            manifest = json.loads((ASSETS / "assets/manifest.webmanifest").read_text())
            manifest.update(scope="/", name="Musica", short_name="Musica", theme_color="#101010")
            content = json.dumps(manifest).encode()
            mime = "application/manifest+json"
        else:
            path = Path(self.translate_path(self.path))
            if not path.exists() and not Path(urlsplit(self.path).path).suffix:
                self.path = "/index.html"
            if send_body:
                super().do_GET()
            else:
                super().do_HEAD()
            return
        if content:
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if send_body:
                self.wfile.write(content)

    def list_directory(self, path: str) -> None:
        self.send_error(404, "Not found")

    def log_message(self, format: str, *args: object) -> None:
        # Request URLs may contain stream authentication parameters; log status only.
        if len(args) > 1 and str(args[1]).startswith(("4", "5")):
            logging.warning("Feishin HTTP %s", args[1])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not (ASSETS / "index.html").is_file():
        raise SystemExit(f"Official Feishin assets are missing from {ASSETS}.")
    port = int(os.environ.get("FEISHIN_PORT", "9180"))
    server = MusicHTTPServer(("127.0.0.1", port), partial(Handler, directory=str(ASSETS)))
    logging.info("Musica / Feishin listening on 127.0.0.1:%d", port)
    server.serve_forever()


if __name__ == "__main__":
    main()
