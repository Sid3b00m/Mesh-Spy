#!/usr/bin/env bash
# Start Mesh-Spy in the foreground on Linux, a Raspberry Pi or macOS.
#
# The setup itself lives in bootstrap.py, which Windows runs too, so this is
# just the convenience wrapper that finds an interpreter. Creates the
# virtualenv and config on first run, so it works straight out of a clone.
#
#   ./run.sh                start the console
#   ./run.sh --list-ports   show serial ports and the config block to paste
#
#   MESH_SPY_SKIP_PIP=1     skip the dependency check
set -euo pipefail
cd "$(dirname "$0")"

PYTHON_BIN="$(command -v python3 || command -v python || true)"
if [[ -z "$PYTHON_BIN" ]]; then
  echo "[Mesh-Spy] error: no python3 on PATH. Install Python 3.9 or newer." >&2
  exit 1
fi

# exec so signals and the exit code pass straight through, which is what an
# init system supervising this expects.
exec "$PYTHON_BIN" bootstrap.py "$@"
