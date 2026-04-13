#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRAIN_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SOLVER_ROOT="${SOLVER_WORKSPACE:-${BRAIN_ROOT}/z3}"
TESTS_ROOT="${TESTS_WORKSPACE:-${BRAIN_ROOT}/z3test}"
SEEDS_ROOT="${FUZZING_SEEDS:-${SOLVER_ROOT}/FUZZING_SEEDS}"

usage() {
  cat <<'USAGE' >&2
Usage: tests.sh

Populates $FUZZING_SEEDS with stable Z3 SMT-LIB seeds copied from the tests workspace.
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
python3 - "$TESTS_ROOT" "$SEEDS_ROOT" <<'PY'
import shutil
import re
import sys
from pathlib import Path

source_root = Path(sys.argv[1])
seeds_root = Path(sys.argv[2])

regressions_root = source_root / "regressions"
if regressions_root.exists():
    search_root = regressions_root
else:
    search_root = source_root

tests = []
for source in sorted(search_root.rglob("*")):
    if not source.is_file():
        continue
    if source.name.endswith(".disabled"):
        continue
    if source.suffix.lower() not in {".smt", ".smt2"}:
        continue
    try:
        content = source.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        continue
    except Exception as exc:
        raise SystemExit(f"failed to read z3 test source {source}: {exc}") from exc
    if re.search(r"\(check-sat-using\b", content, re.IGNORECASE):
        continue
    relative_name = source.relative_to(source_root).as_posix()
    if relative_name == "regressions/smt2/5731.smt2":
        continue
    tests.append(relative_name)

for relative_name in tests:
    source = source_root / relative_name
    if not source.is_file():
        raise SystemExit(f"missing z3 test source: {source}")
    target = seeds_root / relative_name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
PY
