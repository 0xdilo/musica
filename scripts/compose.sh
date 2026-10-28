#!/usr/bin/env bash
set -euo pipefail

if command -v docker-compose >/dev/null 2>&1; then
  exec docker-compose "$@"
fi

if docker compose version >/dev/null 2>&1; then
  exec docker compose "$@"
fi

printf '%s\n' "No Docker Compose command found. Install docker-compose or the docker compose plugin." >&2
exit 1
