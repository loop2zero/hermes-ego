#!/usr/bin/env bash
# Install hermes-ego for one machine (mode A) or the relay/daemon half of a split link (mode B).
# Does not print tokens. An existing non-placeholder token is left in place.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
  echo "FAIL python3 is required to run the installer" >&2
  exit 1
fi
exec python3 "$ROOT/scripts/install.py" --repo "$ROOT" "$@"
