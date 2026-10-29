"""Run the private Shazam companion with its local environment file."""

from __future__ import annotations

import os
from pathlib import Path

from musica_sync.capture_server import main
from musica_sync.cli import load_env_file

ROOT = Path(__file__).resolve().parents[1]


if __name__ == "__main__":
    config = ROOT / "config/capture.env"
    if not config.is_file():
        raise SystemExit("Copy config/capture.env.example to config/capture.env and set your URLs.")
    for key, value in load_env_file(config).items():
        os.environ.setdefault(key, value)
    main()
