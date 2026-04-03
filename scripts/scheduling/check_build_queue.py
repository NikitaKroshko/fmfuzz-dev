#!/usr/bin/env python3
"""Deterministically check whether a commit is still queued for build."""

from __future__ import annotations

import argparse
import sys

from scripts.scheduling.s3_state import S3StateError, get_state_manager


def main() -> int:
    parser = argparse.ArgumentParser(description="Check if a commit is still present in the build queue")
    parser.add_argument("solver", help="Solver name")
    parser.add_argument("commit", help="Commit hash to check")
    args = parser.parse_args()

    try:
        manager = get_state_manager(args.solver)
        in_queue = manager.is_in_build_queue(args.commit)
    except S3StateError as exc:
        print(f"❌ S3 state error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"❌ Unexpected error: {exc}", file=sys.stderr)
        return 1

    print("true" if in_queue else "false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
