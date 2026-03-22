#!/usr/bin/env python3
"""Generate dynamic matrix for Z3 coverage mapping jobs"""

import json
import sys
import re
from pathlib import Path
from coverage_mapper import CoverageMapper


def check_has_unsupported_commands(test_file: Path) -> bool:
    """Check if SMT file uses commands unsupported by CVC5."""
    try:
        content = test_file.read_text()
        return bool(re.search(r'\(check-sat-using\b', content, re.IGNORECASE))
    except Exception:
        return False


def filter_tests(tests: list, z3test_dir: Path) -> list:
    """Filter tests using same logic as coverage_mapper.process_single_test()."""
    SKIP_TESTS = ['regressions/smt2/5731.smt2']  # Times out with expensive regex constraints
    
    filtered = []
    for test_id, test_name in tests:
        if test_name in SKIP_TESTS:
            continue
        test_file = z3test_dir / test_name
        if test_file.exists() and check_has_unsupported_commands(test_file):
            continue
        filtered.append(test_name)
    
    # Re-index to consecutive 1-based indices
    return [(i + 1, test_name) for i, test_name in enumerate(filtered)]


def calculate_jobs(total_tests: int, target_jobs: int, max_job_time_minutes: int, 
                   buffer_minutes: int, avg_test_time_seconds: float) -> tuple[int, int]:
    """Calculate optimal number of jobs and tests per job."""
    if max_job_time_minutes <= buffer_minutes:
        raise ValueError("max_job_time_minutes must be greater than buffer_minutes")

    available_time_seconds = (max_job_time_minutes - buffer_minutes) * 60
    max_tests_per_job = int(available_time_seconds / avg_test_time_seconds)
    min_jobs = (total_tests + max_tests_per_job - 1) // max_tests_per_job
    
    # Try target_jobs, increase if needed
    total_jobs = target_jobs
    while True:
        tests_per_job = max(1, (total_tests + total_jobs - 1) // total_jobs)
        estimated_minutes = (tests_per_job * avg_test_time_seconds + buffer_minutes * 60) / 60.0
        
        if estimated_minutes <= max_job_time_minutes:
            break
        
        if total_jobs >= min_jobs:
            total_jobs = min_jobs
            tests_per_job = max(1, (total_tests + total_jobs - 1) // total_jobs)
            break
        
        total_jobs += 1
    
    return total_jobs, tests_per_job


def generate_matrix(z3test_dir: str = "z3test", max_job_time_minutes: int = 60,
                    buffer_minutes: int = 10, avg_test_time_seconds: float = 9.5):
    """Generate dynamic matrix for coverage mapping jobs."""
    mapper = CoverageMapper(z3test_dir=z3test_dir)
    all_tests = mapper.get_smt2_tests()
    
    if not all_tests:
        print("❌ No tests found", file=sys.stderr)
        return {'matrix': {'include': []}, 'total_tests': 0, 'total_jobs': 0}
    
    tests = filter_tests(all_tests, Path(z3test_dir))
    if not tests:
        print("❌ No tests remaining after filtering", file=sys.stderr)
        return {'matrix': {'include': []}, 'total_tests': 0, 'total_jobs': 0}
    
    total_tests = len(tests)
    print(f"Found {total_tests} tests", file=sys.stderr)
    
    total_jobs, tests_per_job = calculate_jobs(
        total_tests, target_jobs=4, max_job_time_minutes=max_job_time_minutes,
        buffer_minutes=buffer_minutes, avg_test_time_seconds=avg_test_time_seconds
    )
    
    print(f"Total jobs: {total_jobs}, Tests per job: {tests_per_job}", file=sys.stderr)
    
    # Generate matrix
    matrix_entries = []
    for job_id in range(1, total_jobs + 1):
        start_index = (job_id - 1) * tests_per_job + 1
        end_index = min(job_id * tests_per_job, total_tests)
        matrix_entries.append({
            'job_name': f'z3-part{job_id}',
            'start_index': start_index,
            'end_index': end_index
        })
    
    return {
        'matrix': {'include': matrix_entries},
        'total_tests': total_tests,
        'total_jobs': total_jobs,
        'tests_per_job': tests_per_job
    }


if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='Generate dynamic matrix for Z3 coverage mapping')
    parser.add_argument('--z3test-dir', default='z3test', help='Path to z3test directory')
    parser.add_argument('--max-job-time', type=int, default=60, help='Maximum time per job in minutes')
    parser.add_argument('--buffer', type=int, default=10, help='Buffer time for setup/teardown in minutes')
    parser.add_argument('--avg-test-time', type=float, default=9.5, help='Average test execution time in seconds')
    parser.add_argument('--output', default='matrix.json', help='Output JSON file')
    
    args = parser.parse_args()
    
    result = generate_matrix(
        z3test_dir=args.z3test_dir,
        max_job_time_minutes=args.max_job_time,
        buffer_minutes=args.buffer,
        avg_test_time_seconds=args.avg_test_time
    )
    
    with open(args.output, 'w') as f:
        json.dump(result, f, indent=2)
    
    print(f"✅ Matrix written to {args.output}")
    print(f"Total tests: {result['total_tests']}, Total jobs: {result['total_jobs']}")
