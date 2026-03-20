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

from scripts.commit_harness_runner import (  # noqa: E402
    build_cvc5_opensmt_targets,
    ensure_command_available,
    run_commit_harness,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the orchestrator-backed OpenSMT commit harness"
    )
    parser.add_argument("--tests-json", required=True)
    parser.add_argument("--job-id")
    parser.add_argument("--tests-root", default="ci/typefuzz-seeds")
    parser.add_argument("--time-remaining", type=int)
    parser.add_argument("--job-start-time", type=float)
    parser.add_argument("--stop-buffer-minutes", type=int, default=5)
    parser.add_argument("--iterations", type=int, default=250)
    parser.add_argument("--modulo", type=int, default=2)
    parser.add_argument("--workers", type=int, default=max(1, os.cpu_count() or 1))
    parser.add_argument("--bugs-folder", default="bugs")
    parser.add_argument("--opensmt-path", default="opensmt")
    parser.add_argument("--cvc5-path", default="cvc5")
    parser.add_argument("--strict", action="store_true")

    args = parser.parse_args()
    ensure_command_available(args.opensmt_path, "opensmt")
    ensure_command_available(args.cvc5_path, "cvc5")

    return run_commit_harness(
        tests=json.loads(args.tests_json),
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
