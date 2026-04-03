#!/usr/bin/env python3
"""Run a generic harness command across tests with worker orchestration."""

import argparse
import contextlib
import gc
import json
import multiprocessing
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import psutil
except Exception:  # pragma: no cover - lightweight environments may omit psutil
    psutil = None  # type: ignore[assignment]


class CommitHarnessRunner:
    """Coordinate worker processes, harness execution, and bug persistence."""

    EXIT_CODE_BUGS_FOUND = 10
    EXIT_CODE_UNSUPPORTED = 3
    EXIT_CODE_SUCCESS = 0

    RESOURCE_CONFIG = {
        'cpu_critical': 95.0,
        # Warning if less than 2GB available.
        'memory_warning_available_gb': 2.0,
        # Critical if less than 500MB available (real low memory)
        'memory_critical_available_gb': 0.5,
        'check_interval': 2,  # Check every 2 seconds
        'pause_duration': 10,
        # Kill processes exceeding 2GB (normal operation) - allows normal
        # target usage but catches runaway processes
        'max_process_memory_mb': 2048,
        # Stricter threshold (1.5GB) when system memory is low
        'max_process_memory_mb_warning': 1536,
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
        targets: Optional[List[str]] = None,
        harness: Optional[str] = None,
        job_id: Optional[str] = None,
        strict_mode: bool = False,
    ):
        """Initialize runtime configuration and multiprocessing state."""
        self.tests = tests
        self.tests_root = Path(tests_root)
        self.bugs_folder = Path(bugs_folder)
        self.iterations = iterations
        self.modulo = modulo
        self.job_id = job_id
        self.strict_mode = strict_mode
        self.start_time = time.time()
        self.cpu_count = self._cpu_count()

        # Set self.num_workers
        self.num_workers = self._init_determine_num_workers(num_workers)
        # Set self.time_remaining
        self.time_remaining = self._init_determine_time_remaining(
            job_start_time, stop_buffer_minutes, time_remaining)

        # Resolve target identifiers for both {target_args} (tokenized) and
        # {target_clis} (single argument, semicolon-separated) expansion.
        self.target_commands = self._build_target_commands(targets)
        self.target_args = [
            arg for command in self.target_commands for arg in command]
        self.target_clis = ";".join(shlex.join(command)
                                    for command in self.target_commands)
        self.target_binaries = [command[0].lower()
                                for command in self.target_commands if command]

        self.harness_template = self._parse_harness_template(harness)
        if not self.harness_template:
            raise ValueError("Harness command cannot be empty")
        self.harness_requires_targets = any(
            token == "{target_args}" or "{target_clis}" in token
            for token in self.harness_template
        )

        self.harness_binary = self.harness_template[0].lower()
        self.harness_binaries = [self.harness_binary]

        self._validate_targets()
        self.bugs_folder.mkdir(parents=True, exist_ok=True)

        self.test_queue = multiprocessing.Queue()
        self.bugs_lock = multiprocessing.Lock()
        self.shutdown_event = multiprocessing.Event()
        self.strict_exit_code = multiprocessing.Value('i', 0)
        self.strict_lock = multiprocessing.Lock()
        self.resource_lock = multiprocessing.Lock()
        self.manager = None
        self.single_process_mode = False

        if self.num_workers <= 1:
            self.single_process_mode = True
        else:
            try:
                self.manager = multiprocessing.Manager()
            except Exception:
                # Some sandboxed environments disallow Manager sockets. Keep a
                # serial fallback path for lightweight tests and local debugging.
                self.single_process_mode = True

        if self.manager is not None:
            self.resource_state = self.manager.dict({
                'cpu_percent': [0.0] * self.cpu_count,
                'memory_percent': 0.0,
                'status': 'normal',
                'paused': False,
                'last_update': time.time(),
            })

            # Track which test each worker is currently processing
            self.current_tests = self.manager.dict()

            self.stats = self.manager.dict({
                'tests_processed': 0,
                'bugs_found': 0,
                'tests_removed_unsupported': 0,
                'tests_removed_timeout': 0,
                'tests_requeued': 0,
            })
        else:
            self.resource_state = {
                'cpu_percent': [0.0] * self.cpu_count,
                'memory_percent': 0.0,
                'status': 'normal',
                'paused': False,
                'last_update': time.time(),
            }
            self.current_tests = {}
            self.stats = {
                'tests_processed': 0,
                'bugs_found': 0,
                'tests_removed_unsupported': 0,
                'tests_removed_timeout': 0,
                'tests_requeued': 0,
            }

    @staticmethod
    def _cpu_count() -> int:
        """Return the available CPU core count with a safe fallback."""
        if psutil is None:
            return os.cpu_count() or 1
        count = psutil.cpu_count()
        if count:
            return count
        return os.cpu_count() or 1

    def _init_determine_num_workers(self, num_workers):
        """Resolve the worker count, clamping to available CPU cores."""
        if num_workers > self.cpu_count:
            print(
                f"[WARN] Requested {num_workers} workers but only {
                    self.cpu_count} CPU cores available, using {
                    self.cpu_count} workers", file=sys.stderr, )
            return self.cpu_count
        elif num_workers > 0 and num_workers <= self.cpu_count:
            return num_workers
        # Fallback, should never execute with proper inputs, happens when num
        # workers given is less than 1
        return self.cpu_count

    def _init_determine_time_remaining(
            self,
            job_start_time,
            stop_buffer_minutes,
            time_remaining):
        """Resolve the job timeout window from explicit or derived inputs."""
        if job_start_time is not None:
            time_remaining_temp = self._compute_time_remaining(
                job_start_time, stop_buffer_minutes)
            print(
                f"[DEBUG] Job start time: {job_start_time} ({
                    time.ctime(job_start_time)})")
            print(
                f"[DEBUG] Script start time: {
                    self.start_time} ({
                    time.ctime(
                        self.start_time)})")
            build_time = self.start_time - job_start_time
            print(
                f"[DEBUG] Build time: {
                    build_time:.1f}s ({
                    build_time /
                    60:.1f} minutes)")
            print(f"[DEBUG] Stop buffer: {stop_buffer_minutes} minutes")
            print(
                f"[DEBUG] Computed remaining time: {time_remaining_temp}s ({
                    time_remaining_temp /
                    60:.1f} minutes)")
            return time_remaining_temp
        elif time_remaining is not None:
            time_remaining_temp = time_remaining
            print(
                f"[DEBUG] Using provided time_remaining: {time_remaining}s ({
                    time_remaining /
                    60:.1f} minutes)")
            return time_remaining_temp
        else:
            print("[DEBUG] No timeout set (running indefinitely)")
            return None

    # Validate required target identifiers are present when template needs
    # them.
    def _validate_targets(self):
        """Ensure required targets are present for template expansion."""
        if self.harness_requires_targets and not self.target_commands:
            raise ValueError("At least one target identifier must be provided")

    def _monitor_resources(self):
        """Continuously sample resources and trigger protective actions."""
        if psutil is None:
            while not self.shutdown_event.is_set():
                time.sleep(self.RESOURCE_CONFIG['check_interval'])
            return

        while not self.shutdown_event.is_set():
            try:
                cpu_percent = psutil.cpu_percent(interval=1, percpu=True)
                memory = psutil.virtual_memory()
                memory_percent = memory.percent
                # Real available memory (excludes cache/buffers)
                memory_available_gb = memory.available / (1024**3)

                max_cpu = max(cpu_percent) if cpu_percent else 0.0
                avg_cpu = sum(cpu_percent) / \
                    len(cpu_percent) if cpu_percent else 0.0

                status = 'normal'
                if (
                    avg_cpu >= self.RESOURCE_CONFIG['cpu_critical']
                    or memory_available_gb
                    < self.RESOURCE_CONFIG['memory_critical_available_gb']
                ):
                    status = 'critical'

                with self.resource_lock:
                    self.resource_state['cpu_percent'] = cpu_percent
                    self.resource_state['memory_percent'] = memory_percent
                    self.resource_state['memory_available_gb'] = (
                        memory_available_gb
                    )
                    self.resource_state['status'] = status
                    self.resource_state['last_update'] = time.time()
                    self.resource_state['max_cpu'] = max_cpu
                    self.resource_state['avg_cpu'] = avg_cpu
                    self.resource_state['memory_total_gb'] = memory.total / \
                        (1024**3)
                    self.resource_state['memory_used_gb'] = memory.used / \
                        (1024**3)

                # Kill high-memory processes each cycle to catch orphans.
                # Use adaptive threshold: stricter when memory is low
                threshold = (
                    self.RESOURCE_CONFIG['max_process_memory_mb_warning']
                    if memory_available_gb < self.RESOURCE_CONFIG
                    ['memory_warning_available_gb'] else self.RESOURCE_CONFIG
                    ['max_process_memory_mb'])
                self._kill_high_memory_processes(threshold_mb=threshold)

                if status == 'critical':
                    self._handle_critical_resources(
                        cpu_percent,
                        max_cpu,
                        avg_cpu,
                        memory_percent,
                        memory_available_gb,
                        memory.total,
                        memory.used)

                time.sleep(self.RESOURCE_CONFIG['check_interval'])
            except Exception as e:
                print(
                    f"[WARN] Error in resource monitoring: {e}",
                    file=sys.stderr)
                time.sleep(self.RESOURCE_CONFIG['check_interval'])

    def _kill_high_memory_processes(
        self, threshold_mb: Optional[float] = None
    ):
        """Kill processes whose RSS exceeds the configured threshold.

        Uses recursive descendant tracking to catch orphaned worker
        subprocesses.
        """
        if psutil is None:
            return

        if threshold_mb is None:
            threshold_mb = self.RESOURCE_CONFIG['max_process_memory_mb']

        # Threshold for reporting which test caused the issue (14GB = 14336MB)
        HIGH_MEMORY_REPORT_THRESHOLD_MB = 14336

        try:
            # Get all tracked PIDs (main, workers, and all descendants)
            main_pid = os.getpid()
            worker_pids = {}
            if hasattr(self, 'workers'):
                for worker_id, w in enumerate(self.workers, start=1):
                    try:
                        worker_pids[w.pid] = worker_id
                    except (AttributeError, ValueError):
                        pass

            # Build mapping: pid -> worker_id (for finding which worker spawned
            # a process)
            pid_to_worker = {}
            tracked_pids = {main_pid}
            tracked_pids.update(worker_pids.keys())
            for pid in list(tracked_pids):
                # Find which worker this PID belongs to
                worker_id = worker_pids.get(pid)
                descendants = self._get_all_descendant_pids(pid)
                tracked_pids.update(descendants)
                if worker_id:
                    for desc_pid in descendants:
                        pid_to_worker[desc_pid] = worker_id

            killed_count = 0
            for pid in tracked_pids:
                try:
                    proc = psutil.Process(pid)
                    rss_mb = proc.memory_info().rss / (1024 * 1024)

                    if rss_mb > threshold_mb:
                        name = proc.name()
                        # First 3 args for brevity
                        cmdline = ' '.join(proc.cmdline()[:3])
                        print(
                            (
                                f"[RESOURCE] Killing process {pid} ({name}) "
                                f"using {rss_mb:.1f}MB RAM "
                                f"(threshold: {threshold_mb}MB)"
                            ),
                            file=sys.stderr,
                        )
                        print(f"  Command: {cmdline}...", file=sys.stderr)

                        # If process used >= 14GB RAM, report which test caused
                        # it
                        if rss_mb >= HIGH_MEMORY_REPORT_THRESHOLD_MB:
                            worker_id = pid_to_worker.get(pid)
                            if worker_id and worker_id in self.current_tests:
                                test_name = self.current_tests[worker_id]
                                print(
                                    (
                                        "  ⚠️  HIGH RAM USAGE: Process used "
                                        f"{rss_mb:.1f}MB RAM while processing "
                                        f"test: {test_name}"
                                    ),
                                    file=sys.stderr,
                                )
                            else:
                                print(
                                    (
                                        "  ⚠️  HIGH RAM USAGE: Process used "
                                        f"{rss_mb:.1f}MB RAM "
                                        "(could not determine test)"
                                    ),
                                    file=sys.stderr,
                                )

                        proc.kill()
                        killed_count += 1
                except (
                    psutil.NoSuchProcess,
                    psutil.AccessDenied,
                    KeyError,
                    AttributeError,
                ):
                    pass

            if killed_count > 0:
                print(
                    (
                        "[RESOURCE] Killed "
                        f"{killed_count} process(es) exceeding "
                        f"{threshold_mb}MB RAM threshold"
                    ),
                    file=sys.stderr,
                )
        except Exception as e:
            print(
                f"[WARN] Error killing high RAM processes: {e}",
                file=sys.stderr)

    def _get_all_descendant_pids(self, pid):
        """Return all descendant process IDs for `pid`."""
        if psutil is None:
            return set()
        descendant_pids = set()
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

    def _log_cpu_usage_by_process_type(self):
        """Log tracked CPU and RAM usage grouped by process role."""
        if psutil is None:
            return
        try:
            main_pid = os.getpid()
            worker_pids = set()
            if hasattr(self, 'workers'):
                for w in self.workers:
                    try:
                        worker_pids.add(w.pid)
                    except (AttributeError, ValueError):
                        pass

            # Build set of all PIDs we should track (main, workers, and all
            # their descendants)
            tracked_pids = {main_pid}
            tracked_pids.update(worker_pids)
            for pid in list(tracked_pids):
                tracked_pids.update(self._get_all_descendant_pids(pid))

            process_stats = {
                'harness': {
                    'count': 0,
                    'cpu_total': 0.0,
                    'memory_total_mb': 0.0,
                },
                'python': {
                    'count': 0,
                    'cpu_total': 0.0,
                    'memory_total_mb': 0.0,
                },
                'other': {
                    'count': 0,
                    'cpu_total': 0.0,
                    'memory_total_mb': 0.0,
                },
            }
            for target_bin in self.target_binaries:
                process_stats.setdefault(
                    target_bin,
                    {'count': 0, 'cpu_total': 0.0, 'memory_total_mb': 0.0},
                )

            # First pass: get CPU percent (need to call it once to initialize,
            # then wait a bit)
            cpu_cache = {}
            for pid in tracked_pids:
                try:
                    proc = psutil.Process(pid)
                    proc.cpu_percent()  # Initialize CPU tracking
                    cpu_cache[pid] = proc
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

            # Wait a short time for CPU percent to be calculated
            time.sleep(0.1)

            # Second pass: collect actual stats
            for pid in tracked_pids:
                try:
                    proc = cpu_cache.get(pid)
                    if not proc:
                        proc = psutil.Process(pid)

                    proc_info = proc.as_dict(
                        ['name', 'memory_info', 'cmdline'])

                    # Get CPU percent (now should have a value)
                    try:
                        cpu_pct = proc.cpu_percent(interval=None)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        cpu_pct = 0.0

                    memory_info = proc_info.get('memory_info')
                    rss_mb = (
                        memory_info.rss / (1024 * 1024)
                        if memory_info
                        else 0.0
                    )
                    cmdline_values = proc_info.get('cmdline') or []
                    cmdline = ' '.join(cmdline_values)
                    name = (proc_info.get('name') or '').lower()

                    # Categorize process by known roles/binaries
                    lower_cmd = cmdline.lower()
                    if any(harness in lower_cmd or harness in name
                           for harness in self.harness_binaries):
                        bucket = 'harness'
                    elif any(
                        target in lower_cmd or target in name
                        for target in self.target_binaries
                    ):
                        bucket = next(
                            target for target in self.target_binaries
                            if target in lower_cmd or target in name
                        )
                    elif 'python' in name or 'python' in lower_cmd:
                        bucket = 'python'
                    else:
                        bucket = 'other'

                    process_stats[bucket]['count'] += 1
                    process_stats[bucket]['cpu_total'] += cpu_pct
                    process_stats[bucket]['memory_total_mb'] += rss_mb

                except (
                    psutil.NoSuchProcess,
                    psutil.AccessDenied,
                    KeyError,
                    AttributeError,
                ):
                    pass

            # Get system memory for comparison
            memory = psutil.virtual_memory()
            system_memory_used_gb = memory.used / (1024**3)
            tracked_memory_mb = sum(stats['memory_total_mb']
                                    for stats in process_stats.values())

            # Log the breakdown
            print(f"[RESOURCE] CPU usage by process type:", file=sys.stderr)
            total_cpu = 0.0
            for proc_type, stats in process_stats.items():
                if stats['count'] > 0:
                    print(
                        (
                            f"  {proc_type}: {stats['count']} process(es), "
                            f"{stats['cpu_total']:.1f}% CPU, "
                            f"{stats['memory_total_mb']:.1f} MB"
                        ),
                        file=sys.stderr)
                    total_cpu += stats['cpu_total']
            print(
                (
                    f"  Total tracked: {total_cpu:.1f}% CPU, "
                    f"{tracked_memory_mb:.1f} MB RAM"
                ),
                file=sys.stderr)
            print(
                (
                    f"  System total: {system_memory_used_gb:.2f} GB RAM used "
                    f"({memory.percent:.1f}%)"
                ),
                file=sys.stderr)
            ram_gap_mb = system_memory_used_gb * 1024 - tracked_memory_mb
            print(
                (
                    f"  RAM gap: {ram_gap_mb:.1f} MB not tracked "
                    "(likely other system processes)"
                ),
                file=sys.stderr)

        except Exception as e:
            print(
                f"[WARN] Error logging CPU usage by process type: {e}",
                file=sys.stderr)

    def _handle_critical_resources(
        self,
        cpu_percent: List[float],
        max_cpu: float,
        avg_cpu: float,
        memory_percent: float,
        memory_available_gb: float,
        memory_total: int,
        memory_used: int,
    ):
        """Pause work or stop execution when resource usage is critical."""
        try:
            memory_total_gb = memory_total / (1024**3)
            memory_used_gb = memory_used / (1024**3)

            issues = []
            if avg_cpu >= self.RESOURCE_CONFIG['cpu_critical']:
                cpu_details = ", ".join(
                    f"core{i + 1}:{p:.1f}%"
                    for i, p in enumerate(cpu_percent)
                )
                issues.append(
                    (
                        f"CPU: {avg_cpu:.1f}% avg, {max_cpu:.1f}% max "
                        f"({cpu_details}, critical: "
                        f"{self.RESOURCE_CONFIG['cpu_critical']}%)"
                    )
                )
            if (
                memory_available_gb
                < self.RESOURCE_CONFIG['memory_critical_available_gb']
            ):
                critical_mem_threshold = (
                    self.RESOURCE_CONFIG['memory_critical_available_gb']
                )
                issues.append(
                    (
                        f"RAM: {memory_available_gb:.2f}GB available "
                        "(critical threshold: "
                        f"{critical_mem_threshold}GB) - "
                        f"{memory_percent:.1f}% used "
                        f"({memory_used_gb:.2f}GB / {memory_total_gb:.2f}GB)"
                    )
                )

            if issues:
                print(
                    (
                        "[RESOURCE] Critical resource usage detected - "
                        f"{', '.join(issues)} - taking action"
                    ),
                    file=sys.stderr,
                )
            else:
                print(
                    (
                        "[RESOURCE] Critical resource usage detected - "
                        f"CPU: {avg_cpu:.1f}% avg, {max_cpu:.1f}% max, "
                        f"RAM: {memory_available_gb:.2f}GB available "
                        f"({memory_percent:.1f}% used, "
                        f"{memory_used_gb:.2f}GB / {memory_total_gb:.2f}GB) "
                        "- taking action"
                    ),
                    file=sys.stderr,
                )

            # Log CPU usage breakdown by process type
            self._log_cpu_usage_by_process_type()
        except Exception as e:
            print(
                (
                    "[RESOURCE] Critical resource usage detected - "
                    f"CPU: {avg_cpu:.1f}% avg, RAM available: "
                    f"{memory_available_gb:.2f}GB - taking action "
                    f"(error formatting details: {e})"
                ),
                file=sys.stderr,
            )

        # If RAM is critical (low available), stop immediately to preserve bugs
        if (
            memory_available_gb
            < self.RESOURCE_CONFIG['memory_critical_available_gb']
        ):
            self._log_bugs_summary_and_stop()
            return

        with self.resource_lock:
            self.resource_state['paused'] = True

        try:
            gc.collect()
        except Exception:
            pass

        # Process killing already happens every cycle.
        # Just pause to let system recover
        time.sleep(self.RESOURCE_CONFIG['pause_duration'])

        with self.resource_lock:
            self.resource_state['paused'] = False

    def _calculate_folder_size_mb(self, folder_path: Path) -> float:
        """Calculate directory size in megabytes."""
        try:
            if folder_path.exists():
                size_bytes = sum(f.stat().st_size
                                 for f in folder_path.rglob('*')
                                 if f.is_file())
                return size_bytes / (1024 * 1024)
            else:
                return 0.0
        except Exception:
            return 0.0

    def _log_bugs_summary_and_stop(self):
        """Print bug summary details and request an orderly shutdown."""
        print("\n" + "=" * 60, file=sys.stderr)
        print(
            "CRITICAL RAM DETECTED - STOPPING TO PRESERVE BUGS",
            file=sys.stderr)
        print("=" * 60, file=sys.stderr)

        # Collect bugs from main bugs folder
        main_bugs = self._collect_bug_files(self.bugs_folder)
        main_bug_count = len(main_bugs)
        main_bugs_size_mb = self._calculate_folder_size_mb(self.bugs_folder)

        # Collect info from all worker folders
        total_worker_bugs = 0
        worker_folders_info = []
        for worker_id in range(1, self.num_workers + 1):
            worker_bugs_folder = self.bugs_folder / f"worker_{worker_id}"
            worker_bugs = self._collect_bug_files(worker_bugs_folder)
            worker_bug_count = len(worker_bugs)
            total_worker_bugs += worker_bug_count

            # Calculate sizes for all worker folders
            bugs_size_mb = self._calculate_folder_size_mb(worker_bugs_folder)
            scratch_folder = Path(f"scratch_{worker_id}")
            scratch_size_mb = self._calculate_folder_size_mb(scratch_folder)
            log_folder = Path(f"logs_{worker_id}")
            log_size_mb = self._calculate_folder_size_mb(log_folder)

            worker_folders_info.append({
                'id': worker_id,
                'bugs': worker_bug_count,
                'bugs_size_mb': bugs_size_mb,
                'scratch_size_mb': scratch_size_mb,
                'log_size_mb': log_size_mb,
                'total_size_mb': bugs_size_mb + scratch_size_mb + log_size_mb
            })

        total_bugs = main_bug_count + total_worker_bugs

        print(f"\nBUGS SUMMARY:", file=sys.stderr)
        print(f"  Total bugs found: {total_bugs}", file=sys.stderr)
        print(
            f"  Main bugs folder: {main_bug_count} bugs, {
                main_bugs_size_mb:.2f} MB disk space",
            file=sys.stderr)
        print(f"  Worker folders:", file=sys.stderr)
        for info in worker_folders_info:
            print(f"    worker_{info['id']}:", file=sys.stderr)
            print(
                f"      bugs: {
                    info['bugs']} bugs, {
                    info['bugs_size_mb']:.2f} MB disk space",
                file=sys.stderr)
            print(
                f"      scratch: {
                    info['scratch_size_mb']:.2f} MB disk space",
                file=sys.stderr)
            print(
                f"      logs: {
                    info['log_size_mb']:.2f} MB disk space",
                file=sys.stderr)
            print(
                f"      total: {
                    info['total_size_mb']:.2f} MB disk space",
                file=sys.stderr)

        print(f"\nSTATISTICS:", file=sys.stderr)
        print(
            f"  Tests processed: {
                self.stats.get(
                    'tests_processed',
                    0)}",
            file=sys.stderr)
        print(
            f"  Bugs found: {
                self.stats.get(
                    'bugs_found',
                    0)}",
            file=sys.stderr)
        print(
            f"  Tests requeued (bugs found): {
                self.stats.get(
                    'tests_requeued',
                    0)}",
            file=sys.stderr)
        print(
            f"  Tests removed (unsupported): {
                self.stats.get(
                    'tests_removed_unsupported',
                    0)}",
            file=sys.stderr)
        print(
            f"  Tests removed (timeout): {
                self.stats.get(
                    'tests_removed_timeout',
                    0)}",
            file=sys.stderr)

        print("\n" + "=" * 60, file=sys.stderr)
        print("Stopping harness to preserve found bugs...", file=sys.stderr)
        print("=" * 60 + "\n", file=sys.stderr)

        # Stop all workers gracefully
        self.shutdown_event.set()

    def _check_resource_state(self) -> str:
        """Return current resource status."""
        with self.resource_lock:
            return self.resource_state.get('status', 'normal')

    def _is_paused(self) -> bool:
        """Return whether workers are currently paused."""
        with self.resource_lock:
            return self.resource_state.get('paused', False)

    @staticmethod
    def _resolve_target_command(identifier: str) -> List[str]:
        """Split a target identifier string into an argv list."""
        value = identifier.strip()
        if not value:
            raise ValueError("Target identifier cannot be empty")
        parsed = shlex.split(value)
        if not parsed:
            raise ValueError(
                f"Target identifier resolved to empty argv: {
                    identifier!r}")
        return parsed

    def _build_target_commands(
            self, target_identifiers: Optional[List[str]]) -> List[List[str]]:
        """Parse all target identifiers into argv token lists."""
        if not target_identifiers:
            return []
        return [self._resolve_target_command(
            identifier) for identifier in target_identifiers]

    def _parse_harness_template(self, harness: Optional[str | List[str]]) -> List[str]:
        """Parse and validate the harness command template JSON."""
        if harness is None:
            raise ValueError("Harness template is required")

        if isinstance(harness, list):
            if not harness or not all(isinstance(item, str) for item in harness):
                raise ValueError("--harness list must contain only argv strings")
            return harness

        raw = harness.strip()
        if not raw:
            raise ValueError("Harness template cannot be empty")

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in --harness: {e}") from e
        if not isinstance(
                parsed,
                list) or not all(
                isinstance(
                item,
                str) for item in parsed):
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
        """Render a concrete harness argv list from the command template."""
        context: Dict[str, str] = {
            'iterations': str(self.iterations),
            'modulo': str(self.modulo),
            'test_path': str(test_path),
            'bugs_dir': str(bugs_folder),
            'scratch_dir': str(scratch_folder),
            'logs_dir': str(logs_folder),
            'target_clis': self.target_clis,
            # Backward-compatible aliases
            'test': str(test_path),
            'bugs': str(bugs_folder),
            'scratch': str(scratch_folder),
            'log_dir': str(logs_folder),
            'logs': str(logs_folder),
        }

        cmd: List[str] = []
        # Build command from template
        for token in self.harness_template:
            if token == "{target_args}":
                cmd.extend(self.target_args)
                continue
            try:
                cmd.append(token.format(**context))
            except KeyError as e:
                raise ValueError(
                    f"Unknown placeholder in --harness template: {e}. "
                    "Supported: {test_path}, {iterations}, {modulo}, "
                    "{bugs_dir}, {scratch_dir}, {logs}/{logs_dir}, "
                    "{target_args}, {target_clis}"
                ) from e
        return cmd

    def _compute_time_remaining(
            self,
            job_start_time: float,
            stop_buffer_minutes: int) -> int:
        """Compute remaining runtime budget for the job in seconds."""
        GITHUB_TIMEOUT = 21600
        MIN_REMAINING = 600

        build_time = self.start_time - job_start_time
        stop_buffer_seconds = stop_buffer_minutes * 60
        available_time = GITHUB_TIMEOUT - build_time
        remaining = available_time - stop_buffer_seconds

        if remaining < MIN_REMAINING:
            print(
                (
                    "[DEBUG] Computed remaining time "
                    f"({remaining}s) is less than minimum "
                    f"({MIN_REMAINING}s), using {MIN_REMAINING}s"
                )
            )
            remaining = MIN_REMAINING

        return int(remaining)

    def _get_time_remaining(self) -> float:
        """Return remaining time before timeout, or infinity when disabled."""
        if self.time_remaining is None:
            return float('inf')
        return max(0.0, self.time_remaining - (time.time() - self.start_time))

    def _is_time_expired(self) -> bool:
        """Return whether the configured timeout has elapsed."""
        return (
            self.time_remaining is not None
            and self._get_time_remaining() <= 0
        )

    def _collect_bug_files(self, folder: Path) -> List[Path]:
        """Return all bug artifact files under `folder`."""
        if not folder.exists():
            return []
        bug_files = []
        for path in folder.rglob("*"):
            if not path.is_file():
                continue
            if any(part.startswith(".") for part in path.relative_to(folder).parts):
                continue
            bug_files.append(path)
        return sorted(bug_files)

    @contextlib.contextmanager
    def _worker_temp_dirs(self, worker_id: int):
        """Create per-worker scratch and log directories for one test run."""
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

    def _run_harness(
        self,
        test_name: str,
        worker_id: int,
        per_test_timeout: Optional[float] = None,
    ) -> Tuple[int, List[Path], float]:
        """Run the harness for one test and collect bug files and runtime."""
        test_path = self.tests_root / test_name
        if not test_path.exists():
            print(
                (
                    f"[WORKER {worker_id}] Error: "
                    f"Test file not found: {test_path}"
                ),
                file=sys.stderr,
            )
            return (1, [], 0.0)
        with self._worker_temp_dirs(worker_id) as (
            scratch_folder,
            logs_folder,
            bugs_folder,
        ):
            try:
                cmd = self._render_harness_command(
                    test_path, bugs_folder, scratch_folder, logs_folder)
            except ValueError as e:
                print(f"[WORKER {worker_id}] Error: {e}", file=sys.stderr)
                return (1, [], 0.0)

            if per_test_timeout:
                print(
                    (
                        f"[WORKER {worker_id}] Running harness on: "
                        f"{test_name} (timeout: {per_test_timeout}s)"
                    )
                )
            else:
                print(
                    f"[WORKER {worker_id}] Running harness on: {test_name}"
                )

            start_time = time.time()

            try:
                if per_test_timeout and per_test_timeout > 0:
                    result = subprocess.run(
                        cmd, capture_output=True, text=True,
                        timeout=per_test_timeout)
                else:
                    result = subprocess.run(
                        cmd, capture_output=True, text=True)

                exit_code = result.returncode
            except subprocess.TimeoutExpired:
                runtime = time.time() - start_time
                return (124, [], runtime)
            except Exception:
                runtime = time.time() - start_time
                return (1, [], runtime)

            runtime = time.time() - start_time
            bug_files = self._collect_bug_files(bugs_folder)
            return (exit_code, bug_files, runtime)

    def _persist_bug_files(self, worker_id: int, bug_files: List[Path]):
        """Move discovered bug artifacts into the shared bugs directory."""
        if not bug_files:
            return
        with self.bugs_lock:
            for bug_file in bug_files:
                try:
                    dest = self.bugs_folder / bug_file.name
                    if dest.exists():
                        timestamp = int(time.time())
                        dest = self.bugs_folder / \
                            f"{bug_file.stem}_{timestamp}{bug_file.suffix}"
                    shutil.move(str(bug_file), str(dest))
                    self.stats['bugs_found'] += 1
                except Exception as e:
                    print(
                        (
                            f"[WORKER {worker_id}] Warning: Failed to move "
                            f"bug file {bug_file}: {e}"
                        ),
                        file=sys.stderr,
                    )

    def _handle_exit_code(
        self,
        test_name: str,
        exit_code: int,
        bug_files: List[Path],
        runtime: float,
        worker_id: int,
    ) -> str:
        """Handle harness exit status and return the next queue action."""
        if self.strict_mode:
            self._persist_bug_files(worker_id, bug_files)
            if exit_code == 0:
                return 'requeue'

            # In strict mode propagate non-zero harness codes as-is.
            with self.strict_lock:
                if self.strict_exit_code.value == 0:
                    self.strict_exit_code.value = exit_code
            self.shutdown_event.set()
            return 'continue'

        if exit_code == self.EXIT_CODE_BUGS_FOUND:
            if bug_files:
                print(
                    f"[WORKER {worker_id}] ✓ Exit code 10: Found {
                        len(bug_files)} bug(s) on {test_name}")
                self._persist_bug_files(worker_id, bug_files)
            else:
                print(
                    (
                        f"[WORKER {worker_id}] Warning: Exit code 10 but no "
                        f"bugs found for {test_name}"
                    ),
                    file=sys.stderr,
                )
            return 'requeue'

        elif exit_code == self.EXIT_CODE_UNSUPPORTED:
            print(
                (
                    f"[WORKER {worker_id}] ⚠ Exit code 3: {test_name} "
                    "(unsupported operation - removing)"
                )
            )
            self.stats['tests_removed_unsupported'] += 1
            return 'remove'

        elif exit_code == self.EXIT_CODE_SUCCESS:
            if not bug_files:
                print(
                    (
                        f"[WORKER {worker_id}] Exit code 0: No bugs found on "
                        f"{test_name} (runtime: {runtime:.1f}s) - requeuing "
                        "for next cycle"
                    )
                )
                # Always requeue to create ring/queue behavior - tests cycle
                # continuously until time expires
                return 'requeue'
            else:
                print(
                    f"[WORKER {worker_id}] Exit code 0: {test_name} (runtime: {
                        runtime:.1f}s) - bugs found, requeuing")
                return 'requeue'

        else:
            return 'continue'

    def _worker_process(self, worker_id: int):
        """Run the worker loop for dequeuing tests and executing harnesses."""
        print(f"[WORKER {worker_id}] Started")

        while not self.shutdown_event.is_set():
            try:
                if self._is_paused():
                    resource_status = self._check_resource_state()
                    print(
                        (
                            f"[WORKER {worker_id}] Paused due to "
                            f"{resource_status} resource usage"
                        ),
                        file=sys.stderr,
                    )
                    time.sleep(self.RESOURCE_CONFIG['pause_duration'])
                    continue

                try:
                    test_name = self.test_queue.get(timeout=1.0)
                except Exception:
                    if self.shutdown_event.is_set() or self._is_time_expired():
                        break
                    continue

                if self._is_time_expired():
                    try:
                        self.test_queue.put(test_name)
                    except Exception:
                        pass
                    break

                resource_status = self._check_resource_state()
                if resource_status == 'critical':
                    try:
                        self.test_queue.put(test_name)
                    except Exception:
                        pass
                    time.sleep(self.RESOURCE_CONFIG['pause_duration'])
                    continue

                # Track which test this worker is currently processing
                self.current_tests[worker_id] = test_name

                time_remaining = self._get_time_remaining()
                exit_code, bug_files, runtime = self._run_harness(
                    test_name, worker_id, per_test_timeout=time_remaining
                    if self.time_remaining and time_remaining > 0 else None,)

                # Clear test tracking after processing
                if worker_id in self.current_tests:
                    del self.current_tests[worker_id]

                action = self._handle_exit_code(
                    test_name, exit_code, bug_files, runtime, worker_id)

                if action == 'requeue':
                    try:
                        self.test_queue.put(test_name)
                        self.stats['tests_requeued'] += 1
                    except Exception:
                        pass

                self.stats['tests_processed'] += 1

            except Exception as e:
                print(
                    f"[WORKER {worker_id}] Error in worker: {e}",
                    file=sys.stderr)
                continue

        print(f"[WORKER {worker_id}] Stopped")

    def _run_serial(self) -> int:
        """Run the harness loop without multiprocessing.Manager support."""
        pending_tests = list(self.tests)
        worker_id = 1

        print("[INFO] Running commit harness in serial fallback mode")

        while pending_tests and not self.shutdown_event.is_set():
            if self._is_time_expired():
                break

            test_name = pending_tests.pop(0)
            self.current_tests[worker_id] = test_name
            time_remaining = self._get_time_remaining()
            exit_code, bug_files, runtime = self._run_harness(
                test_name,
                worker_id,
                per_test_timeout=time_remaining
                if self.time_remaining and time_remaining > 0 else None,
            )
            self.current_tests.pop(worker_id, None)

            action = self._handle_exit_code(
                test_name,
                exit_code,
                bug_files,
                runtime,
                worker_id,
            )

            if action == 'requeue':
                pending_tests.append(test_name)
                self.stats['tests_requeued'] += 1

            self.stats['tests_processed'] += 1

            if self.strict_mode and self.shutdown_event.is_set():
                break

        if self.strict_mode:
            return self.strict_exit_code.value
        return self.EXIT_CODE_SUCCESS

    def run(self) -> int:
        """Start workers, monitor execution, and print the final summary."""
        if not self.tests:
            print(
                f"No tests provided{
                    ' for job ' +
                    self.job_id if self.job_id else ''}")
            return 0

        print(
            f"Running harness on {
                len(
                    self.tests)} test(s){
                ' for job ' +
                self.job_id if self.job_id else ''}")
        print(f"Tests root: {self.tests_root}")
        print(
            f"Timeout: {
                self.time_remaining}s ({
                self.time_remaining //
                60} minutes)" if self.time_remaining else "No timeout")
        print(f"Iterations per test: {self.iterations}")
        print(f"Modulo: {self.modulo}")
        print(f"CPU cores: {self.cpu_count}")
        print(f"Workers: {self.num_workers}")
        print(f"Strict mode: {self.strict_mode}")
        print(
            "Targets: " +
            (", ".join(
                shlex.join(command) for command in self.target_commands)
             if self.target_commands else "(none)"))
        print(f"Harness template: {self.harness_template}")
        print()

        if self.single_process_mode:
            return self._run_serial()

        for test in self.tests:
            self.test_queue.put(test)

        workers = []
        for worker_id in range(1, self.num_workers + 1):
            worker = multiprocessing.Process(
                target=self._worker_process, args=(worker_id,))
            worker.start()
            workers.append(worker)

        self.workers = workers

        monitor_thread = threading.Thread(
            target=self._monitor_resources, daemon=True)
        monitor_thread.start()
        print("[DEBUG] Resource monitoring started")

        def signal_handler(signum, frame):
            print("\n⏰ Shutdown signal received, stopping workers...")
            self.shutdown_event.set()

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        try:
            if self.time_remaining:
                end_time = self.start_time + self.time_remaining
                while time.time() < end_time and any(w.is_alive()
                                                     for w in workers):
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

        for worker in workers:
            worker.join(timeout=5)
            if worker.is_alive():
                worker_pid = getattr(worker, 'pid', 'unknown')
                print(
                    (
                        f"Warning: Worker {worker_pid} did not terminate, "
                        "killing..."
                    )
                )
                worker.terminate()
                worker.join(timeout=2)
                if worker.is_alive():
                    worker.kill()

        for worker_id in range(1, self.num_workers + 1):
            worker_bugs = self.bugs_folder / f"worker_{worker_id}"
            for bug_file in self._collect_bug_files(worker_bugs):
                try:
                    dest = self.bugs_folder / bug_file.name
                    if dest.exists():
                        timestamp = int(time.time())
                        dest = self.bugs_folder / \
                            f"{bug_file.stem}_{timestamp}{bug_file.suffix}"
                    shutil.move(str(bug_file), str(dest))
                except Exception:
                    pass

        print()
        print("=" * 60)
        print(
            f"FINAL BUG SUMMARY{
                ' FOR JOB ' +
                self.job_id if self.job_id else ''}")
        print("=" * 60)

        bug_files = self._collect_bug_files(self.bugs_folder)
        if bug_files:
            print(f"\nFound {len(bug_files)} bug(s):")
            for i, bug_file in enumerate(bug_files, 1):
                print(f"\nBug #{i}: {bug_file}")
                print("-" * 60)
                try:
                    max_preview_bytes = 8192
                    with open(bug_file, 'rb') as f:
                        data = f.read(max_preview_bytes + 1)
                    truncated = len(data) > max_preview_bytes
                    if truncated:
                        data = data[:max_preview_bytes]

                    try:
                        print(data.decode("utf-8"))
                    except UnicodeDecodeError:
                        # zzuf and similar mutators can produce arbitrary data.
                        # Show a readable preview instead of failing the
                        # summary.
                        print(
                            (
                                "[binary file: UTF-8 decode failed, "
                                "showing replacement preview]"
                            )
                        )
                        print(data.decode("utf-8", errors="replace"))

                    if truncated:
                        print(
                            (
                                f"\n[preview truncated at "
                                f"{max_preview_bytes} bytes]"
                            )
                        )
                except Exception as e:
                    print(f"Error reading bug file: {e}")
                print("-" * 60)
        else:
            print("No bugs found.")

        print()
        print("Statistics:")
        print(f"  Tests processed: {self.stats.get('tests_processed', 0)}")
        print(f"  Bugs found: {self.stats.get('bugs_found', 0)}")
        print(
            f"  Tests requeued (bugs found): {
                self.stats.get(
                    'tests_requeued',
                    0)}")
        print(
            f"  Tests removed (unsupported): {
                self.stats.get(
                    'tests_removed_unsupported',
                    0)}")
        print(
            f"  Tests removed (timeout): {
                self.stats.get(
                    'tests_removed_timeout',
                    0)}")
        print("=" * 60)
        if self.strict_mode:
            return int(self.strict_exit_code.value)
        return 0


CommitTarget = CommitHarnessRunner


def main():
    """Parse CLI arguments, run the harness runner, and exit."""
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
    default_workers = CommitHarnessRunner._cpu_count()

    parser.add_argument(
        "--workers",
        type=int,
        default=default_workers,
        help=(
            "Number of worker processes "
            f"(default: {default_workers}, auto-detected from CPU cores)."
        ),
    )
    parser.add_argument(
        "--bugs-folder",
        default="bugs",
        help="Folder to store bugs (default: bugs)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Propagate harness exit codes directly (generic mode).",
    )

    args = parser.parse_args()

    # Parse tests JSON
    try:
        tests = json.loads(args.tests_json)
        if not isinstance(tests, list):
            raise ValueError("tests-json must be a JSON array")
        if not all(isinstance(test, str) for test in tests):
            raise ValueError("tests-json must be a JSON array of strings")
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in --tests-json: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Create and run harness
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
            targets=args.targets,
            harness=args.harness,
            job_id=args.job_id,
            strict_mode=args.strict,
        )
        exit_code = runner.run()
        if args.strict:
            sys.exit(exit_code)
        # Keep legacy default behavior for CI compatibility.
        sys.exit(0)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        if args.strict:
            sys.exit(1)
        # Keep legacy default behavior for CI compatibility.
        sys.exit(0)


if __name__ == "__main__":
    main()
