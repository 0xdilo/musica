"""Create local configuration and connect an existing Navidrome account."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import shutil

from musica_sync.cli import ensure_directories, read_settings, render_beets_config
from musica_sync.navidrome import ROOT, Subsonic, navidrome_url

LOGGER = logging.getLogger(__name__)


def initialize() -> None:
    for source, destination in [
        (ROOT / ".env.example", ROOT / ".env"),
        (ROOT / "config/feishin.env.example", ROOT / "config/feishin.env"),
    ]:
        if not destination.exists():
            shutil.copyfile(source, destination)
            destination.chmod(0o600)
    ensure_directories()
    (ROOT / "data/navidrome").mkdir(parents=True, exist_ok=True)
    (ROOT / "music/nicotine-inbox").mkdir(parents=True, exist_ok=True)
    render_beets_config(read_settings())


def connect(username: str, password: str, *, replace: bool = False) -> None:
    destination = ROOT / "state/sonic/credentials.json"
    if destination.exists() and not replace:
        raise SystemExit("Credentials already exist. Use --replace to reconnect this installation.")
    username = username.strip()
    if not username or not password:
        raise SystemExit("A Navidrome username and password are required.")
    client = Subsonic(navidrome_url(), username, password)
    client.call("ping")
    content = {
        "navidrome_user": username,
        "navidrome_password": password,
        "audiomuse_token": os.environ.get("AUDIOMUSE_TOKEN", ""),
    }
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.parent.chmod(0o700)
    temporary = destination.with_suffix(".tmp")
    with open(temporary, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
        json.dump(content, output, indent=2)
        output.write("\n")
    temporary.replace(destination)
    LOGGER.info("Navidrome account verified. Local credentials saved with mode 0600.")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--init", action="store_true", help="Only create configuration and folders."
    )
    parser.add_argument("--username", help="Existing Navidrome username.")
    parser.add_argument("--replace", action="store_true", help="Replace local service credentials.")
    args = parser.parse_args()
    initialize()
    if args.init:
        LOGGER.info("Configuration ready. Start Navidrome and create an account, then rerun setup.")
        return
    username = args.username or input("Navidrome username: ")
    password = os.environ.get("NAVIDROME_PASSWORD") or getpass.getpass("Navidrome password: ")
    try:
        connect(username, password, replace=args.replace)
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(f"Cannot connect to Navidrome: {exc}") from None


if __name__ == "__main__":
    main()
