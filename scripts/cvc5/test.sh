#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRAIN_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SOLVER_ROOT="${SOLVER_WORKSPACE:-${BRAIN_ROOT}/cvc5}"
BUILD_DIR="${CVC5_BUILD_DIR:-${SOLVER_ROOT}/build}"

usage() {
  cat <<'USAGE' >&2
Usage: test.sh --discover

Prints newline-separated cvc5 regression test identifiers.
USAGE
}

if [[ "${1:-}" != "--discover" ]]; then
  usage
  exit 2
fi

PYTHONPATH="${BRAIN_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
python3 - "$BUILD_DIR" <<'PY'
import sys

from scripts.local_commit_fuzzer_matrix import discover_cvc5_tests

for test_name in discover_cvc5_tests(sys.argv[1]):
    print(test_name)
PY
