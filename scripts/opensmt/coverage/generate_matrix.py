#!/usr/bin/env python3
"""Generate dynamic matrix for OpenSMT coverage mapping jobs."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.local_commit_fuzzer_matrix import discover_opensmt_tests


def calculate_jobs(
    total_tests: int,
    target_jobs: int,
    max_job_time_minutes: int,
    buffer_minutes: int,
    avg_test_time_seconds: float,
) -> tuple[int, int]:
    """Calculate an execution chunking plan for a corpus."""
    if max_job_time_minutes <= buffer_minutes:
        raise ValueError("max_job_time_minutes must be greater than buffer_minutes")

    available_time_seconds = (max_job_time_minutes - buffer_minutes) * 60
    max_tests_per_job = max(1, int(available_time_seconds / avg_test_time_seconds))
    min_jobs = max(1, math.ceil(total_tests / max_tests_per_job))

    total_jobs = min(max(1, target_jobs), total_tests)
    while True:
        tests_per_job = max(1, math.ceil(total_tests / total_jobs))
        estimated_minutes = (tests_per_job * avg_test_time_seconds + buffer_minutes * 60) / 60.0

        if estimated_minutes <= max_job_time_minutes:
            break

        if total_jobs >= min_jobs or total_jobs >= total_tests:
            total_jobs = min(total_tests, min_jobs)
            tests_per_job = max(1, math.ceil(total_tests / total_jobs))
            break

        total_jobs = min(total_tests, total_jobs + 1)

    return total_jobs, tests_per_job


def generate_matrix(
    opensmt_dir: str = "opensmt",
    max_job_time_minutes: int = 60,
    buffer_minutes: int = 10,
    avg_test_time_seconds: float = 12.0,
):
    """Generate dynamic matrix for coverage mapping jobs."""
    tests = discover_opensmt_tests(opensmt_dir)

    if not tests:
        print("No OpenSMT tests found", file=sys.stderr)
        return {"matrix": {"include": []}, "total_tests": 0, "total_jobs": 0}

    total_tests = len(tests)
    print(f"Found {total_tests} OpenSMT tests", file=sys.stderr)

    total_jobs, tests_per_job = calculate_jobs(
        total_tests,
        target_jobs=4,
        max_job_time_minutes=max_job_time_minutes,
        buffer_minutes=buffer_minutes,
        avg_test_time_seconds=avg_test_time_seconds,
    )

    print(f"Total jobs: {total_jobs}, Tests per job: {tests_per_job}", file=sys.stderr)

    matrix_entries = []
    for job_id in range(1, total_jobs + 1):
        start_index = (job_id - 1) * tests_per_job + 1
        end_index = min(job_id * tests_per_job, total_tests)
        matrix_entries.append(
            {
                "job_name": f"opensmt-part{job_id}",
                "start_index": start_index,
                "end_index": end_index,
            }
        )

    return {
        "matrix": {"include": matrix_entries},
        "total_tests": total_tests,
        "total_jobs": total_jobs,
        "tests_per_job": tests_per_job,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate dynamic matrix for OpenSMT coverage mapping")
    parser.add_argument("--opensmt-dir", default="opensmt", help="Path to OpenSMT repository directory")
    parser.add_argument("--max-job-time", type=int, default=60, help="Maximum time per job in minutes")
    parser.add_argument("--buffer", type=int, default=10, help="Buffer time for setup/teardown in minutes")
    parser.add_argument("--avg-test-time", type=float, default=12.0, help="Average test execution time in seconds")
    parser.add_argument("--output", default="matrix.json", help="Output JSON file")

    args = parser.parse_args()

    result = generate_matrix(
        opensmt_dir=args.opensmt_dir,
        max_job_time_minutes=args.max_job_time,
        buffer_minutes=args.buffer,
        avg_test_time_seconds=args.avg_test_time,
    )

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print(f"✅ Matrix written to {args.output}")
    print(f"Total tests: {result['total_tests']}, Total jobs: {result['total_jobs']}")
