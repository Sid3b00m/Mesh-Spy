#!/usr/bin/env bash
# Start Mesh-Spy in the foreground. Creates the virtualenv and config on first
# run, so this works straight out of a git clone.
#
#   MESH_SPY_SKIP_PIP=1   skip the dependency check (faster start on a Pi)
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi

if [[ "${MESH_SPY_SKIP_PIP:-0}" != "1" ]]; then
  .venv/bin/pip install -q -r requirements.txt
fi

if [[ ! -f config/config.yaml ]]; then
  cp config/config.example.yaml config/config.yaml
fi

mkdir -p data

exec .venv/bin/python -m app.main
