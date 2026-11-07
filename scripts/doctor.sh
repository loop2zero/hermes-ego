#!/usr/bin/env bash
# Check a hermes-ego install. Prints PASS, FAIL, and SKIP lines. Exits non-zero when any check fails.
# Does not print tokens.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
else
  echo "FAIL python3 is not installed" >&2
  exit 1
fi
exec "$PY" "$ROOT/scripts/doctor.py" --repo "$ROOT" "$@"
