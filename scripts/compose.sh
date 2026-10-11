#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if docker info >/dev/null 2>&1; then
    exec docker compose "$@"
else
    exec sudo docker compose "$@"
fi
