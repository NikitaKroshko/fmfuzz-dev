#!/usr/bin/env python3
"""
Unified test counting utility for CVC5, Z3, and OpenSMT solvers.
Reuses existing CoverageMapper logic where available to ensure consistency with coverage generation.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple


def count_cvc5_tests(build_dir: Path) -> Dict:
    """
    Count CVC5 tests using the existing CoverageMapper.

    Args:
        build_dir: Path to CVC5 build directory

    Returns:
        Dictionary with test_count, commit_hash, and solver_version
    """
    # Import from existing coverage_mapper
    cvc5_coverage_path = Path(__file__).parent.parent / 'cvc5' / 'coverage'
    sys.path.insert(0, str(cvc5_coverage_path))

    try:
        from coverage_mapper import CoverageMapper
    except ImportError as e:
        print(f"Error: Could not import CoverageMapper from {cvc5_coverage_path}", file=sys.stderr)
        print(f"Make sure scripts/cvc5/coverage/coverage_mapper.py exists", file=sys.stderr)
        raise

    # Use CoverageMapper to discover tests (same logic as generate_matrix.py)
    mapper = CoverageMapper(build_dir=str(build_dir))
    tests = mapper.get_ctest_tests()  # Returns list of (id, name) tuples

    # Get commit hash from CVC5 repository
    cvc5_dir = build_dir.parent
    try:
        result = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=cvc5_dir,
            capture_output=True,
            text=True,
            check=True
        )
        commit_hash = result.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"Error: Failed to get CVC5 commit hash: {e}", file=sys.stderr)
        commit_hash = "unknown"

    return {
        'test_count': len(tests),
        'commit_hash': commit_hash,
        'solver_version': 'main'
    }


def count_z3_tests(z3test_dir: Path) -> Dict:
    """
    Count Z3 tests using the existing CoverageMapper and filtering logic.

    Args:
        z3test_dir: Path to z3test repository

    Returns:
        Dictionary with test_count, commit_hash, and solver_version
    """
    # Import from existing coverage_mapper and generate_matrix
    z3_coverage_path = Path(__file__).parent.parent / 'z3' / 'coverage'
    sys.path.insert(0, str(z3_coverage_path))

    try:
        from coverage_mapper import CoverageMapper
        from generate_matrix import filter_tests
    except ImportError as e:
        print(f"Error: Could not import from {z3_coverage_path}", file=sys.stderr)
        print(f"Make sure scripts/z3/coverage/ modules exist", file=sys.stderr)
        raise

    # Use CoverageMapper to discover all tests
    mapper = CoverageMapper(z3test_dir=str(z3test_dir))
    all_tests = mapper.get_smt2_tests()  # Returns list of test paths

    # Apply same filtering as generate_matrix.py (skip unsupported commands, known bad tests)
    filtered_tests = filter_tests(all_tests, z3test_dir)

    # Get commit hash from Z3 repository
    z3_dir = z3test_dir.parent / 'z3'
    try:
        result = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=z3_dir,
            capture_output=True,
            text=True,
            check=True
        )
        commit_hash = result.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"Error: Failed to get Z3 commit hash: {e}", file=sys.stderr)
        commit_hash = "unknown"

    return {
        'test_count': len(filtered_tests),
        'commit_hash': commit_hash,
        'solver_version': 'main'
    }


def count_opensmt_tests(opensmt_dir: Path) -> Dict:
    """
    Count OpenSMT regression tests using the local OpenSMT corpus.

    Args:
        opensmt_dir: Path to OpenSMT repository

    Returns:
        Dictionary with test_count, commit_hash, and solver_version
    """
    from scripts.local_commit_fuzzer_matrix import discover_opensmt_tests

    tests = discover_opensmt_tests(str(opensmt_dir))

    # Get commit hash from OpenSMT repository
    try:
        result = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=opensmt_dir,
            capture_output=True,
            text=True,
            check=True
        )
        commit_hash = result.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"Error: Failed to get OpenSMT commit hash: {e}", file=sys.stderr)
        commit_hash = "unknown"

    return {
        'test_count': len(tests),
        'commit_hash': commit_hash,
        'solver_version': 'main'
    }


def main():
    parser = argparse.ArgumentParser(
        description='Count tests for CVC5, Z3, or OpenSMT solver',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Count CVC5 tests
  python3 scripts/coverage/count_tests.py cvc5 --build-dir cvc5/build --output test-count.json

  # Count Z3 tests
  python3 scripts/coverage/count_tests.py z3 --z3test-dir z3test --output test-count.json

  # Count OpenSMT tests
  python3 scripts/coverage/count_tests.py opensmt --opensmt-dir opensmt --output test-count.json
        """
    )

    parser.add_argument(
        'solver',
        choices=['cvc5', 'z3', 'opensmt'],
        help='Solver to count tests for'
    )

    parser.add_argument(
        '--build-dir',
        type=Path,
        help='Path to CVC5 build directory (required for cvc5)'
    )

    parser.add_argument(
        '--z3test-dir',
        type=Path,
        help='Path to z3test repository (required for z3)'
    )

    parser.add_argument(
        '--opensmt-dir',
        type=Path,
        help='Path to OpenSMT repository (required for opensmt)'
    )

    parser.add_argument(
        '--output',
        type=Path,
        help='Output JSON file path (prints to stdout if not specified)'
    )

    args = parser.parse_args()

    # Validate solver-specific arguments
    if args.solver == 'cvc5':
        if not args.build_dir:
            parser.error('--build-dir is required for cvc5')
        if not args.build_dir.exists():
            parser.error(f'Build directory does not exist: {args.build_dir}')
    elif args.solver == 'z3':
        if not args.z3test_dir:
            parser.error('--z3test-dir is required for z3')
        if not args.z3test_dir.exists():
            parser.error(f'z3test directory does not exist: {args.z3test_dir}')
    elif args.solver == 'opensmt':
        if not args.opensmt_dir:
            parser.error('--opensmt-dir is required for opensmt')
        if not args.opensmt_dir.exists():
            parser.error(f'opensmt directory does not exist: {args.opensmt_dir}')

    # Count tests
    print(f"Counting {args.solver.upper()} tests...", file=sys.stderr)

    if args.solver == 'cvc5':
        result = count_cvc5_tests(args.build_dir)
    elif args.solver == 'z3':
        result = count_z3_tests(args.z3test_dir)
    else:
        result = count_opensmt_tests(args.opensmt_dir)

    print(f"✅ Found {result['test_count']} tests at commit {result['commit_hash'][:8]}", file=sys.stderr)

    # Output result
    output_json = json.dumps(result, indent=2)

    if args.output:
        args.output.write_text(output_json)
        print(f"✅ Results written to {args.output}", file=sys.stderr)
    else:
        print(output_json)

    return 0


if __name__ == '__main__':
    sys.exit(main())
