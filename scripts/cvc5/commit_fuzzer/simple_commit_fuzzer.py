#!/usr/bin/env python3
"""Compatibility wrapper that forwards CVC5 commit fuzzing to the shared harness."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.commit_harness_runner import (  # noqa: E402
    build_z3_cvc5_targets,
    ensure_command_available,
    run_commit_harness,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Simple commit fuzzer that runs typefuzz on tests with multiple solvers"
    )
    parser.add_argument(
        "--tests-json",
        required=True,
        help="JSON array of test names (relative to --tests-root)",
    )
    parser.add_argument(
        "--job-id",
        help="Job identifier (optional, for logging)",
    )
    parser.add_argument(
        "--tests-root",
        default="test/regress/cli",
        help="Root directory for tests (default: test/regress/cli)",
    )
    parser.add_argument(
        "--time-remaining",
        type=int,
        help="Remaining time until job timeout in seconds (legacy, use --job-start-time instead)",
    )
    parser.add_argument(
        "--job-start-time",
        type=float,
        help="Unix timestamp when the job started (for automatic time calculation)",
    )
    parser.add_argument(
        "--stop-buffer-minutes",
        type=int,
        default=5,
        help="Minutes before timeout to stop (default: 5, can be set higher for testing)",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=250,
        help="Number of iterations per test (default: 250)",
    )
    parser.add_argument(
        "--modulo",
        type=int,
        default=2,
        help="Modulo parameter for typefuzz -m flag (default: 2)",
    )
    parser.add_argument(
        "--z3-old-path",
        required=False,
        help="Path to z3-4.8.7 binary (not used, kept for compatibility)",
    )
    parser.add_argument(
        "--cvc4-path",
        required=False,
        help="Path to cvc4-1.6 binary (not used, kept for compatibility)",
    )
    parser.add_argument(
        "--cvc5-path",
        default="./build/bin/cvc5",
        help="Path to cvc5 binary (default: ./build/bin/cvc5)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of worker processes (default: 4)",
    )
    parser.add_argument(
        "--bugs-folder",
        default="bugs",
        help="Folder to store bugs (default: bugs)",
    )

    args = parser.parse_args()

    try:
        tests = json.loads(args.tests_json)
        if not isinstance(tests, list):
            raise ValueError("tests-json must be a JSON array")
        if not all(isinstance(test, str) for test in tests):
            raise ValueError("tests-json must be a JSON array of strings")
    except json.JSONDecodeError as exc:
        print(f"Error: Invalid JSON in --tests-json: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        ensure_command_available("z3", "z3")
        ensure_command_available(args.cvc5_path, "cvc5")

        return run_commit_harness(
            tests=tests,
            tests_root=args.tests_root,
            bugs_folder=args.bugs_folder,
            num_workers=args.workers,
            iterations=args.iterations,
            modulo=args.modulo,
            time_remaining=args.time_remaining,
            job_start_time=args.job_start_time,
            stop_buffer_minutes=args.stop_buffer_minutes,
            targets=build_z3_cvc5_targets("z3", args.cvc5_path),
            job_id=args.job_id,
            strict_mode=False,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
