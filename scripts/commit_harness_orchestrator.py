#!/usr/bin/env python3
"""Run a generic harness command across tests with worker orchestration."""

from __future__ import annotations

import argparse
import json
import math
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
    try:
        count = os.cpu_count()
        if count:
            return max(1, int(count))
    except Exception:
        pass
    return 1


class CommitHarnessRunner:
    """Coordinate worker threads, harness execution, and bug persistence."""

    EXIT_CODE_SUCCESS = 0
    EXIT_CODE_BUGS_FOUND = 10
    EXIT_CODE_UNSUPPORTED = 3

    def __init__(
        self,
        tests: Sequence[str],
        tests_root: str,
        bugs_folder: str,
        num_workers: int,
        iterations: int,
        modulo: int,
        time_remaining: Optional[int],
        job_start_time: Optional[float],
        stop_buffer_minutes: int,
        targets: Optional[Sequence[str]],
        harness: Optional[Sequence[str] | str],
        job_id: Optional[str] = None,
        strict_mode: bool = False,
    ) -> None:
        self.tests = list(tests)
        self.tests_root = Path(tests_root)
        self.bugs_folder = Path(bugs_folder)
        self.iterations = iterations
        self.modulo = modulo
        self.job_id = job_id
        self.start_time = time.time()
        self.cpu_count = _cpu_count()
        self.num_workers = self._resolve_num_workers(num_workers)
        self.time_remaining = self._resolve_time_remaining(
            time_remaining=time_remaining,
            job_start_time=job_start_time,
            stop_buffer_minutes=stop_buffer_minutes,
        )
        self.target_commands = self._build_target_commands(targets)
        self.harness_template = self._parse_harness_template(harness)
        self.strict_mode = strict_mode

        self.bugs_folder.mkdir(parents=True, exist_ok=True)

        self.test_queue: queue.Queue[str] = queue.Queue()
        self.shutdown_event = threading.Event()
        self.bugs_lock = threading.Lock()
        self.stats_lock = threading.Lock()
        self.strict_exit_lock = threading.Lock()

        self.strict_exit_code: Optional[int] = None
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
        return max(1, min(num_workers, self.cpu_count))

    def _resolve_time_remaining(
        self,
        time_remaining: Optional[int],
        job_start_time: Optional[float],
        stop_buffer_minutes: int,
    ) -> Optional[int]:
        if job_start_time is not None:
            return self._compute_time_remaining(job_start_time, stop_buffer_minutes)
        if time_remaining is not None:
            return time_remaining
        return None

    def _compute_time_remaining(self, job_start_time: float, stop_buffer_minutes: int) -> int:
        github_timeout = 3600
        minimum_remaining = 300

        build_time = self.start_time - job_start_time
        stop_buffer_seconds = stop_buffer_minutes * 60
        available_time = github_timeout - build_time
        remaining = int(available_time - stop_buffer_seconds)

        if remaining < minimum_remaining:
            remaining = minimum_remaining
        return remaining

    def _get_time_remaining(self) -> float:
        if self.time_remaining is None:
            return float("inf")
        return max(0.0, self.time_remaining - (time.time() - self.start_time))

    def _is_time_expired(self) -> bool:
        return self.time_remaining is not None and self._get_time_remaining() <= 0

    def _resolve_target_command(self, identifier: str | Sequence[str]) -> str:
        if isinstance(identifier, str):
            value = identifier.strip()
            if not value:
                raise ValueError("Target identifier cannot be empty")
            parsed = shlex.split(value)
        else:
            parsed = [str(part).strip() for part in identifier if str(part).strip()]
            if not parsed:
                raise ValueError("Target identifier cannot be empty")

        if not parsed:
            raise ValueError("Target identifier resolved to empty argv")
        return shlex.join(parsed)

    def _build_target_commands(self, target_identifiers: Optional[Sequence[str]]) -> List[str]:
        if not target_identifiers:
            raise ValueError("At least one target identifier must be provided")
        return [self._resolve_target_command(identifier) for identifier in target_identifiers]

    def _parse_harness_template(
        self,
        harness: Optional[Sequence[str] | str],
    ) -> List[str]:
        if harness is None:
            raise ValueError("Harness template is required")

        if isinstance(harness, str):
            try:
                parsed = json.loads(harness)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON in --harness: {exc}") from exc
        else:
            parsed = list(harness)

        if not isinstance(parsed, list) or not parsed or not all(isinstance(item, str) for item in parsed):
            raise ValueError("--harness must be a JSON array of argv strings")

        return list(parsed)

    def _render_harness_command(
        self,
        test_path: Path,
        bugs_folder: Path,
        scratch_folder: Path,
        logs_folder: Path,
    ) -> List[str]:
        target_clis = ";".join(self.target_commands)
        context = {
            "test_path": str(test_path),
            "iterations": str(self.iterations),
            "modulo": str(self.modulo),
            "bugs_dir": str(bugs_folder),
            "scratch_dir": str(scratch_folder),
            "logs": str(logs_folder),
            "logs_dir": str(logs_folder),
            "target_args": target_clis,
            "target_clis": target_clis,
        }

        command: List[str] = []
        for token in self.harness_template:
            try:
                rendered = token.format(**context)
            except KeyError as exc:
                supported = "{test_path}, {iterations}, {modulo}, {bugs_dir}, {scratch_dir}, {logs}/{logs_dir}, {target_args}, {target_clis}"
                raise ValueError(
                    f"Unknown placeholder in --harness template: {exc.args[0]}. Supported: {supported}"
                ) from exc
            if rendered:
                command.append(rendered)

        if not command:
            raise ValueError("Harness command cannot be empty")
        return command

    def _worker_temp_dirs(self, worker_id: int) -> tuple[Path, Path, Path]:
        scratch_folder = Path(f"scratch_{worker_id}")
        logs_folder = Path(f"logs_{worker_id}")
        bugs_folder = self.bugs_folder / f"worker_{worker_id}"

        for folder in (scratch_folder, logs_folder):
            shutil.rmtree(folder, ignore_errors=True)
            folder.mkdir(parents=True, exist_ok=True)
        bugs_folder.mkdir(parents=True, exist_ok=True)

        return scratch_folder, logs_folder, bugs_folder

    def _collect_bug_files(self, folder: Path) -> List[Path]:
        if not folder.exists():
            return []
        return sorted(
            list(folder.glob("*.smt2")) + list(folder.glob("*.smt"))
        )

    def _calculate_folder_size_mb(self, folder: Path) -> float:
        if not folder.exists():
            return 0.0
        size_bytes = sum(file.stat().st_size for file in folder.rglob("*") if file.is_file())
        return size_bytes / (1024 * 1024)

    def _increment_stat(self, key: str, amount: int = 1) -> None:
        with self.stats_lock:
            self.stats[key] = self.stats.get(key, 0) + amount

    def _persist_bug_files(self, worker_id: int, bug_files: Sequence[Path]) -> None:
        if not bug_files:
            return

        worker_bug_dir = self.bugs_folder / f"worker_{worker_id}"
        worker_bug_dir.mkdir(parents=True, exist_ok=True)
        for bug_file in bug_files:
            try:
                destination = self.bugs_folder / bug_file.name
                if destination.exists():
                    timestamp = int(time.time())
                    destination = self.bugs_folder / f"{bug_file.stem}_{timestamp}{bug_file.suffix}"
                shutil.move(str(bug_file), str(destination))
                self._increment_stat("bugs_found")
            except Exception as exc:
                print(f"[WORKER {worker_id}] Warning: Failed to move bug file {bug_file}: {exc}", file=sys.stderr)

    def _run_harness(
        self,
        test_name: str,
        worker_id: int,
        per_test_timeout: Optional[float] = None,
    ) -> tuple[int, List[Path], float]:
        test_path = self.tests_root / test_name
        if not test_path.exists():
            print(f"[WORKER {worker_id}] Error: Test file not found: {test_path}", file=sys.stderr)
            return (1, [], 0.0)

        scratch_folder, logs_folder, bugs_folder = self._worker_temp_dirs(worker_id)
        command = self._render_harness_command(test_path, bugs_folder, scratch_folder, logs_folder)
        start_time = time.time()

        try:
            if per_test_timeout is not None and math.isfinite(per_test_timeout) and per_test_timeout > 0:
                result = subprocess.run(command, capture_output=True, text=True, timeout=per_test_timeout)
            else:
                result = subprocess.run(command, capture_output=True, text=True)

            exit_code = result.returncode
            runtime = time.time() - start_time
            bug_files = self._collect_bug_files(bugs_folder)
            return (exit_code, bug_files, runtime)
        except subprocess.TimeoutExpired:
            runtime = time.time() - start_time
            return (124, [], runtime)
        except Exception as exc:
            runtime = time.time() - start_time
            print(f"[WORKER {worker_id}] Error running harness: {exc}", file=sys.stderr)
            return (1, [], runtime)
        finally:
            for folder in (scratch_folder, logs_folder):
                shutil.rmtree(folder, ignore_errors=True)

    def _handle_exit_code(
        self,
        test_name: str,
        exit_code: int,
        bug_files: Sequence[Path],
        runtime: float,
        worker_id: int,
    ) -> str:
        if exit_code == self.EXIT_CODE_BUGS_FOUND:
            if bug_files:
                print(f"[WORKER {worker_id}] ✓ Exit code 10: Found {len(bug_files)} bug(s) on {test_name}")
                self._persist_bug_files(worker_id, bug_files)
            else:
                print(f"[WORKER {worker_id}] Warning: Exit code 10 but no bugs found for {test_name}", file=sys.stderr)
            return "requeue"

        if exit_code == self.EXIT_CODE_UNSUPPORTED:
            print(f"[WORKER {worker_id}] ⏭️ {test_name} - unsupported command set", file=sys.stderr)
            self._increment_stat("tests_removed_unsupported")
            return "drop"

        if exit_code == 124:
            print(f"[WORKER {worker_id}] ⏱️ {test_name} - timeout after {runtime:.1f}s", file=sys.stderr)
            self._increment_stat("tests_removed_timeout")
            return "drop"

        if exit_code != self.EXIT_CODE_SUCCESS:
            print(f"[WORKER {worker_id}] Warning: harness exit code {exit_code} on {test_name}", file=sys.stderr)
            if self.strict_mode:
                with self.strict_exit_lock:
                    if self.strict_exit_code is None:
                        self.strict_exit_code = exit_code
                self.shutdown_event.set()
                return "stop"

        print(
            f"[WORKER {worker_id}] Exit code {exit_code}: No bugs found on {test_name} "
            f"(runtime: {runtime:.1f}s) - requeuing for next cycle"
        )
        return "requeue"

    def _safe_requeue(self, test_name: str) -> None:
        if self.shutdown_event.is_set() or self._is_time_expired():
            return
        self.test_queue.put(test_name)
        self._increment_stat("tests_requeued")

    def _worker_loop(self, worker_id: int) -> None:
        while not self.shutdown_event.is_set():
            if self._is_time_expired():
                break

            try:
                test_name = self.test_queue.get(timeout=0.5)
            except queue.Empty:
                break

            try:
                per_test_timeout = self._get_time_remaining()
                exit_code, bug_files, runtime = self._run_harness(test_name, worker_id, per_test_timeout)
                self._increment_stat("tests_processed")
                action = self._handle_exit_code(test_name, exit_code, bug_files, runtime, worker_id)

                if action == "requeue":
                    self._safe_requeue(test_name)
                elif action == "stop":
                    break
            finally:
                self.test_queue.task_done()

    def _collect_worker_bug_files(self, worker_id: int) -> List[Path]:
        worker_bugs_folder = self.bugs_folder / f"worker_{worker_id}"
        if not worker_bugs_folder.exists():
            return []

        collected: List[Path] = []
        for bug_file in self._collect_bug_files(worker_bugs_folder):
            try:
                destination = self.bugs_folder / bug_file.name
                if destination.exists():
                    timestamp = int(time.time())
                    destination = self.bugs_folder / f"{bug_file.stem}_{timestamp}{bug_file.suffix}"
                shutil.move(str(bug_file), str(destination))
                collected.append(destination)
            except Exception as exc:
                print(f"[WORKER {worker_id}] Warning: Failed to collect bug file {bug_file}: {exc}", file=sys.stderr)
        return collected

    def _final_summary(self) -> None:
        print()
        print("Statistics:")
        print(f"  Tests processed: {self.stats.get('tests_processed', 0)}")
        print(f"  Bugs found: {self.stats.get('bugs_found', 0)}")
        print(f"  Tests requeued (bugs found): {self.stats.get('tests_requeued', 0)}")
        print(f"  Tests removed (unsupported): {self.stats.get('tests_removed_unsupported', 0)}")
        print(f"  Tests removed (timeout): {self.stats.get('tests_removed_timeout', 0)}")
        print("=" * 60)

    def run(self) -> int:
        if not self.tests:
            print("No tests provided")
            return self.strict_exit_code or self.EXIT_CODE_SUCCESS

        for test in self.tests:
            self.test_queue.put(test)

        def signal_handler(signum: int, frame: object) -> None:  # noqa: ARG001
            print("\n⏰ Shutdown signal received, stopping workers...")
            self.shutdown_event.set()

        previous_int = None
        previous_term = None
        try:
            previous_int = signal.signal(signal.SIGINT, signal_handler)
            previous_term = signal.signal(signal.SIGTERM, signal_handler)
        except ValueError:
            # Not running in the main thread; continue without signal handling.
            previous_int = previous_term = None

        workers: List[threading.Thread] = []
        try:
            for worker_id in range(1, self.num_workers + 1):
                worker = threading.Thread(target=self._worker_loop, args=(worker_id,), daemon=True)
                worker.start()
                workers.append(worker)

            while any(worker.is_alive() for worker in workers):
                if self._is_time_expired():
                    self.shutdown_event.set()
                time.sleep(0.5)

            for worker in workers:
                worker.join()
        finally:
            if previous_int is not None:
                signal.signal(signal.SIGINT, previous_int)
            if previous_term is not None:
                signal.signal(signal.SIGTERM, previous_term)

        for worker_id in range(1, self.num_workers + 1):
            self._collect_worker_bug_files(worker_id)

        self._final_summary()
        return self.strict_exit_code or self.EXIT_CODE_SUCCESS


def _parse_targets(values: Optional[Sequence[str]]) -> List[str]:
    if not values:
        return []
    return [shlex.join(shlex.split(value.strip())) for value in values if value.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Generic commit harness runner")
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
        help="Remaining time until job timeout in seconds (legacy, use --job-start-time instead)",
    )
    parser.add_argument(
        "--job-start-time",
        type=float,
        help="Unix timestamp when the job started (for automatic time calculation)",
    )
    parser.add_argument(
        "--stop-buffer-minutes",
        type=int,
        default=5,
        help="Minutes before timeout to stop (default: 5, can be set higher for testing)",
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
        help="Modulo parameter for typefuzz -m flag (default: 2)",
    )
    parser.add_argument(
        "--targets",
        nargs="+",
        help="List of target identifiers for {target_args}/{target_clis} expansion. Each value is shell-split into argv tokens.",
    )
    parser.add_argument(
        "--harness",
        required=True,
        help="Harness command template as JSON argv list. Supported placeholders: {test_path}, {iterations}, {modulo}, {bugs_dir}, {scratch_dir}, {logs}/{logs_dir}, {target_args}, {target_clis}.",
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
    except json.JSONDecodeError as exc:
        print(f"Error: Invalid JSON in --tests-json: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
