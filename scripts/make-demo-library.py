"""Generate fictional album metadata and silent audio for screenshots."""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
from pathlib import Path

from musica_sync.playlist_art import artwork

ALBUMS = [
    (
        "Paper Satellites",
        "After the Rain",
        "Alternative Rock",
        [
            "Static Bloom",
            "Last Light",
            "Side Streets",
            "Slow Motion",
            "Distant Rooms",
            "Northern Lines",
            "Open Season",
            "Half Awake",
        ],
    ),
    (
        "Low Orbit",
        "Night Windows",
        "Electronic",
        [
            "Late Signal",
            "Soft Focus",
            "Blue Hour",
            "Parallel Lines",
            "Antenna",
            "Glass Stations",
            "Low Tide",
            "Return Signal",
        ],
    ),
    (
        "Juniper Club",
        "Green Room",
        "Indie Pop",
        [
            "Stay Awhile",
            "Small Hours",
            "Westbound",
            "Morning Again",
            "Sunday Film",
            "Soft Landing",
            "Pocket Radio",
            "Backyard Lights",
        ],
    ),
]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="An empty directory for the generated library.")
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        raise SystemExit("Output must be empty. Existing music is never replaced.")
    output.mkdir(parents=True, exist_ok=True)
    for artist, album, genre, titles in ALBUMS:
        folder = output / artist / album
        folder.mkdir(parents=True)
        cover, _ = artwork(album)
        shutil.copyfile(cover, folder / "cover.png")
        for number, title in enumerate(titles, 1):
            command = [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=44100:cl=stereo",
                "-t",
                str(165 + number * 17),
                "-c:a",
                "flac",
            ]
            for key, value in {
                "artist": artist,
                "album_artist": artist,
                "album": album,
                "title": title,
                "genre": genre,
                "track": str(number),
                "date": "2026",
            }.items():
                command += ["-metadata", f"{key}={value}"]
            subprocess.run([*command, str(folder / f"{number:02d}.flac")], check=True)
    logging.info("Created 24 silent demonstration tracks with fictional metadata.")


if __name__ == "__main__":
    main()
