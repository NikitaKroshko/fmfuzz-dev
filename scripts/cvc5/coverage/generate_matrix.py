#!/usr/bin/env python3
"""Generate dynamic matrix for CVC5 coverage mapping jobs"""

import json
import sys
from coverage_mapper import CoverageMapper


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


def generate_job_name(job_id: int) -> str:
    """Generate CVC5 job name (regress0a, regress0b, etc.)."""
    if job_id <= 26:
        return f"regress0{chr(ord('a') + job_id - 1)}"
    group = (job_id - 1) // 26
    letter_idx = (job_id - 1) % 26
    return f"regress{group}{chr(ord('a') + letter_idx)}"


def generate_matrix(build_dir: str = "build", max_job_time_minutes: int = 60,
                    buffer_minutes: int = 10, avg_test_time_seconds: float = 18.6):
    """Generate dynamic matrix for coverage mapping jobs."""
    # ctest --show-only already excludes disabled tests
    mapper = CoverageMapper(build_dir=build_dir)
    tests = mapper.get_ctest_tests()
    
    if not tests:
        print("❌ No tests found", file=sys.stderr)
        return {'matrix': {'include': []}, 'total_tests': 0, 'total_jobs': 0}
    
    total_tests = len(tests)
    print(f"Found {total_tests} tests", file=sys.stderr)
    
    total_jobs, tests_per_job = calculate_jobs(
        total_tests, target_jobs=6, max_job_time_minutes=max_job_time_minutes,
        buffer_minutes=buffer_minutes, avg_test_time_seconds=avg_test_time_seconds
    )
    
    print(f"Total jobs: {total_jobs}, Tests per job: {tests_per_job}", file=sys.stderr)
    
    # Generate matrix
    matrix_entries = []
    for job_id in range(1, total_jobs + 1):
        start_index = (job_id - 1) * tests_per_job + 1
        end_index = min(job_id * tests_per_job, total_tests)
        matrix_entries.append({
            'job_name': generate_job_name(job_id),
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
    
    parser = argparse.ArgumentParser(description='Generate dynamic matrix for CVC5 coverage mapping')
    parser.add_argument('--build-dir', default='build', help='Path to build directory')
    parser.add_argument('--max-job-time', type=int, default=60, help='Maximum time per job in minutes')
    parser.add_argument('--buffer', type=int, default=10, help='Buffer time for setup/teardown in minutes')
    parser.add_argument('--avg-test-time', type=float, default=18.6, help='Average test execution time in seconds')
    parser.add_argument('--output', default='matrix.json', help='Output JSON file')
    
    args = parser.parse_args()
    
    result = generate_matrix(
        build_dir=args.build_dir,
        max_job_time_minutes=args.max_job_time,
        buffer_minutes=args.buffer,
        avg_test_time_seconds=args.avg_test_time
    )
    
    with open(args.output, 'w') as f:
        json.dump(result, f, indent=2)
    
    print(f"✅ Matrix written to {args.output}")
    print(f"Total tests: {result['total_tests']}, Total jobs: {result['total_jobs']}")
