#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRAIN_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SOLVER_ROOT="${SOLVER_WORKSPACE:-${BRAIN_ROOT}/cvc5}"
BUILD_DIR="${CVC5_BUILD_DIR:-${SOLVER_ROOT}/build}"
SEEDS_ROOT="${FUZZING_SEEDS:-${SOLVER_ROOT}/FUZZING_SEEDS}"

usage() {
  cat <<'USAGE' >&2
Usage: test.sh --discover

Compatibility wrapper for the contract-first cvc5 seed discovery path.
USAGE
}

if [[ "${1:-}" != "--discover" ]]; then
  usage
  exit 2
fi

FUZZING_SEEDS="$SEEDS_ROOT" \
CVC5_BUILD_DIR="$BUILD_DIR" \
SOLVER_WORKSPACE="$SOLVER_ROOT" \
"${SCRIPT_DIR}/tests.sh"

PYTHONPATH="${BRAIN_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
python3 - "$SEEDS_ROOT" <<'PY'
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
discovered = []
for current_root, dirnames, filenames in os.walk(root, followlinks=True):
    dirnames.sort()
    for filename in sorted(filenames):
        path = Path(current_root) / filename
        if path.is_file() and path.suffix.lower() in {".smt", ".smt2"}:
            discovered.append(path.relative_to(root).as_posix())

for test_name in sorted(discovered):
    print(test_name)
PY
