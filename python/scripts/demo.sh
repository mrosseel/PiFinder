#!/usr/bin/env bash
# Start PiFinder on this computer with the demo display: the UI inside a
# photo of the device, with a glow on each pressed button.
#
# Options pass through to PiFinder.main, for example:
#   python/scripts/demo.sh --record demo.mp4 --record-audio
#   python/scripts/demo.sh --display pg_demo_176 --script debug
set -euo pipefail

cd "$(dirname "$0")/.."
# The Nix dev shell has its own Python environment. Outside it, use uv.
if [ -n "${IN_NIX_SHELL:-}" ]; then
    python=(python)
else
    python=(uv run python)
fi
exec "${python[@]}" -m PiFinder.main -fh --camera debug --keyboard local \
    --display pg_demo "$@"
