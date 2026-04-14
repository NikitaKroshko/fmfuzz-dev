#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRAIN_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/z3-seeds.XXXXXX")"

usage() {
  cat <<'USAGE' >&2
Usage: test.sh --discover

Compatibility wrapper that populates a temporary FUZZING_SEEDS directory with
the canonical tests.sh entrypoint and then prints the discovered seed names.
USAGE
}

if [[ "${1:-}" != "--discover" ]]; then
  usage
  exit 2
fi

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

FUZZING_SEEDS="$TMP_DIR" \
PYTHONPATH="${BRAIN_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
"${SCRIPT_DIR}/tests.sh"

PYTHONPATH="${BRAIN_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
python3 - "$TMP_DIR" <<'PY'
import sys
from pathlib import Path

seed_root = Path(sys.argv[1])
tests = [
    path.relative_to(seed_root).as_posix()
    for path in sorted(seed_root.rglob("*"))
    if path.is_file() and path.suffix.lower() in {".smt", ".smt2"}
]

for test_name in tests:
    print(test_name)
PY
