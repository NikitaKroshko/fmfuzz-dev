#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRAIN_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SOLVER_ROOT="${SOLVER_WORKSPACE:-${BRAIN_ROOT}/cvc5}"
BUILD_DIR="${CVC5_BUILD_DIR:-${SOLVER_ROOT}/build}"
SOURCE_ROOT="${CVC5_TESTS_ROOT:-${SOLVER_ROOT}/test/regress/cli}"
SEEDS_ROOT="${FUZZING_SEEDS:-${SOLVER_ROOT}/FUZZING_SEEDS}"

usage() {
  cat <<'USAGE' >&2
Usage: tests.sh

Populates $FUZZING_SEEDS with stable CVC5 SMT-LIB seeds copied from the
checked-out cvc5 test corpus.
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ $# -gt 0 ]]; then
  usage
  exit 2
fi

rm -rf "$SEEDS_ROOT"
mkdir -p "$SEEDS_ROOT"

PYTHONPATH="${BRAIN_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
python3 - "$SOURCE_ROOT" "$SEEDS_ROOT" "$BUILD_DIR" <<'PY'
import contextlib
import shutil
import sys
from pathlib import Path

from scripts.local_commit_fuzzer_matrix import discover_cvc5_tests

source_root = Path(sys.argv[1])
seeds_root = Path(sys.argv[2])
build_dir = Path(sys.argv[3])

with contextlib.redirect_stdout(sys.stderr):
    tests = discover_cvc5_tests(str(build_dir))
selected = []
for relative_name in tests:
    if Path(relative_name).suffix.lower() not in {".smt", ".smt2"}:
        continue
    source = source_root / relative_name
    if not source.is_file():
        raise SystemExit(f"missing cvc5 test source: {source}")
    target = seeds_root / relative_name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    selected.append(relative_name)
PY
