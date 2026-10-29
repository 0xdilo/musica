"""Receive explicit Shazam saves and publish imported tracks to Navidrome."""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from musica_sync import new_music
from musica_sync.discovery import DiscoveryError, credentials
from musica_sync.navidrome import ROOT, Subsonic, validate_local_url

PLAYLIST_NAME = "Da Shazam"


def token_path() -> Path:
    return Path(os.environ.get("MUSICA_CAPTURE_TOKEN_FILE", ROOT / "state/capture/token"))


def device_token() -> str:
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(secrets.token_urlsafe(32))
    if path.stat().st_mode & 0o077:
        raise RuntimeError("Capture token permissions must be 0600: " + str(path))
    token = path.read_text().strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise RuntimeError("Invalid capture token file: " + str(path))
    return token


def authenticate(header: str) -> str:
    if not hmac.compare_digest(header.encode(), ("Bearer " + device_token()).encode()):
        raise DiscoveryError(401, "Collega questo dispositivo prima di salvare un brano.")
    return credentials()["navidrome_user"]


def pair(data: dict) -> str:
    username, password = data.get("username"), data.get("password")
    if (
        not isinstance(username, str)
        or not isinstance(password, str)
        or not 0 < len(password) <= 1024
        or username != credentials()["navidrome_user"]
    ):
        raise DiscoveryError(401, "Nome utente o password non corretti.")
    request = Request(
        validate_local_url(os.environ.get("NAVIDROME_URL", "http://127.0.0.1:4533"))
        + "/auth/login",
        data=json.dumps({"username": username, "password": password}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=10) as response:
            result = json.load(response)
    except HTTPError as exc:
        if exc.code in {401, 403, 400}:
            raise DiscoveryError(401, "Nome utente o password non corretti.") from None
        raise DiscoveryError(503, "Navidrome non è disponibile. Riprova tra poco.") from None
    except (URLError, TimeoutError, ValueError):
        raise DiscoveryError(503, "Navidrome non è disponibile. Riprova tra poco.") from None
    if not result.get("token"):
        raise DiscoveryError(401, "Nome utente o password non corretti.")
    return device_token()


def submit(username: str, data: dict) -> dict:
    artist, title = data.get("artist"), data.get("title")
    if not new_music.valid_text(artist) or not new_music.valid_text(title):
        raise DiscoveryError(400, "Shazam non ha restituito titolo e artista validi.")
    artist, title = artist.strip(), title.strip()
    if re.search(r"https?://|[<>]", artist + title, re.I):
        raise DiscoveryError(400, "Invia titolo e artista, senza link o codice HTML.")
    key = new_music.key_for(artist, title)
    with new_music.database() as db:
        db.execute(
            "INSERT INTO recommendations(id,owner,artist,title,reason,score,updated,personal) "
            "VALUES (?,?,?,?,?,?,?,0) ON CONFLICT(id,owner) DO NOTHING",
            (key, username, artist, title, "Shazam", 1, time.time()),
        )
        # Track Shazam saves independently, including songs previously recommended elsewhere.
        db.execute(
            "CREATE TABLE IF NOT EXISTS shazam_saves ("
            "id TEXT,owner TEXT,created REAL,PRIMARY KEY(id,owner))"
        )
    new_music.save(username, {"id": key})
    with new_music.database() as db:
        db.execute(
            "INSERT INTO shazam_saves VALUES (?,?,?) ON CONFLICT(id,owner) DO NOTHING",
            (key, username, time.time()),
        )
        row = db.execute(
            "SELECT r.*,j.status,j.error,j.song_id FROM recommendations r JOIN jobs j "
            "ON r.id=j.id AND r.owner=j.owner WHERE r.id=? AND r.owner=?",
            (key, username),
        ).fetchone()
    track = new_music.public_track(row)
    message = (
        "È già nella tua libreria. Lo aggiungo a Da Shazam."
        if track["status"] == "ready"
        else "Ricevuto. Download e importazione partono automaticamente."
    )
    return {"accepted": True, "message": message, "track": track}


def saved_tracks(username: str) -> list[dict]:
    with new_music.database() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS shazam_saves ("
            "id TEXT,owner TEXT,created REAL,PRIMARY KEY(id,owner))"
        )
        rows = db.execute(
            "SELECT r.*,j.status,j.error,j.song_id,s.created FROM shazam_saves s "
            "JOIN recommendations r ON r.id=s.id AND r.owner=s.owner "
            "JOIN jobs j ON j.id=s.id AND j.owner=s.owner "
            "WHERE s.owner=? ORDER BY s.created DESC",
            (username,),
        ).fetchall()
    return [new_music.public_track(row) for row in rows]


def sync_playlist(username: str, song_ids: list[str], client: Subsonic) -> None:
    """Add missing imported tracks, preserving the listener's playlist edits."""
    if not song_ids:
        return
    playlists = client.call("getPlaylists").get("playlists", {}).get("playlist", [])
    playlist = next(
        (p for p in playlists if p["name"] == PLAYLIST_NAME and p.get("owner") == username), None
    )
    if not playlist:
        client.call("createPlaylist", [("name", PLAYLIST_NAME), *(("songId", s) for s in song_ids)])
        return
    existing = client.call("getPlaylist", [("id", playlist["id"])])["playlist"]
    present = {song["id"] for song in existing.get("entry", [])}
    missing = [song_id for song_id in song_ids if song_id not in present]
    if missing:
        client.call(
            "updatePlaylist",
            [("playlistId", playlist["id"]), *(("songIdToAdd", s) for s in missing)],
        )
