#!/usr/bin/env python3
"""Shared helpers for harness-backed solver commit runners."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import List, Sequence


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.commit_harness_orchestrator import CommitHarnessRunner


TYPEFUZZ_HARNESS_TEMPLATE = [
    "typefuzz",
    "-i",
    "{iterations}",
    "-m",
    "{modulo}",
    "--timeout",
    "120",
    "--bugs",
    "{bugs_dir}",
    "--scratch",
    "{scratch_dir}",
    "--logfolder",
    "{logs}",
    "{target_clis}",
    "{test_path}",
]

Z3_FLAGS = ("smt.threads=1", "memory_max_size=2048", "model_validate=true")
CVC5_FLAGS = ("--check-models", "--check-proofs", "--strings-exp")


def build_command(executable: str, flags: Sequence[str]) -> str:
    """Render an argv list as a shell-escaped string."""
    parts = [executable, *[flag for flag in flags if flag]]
    return shlex.join(parts)


def ensure_command_available(command: str, label: str) -> None:
    """Fail fast if a required solver command cannot be resolved."""
    executable = shlex.split(command)[0]
    if os.path.sep in executable or executable.startswith("."):
        if not Path(executable).exists():
            raise ValueError(f"{label} not found at: {executable}")
        return
    if shutil.which(executable) is None:
        raise ValueError(f"{label} not found in PATH: {executable}")


def build_z3_cvc5_targets(z3_path: str, cvc5_path: str) -> List[str]:
    """Build the paired Z3/CVC5 command strings."""
    return [
        build_command(z3_path, Z3_FLAGS),
        build_command(cvc5_path, CVC5_FLAGS),
    ]


def build_cvc5_opensmt_targets(cvc5_path: str, opensmt_path: str) -> List[str]:
    """Build the paired CVC5/OpenSMT command strings."""
    return [
        build_command(cvc5_path, CVC5_FLAGS),
        build_command(opensmt_path, ()),
    ]


def parse_tests_json(tests_json: str | Sequence[str]) -> List[str]:
    """Normalize tests-json into a list of strings."""
    if isinstance(tests_json, str):
        tests = json.loads(tests_json)
    else:
        tests = list(tests_json)

    if not isinstance(tests, list) or not all(isinstance(item, str) for item in tests):
        raise ValueError("tests-json must be a JSON array of strings")
    return tests


def run_commit_harness(
    tests: str | Sequence[str],
    tests_root: str,
    bugs_folder: str,
    num_workers: int,
    iterations: int,
    modulo: int,
    time_remaining: int | None,
    job_start_time: float | None,
    stop_buffer_minutes: int,
    targets: Sequence[str],
    job_id: str | None = None,
    strict_mode: bool = False,
    harness_template: Sequence[str] | str | None = None,
) -> int:
    """Instantiate the orchestrator-backed harness runner and execute it."""
    runner = CommitHarnessRunner(
        tests=parse_tests_json(tests),
        tests_root=tests_root,
        bugs_folder=bugs_folder,
        num_workers=num_workers,
        iterations=iterations,
        modulo=modulo,
        time_remaining=time_remaining,
        job_start_time=job_start_time,
        stop_buffer_minutes=stop_buffer_minutes,
        targets=list(targets),
        harness=harness_template or TYPEFUZZ_HARNESS_TEMPLATE,
        job_id=job_id,
        strict_mode=strict_mode,
    )
    return runner.run()
