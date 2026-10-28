#!/usr/bin/env bash
set -euo pipefail

exec journalctl --user -u musica-navidrome.service -f
