#!/usr/bin/env python3
"""Run the contract-driven cvc5 commit harness."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.solver_fuzzing_brain import run_contract_harness  # noqa: E402


CONTRACT_PATH = ROOT / "contracts" / "solvers" / "cvc5.yml"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the contract-driven cvc5 commit harness")
    parser.add_argument("--tests-json", required=True)
    parser.add_argument("--job-id")
    parser.add_argument("--tests-root", default="test/regress/cli")
    parser.add_argument("--time-remaining", type=int)
    parser.add_argument("--job-start-time", type=float)
    parser.add_argument("--stop-buffer-minutes", type=int, default=5)
    parser.add_argument("--iterations", type=int, default=250)
    parser.add_argument("--modulo", type=int, default=2)
    parser.add_argument("--workers", type=int, default=max(1, os.cpu_count() or 1))
    parser.add_argument("--bugs-folder", default="bugs")
    parser.add_argument("--z3-path", default="z3")
    parser.add_argument("--cvc5-path", default="./build/bin/cvc5")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    return run_contract_harness(
        CONTRACT_PATH,
        tests=args.tests_json,
        tests_root=args.tests_root,
        target_binary=args.cvc5_path,
        reference_binary=args.z3_path,
        bugs_folder=args.bugs_folder,
        num_workers=args.workers,
        iterations=args.iterations,
        modulo=args.modulo,
        time_remaining=args.time_remaining,
        job_start_time=args.job_start_time,
        stop_buffer_minutes=args.stop_buffer_minutes,
        job_id=args.job_id,
        strict_mode=args.strict,
    )


if __name__ == "__main__":
    raise SystemExit(main())
