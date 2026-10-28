"""Station-scoped SomaFM relay for providers that reject browser user agents."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from musica_sync.navidrome import ROOT, navidrome_url, validate_local_url

KEY_FILE = ROOT / "state/discovery/relay.key"


def relay_key(*, create: bool = False) -> bytes:
    if not KEY_FILE.exists() and create:
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = KEY_FILE.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(secrets.token_bytes(32))
        temporary.replace(KEY_FILE)
    key = KEY_FILE.read_bytes()
    if len(key) != 32:
        raise ValueError("Invalid radio relay key. Restore state/discovery/relay.key.")
    return key


def signature(station_id: str, key: bytes) -> str:
    if not re.fullmatch(r"[A-Za-z0-9]{1,64}", station_id):
        raise ValueError("Invalid radio station ID.")
    return hmac.new(key, station_id.encode(), hashlib.sha256).hexdigest()[:32]


def public_base() -> str:
    config = ROOT / "config/feishin.env"
    values = dict(
        line.split("=", 1)
        for line in config.read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    value = values.get("PUBLIC_PLAYER_URL", "").strip()
    if not value:
        raise ValueError("Set PUBLIC_PLAYER_URL in config/feishin.env before installing the relay.")
    return validate_local_url(value)


def relay_url(station_id: str, base: str | None = None) -> str:
    token = signature(station_id, relay_key(create=True))
    return f"{base or public_base()}/radio/{station_id}/{token}"


def validate_upstream(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not re.fullmatch(r"ice\d+\.somafm\.com", parsed.hostname or "")
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.port not in {None, 443}
        or not re.fullmatch(r"/[A-Za-z0-9-]+", parsed.path)
    ):
        raise ValueError("Radio relay only accepts official SomaFM audio endpoints.")
    return value


def resolve(path: str) -> str | None:
    match = re.fullmatch(r"/radio/([A-Za-z0-9]{1,64})/([a-f0-9]{32})", path)
    if not match:
        return None
    station_id, token = match.groups()
    if not hmac.compare_digest(token, signature(station_id, relay_key())):
        return None
    catalogue = json.loads((ROOT / "state/discovery/radios.json").read_text())
    station = next((s for s in catalogue["stations"] if s["id"] == station_id), None)
    return validate_upstream(station["streamUrl"]) if station else None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("The radio upstream redirected; refresh its official stream URL.")


def open_stream(url: str, *, metadata: bool = False):
    headers = {"User-Agent": "Musica-Radio/1.0"}
    if metadata:
        headers["Icy-MetaData"] = "1"
    return build_opener(NoRedirect()).open(
        Request(validate_upstream(url), headers=headers), timeout=20
    )


def install() -> None:
    from musica_sync.navidrome import Subsonic

    credentials = json.loads((ROOT / "state/sonic/credentials.json").read_text())
    client = Subsonic(
        navidrome_url(), credentials["navidrome_user"], credentials["navidrome_password"]
    )
    path = ROOT / "state/discovery/radios.json"
    catalogue = json.loads(path.read_text())
    base = public_base()
    for station in catalogue["stations"]:
        station["relayUrl"] = relay_url(station["id"], base)
        client.call(
            "updateInternetRadioStation",
            [
                ("id", station["id"]),
                ("name", station["name"]),
                ("homepageUrl", station["homepageUrl"]),
                ("streamUrl", station["relayUrl"]),
            ],
        )
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(catalogue, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


if __name__ == "__main__":
    install()
