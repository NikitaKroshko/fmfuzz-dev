#!/usr/bin/env python3
"""Run the orchestrator-backed OpenSMT commit harness."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.commit_harness_runner import (
    build_cvc5_opensmt_targets,
    ensure_command_available,
    run_commit_harness,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the orchestrator-backed OpenSMT commit harness")
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
        default="test/regression",
        help="Root directory for tests (default: test/regression)",
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
        "--workers",
        type=int,
        default=max(1, os.cpu_count() or 1),
        help="Number of worker threads (default: CPU count)",
    )
    parser.add_argument(
        "--bugs-folder",
        default="bugs",
        help="Folder to store bugs (default: bugs)",
    )
    parser.add_argument(
        "--opensmt-path",
        default="opensmt",
        help="Path to the OpenSMT binary (default: opensmt)",
    )
    parser.add_argument(
        "--cvc5-path",
        default="cvc5",
        help="Path to the CVC5 binary (default: cvc5)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Propagate the first non-zero harness exit code and stop early",
    )

    args = parser.parse_args()

    ensure_command_available(args.opensmt_path, "opensmt")
    ensure_command_available(args.cvc5_path, "cvc5")

    try:
        tests = json.loads(args.tests_json)
        if not isinstance(tests, list) or not all(isinstance(item, str) for item in tests):
            raise ValueError("tests-json must be a JSON array of strings")
    except json.JSONDecodeError as exc:
        print(f"Error: Invalid JSON in --tests-json: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

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
        targets=build_cvc5_opensmt_targets(args.cvc5_path, args.opensmt_path),
        job_id=args.job_id,
        strict_mode=args.strict,
    )


if __name__ == "__main__":
    raise SystemExit(main())
