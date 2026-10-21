#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
command -v docker >/dev/null || { echo 'Install Docker Engine and Compose v2 first.' >&2; exit 1; }
if [ ! -x .venv/bin/python ]; then python3 -m venv .venv; fi
.venv/bin/pip install -q -r requirements-dev.txt
.venv/bin/python scripts/setup.py
# Serial builds keep the Go compiler and pip from competing for RAM.
./scripts/compose.sh build storage
./scripts/compose.sh build api
./scripts/compose.sh up -d --wait
echo 'FileFlow: http://localhost:8080'
echo 'Local account credentials: .local/credentials.txt'
