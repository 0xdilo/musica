"""Import the official SomaFM music catalogue without replacing user stations."""

from __future__ import annotations

import configparser
import json
import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from urllib.parse import urlsplit
from urllib.request import urlopen

from musica_sync.navidrome import ROOT, Subsonic, navidrome_url, request_json, upload_image

LOGGER = logging.getLogger(__name__)
CATALOGUE = "https://somafm.com/channels.json"
NON_MUSIC = {"holiday", "news", "spoken", "specials", "live"}
MUSIC_EXCEPTIONS = {"defcon", "chillits"}


def genre_label(raw: str) -> str:
    labels = {"hiphop": "Hip Hop", "bossanova": "Bossa Nova", "70s": "70s"}
    return " / ".join(
        labels.get(part, part.title()) for part in raw.split("|") if part not in NON_MUSIC
    )


def music_channels(catalogue: dict) -> list[dict]:
    return [
        channel
        for channel in catalogue["channels"]
        if not NON_MUSIC.intersection(channel["genre"].split("|"))
        or channel["id"] in MUSIC_EXCEPTIONS
    ]


def stream_urls(playlist: str) -> list[str]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(playlist)
    result = []
    for key, value in parser["playlist"].items():
        parsed = urlsplit(value)
        if key.startswith("file") and parsed.scheme == "https" and parsed.hostname:
            if parsed.username or parsed.password:
                raise ValueError("Radio stream must not contain credentials.")
            result.append(value)
    if not result:
        raise ValueError("Official playlist contains no HTTPS audio streams.")
    return result


def decode_stream(url: str) -> dict:
    with urlopen(url, timeout=20) as response:
        mime = response.headers.get_content_type()
        if not mime.startswith("audio/"):
            raise ValueError(f"Expected audio from radio station, got {mime}.")
        audio = response.read(64 * 1024)
    decoded = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", "pipe:0", "-t", "0.1", "-f", "null", "-"],
        input=audio,
        capture_output=True,
        timeout=20,
    )
    if len(audio) < 4096 or decoded.returncode:
        raise ValueError("Station stream could not be decoded as audio.")
    return {"mime": mime, "bytes": len(audio), "decoded": True}


def resolve_channel(channel: dict) -> dict:
    playlist = next(
        item["url"]
        for item in channel["playlists"]
        if item["format"] == "mp3" and item["quality"] == "highest"
    )
    with urlopen(playlist, timeout=20) as response:
        candidates = stream_urls(response.read().decode())
    failures = []
    for url in candidates:
        try:
            check = decode_stream(url)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            failures.append(type(exc).__name__)
            continue
        return {
            "name": f"{genre_label(channel['genre'])} · {channel['title']}",
            "genre": channel["genre"],
            "description": channel["description"],
            "homepageUrl": f"https://somafm.com/{channel['id']}/",
            "streamUrl": url,
            "image": channel.get("largeimage", channel.get("image", "")),
            "check": check,
        }
    raise RuntimeError(f"No playable stream for {channel['title']}: {', '.join(failures)}")


def publish(client: Subsonic, channels: list[dict], token: str = "") -> None:
    stations = client.call("getInternetRadioStations").get("internetRadioStations", {})
    existing = {
        item.get("homePageUrl", "").rstrip("/"): item
        for item in stations.get("internetRadioStation", [])
    }
    for channel in channels:
        values = [
            (key, channel.get("relayUrl", channel[key]) if key == "streamUrl" else channel[key])
            for key in ("name", "streamUrl", "homepageUrl")
        ]
        previous = existing.get(channel["homepageUrl"].rstrip("/"))
        if previous:
            client.call("updateInternetRadioStation", [("id", previous["id"]), *values])
        else:
            client.call("createInternetRadioStation", values)
        LOGGER.info("Published %s (%s)", channel["name"], channel["genre"])
    if token:
        current = client.call("getInternetRadioStations")["internetRadioStations"]
        by_homepage = {
            station.get("homePageUrl", "").rstrip("/"): station["id"]
            for station in current.get("internetRadioStation", [])
        }
        for channel in channels:
            with urlopen(channel["image"], timeout=20) as response:
                image = response.read(2 * 1024 * 1024)
            station_id = by_homepage[channel["homepageUrl"].rstrip("/")]
            if (ROOT / "state/discovery/relay.key").exists():
                from musica_sync.radio_relay import relay_url

                channel["relayUrl"] = relay_url(station_id)
                client.call(
                    "updateInternetRadioStation",
                    [
                        ("id", station_id),
                        ("name", channel["name"]),
                        ("homepageUrl", channel["homepageUrl"]),
                        ("streamUrl", channel["relayUrl"]),
                    ],
                )
            channel["id"] = station_id
            upload_image(client.url, token, "radio", station_id, image)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    catalogue = request_json(CATALOGUE)
    selected = music_channels(catalogue)
    # Resolve and decode every station before making any changes to the server.
    with ThreadPoolExecutor(max_workers=6) as pool:
        channels = list(pool.map(resolve_channel, selected))
    report = {"checked_at": datetime.now(UTC).isoformat(), "stations": channels}
    destination = ROOT / "state/discovery/radios.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    credentials = json.loads((ROOT / "state/sonic/credentials.json").read_text())
    client = Subsonic(
        navidrome_url(),
        credentials["navidrome_user"],
        credentials["navidrome_password"],
    )
    auth = request_json(
        client.url + "/auth/login",
        {"username": client.username, "password": client.password},
    )
    if not auth.get("isAdmin"):
        raise RuntimeError(
            "Importing server-wide radio stations requires a Navidrome admin account."
        )
    publish(client, channels, auth["token"])
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    LOGGER.info("%d official music stations imported; every stream decoded.", len(channels))


if __name__ == "__main__":
    main()
