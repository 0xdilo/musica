#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

BIN_PATH="${NAVIDROME_BIN:-$ROOT_DIR/bin/navidrome}"
CONFIG_PATH="${NAVIDROME_CONFIG:-$ROOT_DIR/data/navidrome/navidrome.toml}"

if [[ ! -x "$BIN_PATH" ]]; then
  printf '%s\n' "Navidrome binary not found at $BIN_PATH" >&2
  exit 1
fi

if [[ ! -f "$CONFIG_PATH" ]]; then
  printf '%s\n' "Navidrome config not found at $CONFIG_PATH" >&2
  exit 1
fi

exec "$BIN_PATH" --configfile "$CONFIG_PATH" --nobanner
