#!/usr/bin/env python3
"""Build a local commit-fuzzer matrix without AWS/S3 state.

This script discovers tests from the solver-specific local corpus and splits
them into matrix jobs for the commit-fuzzer workflows.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import List, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


OPENSMT_UNSUPPORTED_LOGIC_MARKERS = ("BV", "FP", "NIA", "NRA", "PB")


def _extract_opensmt_logic_name(relative_name: str) -> str:
    """Return the first SMT-LIB logic-like path component, if present."""
    for part in Path(relative_name).parts:
        upper = part.upper()
        if upper.startswith("QF_"):
            return upper
    return ""


def is_opensmt_preferred_test(relative_name: str) -> bool:
    """Return True for OpenSMT-supported tests we want to schedule first.

    Keep the supported quantifier-free array/UF/linear-arithmetic families in
    the main sample and only push obviously unsupported bitvector, floating
    point, string, pseudo-boolean, or nonlinear families to the fallback set.
    """
    logic_name = _extract_opensmt_logic_name(relative_name)
    if not logic_name:
        return True

    if logic_name.startswith("QF_S"):
        return False

    return not any(marker in logic_name for marker in OPENSMT_UNSUPPORTED_LOGIC_MARKERS)


def _interleave_grouped_tests(grouped_tests: dict[str, list[str]]) -> List[str]:
    """Round-robin tests across logic families so one family does not dominate."""
    ordered: List[str] = []
    families = sorted(grouped_tests)
    positions = {family: 0 for family in families}

    while True:
        progressed = False
        for family in families:
            index = positions[family]
            tests = grouped_tests[family]
            if index < len(tests):
                ordered.append(tests[index])
                positions[family] = index + 1
                progressed = True
        if not progressed:
            break

    return ordered


def check_has_unsupported_commands(test_file: Path) -> bool:
    """Return True if a Z3 SMT2 test uses commands unsupported by CVC5."""
    try:
        content = test_file.read_text()
    except Exception:
        return False
    return bool(re.search(r"\(check-sat-using\b", content, re.IGNORECASE))


def discover_z3_tests(z3test_dir: str) -> List[str]:
    from scripts.z3.coverage.coverage_mapper import CoverageMapper

    z3test_path = Path(z3test_dir)
    mapper = CoverageMapper(z3test_dir=str(z3test_path))
    tests = [name for _, name in mapper.get_smt2_tests()]

    filtered: List[str] = []
    skip_tests = {"regressions/smt2/5731.smt2"}
    for test_name in tests:
        if test_name in skip_tests:
            continue
        test_file = z3test_path / test_name
        if test_file.exists() and check_has_unsupported_commands(test_file):
            continue
        filtered.append(test_name)
    return filtered


def discover_cvc5_tests(build_dir: str) -> List[str]:
    from scripts.cvc5.coverage.coverage_mapper import CoverageMapper

    mapper = CoverageMapper(build_dir=build_dir)
    return [name for _, name in mapper.get_ctest_tests()]


def discover_opensmt_tests(opensmt_dir: str) -> List[str]:
    """Discover OpenSMT regression tests from the local corpus."""
    seed_root = Path(opensmt_dir) / "test" / "regression"
    if not seed_root.exists():
        return []

    preferred_groups: dict[str, list[str]] = {}
    fallback_groups: dict[str, list[str]] = {}

    for test_file in sorted(seed_root.rglob("*")):
        if not test_file.is_file():
            continue
        if test_file.suffix.lower() not in {".smt", ".smt2"}:
            continue
        relative_name = test_file.relative_to(seed_root).as_posix()
        if "splitting/patches" in relative_name:
            continue

        parts = Path(relative_name).parts
        family = "/".join(parts[:2]) if len(parts) >= 2 else parts[0]
        # Keep obviously unsupported families in a fallback bucket so they do
        # not crowd out the supported OpenSMT corpus when tests are limited.
        target_groups = preferred_groups if is_opensmt_preferred_test(relative_name) else fallback_groups
        target_groups.setdefault(family, []).append(relative_name)

    ordered_tests = _interleave_grouped_tests(preferred_groups)
    ordered_tests.extend(_interleave_grouped_tests(fallback_groups))
    return ordered_tests


def maybe_limit_tests(tests: Sequence[str], limit_tests: int | None) -> List[str]:
    if limit_tests is None:
        return list(tests)
    if limit_tests < 1:
        raise ValueError("--limit-tests must be a positive integer")
    return list(tests[:limit_tests])


def build_jobs(
    tests: Sequence[str],
    tests_per_job: int | None,
    max_jobs: int | None,
    solver: str,
) -> tuple[list[dict], int]:
    total_tests = len(tests)
    if total_tests == 0:
        return [], 0

    if tests_per_job is not None and tests_per_job < 1:
        raise ValueError("--tests-per-job must be a positive integer")
    if max_jobs is not None and max_jobs < 1:
        raise ValueError("--max-jobs must be a positive integer")

    if tests_per_job is None:
        if max_jobs is None:
            tests_per_job = 1
        else:
            tests_per_job = max(1, math.ceil(total_tests / max_jobs))
    elif max_jobs is not None:
        calculated_jobs = math.ceil(total_tests / tests_per_job)
        if calculated_jobs > max_jobs:
            tests_per_job = max(1, math.ceil(total_tests / max_jobs))

    jobs: list[dict] = []
    for job_index in range(0, total_tests, tests_per_job):
        job_id = len(jobs) + 1
        job_tests = list(tests[job_index:job_index + tests_per_job])
        jobs.append(
            {
                "job_id": job_id,
                "job_name": f"{solver}-job-{job_id}",
                "tests": job_tests,
            }
        )

    return jobs, tests_per_job


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a local commit-fuzzer matrix")
    parser.add_argument("solver", choices=["z3", "cvc5", "opensmt"], help="Solver name")
    parser.add_argument("--z3test-dir", default="z3test", help="Path to the z3test repository")
    parser.add_argument("--build-dir", default="build", help="Path to the solver build directory")
    parser.add_argument("--opensmt-dir", default="opensmt", help="Path to the OpenSMT repository")
    parser.add_argument("--limit-tests", type=int, default=None, help="Cap the discovered test count")
    parser.add_argument("--tests-per-job", type=int, default=None, help="Number of tests per job")
    parser.add_argument("--max-jobs", type=int, default=None, help="Maximum number of jobs")
    parser.add_argument("--output", default="matrix.json", help="Output JSON file")
    args = parser.parse_args()

    if args.solver == "z3":
        tests = discover_z3_tests(args.z3test_dir)
    elif args.solver == "cvc5":
        tests = discover_cvc5_tests(args.build_dir)
    else:
        tests = discover_opensmt_tests(args.opensmt_dir)

    tests = maybe_limit_tests(tests, args.limit_tests)
    jobs, tests_per_job = build_jobs(tests, args.tests_per_job, args.max_jobs, args.solver)

    matrix_data = {
        "matrix": {"include": jobs},
        "total_tests": len(tests),
        "total_jobs": len(jobs),
        "tests_per_job": tests_per_job,
        "solver": args.solver,
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(matrix_data, f, indent=2)

    print(f"✅ Matrix written to {args.output}")
    print(f"Solver: {args.solver}")
    print(f"Total tests: {len(tests)}")
    print(f"Total jobs: {len(jobs)}")
    print(f"Tests per job: {tests_per_job}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
