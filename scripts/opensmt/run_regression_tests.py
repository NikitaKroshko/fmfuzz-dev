#!/usr/bin/env python3
"""Run OpenSMT regression tests from the local regression corpus."""

from __future__ import annotations

import argparse
import subprocess
import sys
import shutil
from pathlib import Path
from typing import List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.local_commit_fuzzer_matrix import discover_opensmt_tests  # noqa: E402


class OpenSMTRegressionRunner:
    def __init__(
        self,
        build_dir: str = ".",
        opensmt_dir: Optional[str] = None,
        opensmt_path: Optional[str] = None,
        timeout: int = 120,
    ):
        self.build_dir = Path(build_dir).resolve()
        self.opensmt_dir = Path(opensmt_dir or self.build_dir.parent).resolve()
        self.timeout = timeout
        self.opensmt_binary = self._resolve_binary(opensmt_path)

    def _resolve_binary(self, opensmt_path: Optional[str]) -> Optional[Path]:
        if opensmt_path:
            candidate = Path(opensmt_path)
            if candidate.exists():
                return candidate
            return None

        candidates = [
            self.build_dir / "bin" / "opensmt",
            self.build_dir / "opensmt",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate

        resolved = shutil.which("opensmt")
        if resolved:
            candidate = Path(resolved)
            if candidate.exists():
                return candidate
        return None

    @staticmethod
    def _normalize_suite_filter(suite_filter: Optional[str]) -> Optional[str]:
        if not suite_filter:
            return None

        normalized = Path(suite_filter).as_posix().lstrip("./")
        for prefix in ("test/regression/", "regression/"):
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix) :]
        return normalized or None

    def discover_tests(self, suite_filter: Optional[str] = None) -> List[str]:
        tests = discover_opensmt_tests(str(self.opensmt_dir))
        normalized_filter = self._normalize_suite_filter(suite_filter)
        if not normalized_filter:
            return tests

        return [
            test_name
            for test_name in tests
            if test_name == normalized_filter or test_name.startswith(f"{normalized_filter}/")
        ]

    def _run_solver(self, smt_file: Path) -> subprocess.CompletedProcess[str]:
        if not self.opensmt_binary or not self.opensmt_binary.exists():
            raise RuntimeError("OpenSMT binary not found")

        attempts = [
            ([str(self.opensmt_binary), str(smt_file)], None),
            ([str(self.opensmt_binary)], smt_file),
        ]

        last_result: Optional[subprocess.CompletedProcess[str] | Exception] = None
        for argv, stdin_file in attempts:
            try:
                stdin_handle = stdin_file.open("r", encoding="utf-8") if stdin_file else None
                try:
                    result = subprocess.run(
                        argv,
                        cwd=self.build_dir,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=self.timeout,
                        stdin=stdin_handle,
                    )
                finally:
                    if stdin_handle is not None:
                        stdin_handle.close()
            except subprocess.TimeoutExpired:
                raise
            except Exception as exc:
                last_result = exc
                continue

            if result.returncode == 0:
                return result

            last_result = result

        if isinstance(last_result, subprocess.CompletedProcess):
            return last_result
        raise RuntimeError(f"Failed to run OpenSMT on {smt_file}: {last_result}")

    def run_tests(self, tests: Sequence[str]) -> int:
        if not tests:
            print("No OpenSMT regression tests found", file=sys.stderr)
            return 1

        print(f"Running {len(tests)} OpenSMT regression test(s)")
        failures: List[str] = []

        for index, test_name in enumerate(tests, 1):
            smt_file = self.opensmt_dir / "test" / "regression" / test_name
            if not smt_file.exists():
                print(f"❌ {index}/{len(tests)} {test_name} - missing")
                failures.append(test_name)
                continue

            try:
                result = self._run_solver(smt_file)
            except subprocess.TimeoutExpired:
                print(f"⏱️  {index}/{len(tests)} {test_name} - timeout")
                failures.append(test_name)
                continue
            except Exception as exc:
                print(f"❌ {index}/{len(tests)} {test_name} - error: {exc}")
                failures.append(test_name)
                continue

            if result.returncode == 0:
                print(f"✅ {index}/{len(tests)} {test_name}")
            else:
                print(f"❌ {index}/{len(tests)} {test_name} - exit code {result.returncode}")
                if result.stdout.strip():
                    print(result.stdout.rstrip())
                if result.stderr.strip():
                    print(result.stderr.rstrip(), file=sys.stderr)
                failures.append(test_name)

        if failures:
            print(f"Regression failures: {len(failures)}", file=sys.stderr)
            for failure in failures:
                print(f"  - {failure}", file=sys.stderr)
            return 1

        print("All OpenSMT regression tests completed successfully")
        return 0

    def run(self, suite_filter: Optional[str] = None) -> int:
        tests = self.discover_tests(suite_filter)
        if not tests:
            print("No OpenSMT regression tests found", file=sys.stderr)
            return 1
        return self.run_tests(tests)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run OpenSMT regression tests")
    parser.add_argument("suite", nargs="?", default=None, help="Optional suite or path prefix to run")
    parser.add_argument("--build-dir", default=".", help="OpenSMT build directory (default: current directory)")
    parser.add_argument(
        "--opensmt-dir",
        default=None,
        help="OpenSMT repository root (default: parent of build directory)",
    )
    parser.add_argument(
        "--opensmt-path",
        default=None,
        help="Path to the OpenSMT binary (default: build/bin/opensmt or PATH)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Timeout per test in seconds (default: 120)",
    )

    args = parser.parse_args()
    runner = OpenSMTRegressionRunner(
        build_dir=args.build_dir,
        opensmt_dir=args.opensmt_dir,
        opensmt_path=args.opensmt_path,
        timeout=args.timeout,
    )
    return runner.run(args.suite)


if __name__ == "__main__":
    raise SystemExit(main())
