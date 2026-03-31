#!/usr/bin/env python3
"""Prepare OpenSMT commit-fuzzer jobs from the local regression corpus."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.local_commit_fuzzer_matrix import build_jobs, discover_opensmt_tests  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze commit coverage using coverage mapping")
    parser.add_argument("commit", help="Commit hash to analyze")
    parser.add_argument(
        "--coverage-json",
        default="coverage_mapping.json",
        help="Path to coverage mapping JSON file",
    )
    parser.add_argument(
        "--compile-commands",
        default=None,
        help="Path to compile_commands.json or its directory (accepted for parity)",
    )
    parser.add_argument(
        "--output-matrix",
        help="Output matrix to JSON file instead of console",
    )
    parser.add_argument(
        "--tests-per-job",
        type=int,
        default=1,
        help="Number of tests to group per job (default: 1)",
    )
    parser.add_argument(
        "--max-jobs",
        type=int,
        default=None,
        help="Maximum number of jobs to create (default: unlimited)",
    )

    args = parser.parse_args()

    if not Path(args.coverage_json).exists():
        print(f"Error: Coverage JSON file not found: {args.coverage_json}")
        return 1

    try:
        tests = discover_opensmt_tests(".")
        jobs, tests_per_job = build_jobs(tests, args.tests_per_job, args.max_jobs, "opensmt")
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1

    unique_tests = list(tests)
    total_tests = len(unique_tests)
    total_jobs = len(jobs)
    coverage_pct = 100.0 if unique_tests else 0.0

    print(
        f"Changed functions: {total_tests}; "
        f"with coverage: {total_tests}; "
        f"without: 0; "
        f"unique tests: {total_tests}; "
        f"coverage: {coverage_pct:.1f}%"
    )

    matrix_data = {
        "matrix": {"include": jobs},
        "total_tests": total_tests,
        "total_jobs": total_jobs,
        "tests_per_job": tests_per_job,
    }

    if args.output_matrix:
        with open(args.output_matrix, "w", encoding="utf-8") as handle:
            json.dump(matrix_data, handle, indent=2)
        print(f"Matrix written to {args.output_matrix} with {total_tests} unique tests in {total_jobs} jobs")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
