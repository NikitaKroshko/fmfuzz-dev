#!/usr/bin/env python3
"""
Coverage Mapper for OpenSMT
Processes SMT test files and extracts coverage data using fastcov.
"""

from __future__ import annotations

import argparse
import gc
import json
import subprocess
import sys
import time
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import psutil

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.local_commit_fuzzer_matrix import discover_opensmt_tests


class CoverageMapper:
    def __init__(self, build_dir: str = "build", opensmt_dir: str = "opensmt", opensmt_path: Optional[str] = None):
        self.build_dir = Path(build_dir)
        self.opensmt_dir = Path(opensmt_dir)
        self.opensmt_binary = self._resolve_binary(opensmt_path)
        self.demangle_cache: Dict[str, str] = {}
        self.max_memory_mb = 10000
        self.memory_check_interval = 50

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

        resolved_path = shutil.which("opensmt")
        if resolved_path:
            candidate = Path(resolved_path)
            if candidate.exists():
                return candidate
        return None

    def demangle_function_name(self, mangled_name: str) -> str:
        if mangled_name in self.demangle_cache:
            return self.demangle_cache[mangled_name]

        try:
            result = subprocess.run(["c++filt", mangled_name], capture_output=True, text=True, check=False)
            demangled = result.stdout.strip() if result.returncode == 0 else mangled_name
        except FileNotFoundError:
            demangled = mangled_name

        self.demangle_cache[mangled_name] = demangled
        return demangled

    def simplify_file_path(self, file_path: str) -> str:
        if "/src/" in file_path:
            parts = file_path.split("/src/")
            if len(parts) > 1:
                return "src/" + parts[1]
        return file_path

    def get_memory_usage_mb(self) -> float:
        try:
            process = psutil.Process()
            return process.memory_info().rss / 1024 / 1024
        except Exception:
            return 0.0

    def check_memory_limit(self) -> bool:
        memory_mb = self.get_memory_usage_mb()
        if memory_mb > self.max_memory_mb:
            print(f"⚠️ Memory limit exceeded: {memory_mb:.1f}MB > {self.max_memory_mb}MB")
            return False
        return True

    def cleanup_memory(self) -> None:
        if len(self.demangle_cache) > 1000:
            self.demangle_cache.clear()
        gc.collect()

    def get_opensmt_tests(self) -> List[Tuple[int, str]]:
        tests = discover_opensmt_tests(str(self.opensmt_dir))
        indexed = [(index + 1, test_name) for index, test_name in enumerate(tests)]
        print(f"Found {len(indexed)} OpenSMT tests")
        sys.stdout.flush()
        return indexed

    def reset_coverage_counters(self) -> None:
        subprocess.run(
            [
                "fastcov",
                "--zerocounters",
                "--search-directory",
                str(self.build_dir),
                "--exclude",
                "/usr/include/*",
                "--exclude",
                "*/deps/*",
            ],
            cwd=self.build_dir.parent,
            capture_output=True,
            text=True,
            check=False,
        )

    def _run_solver(self, smt_file: Path, timeout: int = 120):
        if not self.opensmt_binary or not self.opensmt_binary.exists():
            raise RuntimeError("OpenSMT binary not found")

        attempts = [
            ([
                str(self.opensmt_binary),
                str(smt_file),
            ], None),
            ([
                str(self.opensmt_binary),
            ], smt_file),
        ]

        last_result = None
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
                        timeout=timeout,
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

    def extract_coverage_data(self, test_name: str) -> Optional[Dict]:
        fastcov_output = self.build_dir / f"fastcov_{test_name.replace('/', '_')}.json"
        result = subprocess.run(
            [
                "fastcov",
                "--gcov",
                "gcov",
                "--search-directory",
                str(self.build_dir),
                "--output",
                str(fastcov_output),
                "--exclude",
                "/usr/include/*",
                "--exclude",
                "*/deps/*",
                "--jobs",
                "4",
            ],
            cwd=self.build_dir.parent,
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0 or not fastcov_output.exists():
            return None

        try:
            result_data = self.parse_fastcov_json(fastcov_output, test_name)
        finally:
            try:
                fastcov_output.unlink()
            except Exception:
                pass

        return result_data

    def parse_fastcov_json(self, fastcov_file: Path, test_name: str) -> Optional[Dict]:
        with open(fastcov_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        functions = set()

        if "sources" in data:
            for file_path, file_data in data["sources"].items():
                if self.is_opensmt_source_file(file_path):
                    file_functions = file_data.get("", {}).get("functions", {})
                    for func_name, func_data in file_functions.items():
                        if func_data.get("execution_count", 0) > 0:
                            demangled_name = self.demangle_function_name(func_name)
                            simplified_path = self.simplify_file_path(file_path)
                            line_num = func_data.get("start_line", 0)
                            functions.add(f"{simplified_path}:{demangled_name}:{line_num}")

        if not functions:
            return None

        return {"test_name": test_name, "functions": sorted(functions)}

    def is_opensmt_source_file(self, file_path: str) -> bool:
        has_src_dir = "src/" in file_path
        excluded_patterns = [
            "/usr/include/",
            "/usr/lib/",
            "/System/",
            "/Library/",
            "/Applications/",
            "/opt/",
            "/deps/",
            "/build/deps/",
            "/build/src/",
            "/build/",
            "/include/",
            "/lib/",
            "/bin/",
            "/share/",
            "CMakeFiles/",
            "cmake/",
            "Makefile",
        ]
        return has_src_dir and not any(pattern in file_path for pattern in excluded_patterns)

    def process_single_test(self, test_info: Tuple[int, str]) -> Optional[Dict]:
        test_id, test_name = test_info

        for gcda in self.build_dir.rglob("*.gcda"):
            gcda.unlink()

        self.reset_coverage_counters()

        smt_file = self.opensmt_dir / "test" / "regression" / test_name
        if not smt_file.exists():
            print(f"⚠️ Test file not found: {smt_file}")
            sys.stdout.flush()
            return None

        start_time = time.time()

        try:
            result = self._run_solver(smt_file)
        except subprocess.TimeoutExpired:
            print(f"⏱️ {test_name} - timeout after 120s (skipping)")
            sys.stdout.flush()
            return None
        except Exception as exc:
            print(f"⚠️ {test_name} - error running OpenSMT: {exc} (skipping)")
            sys.stdout.flush()
            return None

        execution_time = round(time.time() - start_time, 2)

        if result.returncode != 0:
            print(f"⚠️ {test_name} - exit code {result.returncode} - {execution_time}s (skipping)")
            if result.stdout:
                for line in result.stdout.strip().split("\n"):
                    if line.strip():
                        print(f"   {line}")
            if result.stderr:
                for line in result.stderr.strip().split("\n"):
                    if line.strip():
                        print(f"   {line}")
            sys.stdout.flush()
            return None

        coverage_data = self.extract_coverage_data(test_name)
        if coverage_data:
            print(f"✅ {test_name} - {len(coverage_data['functions'])} functions - {execution_time}s")
        else:
            print(f"❌ {test_name} - {execution_time}s")
        sys.stdout.flush()

        self.cleanup_memory()
        return coverage_data

    def process_tests(self, tests: List[Tuple[int, str]], output_file: Path, max_tests: Optional[int] = None) -> str:
        if max_tests is not None:
            tests = tests[:max_tests]

        print(f"🚀 Processing {len(tests)} tests")
        print(f"💾 Memory limit: {self.max_memory_mb}MB")
        sys.stdout.flush()

        function_to_tests: Dict[str, List[str]] = {}

        for index, test_info in enumerate(tests, 1):
            test_id, test_name = test_info
            print(f"Test {index}/{len(tests)} (test #{test_id}): {test_name}")
            sys.stdout.flush()

            if index % self.memory_check_interval == 0:
                if not self.check_memory_limit():
                    print(f"🛑 Stopping at test {index} due to memory limit")
                    sys.stdout.flush()
                    break
                self.cleanup_memory()
                print(f"💾 Memory usage: {self.get_memory_usage_mb():.1f}MB")
                sys.stdout.flush()

            result = self.process_single_test(test_info)
            if not result:
                continue

            test_name = result["test_name"]
            for func in result["functions"]:
                function_to_tests.setdefault(func, []).append(test_name)

        for func in function_to_tests:
            function_to_tests[func] = sorted(set(function_to_tests[func]))

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(function_to_tests, f, separators=(",", ":"))

        print(f"✅ Wrote coverage mapping to {output_file}")
        return str(output_file)


def main() -> int:
    parser = argparse.ArgumentParser(description="Coverage Mapper for OpenSMT")
    parser.add_argument("--build-dir", default="build", help="OpenSMT build directory")
    parser.add_argument("--opensmt-dir", default="opensmt", help="OpenSMT repository directory")
    parser.add_argument("--opensmt-path", help="Path to OpenSMT binary (default: auto-detect)")
    parser.add_argument("--start-index", type=int, help="Start index for test slice (1-based)")
    parser.add_argument("--end-index", type=int, help="End index for test slice (1-based, inclusive)")
    parser.add_argument("--output", default="coverage_mapping.json", help="Output JSON file")

    args = parser.parse_args()

    if (args.start_index is None) ^ (args.end_index is None):
        parser.error("--start-index and --end-index must be provided together")

    mapper = CoverageMapper(args.build_dir, args.opensmt_dir, args.opensmt_path)
    tests = mapper.get_opensmt_tests()

    if args.start_index is not None and args.end_index is not None:
        start_index = max(1, args.start_index)
        end_index = max(start_index, args.end_index)
        tests = tests[start_index - 1 : end_index]

    if not tests:
        print("No tests to process", file=sys.stderr)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump({}, f, separators=(",", ":"))
        return 0

    mapper.process_tests(tests, Path(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
