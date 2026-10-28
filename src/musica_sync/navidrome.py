"""Authenticated local music-server clients shared by discovery tools."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]


def service_url(name: str, default: str) -> str:
    from musica_sync.cli import load_env_file

    values = load_env_file(ROOT / ".env")
    return validate_local_url(os.environ.get(name, values.get(name, default)))


def navidrome_url() -> str:
    return service_url("NAVIDROME_URL", "http://127.0.0.1:4533")


def audiomuse_url() -> str:
    return service_url("AUDIOMUSE_URL", "http://127.0.0.1:8000")


def request_json(url: str, data: dict | None = None, token: str = "") -> dict:
    headers = {"Accept": "application/json"}
    body = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urlopen(Request(url, data=body, headers=headers), timeout=180) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"Music API returned HTTP {exc.code}; check service logs.") from None
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(
            "Music API unavailable; check Navidrome and AudioMuse services."
        ) from exc


def validate_local_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Music service URLs must use http:// or https:// with a hostname.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Music service URLs must not contain credentials, queries or fragments.")
    return value.rstrip("/")


class Subsonic:
    def __init__(self, url: str, username: str, password: str):
        self.url = validate_local_url(url)
        self.username = username
        self.password = password

    def request(self, method: str, values: list[tuple[str, str]] | None = None) -> Request:
        salt = secrets.token_hex(12)
        token = hashlib.md5((self.password + salt).encode()).hexdigest()
        params = [
            ("u", self.username),
            ("t", token),
            ("s", salt),
            ("v", "1.16.1"),
            ("c", "musica-discovery"),
            ("f", "json"),
            *(values or []),
        ]
        return Request(
            f"{self.url}/rest/{method}.view",
            data=urlencode(params).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    def call(self, method: str, values: list[tuple[str, str]] | None = None) -> dict:
        try:
            with urlopen(self.request(method, values), timeout=60) as response:
                result = json.load(response)["subsonic-response"]
        except HTTPError as exc:
            raise RuntimeError(f"Navidrome returned HTTP {exc.code} for {method}.") from None
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"Navidrome unavailable during {method}.") from exc
        if result.get("status") != "ok":
            error = result.get("error", {})
            raise RuntimeError(f"Navidrome {method}: {error.get('message', 'unknown API error')}")
        return result


def upload_image(url: str, token: str, entity: str, entity_id: str, content: bytes) -> None:
    """Upload artwork using Navidrome's native authenticated endpoint."""
    if entity not in {"radio", "playlist"} or not entity_id.isalnum():
        raise ValueError("Unsupported artwork target.")
    boundary = secrets.token_hex(16)
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="image"; '
            'filename="cover.png"\r\nContent-Type: image/png\r\n\r\n'
        ).encode()
        + content
        + f"\r\n--{boundary}--\r\n".encode()
    )
    request = Request(
        f"{validate_local_url(url)}/api/{entity}/{entity_id}/image",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "X-ND-Authorization": "Bearer " + token,
        },
    )
    with urlopen(request, timeout=30) as response:
        response.read()
