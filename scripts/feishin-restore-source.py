"""Restore the pinned upstream sources and Musica patch into a fresh vendor directory."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = json.loads((ROOT / "config/feishin-source.json").read_text())
    destinations = {name: ROOT / "vendor" / name for name in config}
    if any(path.exists() for path in destinations.values()):
        raise SystemExit("Existing sources found. Preserve them and use a fresh vendor directory.")
    for name, source in config.items():
        destination = destinations[name]
        subprocess.run(["git", "clone", source["url"], str(destination)], check=True)
        subprocess.run(
            ["git", "checkout", "--detach", source["commit"]], cwd=destination, check=True
        )
    subprocess.run(
        ["git", "switch", "-c", "feature/musica-discovery"], cwd=destinations["feishin"], check=True
    )
    subprocess.run(
        ["git", "apply", str(ROOT / "config/feishin-musica.patch")],
        cwd=destinations["feishin"],
        check=True,
    )
    logging.info("Musica sources restored. Run uv run python scripts/feishin-build.py.")


if __name__ == "__main__":
    main()
