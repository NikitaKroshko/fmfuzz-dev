#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRAIN_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
TESTS_ROOT="${TESTS_WORKSPACE:-${BRAIN_ROOT}/z3test}"

usage() {
  cat <<'USAGE' >&2
Usage: test.sh --discover

Prints newline-separated z3 test identifiers from the configured tests workspace.
USAGE
}

if [[ "${1:-}" != "--discover" ]]; then
  usage
  exit 2
fi

PYTHONPATH="${BRAIN_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
python3 - "$TESTS_ROOT" <<'PY'
import sys

from scripts.local_commit_fuzzer_matrix import discover_z3_tests

for test_name in discover_z3_tests(sys.argv[1]):
    print(test_name)
PY
