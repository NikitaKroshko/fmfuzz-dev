#!/usr/bin/env python3
"""Run a generic harness command across tests with worker orchestration."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import queue
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence


def _cpu_count() -> int:
    """Return the available CPU count with a safe fallback."""
    return os.cpu_count() or 1


class CommitHarnessRunner:
    """Coordinate worker threads, harness execution, and bug persistence."""

    EXIT_CODE_BUGS_FOUND = 10
    EXIT_CODE_UNSUPPORTED = 3
    EXIT_CODE_SUCCESS = 0

    def __init__(
        self,
        tests: Sequence[str],
        tests_root: str,
        bugs_folder: str = "bugs",
        num_workers: int = 4,
        iterations: int = 250,
        modulo: int = 2,
        time_remaining: Optional[int] = None,
        job_start_time: Optional[float] = None,
        stop_buffer_minutes: int = 5,
        targets: Optional[Sequence[str]] = None,
        harness: Optional[Sequence[str] | str] = None,
        job_id: Optional[str] = None,
        strict_mode: bool = False,
    ):
        self.tests = [str(test) for test in tests]
        self.tests_root = Path(tests_root)
        self.bugs_folder = Path(bugs_folder)
        self.iterations = int(iterations)
        self.modulo = int(modulo)
        self.job_id = job_id
        self.strict_mode = strict_mode
        self.start_time = time.time()
        self.cpu_count = _cpu_count()
        self.num_workers = self._resolve_num_workers(num_workers)
        self.time_remaining = self._resolve_time_remaining(
            job_start_time, stop_buffer_minutes, time_remaining
        )

        self.target_commands = self._build_target_commands(targets)
        self.target_args = [
            arg for command in self.target_commands for arg in command
        ]
        self.target_clis = ";".join(
            shlex.join(command) for command in self.target_commands
        )

        self.harness_template = self._parse_harness_template(harness)
        if not self.harness_template:
            raise ValueError("Harness command cannot be empty")
        self.harness_requires_targets = any(
            token == "{target_args}" or "{target_clis}" in token
            for token in self.harness_template
        )
        if self.harness_requires_targets and not self.target_commands:
            raise ValueError("At least one target identifier must be provided")

        self.bugs_folder.mkdir(parents=True, exist_ok=True)

        self.test_queue: "queue.Queue[str]" = queue.Queue()
        self.shutdown_event = threading.Event()
        self.bugs_lock = threading.Lock()
        self.stats_lock = threading.Lock()
        self.strict_exit_code = 0
        self.strict_exit_lock = threading.Lock()
        self.stats: Dict[str, int] = {
            "tests_processed": 0,
            "bugs_found": 0,
            "tests_removed_unsupported": 0,
            "tests_removed_timeout": 0,
            "tests_requeued": 0,
        }

    def _resolve_num_workers(self, num_workers: int) -> int:
        if num_workers <= 0:
            return self.cpu_count
        return min(num_workers, self.cpu_count)

    def _resolve_time_remaining(
        self,
        job_start_time: Optional[float],
        stop_buffer_minutes: int,
        time_remaining: Optional[int],
    ) -> Optional[int]:
        if job_start_time is not None:
            return self._compute_time_remaining(job_start_time, stop_buffer_minutes)
        if time_remaining is not None:
            return max(0, int(time_remaining))
        return None

    def _compute_time_remaining(
        self,
        job_start_time: float,
        stop_buffer_minutes: int,
    ) -> int:
        github_timeout = 21600
        minimum_remaining = 600

        build_time = self.start_time - job_start_time
        stop_buffer_seconds = stop_buffer_minutes * 60
        available_time = github_timeout - build_time
        remaining = int(available_time - stop_buffer_seconds)
        if remaining < minimum_remaining:
            remaining = minimum_remaining
        return remaining

    @staticmethod
    def _resolve_target_command(identifier: str) -> List[str]:
        value = identifier.strip()
        if not value:
            raise ValueError("Target identifier cannot be empty")
        parsed = shlex.split(value)
        if not parsed:
            raise ValueError(f"Target identifier resolved to empty argv: {identifier!r}")
        return parsed

    def _build_target_commands(
        self, target_identifiers: Optional[Sequence[str]]
    ) -> List[List[str]]:
        if not target_identifiers:
            return []
        return [self._resolve_target_command(identifier) for identifier in target_identifiers]

    def _parse_harness_template(
        self, harness: Optional[Sequence[str] | str]
    ) -> List[str]:
        if harness is None:
            raise ValueError("Harness template is required")

        if isinstance(harness, str):
            raw = harness.strip()
            if not raw:
                raise ValueError("Harness template cannot be empty")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON in --harness: {exc}") from exc
        else:
            parsed = list(harness)

        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            raise ValueError("--harness must be a JSON array of argv strings")
        if not parsed:
            raise ValueError("--harness JSON array cannot be empty")
        return parsed

    def _render_harness_command(
        self,
        test_path: Path,
        bugs_folder: Path,
        scratch_folder: Path,
        logs_folder: Path,
    ) -> List[str]:
        context: Dict[str, str] = {
            "iterations": str(self.iterations),
            "modulo": str(self.modulo),
            "test_path": str(test_path),
            "bugs_dir": str(bugs_folder),
            "scratch_dir": str(scratch_folder),
            "logs_dir": str(logs_folder),
            "target_clis": self.target_clis,
            "test": str(test_path),
            "bugs": str(bugs_folder),
            "scratch": str(scratch_folder),
            "log_dir": str(logs_folder),
            "logs": str(logs_folder),
        }

        command: List[str] = []
        for token in self.harness_template:
            if token == "{target_args}":
                command.extend(self.target_args)
                continue
            try:
                command.append(token.format(**context))
            except KeyError as exc:
                raise ValueError(
                    f"Unknown placeholder in --harness template: {exc}. "
                    "Supported: {test_path}, {iterations}, {modulo}, "
                    "{bugs_dir}, {scratch_dir}, {logs}/{logs_dir}, "
                    "{target_args}, {target_clis}"
                ) from exc
        return command

    @staticmethod
    def _collect_bug_files(folder: Path) -> List[Path]:
        if not folder.exists():
            return []
        return sorted(path for path in folder.rglob("*") if path.is_file())

    @staticmethod
    def _calculate_folder_size_mb(folder: Path) -> float:
        try:
            if not folder.exists():
                return 0.0
            size_bytes = sum(
                file.stat().st_size for file in folder.rglob("*") if file.is_file()
            )
            return size_bytes / (1024 * 1024)
        except Exception:
            return 0.0

    @contextlib.contextmanager
    def _worker_temp_dirs(self, worker_id: int):
        scratch_folder = Path(f"scratch_{worker_id}")
        logs_folder = Path(f"logs_{worker_id}")
        bugs_folder = self.bugs_folder / f"worker_{worker_id}"

        for folder in (scratch_folder, logs_folder):
            shutil.rmtree(folder, ignore_errors=True)
            folder.mkdir(parents=True, exist_ok=True)
        bugs_folder.mkdir(parents=True, exist_ok=True)

        try:
            yield scratch_folder, logs_folder, bugs_folder
        finally:
            for folder in (scratch_folder, logs_folder):
                shutil.rmtree(folder, ignore_errors=True)

    def _increment_stat(self, key: str, amount: int = 1) -> None:
        with self.stats_lock:
            self.stats[key] = self.stats.get(key, 0) + amount

    def _persist_bug_files(self, worker_id: int, bug_files: Sequence[Path]) -> None:
        if not bug_files:
            return

        with self.bugs_lock:
            for bug_file in bug_files:
                try:
                    destination = self.bugs_folder / bug_file.name
                    if destination.exists():
                        timestamp = int(time.time())
                        destination = self.bugs_folder / (
                            f"{bug_file.stem}_{timestamp}{bug_file.suffix}"
                        )
                    shutil.move(str(bug_file), str(destination))
                    self._increment_stat("bugs_found")
                except Exception as exc:
                    print(
                        f"[WORKER {worker_id}] Warning: Failed to move bug file "
                        f"{bug_file}: {exc}",
                        file=sys.stderr,
                    )

    def _run_harness(
        self,
        test_name: str,
        worker_id: int,
        per_test_timeout: Optional[float] = None,
    ) -> tuple[int, List[Path], float]:
        test_path = Path(test_name)
        if not test_path.is_absolute():
            test_path = self.tests_root / test_name
        if not test_path.exists():
            print(
                f"[WORKER {worker_id}] Error: Test file not found: {test_path}",
                file=sys.stderr,
            )
            return (1, [], 0.0)

        with self._worker_temp_dirs(worker_id) as (
            scratch_folder,
            logs_folder,
            bugs_folder,
        ):
            try:
                command = self._render_harness_command(
                    test_path, bugs_folder, scratch_folder, logs_folder
                )
            except ValueError as exc:
                print(f"[WORKER {worker_id}] Error: {exc}", file=sys.stderr)
                return (1, [], 0.0)

            if per_test_timeout and per_test_timeout > 0:
                print(
                    f"[WORKER {worker_id}] Running harness on: {test_name} "
                    f"(timeout: {per_test_timeout}s)"
                )
            else:
                print(f"[WORKER {worker_id}] Running harness on: {test_name}")

            start_time = time.time()
            timeout = per_test_timeout if per_test_timeout and per_test_timeout > 0 else None
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                exit_code = result.returncode
                if exit_code not in {
                    self.EXIT_CODE_SUCCESS,
                    self.EXIT_CODE_BUGS_FOUND,
                    self.EXIT_CODE_UNSUPPORTED,
                }:
                    if result.stdout.strip():
                        print(result.stdout, end="")
                    if result.stderr.strip():
                        print(result.stderr, end="", file=sys.stderr)
            except subprocess.TimeoutExpired as exc:
                if exc.stdout:
                    print(exc.stdout, end="")
                if exc.stderr:
                    print(exc.stderr, end="", file=sys.stderr)
                return (124, self._collect_bug_files(bugs_folder), time.time() - start_time)
            except Exception as exc:
                print(f"[WORKER {worker_id}] Error running harness: {exc}", file=sys.stderr)
                return (1, self._collect_bug_files(bugs_folder), time.time() - start_time)

            runtime = time.time() - start_time
            bug_files = self._collect_bug_files(bugs_folder)
            return (exit_code, bug_files, runtime)

    def _handle_exit_code(
        self,
        test_name: str,
        exit_code: int,
        bug_files: List[Path],
        runtime: float,
        worker_id: int,
    ) -> str:
        if self.strict_mode:
            self._persist_bug_files(worker_id, bug_files)
            if exit_code == self.EXIT_CODE_SUCCESS:
                return "requeue"
            with self.strict_exit_lock:
                if self.strict_exit_code == 0:
                    self.strict_exit_code = exit_code
            self.shutdown_event.set()
            return "continue"

        if exit_code == self.EXIT_CODE_BUGS_FOUND:
            if bug_files:
                print(
                    f"[WORKER {worker_id}] ✓ Exit code 10: Found "
                    f"{len(bug_files)} bug(s) on {test_name}"
                )
                self._persist_bug_files(worker_id, bug_files)
            else:
                print(
                    f"[WORKER {worker_id}] Warning: Exit code 10 but no bugs "
                    f"found for {test_name}",
                    file=sys.stderr,
                )
            return "requeue"

        if exit_code == self.EXIT_CODE_UNSUPPORTED:
            print(
                f"[WORKER {worker_id}] ⚠ Exit code 3: {test_name} "
                "(unsupported operation - removing)"
            )
            self._increment_stat("tests_removed_unsupported")
            return "remove"

        if exit_code == self.EXIT_CODE_SUCCESS:
            if not bug_files:
                print(
                    f"[WORKER {worker_id}] Exit code 0: No bugs found on "
                    f"{test_name} (runtime: {runtime:.1f}s) - requeuing for "
                    "next cycle"
                )
            else:
                print(
                    f"[WORKER {worker_id}] Exit code 0: {test_name} "
                    f"(runtime: {runtime:.1f}s) - bugs found, requeuing"
                )
            return "requeue"

        if exit_code == 124:
            print(
                f"[WORKER {worker_id}] ⚠ Harness timeout on {test_name} "
                f"after {runtime:.1f}s",
                file=sys.stderr,
            )
            self._increment_stat("tests_removed_timeout")
            return "remove"

        return "continue"

    def _worker_loop(self, worker_id: int) -> None:
        print(f"[WORKER {worker_id}] Started")

        while not self.shutdown_event.is_set():
            try:
                test_name = self.test_queue.get(timeout=1.0)
            except queue.Empty:
                if self._is_time_expired():
                    break
                continue

            if self._is_time_expired():
                self._safe_requeue(test_name)
                break

            time_remaining = self._get_time_remaining()
            timeout = time_remaining if self.time_remaining and time_remaining > 0 else None
            exit_code, bug_files, runtime = self._run_harness(
                test_name, worker_id, per_test_timeout=timeout
            )

            action = self._handle_exit_code(
                test_name, exit_code, bug_files, runtime, worker_id
            )

            if action == "requeue":
                self._safe_requeue(test_name)

            self._increment_stat("tests_processed")

        print(f"[WORKER {worker_id}] Stopped")

    def _safe_requeue(self, test_name: str) -> None:
        try:
            self.test_queue.put_nowait(test_name)
            self._increment_stat("tests_requeued")
        except Exception:
            pass

    def _get_time_remaining(self) -> float:
        if self.time_remaining is None:
            return float("inf")
        return max(0.0, self.time_remaining - (time.time() - self.start_time))

    def _is_time_expired(self) -> bool:
        return self.time_remaining is not None and self._get_time_remaining() <= 0

    def _collect_worker_bug_files(self) -> None:
        for worker_id in range(1, self.num_workers + 1):
            worker_bugs_folder = self.bugs_folder / f"worker_{worker_id}"
            for bug_file in self._collect_bug_files(worker_bugs_folder):
                try:
                    destination = self.bugs_folder / bug_file.name
                    if destination.exists():
                        timestamp = int(time.time())
                        destination = self.bugs_folder / (
                            f"{bug_file.stem}_{timestamp}{bug_file.suffix}"
                        )
                    shutil.move(str(bug_file), str(destination))
                except Exception:
                    pass

    def _final_summary(self) -> int:
        bug_files = self._collect_bug_files(self.bugs_folder)
        print()
        print("=" * 60)
        print(f"FINAL BUG SUMMARY{' FOR JOB ' + self.job_id if self.job_id else ''}")
        print("=" * 60)

        if bug_files:
            print(f"\nFound {len(bug_files)} bug(s):")
            for index, bug_file in enumerate(bug_files, 1):
                print(f"\nBug #{index}: {bug_file}")
                print("-" * 60)
                try:
                    with open(bug_file, "r", encoding="utf-8", errors="replace") as handle:
                        print(handle.read())
                except Exception as exc:
                    print(f"Error reading bug file: {exc}")
                print("-" * 60)
        else:
            print("No bugs found.")

        print()
        print("Statistics:")
        print(f"  Tests processed: {self.stats.get('tests_processed', 0)}")
        print(f"  Bugs found: {self.stats.get('bugs_found', 0)}")
        print(f"  Tests requeued (bugs found): {self.stats.get('tests_requeued', 0)}")
        print(
            f"  Tests removed (unsupported): "
            f"{self.stats.get('tests_removed_unsupported', 0)}"
        )
        print(
            f"  Tests removed (timeout): {self.stats.get('tests_removed_timeout', 0)}"
        )
        print("=" * 60)

        if self.strict_mode:
            return int(self.strict_exit_code)
        return 0

    def run(self) -> int:
        if not self.tests:
            print(f"No tests provided{' for job ' + self.job_id if self.job_id else ''}")
            return 0

        print(
            f"Running harness on {len(self.tests)} test(s)"
            f"{' for job ' + self.job_id if self.job_id else ''}"
        )
        print(f"Tests root: {self.tests_root}")
        print(
            "Timeout: "
            f"{self.time_remaining}s ({self.time_remaining // 60} minutes)"
            if self.time_remaining
            else "No timeout"
        )
        print(f"Iterations per test: {self.iterations}")
        print(f"Modulo: {self.modulo}")
        print(f"CPU cores: {self.cpu_count}")
        print(f"Workers: {self.num_workers}")
        print(f"Strict mode: {self.strict_mode}")
        print(
            "Targets: "
            + (
                ", ".join(shlex.join(command) for command in self.target_commands)
                if self.target_commands
                else "(none)"
            )
        )
        print(f"Harness template: {self.harness_template}")
        print()

        for test in self.tests:
            self.test_queue.put(test)

        workers = []
        for worker_id in range(1, self.num_workers + 1):
            worker = threading.Thread(
                target=self._worker_loop,
                args=(worker_id,),
                daemon=True,
            )
            worker.start()
            workers.append(worker)

        def signal_handler(signum, frame):  # noqa: ARG001
            print("\n⏰ Shutdown signal received, stopping workers...")
            self.shutdown_event.set()

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        try:
            if self.time_remaining:
                deadline = self.start_time + self.time_remaining
                while time.time() < deadline and any(worker.is_alive() for worker in workers):
                    time.sleep(1)
                if time.time() >= deadline:
                    print("⏰ Timeout reached, stopping workers...")
                    self.shutdown_event.set()
            else:
                while any(worker.is_alive() for worker in workers):
                    time.sleep(1)
        except KeyboardInterrupt:
            print("\n⏰ Interrupted, stopping workers...")
            self.shutdown_event.set()

        for worker in workers:
            worker.join(timeout=5)

        self._collect_worker_bug_files()
        return self._final_summary()


def _parse_targets(values: Optional[Sequence[str]]) -> List[str]:
    if not values:
        return []
    return [shlex.join(shlex.split(value)) for value in values if value.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generic commit harness runner for CI"
    )
    parser.add_argument(
        "--tests-json",
        required=True,
        help="JSON array of test names (relative to --tests-root)",
    )
    parser.add_argument(
        "--job-id",
        help="Job identifier (optional, for logging)",
    )
    parser.add_argument(
        "--tests-root",
        default="test/regress/cli",
        help="Root directory for tests (default: test/regress/cli)",
    )
    parser.add_argument(
        "--time-remaining",
        type=int,
        help=(
            "Remaining time until job timeout in seconds "
            "(legacy, use --job-start-time instead)"
        ),
    )
    parser.add_argument(
        "--job-start-time",
        type=float,
        help=(
            "Unix timestamp when the job started "
            "(for automatic time calculation)"
        ),
    )
    parser.add_argument(
        "--stop-buffer-minutes",
        type=int,
        default=5,
        help=(
            "Minutes before timeout to stop "
            "(default: 5, can be set higher for testing)"
        ),
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=250,
        help="Number of iterations per test (default: 250)",
    )
    parser.add_argument(
        "--modulo",
        type=int,
        default=2,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--targets",
        nargs="+",
        help=(
            "List of target identifiers for "
            "{target_args}/{target_clis} expansion. "
            "Each value is shell-split into argv tokens."
        ),
    )
    parser.add_argument(
        "--harness",
        required=True,
        help=(
            "Harness command template as JSON argv list. "
            "Supported placeholders: {test_path}, {iterations}, {modulo}, "
            "{bugs_dir}, {scratch_dir}, {logs}/{logs_dir}, "
            "{target_args}, {target_clis}."
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=_cpu_count(),
        help="Number of worker threads (default: CPU count)",
    )
    parser.add_argument(
        "--bugs-folder",
        default="bugs",
        help="Folder to store bugs (default: bugs)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Propagate the first non-zero harness exit code and stop early",
    )

    args = parser.parse_args()

    try:
        tests = json.loads(args.tests_json)
        if not isinstance(tests, list) or not all(isinstance(item, str) for item in tests):
            raise ValueError("tests-json must be a JSON array of strings")
    except json.JSONDecodeError as exc:
        print(f"Error: Invalid JSON in --tests-json: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        runner = CommitHarnessRunner(
            tests=tests,
            tests_root=args.tests_root,
            bugs_folder=args.bugs_folder,
            num_workers=args.workers,
            iterations=args.iterations,
            modulo=args.modulo,
            time_remaining=args.time_remaining,
            job_start_time=args.job_start_time,
            stop_buffer_minutes=args.stop_buffer_minutes,
            targets=_parse_targets(args.targets),
            harness=args.harness,
            job_id=args.job_id,
            strict_mode=args.strict,
        )
        return runner.run()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
