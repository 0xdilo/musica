"""Run the player with the local, untracked frontend configuration."""

from __future__ import annotations

import os
import runpy
from pathlib import Path

from musica_sync.cli import load_env_file

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    config = ROOT / "config/feishin.env"
    if not config.is_file():
        raise SystemExit("Run uv run python scripts/setup.py --init first.")
    for key, value in load_env_file(config).items():
        os.environ.setdefault(key, value)
    os.environ.setdefault("FEISHIN_ASSETS", str(ROOT / "bin/feishin-musica"))
    runpy.run_path(str(ROOT / "scripts/feishin-web.py"), run_name="__main__")


if __name__ == "__main__":
    main()
