#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRAIN_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
TESTS_ROOT="${TESTS_WORKSPACE:-${SOLVER_WORKSPACE:-${BRAIN_ROOT}/opensmt}}"

usage() {
  cat <<'USAGE' >&2
Usage: test.sh --discover

Prints newline-separated OpenSMT regression test identifiers.
USAGE
}

if [[ "${1:-}" != "--discover" ]]; then
  usage
  exit 2
fi

PYTHONPATH="${BRAIN_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
python3 - "$TESTS_ROOT" <<'PY'
import sys

from scripts.local_commit_fuzzer_matrix import discover_opensmt_tests

for test_name in discover_opensmt_tests(sys.argv[1]):
    print(test_name)
PY
