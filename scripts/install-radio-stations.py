"""Verify curated official station streams, then optionally add them to Navidrome."""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request, urlopen

from musica_sync.discovery import credentials
from musica_sync.navidrome import ROOT, Subsonic, validate_local_url
from musica_sync.radios import publish

LOGGER = logging.getLogger(__name__)


def check_station(station: dict) -> dict:
    url = validate_local_url(station["streamUrl"])
    if not url.startswith("https://"):
        raise ValueError("Station streams must use HTTPS.")
    request = Request(url, headers={"User-Agent": "Amperfy/2.1.1"})
    with urlopen(request, timeout=20) as response:
        if not response.headers.get_content_type().startswith("audio/"):
            raise ValueError(f"{station['name']}: the stream did not return audio.")
        sample = response.read(65536)
    decoded = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", "pipe:0", "-t", "0.2", "-f", "null", "-"],
        input=sample,
        capture_output=True,
        timeout=20,
    )
    if decoded.returncode or len(sample) < 4096:
        raise ValueError(f"{station['name']}: audio could not be decoded.")
    LOGGER.info("Audio verified: %s", station["name"])
    return station


def main() -> None:
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", help="Publish verified stations.")
    args = parser.parse_args()
    stations = json.loads((ROOT / "config/radio-stations.json").read_text())["stations"]
    with ThreadPoolExecutor(max_workers=4) as executor:
        checked = list(executor.map(check_station, stations))
    if args.install:
        account = credentials()
        client = Subsonic(
            os.environ.get("NAVIDROME_URL", "http://127.0.0.1:4533"),
            account["navidrome_user"],
            account["navidrome_password"],
        )
        previous = client.call("getInternetRadioStations")
        backup = ROOT / "state/discovery/radios-before-curation.json"
        if not backup.exists():
            backup.write_text(json.dumps(previous, indent=2) + "\n")
        publish(client, checked)
        LOGGER.info("%d verified curated stations installed.", len(checked))


if __name__ == "__main__":
    main()
