#!/usr/bin/env python3

from __future__ import annotations

import argparse
import gc
import json
import multiprocessing
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

try:
    import psutil
except Exception:  # pragma: no cover - optional dependency in lightweight test environments
    psutil = None  # type: ignore[assignment]


class SimpleCommitFuzzer:
    EXIT_CODE_BUGS_FOUND = 10
    EXIT_CODE_UNSUPPORTED = 3
    EXIT_CODE_SUCCESS = 0

    RESOURCE_CONFIG = {
        "cpu_warning": 85.0,
        "cpu_critical": 95.0,
        "memory_warning_available_gb": 2.0,
        "memory_critical_available_gb": 0.5,
        "check_interval": 2,
        "pause_duration": 10,
        "max_process_memory_mb": 2048,
        "max_process_memory_mb_warning": 1536,
    }

    def __init__(
        self,
        tests: List[str],
        tests_root: str,
        bugs_folder: str = "bugs",
        num_workers: int = 4,
        iterations: int = 250,
        modulo: int = 2,
        time_remaining: Optional[int] = None,
        job_start_time: Optional[float] = None,
        stop_buffer_minutes: int = 5,
        opensmt_path: str = "./build/bin/opensmt",
        cvc5_path: str = "cvc5",
        job_id: Optional[str] = None,
    ):
        self.tests = tests
        self.tests_root = Path(tests_root)
        self.bugs_folder = Path(bugs_folder)
        self.iterations = iterations
        self.modulo = modulo
        self.job_id = job_id
        self.start_time = time.time()
        self.opensmt_path = Path(opensmt_path)
        self.cvc5_path = Path(cvc5_path)

        if psutil is None:
            self.cpu_count = max(1, os.cpu_count() or 1)
        else:
            try:
                self.cpu_count = max(1, psutil.cpu_count() or 1)
            except Exception:
                self.cpu_count = max(1, os.cpu_count() or 1)

        self.num_workers = min(num_workers, self.cpu_count) if num_workers > 0 else self.cpu_count
        if num_workers > self.cpu_count:
            print(
                f"[WARN] Requested {num_workers} workers but only {self.cpu_count} CPU cores available, using {self.num_workers} workers",
                file=sys.stderr,
            )

        if job_start_time is not None:
            self.time_remaining = self._compute_time_remaining(job_start_time, stop_buffer_minutes)
            print(f"[DEBUG] Job start time: {job_start_time} ({time.ctime(job_start_time)})")
            print(f"[DEBUG] Script start time: {self.start_time} ({time.ctime(self.start_time)})")
            build_time = self.start_time - job_start_time
            print(f"[DEBUG] Build time: {build_time:.1f}s ({build_time / 60:.1f} minutes)")
            print(f"[DEBUG] Stop buffer: {stop_buffer_minutes} minutes")
            print(
                f"[DEBUG] Computed remaining time: {self.time_remaining}s ({self.time_remaining / 60:.1f} minutes)"
            )
        elif time_remaining is not None:
            self.time_remaining = time_remaining
            print(f"[DEBUG] Using provided time_remaining: {time_remaining}s ({time_remaining / 60:.1f} minutes)")
        else:
            self.time_remaining = None
            print("[DEBUG] No timeout set (running indefinitely)")

        self._validate_solvers()
        self.bugs_folder.mkdir(parents=True, exist_ok=True)

        self.test_queue = multiprocessing.Queue()
        self.bugs_lock = multiprocessing.Lock()
        self.shutdown_event = multiprocessing.Event()
        self.resource_lock = multiprocessing.Lock()
        self.resource_status = multiprocessing.Value("i", 0)
        self.resource_paused = multiprocessing.Value("b", False)
        self.stats = {
            "tests_processed": 0,
            "bugs_found": 0,
            "tests_removed_unsupported": 0,
            "tests_removed_timeout": 0,
            "tests_requeued": 0,
        }

    def _command_exists(self, command: Path) -> bool:
        return command.exists() or shutil.which(str(command)) is not None

    def _validate_solvers(self) -> None:
        if not self._command_exists(self.opensmt_path):
            raise ValueError(f"opensmt not found at: {self.opensmt_path}")
        if not self._command_exists(self.cvc5_path):
            raise ValueError(f"cvc5 not found at: {self.cvc5_path}")

    def _compute_time_remaining(self, job_start_time: float, stop_buffer_minutes: int) -> int:
        github_timeout = 3600
        min_remaining = 300
        build_time = self.start_time - job_start_time
        stop_buffer_seconds = stop_buffer_minutes * 60
        remaining = github_timeout - build_time - stop_buffer_seconds
        if remaining < min_remaining:
            print(
                f"[DEBUG] Computed remaining time ({remaining}s) is less than minimum ({min_remaining}s), using {min_remaining}s"
            )
            remaining = min_remaining
        return int(remaining)

    def _get_time_remaining(self) -> float:
        if self.time_remaining is None:
            return float("inf")
        return max(0.0, self.time_remaining - (time.time() - self.start_time))

    def _is_time_expired(self) -> bool:
        return self.time_remaining is not None and self._get_time_remaining() <= 0

    def _collect_bug_files(self, folder: Path) -> List[Path]:
        if not folder.exists():
            return []
        return list(folder.glob("*.smt2")) + list(folder.glob("*.smt"))

    def _get_solver_clis(self) -> str:
        return ";".join(
            [
                f"{self.cvc5_path} --check-models --check-proofs --strings-exp",
                str(self.opensmt_path),
            ]
        )

    def _get_all_descendant_pids(self, pid: int) -> set[int]:
        if psutil is None:
            return set()
        descendant_pids: set[int] = set()
        try:
            proc = psutil.Process(pid)
            for child in proc.children(recursive=True):
                try:
                    descendant_pids.add(child.pid)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        return descendant_pids

    def _kill_high_memory_processes(self, threshold_mb: Optional[float] = None) -> None:
        if psutil is None:
            return
        if threshold_mb is None:
            threshold_mb = self.RESOURCE_CONFIG["max_process_memory_mb"]

        tracked_pids = {os.getpid()}
        if hasattr(self, "workers"):
            for worker in self.workers:
                worker_pid = getattr(worker, "pid", None)
                if worker_pid:
                    tracked_pids.add(worker_pid)

        for pid in list(tracked_pids):
            tracked_pids.update(self._get_all_descendant_pids(pid))

        killed_count = 0
        for pid in tracked_pids:
            try:
                proc = psutil.Process(pid)
                rss_mb = proc.memory_info().rss / (1024 * 1024)
                if rss_mb > threshold_mb:
                    print(
                        f"[RESOURCE] Killing process {pid} ({proc.name()}) using {rss_mb:.1f}MB RAM (threshold: {threshold_mb}MB)",
                        file=sys.stderr,
                    )
                    proc.kill()
                    killed_count += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
                pass

        if killed_count > 0:
            print(
                f"[RESOURCE] Killed {killed_count} process(es) exceeding {threshold_mb}MB RAM threshold",
                file=sys.stderr,
            )

    def _handle_warning_resources(self) -> None:
        pass

    def _log_cpu_usage_by_process_type(self) -> None:
        if psutil is None:
            return
        try:
            main_pid = os.getpid()
            tracked_pids = {main_pid}
            if hasattr(self, "workers"):
                for worker in self.workers:
                    worker_pid = getattr(worker, "pid", None)
                    if worker_pid:
                        tracked_pids.add(worker_pid)
            for pid in list(tracked_pids):
                tracked_pids.update(self._get_all_descendant_pids(pid))

            totals = {"typefuzz": [0, 0.0], "opensmt": [0, 0.0], "cvc5": [0, 0.0], "python": [0, 0.0], "other": [0, 0.0]}
            for pid in tracked_pids:
                try:
                    proc = psutil.Process(pid)
                    info = proc.as_dict(["name", "memory_info", "cmdline"])
                    cmdline = " ".join(info.get("cmdline") or [])
                    name = (info.get("name") or "").lower()
                    rss_mb = info.get("memory_info").rss / (1024 * 1024) if info.get("memory_info") else 0.0
                    if "typefuzz" in cmdline.lower() or "typefuzz" in name:
                        bucket = "typefuzz"
                    elif "opensmt" in cmdline.lower() or "opensmt" in name:
                        bucket = "opensmt"
                    elif "cvc5" in cmdline.lower() or "cvc5" in name:
                        bucket = "cvc5"
                    elif "python" in cmdline.lower() or "python" in name:
                        bucket = "python"
                    else:
                        bucket = "other"
                    totals[bucket][0] += 1
                    totals[bucket][1] += rss_mb
                except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError, AttributeError):
                    pass

            memory = psutil.virtual_memory()
            print("[RESOURCE] CPU usage by process type:", file=sys.stderr)
            for proc_type, (count, mem_mb) in totals.items():
                if count > 0:
                    print(f"  {proc_type}: {count} process(es), {mem_mb:.1f} MB", file=sys.stderr)
            print(
                f"  System total: {memory.used / (1024**3):.2f} GB RAM used ({memory.percent:.1f}%)",
                file=sys.stderr,
            )
        except Exception as exc:
            print(f"[WARN] Error logging CPU usage by process type: {exc}", file=sys.stderr)

    def _log_bugs_summary_and_stop(self) -> None:
        print("\n" + "=" * 60, file=sys.stderr)
        print("CRITICAL RAM DETECTED - STOPPING TO PRESERVE BUGS", file=sys.stderr)
        print("=" * 60, file=sys.stderr)

        main_bugs = self._collect_bug_files(self.bugs_folder)
        main_bug_count = len(main_bugs)
        main_bugs_size_mb = self._calculate_folder_size_mb(self.bugs_folder)

        total_worker_bugs = 0
        worker_folders_info = []
        for worker_id in range(1, self.num_workers + 1):
            worker_bugs_folder = self.bugs_folder / f"worker_{worker_id}"
            worker_bugs = self._collect_bug_files(worker_bugs_folder)
            worker_bug_count = len(worker_bugs)
            total_worker_bugs += worker_bug_count
            bugs_size_mb = self._calculate_folder_size_mb(worker_bugs_folder)
            scratch_folder = Path(f"scratch_{worker_id}")
            scratch_size_mb = self._calculate_folder_size_mb(scratch_folder)
            log_folder = Path(f"logs_{worker_id}")
            log_size_mb = self._calculate_folder_size_mb(log_folder)
            worker_folders_info.append(
                {
                    "id": worker_id,
                    "bugs": worker_bug_count,
                    "bugs_size_mb": bugs_size_mb,
                    "scratch_size_mb": scratch_size_mb,
                    "log_size_mb": log_size_mb,
                    "total_size_mb": bugs_size_mb + scratch_size_mb + log_size_mb,
                }
            )

        total_bugs = main_bug_count + total_worker_bugs
        print("\nBUGS SUMMARY:", file=sys.stderr)
        print(f"  Total bugs found: {total_bugs}", file=sys.stderr)
        print(f"  Main bugs folder: {main_bug_count} bugs, {main_bugs_size_mb:.2f} MB disk space", file=sys.stderr)
        print("  Worker folders:", file=sys.stderr)
        for info in worker_folders_info:
            print(f"    worker_{info['id']}:", file=sys.stderr)
            print(f"      bugs: {info['bugs']} bugs, {info['bugs_size_mb']:.2f} MB disk space", file=sys.stderr)
            print(f"      scratch: {info['scratch_size_mb']:.2f} MB disk space", file=sys.stderr)
            print(f"      logs: {info['log_size_mb']:.2f} MB disk space", file=sys.stderr)
            print(f"      total: {info['total_size_mb']:.2f} MB disk space", file=sys.stderr)

        print("\nSTATISTICS:", file=sys.stderr)
        print(f"  Tests processed: {self.stats.get('tests_processed', 0)}", file=sys.stderr)
        print(f"  Bugs found: {self.stats.get('bugs_found', 0)}", file=sys.stderr)
        print(f"  Tests requeued (bugs found): {self.stats.get('tests_requeued', 0)}", file=sys.stderr)
        print(f"  Tests removed (unsupported): {self.stats.get('tests_removed_unsupported', 0)}", file=sys.stderr)
        print(f"  Tests removed (timeout): {self.stats.get('tests_removed_timeout', 0)}", file=sys.stderr)

        print("\n" + "=" * 60, file=sys.stderr)
        print("Stopping fuzzer to preserve found bugs...", file=sys.stderr)
        print("=" * 60 + "\n", file=sys.stderr)
        self.shutdown_event.set()

    def _handle_critical_resources(
        self,
        cpu_percent: List[float],
        max_cpu: float,
        avg_cpu: float,
        memory_percent: float,
        memory_available_gb: float,
        memory_total: int,
        memory_used: int,
    ) -> None:
        try:
            memory_total_gb = memory_total / (1024**3)
            memory_used_gb = memory_used / (1024**3)
            print(
                f"[RESOURCE] Critical resource usage detected - CPU: {avg_cpu:.1f}% avg, {max_cpu:.1f}% max, RAM: {memory_available_gb:.2f}GB available ({memory_percent:.1f}% used, {memory_used_gb:.2f}GB / {memory_total_gb:.2f}GB) - taking action",
                file=sys.stderr,
            )
            self._log_cpu_usage_by_process_type()
        except Exception as exc:
            print(
                f"[RESOURCE] Critical resource usage detected - CPU: {avg_cpu:.1f}% avg, RAM available: {memory_available_gb:.2f}GB - taking action (error formatting details: {exc})",
                file=sys.stderr,
            )

        if memory_available_gb < self.RESOURCE_CONFIG["memory_critical_available_gb"]:
            self._log_bugs_summary_and_stop()
            return

        with self.resource_lock:
            self.resource_paused.value = True

        try:
            gc.collect()
        except Exception:
            pass

        time.sleep(self.RESOURCE_CONFIG["pause_duration"])

        with self.resource_lock:
            self.resource_paused.value = False

    def _calculate_folder_size_mb(self, folder_path: Path) -> float:
        try:
            if folder_path.exists():
                size_bytes = sum(path.stat().st_size for path in folder_path.rglob("*") if path.is_file())
                return size_bytes / (1024 * 1024)
        except Exception:
            pass
        return 0.0

    def _check_resource_state(self) -> str:
        with self.resource_lock:
            status_value = self.resource_status.value
        return {0: "normal", 1: "warning", 2: "critical"}.get(status_value, "normal")

    def _is_paused(self) -> bool:
        with self.resource_lock:
            return bool(self.resource_paused.value)

    def _monitor_resources(self) -> None:
        if psutil is None:
            print("[WARN] psutil not available, skipping resource monitoring", file=sys.stderr)
            return
        while not self.shutdown_event.is_set():
            try:
                cpu_percent = psutil.cpu_percent(interval=1, percpu=True)
                memory = psutil.virtual_memory()
                memory_available_gb = memory.available / (1024**3)
                max_cpu = max(cpu_percent) if cpu_percent else 0.0
                avg_cpu = sum(cpu_percent) / len(cpu_percent) if cpu_percent else 0.0
                status = "normal"
                if avg_cpu >= self.RESOURCE_CONFIG["cpu_critical"] or memory_available_gb < self.RESOURCE_CONFIG["memory_critical_available_gb"]:
                    status = "critical"
                elif avg_cpu >= self.RESOURCE_CONFIG["cpu_warning"] or memory_available_gb < self.RESOURCE_CONFIG["memory_warning_available_gb"]:
                    status = "warning"

                with self.resource_lock:
                    self.resource_status.value = {"normal": 0, "warning": 1, "critical": 2}[status]

                threshold = (
                    self.RESOURCE_CONFIG["max_process_memory_mb_warning"]
                    if memory_available_gb < self.RESOURCE_CONFIG["memory_warning_available_gb"]
                    else self.RESOURCE_CONFIG["max_process_memory_mb"]
                )
                self._kill_high_memory_processes(threshold_mb=threshold)

                if status == "critical":
                    self._handle_critical_resources(
                        cpu_percent,
                        max_cpu,
                        avg_cpu,
                        memory.percent,
                        memory_available_gb,
                        memory.total,
                        memory.used,
                    )
                elif status == "warning":
                    self._handle_warning_resources()
                    self._log_cpu_usage_by_process_type()
            except (ImportError, AttributeError) as exc:
                print(f"[WARN] psutil not available, skipping resource monitoring: {exc}", file=sys.stderr)
                break
            except Exception as exc:
                print(f"[WARN] Error in resource monitoring: {exc}", file=sys.stderr)
                time.sleep(self.RESOURCE_CONFIG["check_interval"])
                continue

            time.sleep(self.RESOURCE_CONFIG["check_interval"])

    def _run_typefuzz(
        self,
        test_name: str,
        worker_id: int,
        per_test_timeout: Optional[float] = None,
    ) -> Tuple[int, List[Path], float]:
        test_path = self.tests_root / test_name
        if not test_path.exists():
            print(f"[WORKER {worker_id}] Error: Test file not found: {test_path}", file=sys.stderr)
            return (1, [], 0.0)

        bugs_folder = self.bugs_folder / f"worker_{worker_id}"
        scratch_folder = Path(f"scratch_{worker_id}")
        log_folder = Path(f"logs_{worker_id}")
        for folder in (scratch_folder, log_folder):
            shutil.rmtree(folder, ignore_errors=True)
            folder.mkdir(parents=True, exist_ok=True)
        bugs_folder.mkdir(parents=True, exist_ok=True)

        cmd = [
            "typefuzz",
            "-i",
            str(self.iterations),
            "-m",
            str(self.modulo),
            "--timeout",
            "120",
            "--bugs",
            str(bugs_folder),
            "--scratch",
            str(scratch_folder),
            "--logfolder",
            str(log_folder),
            self._get_solver_clis(),
            str(test_path),
        ]

        print(
            f"[WORKER {worker_id}] Running typefuzz on: {test_name} (timeout: {per_test_timeout}s)"
            if per_test_timeout
            else f"[WORKER {worker_id}] Running typefuzz on: {test_name}"
        )

        start_time = time.time()
        try:
            if per_test_timeout and per_test_timeout > 0:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=per_test_timeout)
            else:
                result = subprocess.run(cmd, capture_output=True, text=True)

            if result.returncode != 0:
                print(f"✗ typefuzz failed (exit code {result.returncode})", end=" ", flush=True)
                if result.stderr:
                    first_line = result.stderr.strip().split("\n")[0]
                    if first_line:
                        print(f"- {first_line[:100]}")
                    else:
                        print("")
                else:
                    print("")

            bug_files = self._collect_bug_files(bugs_folder)
            return (result.returncode, bug_files, time.time() - start_time)
        except subprocess.TimeoutExpired:
            return (124, [], time.time() - start_time)
        except FileNotFoundError:
            print(f"✗ typefuzz command not found", file=sys.stderr)
            return (1, [], time.time() - start_time)
        except Exception as exc:
            print(f"✗ Error running typefuzz: {exc}", file=sys.stderr)
            return (1, [], time.time() - start_time)
        finally:
            for folder in (scratch_folder, log_folder):
                shutil.rmtree(folder, ignore_errors=True)

    def _handle_exit_code(
        self,
        test_name: str,
        exit_code: int,
        bug_files: List[Path],
        runtime: float,
        worker_id: int,
    ) -> str:
        if exit_code == self.EXIT_CODE_BUGS_FOUND:
            if bug_files:
                print(f"[WORKER {worker_id}] ✓ Exit code 10: Found {len(bug_files)} bug(s) on {test_name}")
                with self.bugs_lock:
                    for bug_file in bug_files:
                        try:
                            dest = self.bugs_folder / bug_file.name
                            if dest.exists():
                                timestamp = int(time.time())
                                dest = self.bugs_folder / f"{bug_file.stem}_{timestamp}{bug_file.suffix}"
                            shutil.move(str(bug_file), str(dest))
                            self.stats["bugs_found"] += 1
                        except Exception as exc:
                            print(f"[WORKER {worker_id}] Warning: Failed to move bug file {bug_file}: {exc}", file=sys.stderr)
            else:
                print(f"[WORKER {worker_id}] Warning: Exit code 10 but no bugs found for {test_name}", file=sys.stderr)
            return "requeue"

        if exit_code == self.EXIT_CODE_UNSUPPORTED:
            print(f"[WORKER {worker_id}] ⚠ Exit code 3: {test_name} (unsupported operation - removing)")
            self.stats["tests_removed_unsupported"] += 1
            return "remove"

        if exit_code == 124:
            print(f"[WORKER {worker_id}] ⚠ Exit code 124: {test_name} (timeout - removing)")
            self.stats["tests_removed_timeout"] += 1
            return "remove"

        if exit_code == self.EXIT_CODE_SUCCESS:
            if not bug_files:
                print(f"[WORKER {worker_id}] Exit code 0: No bugs found on {test_name} (runtime: {runtime:.1f}s) - requeuing for next cycle")
            else:
                print(f"[WORKER {worker_id}] Exit code 0: {test_name} (runtime: {runtime:.1f}s) - bugs found, requeuing")
            return "requeue"

        return "continue"

    def _worker_process(self, worker_id: int) -> None:
        print(f"[WORKER {worker_id}] Started")
        while not self.shutdown_event.is_set():
            try:
                if self._is_paused():
                    resource_status = self._check_resource_state()
                    print(f"[WORKER {worker_id}] Paused due to {resource_status} resource usage", file=sys.stderr)
                    time.sleep(self.RESOURCE_CONFIG["pause_duration"])
                    continue

                try:
                    test_name = self.test_queue.get(timeout=1.0)
                except Exception:
                    if self.shutdown_event.is_set() or self._is_time_expired() or self.test_queue.empty():
                        break
                    continue

                if self._is_time_expired():
                    try:
                        self.test_queue.put(test_name)
                    except Exception:
                        pass
                    break

                resource_status = self._check_resource_state()
                if resource_status == "warning":
                    time.sleep(2)
                elif resource_status == "critical":
                    try:
                        self.test_queue.put(test_name)
                    except Exception:
                        pass
                    time.sleep(self.RESOURCE_CONFIG["pause_duration"])
                    continue

                time_remaining = self._get_time_remaining()
                exit_code, bug_files, runtime = self._run_typefuzz(
                    test_name,
                    worker_id,
                    per_test_timeout=time_remaining if self.time_remaining and time_remaining > 0 else None,
                )

                action = self._handle_exit_code(test_name, exit_code, bug_files, runtime, worker_id)
                if action == "requeue":
                    try:
                        self.test_queue.put(test_name)
                        self.stats["tests_requeued"] += 1
                    except Exception:
                        pass

                self.stats["tests_processed"] += 1
            except Exception as exc:
                print(f"[WORKER {worker_id}] Error in worker: {exc}", file=sys.stderr)
                continue

        print(f"[WORKER {worker_id}] Stopped")

    def _move_worker_bug_files(self) -> None:
        for worker_id in range(1, self.num_workers + 1):
            worker_bugs = self.bugs_folder / f"worker_{worker_id}"
            for bug_file in self._collect_bug_files(worker_bugs):
                try:
                    dest = self.bugs_folder / bug_file.name
                    if dest.exists():
                        timestamp = int(time.time())
                        dest = self.bugs_folder / f"{bug_file.stem}_{timestamp}{bug_file.suffix}"
                    shutil.move(str(bug_file), str(dest))
                except Exception:
                    pass

    def run(self) -> int:
        if not self.tests:
            print(f"No tests provided{' for job ' + self.job_id if self.job_id else ''}")
            return 0

        print(f"Running fuzzer on {len(self.tests)} test(s){' for job ' + self.job_id if self.job_id else ''}")
        print(f"Tests root: {self.tests_root}")
        print(f"Timeout: {self.time_remaining}s ({self.time_remaining // 60} minutes)" if self.time_remaining else "No timeout")
        print(f"Iterations per test: {self.iterations}, Modulo: {self.modulo}")
        print(f"CPU cores: {self.cpu_count}")
        print(f"Workers: {self.num_workers}")
        print(
            f"Solvers: cvc5={self.cvc5_path} --check-models --check-proofs --strings-exp, opensmt={self.opensmt_path}"
        )
        print()

        for test in self.tests:
            self.test_queue.put(test)

        workers = []
        for worker_id in range(1, self.num_workers + 1):
            worker = multiprocessing.Process(target=self._worker_process, args=(worker_id,))
            worker.start()
            workers.append(worker)
        self.workers = workers

        monitor_thread = threading.Thread(target=self._monitor_resources, daemon=True)
        monitor_thread.start()
        print("[DEBUG] Resource monitoring started")

        def signal_handler(signum, frame):  # noqa: ARG001
            print("\n⏰ Shutdown signal received, stopping workers...")
            self.shutdown_event.set()

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        try:
            if self.time_remaining:
                end_time = self.start_time + self.time_remaining
                while time.time() < end_time and any(worker.is_alive() for worker in workers):
                    time.sleep(1)
                if time.time() >= end_time:
                    print("⏰ Timeout reached, stopping workers...")
                    self.shutdown_event.set()
            else:
                for worker in workers:
                    worker.join()
        except KeyboardInterrupt:
            print("\n⏰ Interrupted, stopping workers...")
            self.shutdown_event.set()
        finally:
            for worker in workers:
                worker.join(timeout=5)
                if worker.is_alive():
                    worker_pid = getattr(worker, "pid", "unknown")
                    print(f"Warning: Worker {worker_pid} did not terminate, killing...")
                    worker.terminate()
                    worker.join(timeout=2)
                    if worker.is_alive():
                        worker.kill()

            self.shutdown_event.set()
            self._move_worker_bug_files()

            print()
            print("=" * 60)
            print(f"FINAL BUG SUMMARY{' FOR JOB ' + self.job_id if self.job_id else ''}")
            print("=" * 60)

            bug_files = self._collect_bug_files(self.bugs_folder)
            if bug_files:
                print(f"\nFound {len(bug_files)} bug(s):")
                for index, bug_file in enumerate(bug_files, 1):
                    print(f"\nBug #{index}: {bug_file}")
                    print("-" * 60)
                    try:
                        with open(bug_file, "r", encoding="utf-8", errors="ignore") as handle:
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
            print(f"  Tests removed (unsupported): {self.stats.get('tests_removed_unsupported', 0)}")
            print(f"  Tests removed (timeout): {self.stats.get('tests_removed_timeout', 0)}")
            print("=" * 60)

        return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Simple commit fuzzer that runs typefuzz on OpenSMT tests"
    )
    parser.add_argument(
        "--tests-json",
        required=True,
        help="JSON array of test names (relative to --tests-root)",
    )
    parser.add_argument("--job-id", help="Job identifier (optional, for logging)")
    parser.add_argument(
        "--tests-root",
        default="test/regression",
        help="Root directory for tests (default: test/regression)",
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
        "--opensmt-path",
        default="./build/bin/opensmt",
        help="Path to the OpenSMT binary (default: ./build/bin/opensmt)",
    )
    parser.add_argument(
        "--cvc5-path",
        default="cvc5",
        help="Path to cvc5 binary (default: cvc5)",
    )
    try:
        default_workers = psutil.cpu_count() or 4
    except Exception:
        default_workers = 4
    parser.add_argument(
        "--workers",
        type=int,
        default=default_workers,
        help=f"Number of worker processes (default: {default_workers}, auto-detected from CPU cores). Each worker runs typefuzz with 2 solvers",
    )
    parser.add_argument(
        "--bugs-folder",
        default="bugs",
        help="Folder to store bugs (default: bugs)",
    )

    args = parser.parse_args()

    try:
        tests = json.loads(args.tests_json)
        if not isinstance(tests, list):
            raise ValueError("tests-json must be a JSON array")
        if not all(isinstance(item, str) for item in tests):
            raise ValueError("tests-json must be a JSON array of strings")
    except json.JSONDecodeError as exc:
        print(f"Error: Invalid JSON in --tests-json: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        fuzzer = SimpleCommitFuzzer(
            tests=tests,
            tests_root=args.tests_root,
            bugs_folder=args.bugs_folder,
            num_workers=args.workers,
            iterations=args.iterations,
            modulo=args.modulo,
            time_remaining=args.time_remaining,
            job_start_time=args.job_start_time,
            stop_buffer_minutes=args.stop_buffer_minutes,
            opensmt_path=args.opensmt_path,
            cvc5_path=args.cvc5_path,
            job_id=args.job_id,
        )
        fuzzer.run()
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
