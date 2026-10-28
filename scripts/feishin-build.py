"""Build the pinned Musica frontend with Bun and publish assets atomically."""

from __future__ import annotations

import fcntl
import gzip
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "vendor/feishin"
DESTINATION = ROOT / "bin/feishin-musica"


def build() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for command in (
        ["bun", "install", "--frozen-lockfile", "--ignore-scripts"],
        ["bun", "lint"],
        ["bun", "test"],
        ["bun", "run", "build:web"],
    ):
        subprocess.run(command, cwd=SOURCE, check=True)
    built = SOURCE / "out/web"
    DESTINATION.mkdir(parents=True, exist_ok=True)
    for source in built.rglob("*"):
        if not source.is_file() or source.name == "index.html":
            continue
        destination = DESTINATION / source.relative_to(built)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        shutil.copy2(source, temporary)
        temporary.replace(destination)
        if source.suffix in {".js", ".css", ".json", ".svg"}:
            compressed = destination.with_suffix(destination.suffix + ".gz")
            temporary = compressed.with_suffix(".tmp")
            temporary.write_bytes(gzip.compress(source.read_bytes(), compresslevel=9, mtime=0))
            temporary.replace(compressed)
    # Existing browser tabs may still request old hashed chunks. Keep those files.
    temporary = DESTINATION / "index.html.tmp"
    shutil.copy2(built / "index.html", temporary)
    temporary.replace(DESTINATION / "index.html")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip()
    report = {
        "upstream": commit,
        "version": json.loads((SOURCE / "package.json").read_text())["version"],
        "assets": str(DESTINATION),
        "bun": subprocess.check_output(["bun", "--version"], text=True).strip(),
    }
    (ROOT / "state/improvements/frontend-build.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    logging.info("Verified frontend installed at %s", DESTINATION)
    if os.environ.get("RESTART_FEISHIN") == "true":
        subprocess.run(["systemctl", "--user", "restart", "musica-feishin.service"], check=True)


def main() -> None:
    lock = ROOT / "state/improvements/frontend-build.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another frontend build is running; wait for it to finish.") from None
        build()


if __name__ == "__main__":
    main()
