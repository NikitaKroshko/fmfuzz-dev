#!/usr/bin/env python3
"""Compatibility wrapper for the contract-driven OpenSMT commit harness."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.solver_fuzzing_brain import run_contract_harness  # noqa: E402


CONTRACT_PATH = ROOT / "contracts" / "solvers" / "opensmt.yml"


class SimpleCommitFuzzer:
    """Small compatibility shim for tests that still exercise helper methods."""

    def __init__(
        self,
        *,
        tests,
        tests_root: str,
        bugs_folder: str = "bugs",
        num_workers: int = 4,
        iterations: int = 250,
        modulo: int = 2,
        opensmt_path: str = "./build/bin/opensmt",
        cvc5_path: str = "cvc5",
        **_kwargs,
    ) -> None:
        self.tests = list(tests)
        self.tests_root = Path(tests_root)
        self.bugs_folder = Path(bugs_folder)
        self.num_workers = num_workers
        self.iterations = iterations
        self.modulo = modulo
        self.opensmt_path = opensmt_path
        self.cvc5_path = cvc5_path
        self.stats = {"bugs_found": 0}
        self.bugs_folder.mkdir(parents=True, exist_ok=True)

    def _get_stat(self, key: str) -> int:
        return self.stats.get(key, 0)

    def _move_worker_bug_files(self) -> None:
        for worker_dir in sorted(self.bugs_folder.glob("worker_*")):
            if not worker_dir.is_dir():
                continue
            for bug_file in sorted(path for path in worker_dir.iterdir() if path.is_file()):
                destination = self.bugs_folder / bug_file.name
                if destination.exists():
                    destination = self.bugs_folder / f"{bug_file.stem}_moved{bug_file.suffix}"
                shutil.move(str(bug_file), str(destination))
                self.stats["bugs_found"] += 1

    def summarize_bugs(self) -> None:
        bug_files = sorted(path for path in self.bugs_folder.iterdir() if path.is_file())
        print(f"Found {len(bug_files)} bug(s):")
        for index, bug_file in enumerate(bug_files, start=1):
            print(f"Bug #{index}: {bug_file.as_posix()}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compatibility wrapper for the contract-driven OpenSMT commit harness"
    )
    parser.add_argument("--tests-json", required=True)
    parser.add_argument("--job-id")
    parser.add_argument("--tests-root", default="test/regression")
    parser.add_argument("--time-remaining", type=int)
    parser.add_argument("--job-start-time", type=float)
    parser.add_argument("--stop-buffer-minutes", type=int, default=5)
    parser.add_argument("--iterations", type=int, default=250)
    parser.add_argument("--modulo", type=int, default=2)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--bugs-folder", default="bugs")
    parser.add_argument("--opensmt-path", default="./build/bin/opensmt")
    parser.add_argument("--cvc5-path", default="cvc5")
    args = parser.parse_args()

    tests = json.loads(args.tests_json)
    print(f"Running fuzzer on {len(tests)} test(s)")
    print(f"Workers: {args.workers}")

    exit_code = run_contract_harness(
        CONTRACT_PATH,
        tests=args.tests_json,
        tests_root=args.tests_root,
        target_binary=args.opensmt_path,
        reference_binary=args.cvc5_path,
        bugs_folder=args.bugs_folder,
        num_workers=args.workers,
        iterations=args.iterations,
        modulo=args.modulo,
        time_remaining=args.time_remaining,
        job_start_time=args.job_start_time,
        stop_buffer_minutes=args.stop_buffer_minutes,
        job_id=args.job_id,
        strict_mode=False,
    )

    SimpleCommitFuzzer(
        tests=tests,
        tests_root=args.tests_root,
        bugs_folder=args.bugs_folder,
        num_workers=args.workers,
        iterations=args.iterations,
        modulo=args.modulo,
        opensmt_path=args.opensmt_path,
        cvc5_path=args.cvc5_path,
    ).summarize_bugs()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
