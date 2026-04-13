#!/usr/bin/env python3
"""Shared contract-driven build, discovery, harness, and artifact logic."""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.local_commit_fuzzer_matrix import (  # noqa: E402
    build_jobs,
    maybe_limit_tests,
)
from scripts.solver_contract import (  # noqa: E402
    ContractError,
    derive_github_issues_url,
    SolverContract,
    WorkspaceLayout,
    load_solver_contract,
)


class BrainError(RuntimeError):
    """Raised when a contract-driven step fails."""

    def __init__(
        self,
        *,
        solver_name: str,
        repository_url: str,
        step: str,
        message: str,
        command: Optional[str] = None,
        exit_code: Optional[int] = None,
        log_path: Optional[Path] = None,
        stderr: Optional[str] = None,
        issues_url: Optional[str] = None,
        hint: Optional[str] = None,
        category: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.solver_name = solver_name
        self.repository_url = repository_url
        self.step = step
        self.message = message
        self.command = command
        self.exit_code = exit_code
        self.log_path = log_path
        self.stderr = stderr
        self.issues_url = issues_url
        self.hint = hint
        self.category = category

    def render(self) -> str:
        lines = [f"[solver={self.solver_name}][step={self.step}] {self.message}"]
        lines.append(f"category: {self.category or '<unknown>'}")
        lines.append(f"command: {self.command or '<unknown>'}")
        if self.exit_code is not None:
            lines.append(f"exit code: {self.exit_code}")
        lines.append(f"repository url: {self.repository_url}")
        issue_tracker = self.issues_url or derive_github_issues_url(self.repository_url) or "<unknown>"
        lines.append(f"issue tracker: {issue_tracker}")
        if self.log_path:
            lines.append(f"log: {self.log_path}")
        if self.stderr:
            stderr = self.stderr.strip()
            if stderr:
                if len(stderr) > 2000:
                    stderr = stderr[-2000:]
                lines.append("stderr:")
                lines.append(stderr)
        lines.append(f"hint: {self.hint or '<unknown>'}")
        return "\n".join(lines)


@dataclass(frozen=True)
class CommandExecutionResult:
    argv: List[str]
    cwd: Path
    stdout: str
    stderr: str
    returncode: int
    log_path: Optional[Path]

    @property
    def command_string(self) -> str:
        return shlex.join(self.argv)


@dataclass(frozen=True)
class BuildResult:
    mode: str
    binary_path: Path
    commit_hash: Optional[str]
    artifact_dir: Optional[Path]
    artifact_archive: Optional[Path]
    upload_target: Optional[str]
    warnings: tuple[str, ...]
    log_path: Optional[Path]


@dataclass(frozen=True)
class ArtifactResult:
    artifact_dir: Optional[Path]
    artifact_archive: Optional[Path]
    upload_target: Optional[str]
    warnings: tuple[str, ...]


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

_SUMMARY_RE = re.compile(
    r"Changed functions: (?P<total>\d+); with coverage: (?P<with>\d+); without: (?P<without>\d+);"
)


def ensure_command_available(command: str, label: str) -> None:
    """Fail fast if a required command cannot be resolved."""
    executable = shlex.split(command)[0]
    if os.path.sep in executable or executable.startswith("."):
        if not Path(executable).exists():
            raise ValueError(f"{label} not found at: {executable}")
        return
    if shutil.which(executable) is None:
        raise ValueError(f"{label} not found in PATH: {executable}")


def calculate_job_chunks(
    *,
    total_tests: int,
    target_jobs: int,
    max_job_time_minutes: int,
    buffer_minutes: int,
    avg_test_time_seconds: float,
) -> tuple[int, int]:
    if total_tests <= 0:
        return 0, 0
    if target_jobs < 1:
        raise ValueError("target_jobs must be a positive integer")
    if max_job_time_minutes <= buffer_minutes:
        raise ValueError("max_job_time_minutes must be greater than buffer_minutes")
    if avg_test_time_seconds <= 0:
        raise ValueError("avg_test_time_seconds must be positive")

    available_time_seconds = (max_job_time_minutes - buffer_minutes) * 60
    max_tests_per_job = max(1, int(available_time_seconds / avg_test_time_seconds))
    min_jobs = max(1, math.ceil(total_tests / max_tests_per_job))

    total_jobs = min(max(1, target_jobs), total_tests)
    while True:
        tests_per_job = max(1, math.ceil(total_tests / total_jobs))
        estimated_minutes = (tests_per_job * avg_test_time_seconds + buffer_minutes * 60) / 60.0
        if estimated_minutes <= max_job_time_minutes:
            break
        if total_jobs >= min_jobs or total_jobs >= total_tests:
            total_jobs = min(total_tests, min_jobs)
            tests_per_job = max(1, math.ceil(total_tests / total_jobs))
            break
        total_jobs = min(total_tests, total_jobs + 1)

    return total_jobs, tests_per_job


def run_contract_harness(
    contract_path: str | Path,
    *,
    tests: Sequence[str] | str,
    tests_root: Optional[str] = None,
    target_binary: Optional[str] = None,
    reference_binary: Optional[str] = None,
    mode: str = "production",
    bugs_folder: str = "bugs",
    num_workers: int = 4,
    iterations: int = 250,
    modulo: int = 2,
    time_remaining: Optional[int] = None,
    job_start_time: Optional[float] = None,
    stop_buffer_minutes: int = 5,
    job_id: Optional[str] = None,
    strict_mode: bool = False,
    workspace_root: Optional[str | Path] = None,
    harness_template: Optional[Sequence[str]] = None,
) -> int:
    from scripts.commit_harness_runner import run_commit_harness

    brain = SolverFuzzingBrain(contract_path, workspace_root=workspace_root)
    return brain.run_harness(
        tests=tests,
        tests_root=tests_root,
        target_binary=target_binary,
        reference_binary=reference_binary,
        mode=mode,
        bugs_folder=bugs_folder,
        num_workers=num_workers,
        iterations=iterations,
        modulo=modulo,
        time_remaining=time_remaining,
        job_start_time=job_start_time,
        stop_buffer_minutes=stop_buffer_minutes,
        job_id=job_id,
        strict_mode=strict_mode,
        harness_template=harness_template,
    )


class SolverFuzzingBrain:
    """Own the shared contract-driven solver fuzzing behavior."""

    def __init__(
        self,
        contract_path: str | Path,
        *,
        workspace_root: Optional[str | Path] = None,
    ) -> None:
        self.contract = load_solver_contract(contract_path)
        self.brain_root = ROOT
        self.layout = self.contract.resolve_layout(workspace_root or self.brain_root)

    def checkout_repositories(
        self,
        *,
        commit_hash: Optional[str] = None,
        tests_commit_hash: Optional[str] = None,
    ) -> Dict[str, Optional[str]]:
        self._checkout_repository(
            repository_url=self.contract.repository_url,
            destination=self.layout.solver_workspace,
            commit_hash=commit_hash,
            label="solver",
        )

        tests_sha: Optional[str] = None
        if self.contract.uses_split_test_repository:
            self._checkout_repository(
                repository_url=self.contract.tests_repository_url or "",
                destination=self.layout.tests_workspace,
                commit_hash=tests_commit_hash,
                label="tests",
            )
            tests_sha = self.resolve_commit_hash(self.layout.tests_workspace)

        return {
            "solver_commit_hash": self.resolve_commit_hash(self.layout.solver_workspace),
            "tests_commit_hash": tests_sha,
        }

    def build(
        self,
        *,
        mode: str,
        extra_args: Optional[Sequence[str]] = None,
        artifacts_dir: Optional[str | Path] = None,
        artifact_archive: Optional[str | Path] = None,
        upload_s3: bool = False,
        s3_bucket: Optional[str] = None,
        s3_prefix: Optional[str] = None,
    ) -> BuildResult:
        self._ensure_workspace_exists()
        normalized_mode = self._normalize_mode(mode)
        command_template = (
            self.contract.build_command
            if normalized_mode == "production"
            else self.contract.coverage_build_command
        )

        artifact_dir_path = Path(artifacts_dir).resolve() if artifacts_dir else None
        if artifact_dir_path:
            artifact_dir_path.mkdir(parents=True, exist_ok=True)
        log_path = (
            artifact_dir_path / "logs" / f"{normalized_mode}-build.log"
            if artifact_dir_path
            else None
        )

        warnings: List[str] = []
        try:
            argv = self._parse_command_template(
                command_template,
                self._command_context(mode=normalized_mode),
                step=f"{normalized_mode} build",
                hint=f"fix `{normalized_mode} build` command in `{self.contract.contract_path.name}`",
            )
            if extra_args:
                argv.extend(list(extra_args))
            result = self._execute_argv(
                argv=argv,
                log_path=log_path,
                cwd=self.brain_root,
                extra_env={},
                timeout_seconds=None,
            )
            if result.returncode != 0:
                raise BrainError(
                    solver_name=self.contract.solver_name,
                    repository_url=self.contract.repository_url,
                    step=f"{normalized_mode} build",
                    message="command failed",
                    command=result.command_string,
                    exit_code=result.returncode,
                    log_path=result.log_path,
                    stderr=result.stderr,
                    issues_url=self.contract.resolved_issues_url,
                    hint=(
                        f"{normalized_mode}_binary_path did not resolve to an executable"
                        if normalized_mode == "instrumentation"
                        else "production_binary_path did not resolve to an executable"
                    ),
                    category="build problem",
                )
            binary_path = self._extract_binary_path(
                result.stdout,
                self.contract.production_binary_path
                if normalized_mode == "production"
                else self.contract.coverage_binary_path,
                step=f"{normalized_mode} build",
                log_path=result.log_path,
            )
            if artifact_dir_path:
                warnings.extend(self.collect_artifacts(artifact_dir_path))
        except BrainError:
            if artifact_dir_path:
                warnings.extend(self.collect_artifacts(artifact_dir_path))
            raise

        archive_path: Optional[Path] = None
        if artifact_dir_path and artifact_archive:
            archive_path = Path(artifact_archive).resolve()
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            self.create_artifact_archive(artifact_dir_path, archive_path)

        upload_target = None
        if upload_s3:
            upload_source = archive_path or artifact_dir_path
            if not upload_source:
                raise BrainError(
                    solver_name=self.contract.solver_name,
                    repository_url=self.contract.repository_url,
                    step=f"{normalized_mode} build",
                    message="cannot upload artifacts without an artifact directory or archive",
                    issues_url=self.contract.resolved_issues_url,
                    hint="provide --artifacts-dir or --artifact-archive before enabling --upload-s3",
                    category="bad contract problem",
                )
            upload_target = self.upload_to_s3(
                upload_source,
                bucket=s3_bucket or self.contract.artifact_s3_bucket,
                prefix=s3_prefix if s3_prefix is not None else self.contract.artifact_s3_prefix,
                step=f"{normalized_mode} build",
            )

        return BuildResult(
            mode=normalized_mode,
            binary_path=binary_path,
            commit_hash=self.resolve_commit_hash(self.layout.solver_workspace),
            artifact_dir=artifact_dir_path,
            artifact_archive=archive_path,
            upload_target=upload_target,
            warnings=tuple(warnings),
            log_path=log_path,
        )

    def prepare_seeds(self, *, log_path: Optional[str | Path] = None) -> Tuple[Path, List[str]]:
        """Run the configured tests script and discover SMT seeds from FUZZING_SEEDS."""
        if not self.contract.tests_command:
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step="seed preparation",
                message="contract does not declare a tests script or tests command",
                issues_url=self.contract.resolved_issues_url,
                hint=(
                    f"add `tests_script` or `tests_command` to `{self.contract.contract_path.name}`, "
                    "or keep using legacy `test_discovery_command`"
                ),
                category="bad contract problem",
            )

        self._ensure_workspace_exists()
        seeds_root = self.resolve_seeds_dir()
        optional_log = Path(log_path).resolve() if log_path else None
        context = self._command_context(mode="production")
        context["seeds_dir"] = str(seeds_root)
        result = self._run_command(
            step="seed preparation",
            command_template=self.contract.tests_command,
            context=context,
            log_path=optional_log,
            category="bad contract problem",
            hint="fix `tests_script`/`tests_command` so it creates FUZZING_SEEDS with .smt/.smt2 files",
            cwd=self.layout.solver_workspace,
            extra_env={"FUZZING_SEEDS": str(seeds_root)},
        )
        if not seeds_root.exists():
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step="seed preparation",
                message=f"tests command did not create FUZZING_SEEDS: {seeds_root}",
                command=result.command_string,
                log_path=result.log_path,
                stderr=result.stderr,
                issues_url=self.contract.resolved_issues_url,
                hint="create the directory named by $FUZZING_SEEDS, or configure `seeds_dir`",
                category="bad contract problem",
            )
        if not seeds_root.is_dir():
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step="seed preparation",
                message=f"FUZZING_SEEDS is not a directory: {seeds_root}",
                command=result.command_string,
                log_path=result.log_path,
                stderr=result.stderr,
                issues_url=self.contract.resolved_issues_url,
                hint="`tests.sh` must create a directory, stable symlink, or populated folder",
                category="bad contract problem",
            )

        tests = self._discover_smt_files(seeds_root)
        return seeds_root, tests

    def discover_tests(self, *, log_path: Optional[str | Path] = None) -> List[str]:
        self._ensure_workspace_exists()
        optional_log = Path(log_path).resolve() if log_path else None
        discovery_stderr: Optional[str] = None

        if self.contract.tests_command:
            _, tests = self.prepare_seeds(log_path=optional_log)
        elif self.contract.test_discovery_command:
            result = self._run_command(
                step="test discovery",
                command_template=self.contract.test_discovery_command,
                context=self._command_context(mode="production"),
                log_path=optional_log,
                category="bad contract problem",
                hint=(
                    "fix `test_discovery_command` or provide `test_root` if simple recursive "
                    "discovery is enough"
                ),
            )
            discovery_stderr = result.stderr
            tests = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        else:
            test_root = self.resolve_test_root()
            if not test_root.exists():
                raise BrainError(
                    solver_name=self.contract.solver_name,
                    repository_url=self.contract.repository_url,
                    step="test discovery",
                    message=f"test root does not exist: {test_root}",
                    issues_url=self.contract.resolved_issues_url,
                    hint=f"fix `test_root` in `{self.contract.contract_path.name}`",
                    category="bad contract problem",
                )
            tests = [
                path.relative_to(test_root).as_posix()
                for path in sorted(test_root.rglob("*"))
                if path.is_file()
            ]

        if not tests:
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step="test discovery",
                message="test discovery produced zero tests",
                log_path=optional_log,
                stderr=discovery_stderr,
                issues_url=self.contract.resolved_issues_url,
                hint="fix `test_discovery_command` or `test_root` so at least one test is discovered",
                category="bad contract problem",
            )
        return tests

    def build_matrix(
        self,
        *,
        limit_tests: Optional[int] = None,
        tests_per_job: Optional[int] = None,
        max_jobs: Optional[int] = None,
    ) -> Dict[str, object]:
        tests = maybe_limit_tests(self.discover_tests(), limit_tests)
        jobs, resolved_tests_per_job = build_jobs(
            tests,
            tests_per_job,
            max_jobs,
            self.contract.solver_name,
        )
        return {
            "matrix": {"include": jobs},
            "total_tests": len(tests),
            "total_jobs": len(jobs),
            "tests_per_job": resolved_tests_per_job,
            "solver": self.contract.solver_name,
        }

    def build_coverage_matrix(
        self,
        *,
        max_job_time_minutes: int = 60,
        buffer_minutes: int = 10,
        avg_test_time_seconds: Optional[float] = None,
        target_jobs: Optional[int] = None,
    ) -> Dict[str, object]:
        try:
            tests = self.discover_tests()
        except BrainError as exc:
            if exc.step == "test discovery" and "zero tests" in exc.message:
                return {"matrix": {"include": []}, "total_tests": 0, "total_jobs": 0}
            raise
        total_tests = len(tests)
        if total_tests == 0:
            return {"matrix": {"include": []}, "total_tests": 0, "total_jobs": 0}

        resolved_target_jobs = target_jobs or self.contract.coverage_target_job_count or 4
        resolved_avg_test_time = (
            avg_test_time_seconds
            if avg_test_time_seconds is not None
            else self.contract.coverage_average_test_time_seconds or 10.0
        )
        total_jobs, tests_per_job = calculate_job_chunks(
            total_tests=total_tests,
            target_jobs=resolved_target_jobs,
            max_job_time_minutes=max_job_time_minutes,
            buffer_minutes=buffer_minutes,
            avg_test_time_seconds=resolved_avg_test_time,
        )

        matrix_entries: List[Dict[str, object]] = []
        for job_id in range(1, total_jobs + 1):
            start_index = (job_id - 1) * tests_per_job + 1
            if start_index > total_tests:
                break
            end_index = min(job_id * tests_per_job, total_tests)
            matrix_entries.append(
                {
                    "job_name": f"{self.contract.solver_name}-coverage-{job_id}",
                    "start_index": start_index,
                    "end_index": end_index,
                }
            )

        return {
            "matrix": {"include": matrix_entries},
            "total_tests": total_tests,
            "total_jobs": len(matrix_entries),
            "tests_per_job": tests_per_job,
            "solver": self.contract.solver_name,
        }

    def run_coverage_shard(
        self,
        *,
        start_index: int,
        end_index: int,
        output_path: Optional[str | Path] = None,
        target_binary: Optional[str] = None,
        reference_binary: Optional[str] = None,
        log_path: Optional[str | Path] = None,
    ) -> Path:
        if not self.contract.coverage_mapper_command:
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step="coverage shard",
                message="contract does not declare a coverage mapper command",
                issues_url=self.contract.resolved_issues_url,
                hint=f"add `coverage_mapper_command` to `{self.contract.contract_path.name}`",
                category="bad contract problem",
            )
        if start_index < 1 or end_index < start_index:
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step="coverage shard",
                message=f"invalid shard range: {start_index}-{end_index}",
                issues_url=self.contract.resolved_issues_url,
                hint="use one-based indices and ensure end_index >= start_index",
                category="bad contract problem",
            )

        self._ensure_workspace_exists()
        build_dir = self.resolve_build_directory(mode="instrumentation")
        resolved_output = (
            Path(output_path).resolve()
            if output_path
            else (build_dir / f"coverage_mapping_{start_index}_{end_index}.json").resolve()
        )
        resolved_output.parent.mkdir(parents=True, exist_ok=True)
        optional_log = Path(log_path).resolve() if log_path else None

        context = self._command_context(
            mode="instrumentation",
            target_binary=str(self.resolve_binary(mode="instrumentation", override=target_binary)),
            reference_binary=(
                self._normalize_external_command(reference_binary, Path.cwd())
                if reference_binary
                else ""
            ),
        )
        context.update(
            {
                "build_dir": str(build_dir),
                "output_path": str(resolved_output),
            }
        )
        argv = self._parse_command_template(
            self.contract.coverage_mapper_command,
            context,
            step="coverage shard",
            hint=f"fix `coverage_mapper_command` in `{self.contract.contract_path.name}`",
        )
        argv.extend(
            [
                "--start-index",
                str(start_index),
                "--end-index",
                str(end_index),
                "--output",
                str(resolved_output),
            ]
        )
        result = self._execute_argv(
            argv=argv,
            log_path=optional_log,
            cwd=self.brain_root,
            extra_env={},
            timeout_seconds=None,
        )
        if result.returncode != 0:
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step="coverage shard",
                message="coverage mapper command failed",
                command=result.command_string,
                exit_code=result.returncode,
                log_path=result.log_path,
                stderr=result.stderr,
                issues_url=self.contract.resolved_issues_url,
                hint="check mapper stderr and the declared coverage mapper command",
                category="coverage problem",
            )
        if not resolved_output.exists():
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step="coverage shard",
                message=f"coverage mapper did not produce the expected output: {resolved_output}",
                command=result.command_string,
                log_path=result.log_path,
                issues_url=self.contract.resolved_issues_url,
                hint="ensure the coverage mapper honors `--output`",
                category="coverage problem",
            )
        return resolved_output

    def join_coverage_mappings(
        self,
        *,
        mappings_dir: str | Path,
        output_path: str | Path = "coverage_mapping.json",
        gzip_output: bool = True,
    ) -> tuple[Path, Optional[Path]]:
        input_root = Path(mappings_dir).resolve()
        if not input_root.exists():
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step="coverage join",
                message=f"coverage mappings directory not found: {input_root}",
                issues_url=self.contract.resolved_issues_url,
                hint="download or generate coverage shard artifacts first",
                category="coverage problem",
            )

        mapping_files = sorted(input_root.rglob("coverage_mapping_*.json"))
        if not mapping_files:
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step="coverage join",
                message=f"no coverage shard mappings found under {input_root}",
                issues_url=self.contract.resolved_issues_url,
                hint="ensure shard artifacts contain coverage_mapping_*.json files",
                category="coverage problem",
            )

        merged: Dict[str, set[str]] = {}
        for path in mapping_files:
            data = json.loads(path.read_text(encoding="utf-8"))
            for function_key, tests in data.items():
                merged.setdefault(function_key, set()).update(str(test) for test in tests)

        resolved_output = Path(output_path).resolve()
        resolved_output.parent.mkdir(parents=True, exist_ok=True)
        payload = {key: sorted(values) for key, values in sorted(merged.items())}
        resolved_output.write_text(
            json.dumps(payload, separators=(",", ":")),
            encoding="utf-8",
        )

        gzip_path: Optional[Path] = None
        if gzip_output:
            gzip_path = resolved_output.with_suffix(resolved_output.suffix + ".gz")
            with resolved_output.open("rb") as source, gzip.open(gzip_path, "wb") as destination:
                shutil.copyfileobj(source, destination)

        return resolved_output, gzip_path

    def count_tests(self) -> Dict[str, object]:
        tests = self.discover_tests()
        return {
            "test_count": len(tests),
            "commit_hash": self.resolve_commit_hash(self.layout.solver_workspace) or "unknown",
            "solver_version": "main",
        }

    def setup_reference(self) -> Optional[Path]:
        if self.contract.reference_contract_path:
            reference_contract = self._resolve_reference_contract_path(step="reference setup")
            reference_brain = SolverFuzzingBrain(reference_contract, workspace_root=self.layout.workspace_root)
            reference_brain.checkout_repositories()
            return reference_brain.build(mode="production").binary_path

        if self.contract.reference_setup_command:
            result = self._run_command(
                step="reference setup",
                command_template=self.contract.reference_setup_command,
                context=self._command_context(mode="production"),
                log_path=None,
                category="environment problem",
                hint=f"fix `reference_setup_command` in `{self.contract.contract_path.name}`",
                cwd=self.brain_root,
            )
            binary_from_output = self._try_extract_binary_path(result.stdout)
            if binary_from_output:
                return binary_from_output

        if self.contract.reference_binary_path:
            context = self._command_context(mode="production")
            try:
                rendered = self.contract.reference_binary_path.format(**context)
            except KeyError as exc:
                raise BrainError(
                    solver_name=self.contract.solver_name,
                    repository_url=self.contract.repository_url,
                    step="reference setup",
                    message=f"missing placeholder `{exc.args[0]}` while rendering reference_binary_path",
                    issues_url=self.contract.resolved_issues_url,
                    hint=f"fix `reference_binary_path` in `{self.contract.contract_path.name}`",
                    category="bad contract problem",
                ) from exc
            reference_binary = self._resolve_path_value(rendered, self.layout.workspace_root)
            if not reference_binary.exists():
                raise BrainError(
                    solver_name=self.contract.solver_name,
                    repository_url=self.contract.repository_url,
                    step="reference setup",
                    message=f"reference binary does not exist: {reference_binary}",
                    issues_url=self.contract.resolved_issues_url,
                    hint="fix `reference_binary_path` or `reference_setup_command`",
                    category="environment problem",
                )
            return reference_binary

        return None

    def run_regression(
        self,
        *,
        suite: Optional[str] = None,
        mode: str = "production",
        target_binary: Optional[str] = None,
        workers: Optional[int] = None,
    ) -> int:
        if not self.contract.regression_kind or not self.contract.regression_command:
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step="regression",
                message="contract does not declare regression execution",
                issues_url=self.contract.resolved_issues_url,
                hint=f"add `regression_kind` and `regression_command` to `{self.contract.contract_path.name}`",
                category="bad contract problem",
            )

        self._ensure_workspace_exists()
        resolved_target_binary = self.resolve_binary(mode=mode, override=target_binary)
        workdir = self.resolve_regression_working_directory()
        extra_env = self._environment_assignments(self.contract.regression_environment)
        worker_count = workers or max(1, os.cpu_count() or 1)

        if self.contract.regression_kind == "command":
            context = self._command_context(mode=mode, target_binary=str(resolved_target_binary))
            context.update({"suite": suite or "", "workers": str(worker_count)})
            result = self._execute_command(
                step="regression",
                command_template=self.contract.regression_command,
                context=context,
                log_path=None,
                cwd=workdir,
                extra_env=extra_env,
                timeout_seconds=self.contract.regression_timeout_seconds,
            )
            if result.stdout:
                print(result.stdout, end="")
            if result.stderr:
                print(result.stderr, file=sys.stderr, end="")
            return result.returncode

        tests = self._filter_tests_by_suite(self.discover_tests(), suite)
        if not tests:
            print("No regression tests found", file=sys.stderr)
            return 1

        failures: List[str] = []
        for index, test_name in enumerate(tests, 1):
            test_path = self.resolve_test_root() / test_name
            context = self._command_context(mode=mode, target_binary=str(resolved_target_binary))
            context.update(
                {
                    "suite": suite or "",
                    "workers": str(worker_count),
                    "test_file": test_name,
                    "test_path": str(test_path.resolve()),
                }
            )
            result = self._execute_command(
                step="regression",
                command_template=self.contract.regression_command,
                context=context,
                log_path=None,
                cwd=workdir,
                extra_env=extra_env,
                timeout_seconds=self.contract.regression_timeout_seconds,
            )
            if result.returncode == 0:
                print(f"✅ {index}/{len(tests)} {test_name}")
                continue
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

        print("All regression tests completed successfully")
        return 0

    def prepare_commit(
        self,
        *,
        commit_hash: str,
        coverage_json: str | Path,
        compile_commands: Optional[str | Path] = None,
        output_matrix: Optional[str | Path] = None,
        tests_per_job: int = 1,
        max_jobs: Optional[int] = None,
    ) -> CommandExecutionResult:
        if not self.contract.commit_prepare_command:
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step="commit preparation",
                message="contract does not declare a commit preparation command",
                issues_url=self.contract.resolved_issues_url,
                hint=f"add `commit_prepare_command` to `{self.contract.contract_path.name}`",
                category="bad contract problem",
            )

        context = self._command_context(mode="instrumentation")
        argv = self._parse_command_template(
            self.contract.commit_prepare_command,
            context,
            step="commit preparation",
            hint=f"fix `commit_prepare_command` in `{self.contract.contract_path.name}`",
        )
        argv.append(commit_hash)
        argv.extend(["--coverage-json", str(self._resolve_user_path(coverage_json))])

        resolved_compile_commands = self._resolve_compile_commands_path(compile_commands)
        if resolved_compile_commands is not None:
            argv.extend(["--compile-commands", str(resolved_compile_commands)])

        if output_matrix is not None:
            argv.extend(["--output-matrix", str(self._resolve_user_path(output_matrix))])
            argv.extend(["--tests-per-job", str(tests_per_job)])
            if max_jobs is not None:
                argv.extend(["--max-jobs", str(max_jobs)])

        return self._execute_argv(
            argv=argv,
            log_path=None,
            cwd=self.brain_root,
            extra_env={},
            timeout_seconds=None,
        )

    def prepare_commit_history(
        self,
        *,
        commits_to_analyze: int,
        coverage_json: str | Path,
        commit_hash: Optional[str] = None,
        compile_commands: Optional[str | Path] = None,
        output_matrix: Optional[str | Path] = None,
        tests_per_job: int = 1,
        max_jobs: Optional[int] = None,
        skip_coverage_enforcement: bool = False,
        min_overall_coverage: int = 80,
    ) -> int:
        if commits_to_analyze < 1:
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step="commit preparation",
                message="commits_to_analyze must be a positive integer",
                issues_url=self.contract.resolved_issues_url,
                hint="pass a value >= 1",
                category="bad contract problem",
            )

        commits = [commit_hash] if commit_hash else self._recent_commits(commits_to_analyze)
        if not commits:
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step="commit preparation",
                message="no commits found in git history",
                issues_url=self.contract.resolved_issues_url,
                hint="check out a repository with commit history before preparing commit fuzzing",
                category="environment problem",
            )

        total_functions = 0
        total_with = 0
        total_without = 0

        for index, current_commit in enumerate(commits, 1):
            print("==========================================")
            print(f"ANALYZING COMMIT {index}/{len(commits)}")
            print("==========================================")
            print(f"Commit: {current_commit}")

            result = self.prepare_commit(
                commit_hash=current_commit,
                coverage_json=coverage_json,
                compile_commands=compile_commands,
                output_matrix=output_matrix if len(commits) == 1 else None,
                tests_per_job=tests_per_job,
                max_jobs=max_jobs,
            )
            if result.stdout:
                print(result.stdout, end="")
            if result.stderr:
                print(result.stderr, file=sys.stderr, end="")
            if result.returncode != 0:
                return result.returncode

            summary_match = _SUMMARY_RE.search(result.stdout)
            if summary_match:
                total_functions += int(summary_match.group("total"))
                total_with += int(summary_match.group("with"))
                total_without += int(summary_match.group("without"))
            print("")

        overall_coverage = (100.0 * total_with / total_functions) if total_functions else 0.0
        print("==========================================")
        print("Analysis complete!")
        print("==========================================")
        print(
            "OVERALL SUMMARY: "
            f"commits={len(commits)}; "
            f"total_functions={total_functions}; "
            f"with_coverage={total_with}; "
            f"without_coverage={total_without}; "
            f"overall_coverage={overall_coverage:.1f}%"
        )

        if not skip_coverage_enforcement and int(overall_coverage) < min_overall_coverage:
            print(
                f"Minimum overall coverage ({min_overall_coverage}%) not met: "
                f"{overall_coverage:.1f}%"
            )
            return 2
        return 0

    def render_target_commands(
        self,
        *,
        mode: str,
        target_binary: Optional[str] = None,
        reference_binary: Optional[str] = None,
    ) -> List[str]:
        target_binary_value = self.resolve_binary(mode=mode, override=target_binary)
        context = self._command_context(
            mode=mode,
            target_binary=str(target_binary_value),
            reference_binary=(
                self._normalize_external_command(reference_binary, self.layout.workspace_root)
                if reference_binary
                else None
            ),
        )

        rendered: List[str] = []
        for template in self.contract.target_commands:
            argv = self._parse_command_template(
                template,
                context,
                step="target command resolution",
                hint="fix `target_commands` placeholders in the contract",
            )
            ensure_command_available(shlex.join(argv), argv[0])
            rendered.append(shlex.join(argv))
        return rendered

    def run_harness(
        self,
        *,
        tests: Sequence[str] | str,
        tests_root: Optional[str] = None,
        target_binary: Optional[str] = None,
        reference_binary: Optional[str] = None,
        mode: str = "production",
        bugs_folder: str = "bugs",
        num_workers: int = 4,
        iterations: int = 250,
        modulo: int = 2,
        time_remaining: Optional[int] = None,
        job_start_time: Optional[float] = None,
        stop_buffer_minutes: int = 5,
        job_id: Optional[str] = None,
        strict_mode: bool = False,
        harness_template: Optional[Sequence[str]] = None,
    ) -> int:
        from scripts.commit_harness_runner import run_commit_harness

        resolved_tests_root = Path(tests_root).resolve() if tests_root else self.resolve_test_root()
        targets = self.render_target_commands(
            mode=mode,
            target_binary=target_binary,
            reference_binary=reference_binary,
        )
        return run_commit_harness(
            tests=tests,
            tests_root=str(resolved_tests_root),
            bugs_folder=bugs_folder,
            num_workers=num_workers,
            iterations=iterations,
            modulo=modulo,
            time_remaining=time_remaining,
            job_start_time=job_start_time,
            stop_buffer_minutes=stop_buffer_minutes,
            targets=targets,
            job_id=job_id,
            strict_mode=strict_mode,
            harness_template=list(harness_template or TYPEFUZZ_HARNESS_TEMPLATE),
        )

    def run_oracle(
        self,
        *,
        test_file: str,
        mode: str = "production",
        target_binary: Optional[str] = None,
        reference_binary: Optional[str] = None,
        log_path: Optional[str | Path] = None,
    ) -> CommandExecutionResult:
        if not self.contract.oracle_command:
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step="oracle",
                message="contract does not declare an oracle command",
                issues_url=self.contract.resolved_issues_url,
                hint=f"add `oracle_command` to `{self.contract.contract_path.name}`",
                category="bad contract problem",
            )
        context = self._command_context(
            mode=mode,
            target_binary=str(self.resolve_binary(mode=mode, override=target_binary)),
            reference_binary=(
                self._normalize_external_command(reference_binary, self.layout.workspace_root)
                if reference_binary
                else None
            ),
            test_file=test_file,
        )
        log_file = Path(log_path).resolve() if log_path else None
        return self._execute_command(
            step="oracle",
            command_template=self.contract.oracle_command,
            context=context,
            log_path=log_file,
        )

    def resolve_binary(self, *, mode: str, override: Optional[str] = None) -> Path:
        normalized_mode = self._normalize_mode(mode)
        if override:
            return self._resolve_path_value(override, Path.cwd())
        configured = (
            self.contract.production_binary_path
            if normalized_mode == "production"
            else self.contract.coverage_binary_path
        )
        return self._resolve_path_value(configured, self.layout.solver_workspace)

    def resolve_build_directory(self, *, mode: str) -> Path:
        binary_path = self.resolve_binary(mode=mode)
        if binary_path.parent.name == "bin":
            return binary_path.parent.parent
        return binary_path.parent

    def resolve_test_root(self) -> Path:
        if self.contract.tests_command:
            return self.resolve_seeds_dir()
        if not self.contract.test_root:
            return self.layout.tests_workspace
        return self._resolve_path_value(self.contract.test_root, self.layout.tests_workspace)

    def resolve_seeds_dir(self) -> Path:
        configured = self.contract.seeds_dir or "FUZZING_SEEDS"
        return self._resolve_path_value(configured, self.layout.tests_workspace)

    def resolve_regression_working_directory(self) -> Path:
        configured = self.contract.regression_working_directory
        if not configured:
            return self.layout.solver_workspace
        return self._resolve_path_value(configured, self.layout.solver_workspace)

    def resolve_commit_hash(self, repository_root: Path) -> Optional[str]:
        if not (repository_root / ".git").exists():
            return None
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        value = result.stdout.strip()
        return value or None

    def collect_artifacts(self, destination_dir: str | Path) -> List[str]:
        dest = Path(destination_dir).resolve()
        dest.mkdir(parents=True, exist_ok=True)
        warnings: List[str] = []

        for entry in self.contract.artifact_paths:
            prefix, relative = self._artifact_base_and_relative(entry)
            source = self._resolve_path_value(relative, prefix)
            if not source.exists():
                warnings.append(f"missing artifact path: {entry}")
                continue
            target = dest / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            else:
                shutil.copy2(source, target)
        return warnings

    def create_artifact_archive(
        self,
        artifact_dir: str | Path,
        output_path: str | Path,
    ) -> Path:
        source_dir = Path(artifact_dir).resolve()
        destination = Path(output_path).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(destination, "w:gz") as archive:
            for path in sorted(source_dir.rglob("*")):
                archive.add(path, arcname=path.relative_to(source_dir))
        return destination

    def upload_to_s3(
        self,
        source_path: str | Path,
        *,
        bucket: Optional[str],
        prefix: Optional[str],
        step: str,
    ) -> str:
        if not bucket:
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step=step,
                message="S3 upload requested but no bucket is configured",
                issues_url=self.contract.resolved_issues_url,
                hint="set `artifact_s3_bucket` in the contract or pass --s3-bucket",
                category="environment problem",
            )

        try:
            import boto3  # type: ignore
        except Exception as exc:  # pragma: no cover - exercised by mocks
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step=step,
                message=f"failed to import boto3: {exc}",
                issues_url=self.contract.resolved_issues_url,
                hint="install boto3 or disable S3 upload",
                category="environment problem",
            ) from exc

        source = Path(source_path).resolve()
        if not source.exists():
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step=step,
                message=f"cannot upload missing artifact: {source}",
                issues_url=self.contract.resolved_issues_url,
                hint="collect artifacts locally before uploading to S3",
                category="artifact problem",
            )

        key_prefix = (prefix or "").strip("/")
        key = f"{key_prefix}/{source.name}" if key_prefix else source.name

        try:
            client = boto3.client("s3")
            client.upload_file(str(source), bucket, key)
        except Exception as exc:
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step=step,
                message=f"failed to upload artifact to s3://{bucket}/{key}: {exc}",
                issues_url=self.contract.resolved_issues_url,
                hint="local artifacts were kept; fix credentials, bucket, or prefix and retry",
                category="environment problem",
            ) from exc

        return f"s3://{bucket}/{key}"

    def collect_existing_artifacts(
        self,
        *,
        artifacts_dir: Optional[str | Path] = None,
        artifact_archive: Optional[str | Path] = None,
        upload_s3: bool = False,
        s3_bucket: Optional[str] = None,
        s3_prefix: Optional[str] = None,
    ) -> ArtifactResult:
        self._ensure_workspace_exists()

        artifact_dir_path = Path(artifacts_dir).resolve() if artifacts_dir else None
        if artifact_dir_path:
            shutil.rmtree(artifact_dir_path, ignore_errors=True)
            artifact_dir_path.mkdir(parents=True, exist_ok=True)

        warnings: List[str] = []
        if artifact_dir_path:
            warnings.extend(self.collect_artifacts(artifact_dir_path))

        archive_path: Optional[Path] = None
        if artifact_dir_path and artifact_archive:
            archive_path = Path(artifact_archive).resolve()
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            self.create_artifact_archive(artifact_dir_path, archive_path)

        upload_target = None
        if upload_s3:
            if archive_path is None:
                raise BrainError(
                    solver_name=self.contract.solver_name,
                    repository_url=self.contract.repository_url,
                    step="artifact upload",
                    message="cannot upload artifacts because no archive was created",
                    issues_url=self.contract.resolved_issues_url,
                    hint="pass both --artifacts-dir and --artifact-archive before enabling S3 upload",
                    category="artifact problem",
                )
            upload_target = self.upload_to_s3(
                archive_path,
                bucket=s3_bucket or self.contract.artifact_s3_bucket,
                prefix=s3_prefix if s3_prefix is not None else self.contract.artifact_s3_prefix,
                step="artifact upload",
            )

        return ArtifactResult(
            artifact_dir=artifact_dir_path,
            artifact_archive=archive_path,
            upload_target=upload_target,
            warnings=tuple(warnings),
        )

    def validate_integration(
        self,
        *,
        run_script_checks: bool = False,
        script_check_workspace: Optional[str | Path] = None,
    ) -> Dict[str, object]:
        """Validate contract wiring without requiring a real solver build by default."""
        self._validate_layout()
        checked_commands = self._validate_static_commands()

        script_checks: Dict[str, object] = {}
        if run_script_checks:
            script_checks = self._run_lightweight_script_checks(script_check_workspace)

        return {
            "solver_name": self.contract.solver_name,
            "contract_path": str(self.contract.contract_path),
            "repository_url": self.contract.repository_url,
            "workspace_layout": {
                "workspace_root": str(self.layout.workspace_root),
                "solver_workspace": str(self.layout.solver_workspace),
                "tests_workspace": str(self.layout.tests_workspace),
                "seeds_dir": str(self.resolve_seeds_dir()),
            },
            "checked_commands": checked_commands,
            "script_checks": script_checks,
            "status": "ok",
        }

    def _validate_layout(self) -> None:
        if self.layout.solver_workspace == self.layout.workspace_root:
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step="contract validation",
                message="repository_path resolves to the workspace root",
                issues_url=self.contract.resolved_issues_url,
                hint="set `repository_path` to a child directory such as the solver name",
                category="bad contract problem",
            )
        if self.contract.uses_split_test_repository and self.layout.tests_workspace == self.layout.solver_workspace:
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step="contract validation",
                message="tests_repository_path resolves to the solver workspace",
                issues_url=self.contract.resolved_issues_url,
                hint="use a distinct tests repository path for split-repo mode",
                category="bad contract problem",
            )

    def _validate_static_commands(self) -> List[str]:
        context = self._command_context(
            mode="production",
            target_binary="/tmp/fmfuzz-target",
            reference_binary="/tmp/fmfuzz-reference",
        )
        context.update(
            {
                "build_dir": str(self.resolve_build_directory(mode="instrumentation")),
                "output_path": "/tmp/fmfuzz-coverage.json",
            }
        )
        checks: List[Tuple[str, Optional[str], str]] = [
            ("build_command", self.contract.build_command, "fix `build_command` or `build_script`"),
            (
                "coverage_build_command",
                self.contract.coverage_build_command,
                "fix `coverage_build_command` or provide a build script that accepts --instrumented",
            ),
            (
                "coverage_mapper_command",
                self.contract.coverage_mapper_command,
                "fix `coverage_mapper_command`",
            ),
            (
                "commit_prepare_command",
                self.contract.commit_prepare_command,
                "fix `commit_prepare_command`",
            ),
            (
                "reference_setup_command",
                self.contract.reference_setup_command,
                "fix `reference_setup_command`",
            ),
        ]
        checked: List[str] = []
        for label, command_template, hint in checks:
            if not command_template:
                continue
            self._validate_command_resolves(label, command_template, context, hint)
            checked.append(label)

        if self.contract.tests_command:
            self._validate_command_resolves(
                "tests_command",
                self.contract.tests_command,
                context,
                "fix `tests_command` or `tests_script`",
            )
            checked.append("tests_command")
        elif self.contract.test_discovery_command:
            self._validate_command_resolves(
                "test_discovery_command",
                self.contract.test_discovery_command,
                context,
                "fix `test_discovery_command` or use `tests_script`",
            )
            checked.append("test_discovery_command")
        else:
            test_root = self.resolve_test_root()
            if not test_root.exists():
                raise BrainError(
                    solver_name=self.contract.solver_name,
                    repository_url=self.contract.repository_url,
                    step="contract validation",
                    message=f"test root does not exist: {test_root}",
                    command=str(test_root),
                    issues_url=self.contract.resolved_issues_url,
                    hint=f"fix `test_root` in `{self.contract.contract_path.name}`",
                    category="bad contract problem",
                )
            checked.append("test_root")

        for index, command_template in enumerate(self.contract.target_commands, start=1):
            argv = self._parse_command_template(
                command_template,
                context,
                step="contract validation",
                hint="fix `target_commands` placeholders in the contract",
            )
            executable = argv[0]
            placeholder_values = set()
            for key in ("target_binary", "reference_binary"):
                value = context.get(key)
                if not value:
                    continue
                placeholder_values.add(value)
                placeholder_values.add(str(Path(value).resolve()))
            if executable not in placeholder_values:
                self._validate_executable_token(
                    f"target_commands[{index}]",
                    executable,
                    "fix `target_commands` placeholders in the contract",
                )
            checked.append(f"target_commands[{index}]")
        if self.contract.reference_contract_path:
            reference_contract = self._resolve_reference_contract_path(step="contract validation")
            load_solver_contract(reference_contract)
            checked.append("reference_contract_path")
        return checked

    def _validate_command_resolves(
        self,
        label: str,
        command_template: str,
        context: Dict[str, str],
        hint: str,
    ) -> None:
        argv = self._parse_command_template(
            command_template,
            context,
            step="contract validation",
            hint=hint,
        )
        self._validate_executable_token(label, argv[0], hint)

    def _validate_executable_token(self, label: str, executable: str, hint: str) -> None:
        if os.path.sep in executable or executable.startswith("."):
            if not Path(executable).exists():
                raise BrainError(
                    solver_name=self.contract.solver_name,
                    repository_url=self.contract.repository_url,
                    step="contract validation",
                    message=f"{label} executable does not exist: {executable}",
                    command=executable,
                    issues_url=self.contract.resolved_issues_url,
                    hint=hint,
                    category="bad contract problem",
                )
            return
        if shutil.which(executable) is None:
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step="contract validation",
                message=f"{label} executable is not on PATH: {executable}",
                command=executable,
                issues_url=self.contract.resolved_issues_url,
                hint=hint,
                category="environment problem",
            )

    def _run_lightweight_script_checks(
        self,
        script_check_workspace: Optional[str | Path],
    ) -> Dict[str, object]:
        if script_check_workspace is None:
            temp_dir = tempfile.TemporaryDirectory(prefix="fmfuzz-doctor-")
            self._script_check_temp_dir = temp_dir  # keep alive until method returns
            workspace_root = Path(temp_dir.name)
        else:
            workspace_root = Path(script_check_workspace).resolve()
            workspace_root.mkdir(parents=True, exist_ok=True)

        check_brain = SolverFuzzingBrain(self.contract.contract_path, workspace_root=workspace_root)
        check_brain.layout.solver_workspace.mkdir(parents=True, exist_ok=True)
        check_brain.layout.tests_workspace.mkdir(parents=True, exist_ok=True)

        production = check_brain.build(mode="production")
        instrumentation = check_brain.build(mode="instrumentation")
        seeds_payload: Optional[Dict[str, object]] = None
        if check_brain.contract.tests_command:
            seeds_dir, tests = check_brain.prepare_seeds()
            seeds_payload = {"seeds_dir": str(seeds_dir), "test_count": len(tests)}

        return {
            "production_binary": str(production.binary_path),
            "instrumentation_binary": str(instrumentation.binary_path),
            "seeds": seeds_payload,
        }

    def _checkout_repository(
        self,
        *,
        repository_url: str,
        destination: Path,
        commit_hash: Optional[str],
        label: str,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if (destination / ".git").exists():
            self._run_checkout_git(
                ["git", "fetch", "--all", "--tags"],
                cwd=destination,
                repository_url=repository_url,
                label=label,
                hint="check network access, repository permissions, and remote configuration",
            )
        elif destination.exists() and any(destination.iterdir()):
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=repository_url,
                step=f"{label} checkout",
                message=f"destination exists but is not a git checkout: {destination}",
                issues_url=self.contract.resolved_issues_url,
                hint="remove the directory or point the contract to an empty checkout path",
                category="environment problem",
            )
        else:
            self._run_checkout_git(
                ["git", "clone", repository_url, str(destination)],
                cwd=destination.parent,
                repository_url=repository_url,
                label=label,
                hint="check repository_url, credentials, and network access",
            )

        if commit_hash:
            self._run_checkout_git(
                ["git", "checkout", commit_hash],
                cwd=destination,
                repository_url=repository_url,
                label=label,
                hint="check that the requested commit exists in the configured repository",
            )

    def _run_checkout_git(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        repository_url: str,
        label: str,
        hint: str,
    ) -> None:
        result = subprocess.run(
            list(argv),
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return
        raise BrainError(
            solver_name=self.contract.solver_name,
            repository_url=repository_url,
            step=f"{label} checkout",
            message="git command failed",
            command=shlex.join(list(argv)),
            exit_code=result.returncode,
            stderr=result.stderr,
            issues_url=self.contract.resolved_issues_url,
            hint=hint,
            category="checkout problem",
        )

    def _ensure_workspace_exists(self) -> None:
        if not self.layout.solver_workspace.exists():
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step="workspace validation",
                message=f"solver workspace not found: {self.layout.solver_workspace}",
                issues_url=self.contract.resolved_issues_url,
                hint="run checkout first or fix `repository_path` in the contract",
                category="bad contract problem",
            )
        if self.contract.uses_split_test_repository and not self.layout.tests_workspace.exists():
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step="workspace validation",
                message=f"tests workspace not found: {self.layout.tests_workspace}",
                issues_url=self.contract.resolved_issues_url,
                hint="run checkout first or fix `tests_repository_path` in the contract",
                category="bad contract problem",
            )

    def _command_context(
        self,
        *,
        mode: str,
        target_binary: Optional[str] = None,
        reference_binary: Optional[str] = None,
        test_file: Optional[str] = None,
    ) -> Dict[str, str]:
        normalized_mode = self._normalize_mode(mode)
        context = {
            "brain_workspace": str(self.brain_root),
            "workspace_root": str(self.layout.workspace_root),
            "solver_workspace": str(self.layout.solver_workspace),
            "tests_workspace": str(self.layout.tests_workspace),
            "mode": normalized_mode,
            "production_binary": str(
                self._resolve_path_value(
                    self.contract.production_binary_path,
                    self.layout.solver_workspace,
                )
            ),
            "coverage_binary": str(
                self._resolve_path_value(
                    self.contract.coverage_binary_path,
                    self.layout.solver_workspace,
                )
            ),
            "seeds_dir": str(self.resolve_seeds_dir()),
        }
        if target_binary is not None:
            context["target_binary"] = target_binary
        if reference_binary is not None:
            context["reference_binary"] = reference_binary
        if test_file is not None:
            context["test_file"] = test_file
        return context

    def _normalize_mode(self, mode: str) -> str:
        normalized = mode.strip().lower()
        aliases = {
            "coverage": "instrumentation",
            "instrumented": "instrumentation",
            "instrumented-coverage": "instrumentation",
        }
        normalized = aliases.get(normalized, normalized)
        if normalized not in {"production", "instrumentation"}:
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step="mode validation",
                message=f"unsupported mode: {mode}",
                issues_url=self.contract.resolved_issues_url,
                hint="use `production` or `instrumentation`",
                category="bad contract problem",
            )
        return normalized

    def _discover_smt_files(self, root: Path) -> List[str]:
        return [
            path.relative_to(root).as_posix()
            for path in sorted(root.rglob("*"))
            if path.is_file() and path.suffix.lower() in {".smt", ".smt2"}
        ]

    def _resolve_path_value(self, value: str, base: Path) -> Path:
        prefixes = {
            "solver:": self.layout.solver_workspace,
            "tests:": self.layout.tests_workspace,
            "brain:": self.brain_root,
            "workspace:": self.layout.workspace_root,
            "contract:": self.contract.contract_path.parent,
        }
        for prefix, prefix_base in prefixes.items():
            if value.startswith(prefix):
                relative = value[len(prefix) :].lstrip("/")
                return (prefix_base / relative).resolve()
        candidate = Path(value)
        if candidate.is_absolute():
            return candidate.resolve()
        return (base / candidate).resolve()

    def _resolve_contract_path_value(self, value: str) -> Path:
        prefixes = ("solver:", "tests:", "brain:", "workspace:", "contract:")
        if value.startswith(prefixes):
            return self._resolve_path_value(value, self.brain_root)

        candidate = Path(value)
        if candidate.is_absolute():
            return candidate.resolve()

        candidates = [
            (self.brain_root / candidate).resolve(),
            (self.layout.workspace_root / candidate).resolve(),
            (self.contract.contract_path.parent / candidate).resolve(),
        ]
        seen: set[Path] = set()
        unique_candidates: List[Path] = []
        for path in candidates:
            if path not in seen:
                seen.add(path)
                unique_candidates.append(path)

        for path in unique_candidates:
            if path.exists():
                return path

        if candidate.parent == Path("."):
            return (self.contract.contract_path.parent / candidate).resolve()
        return (self.brain_root / candidate).resolve()

    def _resolve_reference_contract_path(self, *, step: str) -> Path:
        value = self.contract.reference_contract_path
        if not value:
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step=step,
                message="reference_contract_path is not configured",
                command="reference_contract_path",
                issues_url=self.contract.resolved_issues_url,
                hint="add `reference_contract_path` or use `reference_setup_command`",
                category="bad contract problem",
            )
        reference_contract = self._resolve_contract_path_value(value)
        if not reference_contract.exists():
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step=step,
                message=f"reference contract file does not exist: {reference_contract}",
                command=value,
                issues_url=self.contract.resolved_issues_url,
                hint=(
                    "`reference_contract_path` is repo-root relative by default; use "
                    "`contract:path.yml` for a path relative to the current contract file"
                ),
                category="bad contract problem",
            )
        if not reference_contract.is_file():
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step=step,
                message=f"reference contract path is not a file: {reference_contract}",
                command=value,
                issues_url=self.contract.resolved_issues_url,
                hint="point `reference_contract_path` at a YAML contract file",
                category="bad contract problem",
            )
        return reference_contract

    def _normalize_external_command(self, value: str, base: Path) -> str:
        token = value.strip()
        path_candidate = Path(token)
        if path_candidate.is_absolute():
            return str(path_candidate.resolve())
        if os.path.sep in token or token.startswith("."):
            return str((base / path_candidate).resolve())
        return token

    def _artifact_base_and_relative(self, entry: str) -> tuple[Path, str]:
        prefixes = {
            "solver:": self.layout.solver_workspace,
            "tests:": self.layout.tests_workspace,
            "brain:": self.brain_root,
            "workspace:": self.layout.workspace_root,
        }
        for prefix, base in prefixes.items():
            if entry.startswith(prefix):
                relative = entry[len(prefix) :].lstrip("/")
                return base, relative
        return self.layout.solver_workspace, entry

    def _extract_binary_path(
        self,
        stdout: str,
        configured_path: str,
        *,
        step: str,
        log_path: Optional[Path],
    ) -> Path:
        binary_path_value: Optional[str] = None
        for line in reversed(stdout.splitlines()):
            stripped = line.strip()
            if stripped.startswith("BINARY_PATH="):
                binary_path_value = stripped.split("=", 1)[1].strip()
                break
        if not binary_path_value:
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step=step,
                message="build output did not include a final `BINARY_PATH=...` line",
                log_path=log_path,
                issues_url=self.contract.resolved_issues_url,
                hint="update build.sh to print `BINARY_PATH=/abs/path/to/binary` on the final stdout line",
                category="bad contract problem",
            )

        binary_path = self._resolve_path_value(binary_path_value, self.layout.solver_workspace)
        self._verify_executable(binary_path, configured_path, step=step, log_path=log_path)
        return binary_path

    def _try_extract_binary_path(self, stdout: str) -> Optional[Path]:
        for line in reversed(stdout.splitlines()):
            stripped = line.strip()
            if stripped.startswith("BINARY_PATH="):
                return self._resolve_path_value(
                    stripped.split("=", 1)[1].strip(),
                    self.layout.solver_workspace,
                )
        return None

    def _verify_executable(
        self,
        binary_path: Path,
        configured_path: str,
        *,
        step: str,
        log_path: Optional[Path],
    ) -> None:
        if not binary_path.exists():
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step=step,
                message=f"resolved binary does not exist: {binary_path}",
                log_path=log_path,
                issues_url=self.contract.resolved_issues_url,
                hint=f"`{configured_path}` did not resolve to an existing file",
                category="build problem",
            )
        if not binary_path.is_file():
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step=step,
                message=f"resolved binary is not a file: {binary_path}",
                log_path=log_path,
                issues_url=self.contract.resolved_issues_url,
                hint=f"`{configured_path}` must point to an executable file",
                category="build problem",
            )
        if not os.access(binary_path, os.X_OK):
            mode_value = binary_path.stat().st_mode
            if not mode_value & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
                raise BrainError(
                    solver_name=self.contract.solver_name,
                    repository_url=self.contract.repository_url,
                    step=step,
                    message=f"resolved binary is not executable: {binary_path}",
                    log_path=log_path,
                    issues_url=self.contract.resolved_issues_url,
                    hint=f"`{configured_path}` must resolve to an executable file",
                    category="build problem",
                )

    def _parse_command_template(
        self,
        template: str,
        context: Dict[str, str],
        *,
        step: str,
        hint: str,
    ) -> List[str]:
        try:
            rendered = template.format(**context)
        except KeyError as exc:
            missing = exc.args[0]
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step=step,
                message=f"missing placeholder `{missing}` while rendering contract command",
                command=template,
                issues_url=self.contract.resolved_issues_url,
                hint=hint,
                category="bad contract problem",
            ) from exc

        try:
            argv = shlex.split(rendered)
        except ValueError as exc:
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step=step,
                message=f"failed to parse command `{rendered}`: {exc}",
                command=template,
                issues_url=self.contract.resolved_issues_url,
                hint=hint,
                category="bad contract problem",
            ) from exc

        if not argv:
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step=step,
                message="rendered command is empty",
                command=template,
                issues_url=self.contract.resolved_issues_url,
                hint=hint,
                category="bad contract problem",
            )

        argv[0] = self._resolve_command_executable(argv[0])
        return argv

    def _resolve_command_executable(self, executable: str) -> str:
        token = executable.strip()
        if not token:
            return token
        path_candidate = Path(token)
        if path_candidate.is_absolute():
            return str(path_candidate.resolve())
        for base in (
            self.brain_root,
            self.contract.contract_path.parent,
            self.layout.solver_workspace,
            self.layout.tests_workspace,
            self.layout.workspace_root,
        ):
            candidate = (base / path_candidate).resolve()
            if candidate.exists():
                return str(candidate)
        return token

    def _execute_command(
        self,
        *,
        step: str,
        command_template: str,
        context: Dict[str, str],
        log_path: Optional[Path],
        cwd: Optional[Path] = None,
        extra_env: Optional[Dict[str, str]] = None,
        timeout_seconds: Optional[int] = None,
    ) -> CommandExecutionResult:
        argv = self._parse_command_template(
            command_template,
            context,
            step=step,
            hint=f"fix `{step}` command in `{self.contract.contract_path.name}`",
        )
        return self._execute_argv(
            argv=argv,
            log_path=log_path,
            cwd=cwd or self.brain_root,
            extra_env=extra_env or {},
            timeout_seconds=timeout_seconds,
        )

    def _execute_argv(
        self,
        *,
        argv: Sequence[str],
        log_path: Optional[Path],
        cwd: Path,
        extra_env: Dict[str, str],
        timeout_seconds: Optional[int],
    ) -> CommandExecutionResult:
        environment = os.environ.copy()
        environment.update(
            {
                "BRAIN_WORKSPACE": str(self.brain_root),
                "WORKSPACE_ROOT": str(self.layout.workspace_root),
                "SOLVER_WORKSPACE": str(self.layout.solver_workspace),
                "TESTS_WORKSPACE": str(self.layout.tests_workspace),
                "FUZZING_SEEDS": str(self.resolve_seeds_dir()),
                "PYTHONPATH": self._pythonpath_with_root(os.environ.get("PYTHONPATH")),
            }
        )
        environment.update(extra_env)

        try:
            result = subprocess.run(
                list(argv),
                cwd=cwd,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
            stdout = result.stdout
            stderr = result.stderr
            returncode = result.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            returncode = 124

        if log_path:
            self._write_command_log(
                log_path=log_path,
                argv=list(argv),
                cwd=cwd,
                stdout=stdout,
                stderr=stderr,
            )

        return CommandExecutionResult(
            argv=list(argv),
            cwd=cwd,
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            log_path=log_path,
        )

    def _run_command(
        self,
        *,
        step: str,
        command_template: str,
        context: Dict[str, str],
        log_path: Optional[Path],
        category: str,
        hint: str,
        cwd: Optional[Path] = None,
        extra_env: Optional[Dict[str, str]] = None,
        timeout_seconds: Optional[int] = None,
    ) -> CommandExecutionResult:
        result = self._execute_command(
            step=step,
            command_template=command_template,
            context=context,
            log_path=log_path,
            cwd=cwd,
            extra_env=extra_env,
            timeout_seconds=timeout_seconds,
        )
        if result.returncode != 0:
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step=step,
                message="command failed",
                command=result.command_string,
                exit_code=result.returncode,
                log_path=result.log_path,
                stderr=result.stderr,
                issues_url=self.contract.resolved_issues_url,
                hint=hint,
                category=category,
            )
        return result

    def _write_command_log(
        self,
        *,
        log_path: Path,
        argv: Sequence[str],
        cwd: Path,
        stdout: str,
        stderr: str,
    ) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "\n".join(
                [
                    f"command: {shlex.join(list(argv))}",
                    f"cwd: {cwd}",
                    "",
                    "stdout:",
                    stdout.rstrip(),
                    "",
                    "stderr:",
                    stderr.rstrip(),
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def _pythonpath_with_root(self, current: Optional[str]) -> str:
        if not current:
            return str(self.brain_root)
        return f"{self.brain_root}{os.pathsep}{current}"

    def _resolve_user_path(self, value: str | Path) -> Path:
        candidate = Path(value)
        if candidate.is_absolute():
            return candidate.resolve()
        return (Path.cwd() / candidate).resolve()

    def _resolve_compile_commands_path(
        self,
        compile_commands: Optional[str | Path],
    ) -> Optional[Path]:
        if compile_commands is not None:
            return self._resolve_user_path(compile_commands)

        build_dir = self.layout.solver_workspace / "build"
        compile_commands_json = build_dir / "compile_commands.json"
        if compile_commands_json.exists():
            return compile_commands_json.resolve()
        if build_dir.exists():
            return build_dir.resolve()
        return None

    def _recent_commits(self, limit: int) -> List[str]:
        result = subprocess.run(
            ["git", "log", "--format=%H", "-n", str(limit)],
            cwd=self.layout.solver_workspace,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def _environment_assignments(self, entries: Sequence[str]) -> Dict[str, str]:
        assignments: Dict[str, str] = {}
        for entry in entries:
            key, separator, value = entry.partition("=")
            if not separator:
                assignments[key] = os.environ.get(key, "")
            else:
                assignments[key] = value
        return assignments

    def _filter_tests_by_suite(self, tests: Sequence[str], suite: Optional[str]) -> List[str]:
        if not suite:
            return list(tests)
        normalized = Path(suite).as_posix().lstrip("./")
        for prefix in ("test/regression/", "regression/"):
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix) :]
        return [
            test_name
            for test_name in tests
            if test_name == normalized or test_name.startswith(f"{normalized}/")
        ]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Contract-driven solver fuzzing brain")
    parser.add_argument(
        "--contract",
        required=True,
        help="Path to the solver contract YAML file",
    )
    parser.add_argument(
        "--workspace-root",
        default=None,
        help="Workspace root where repository_path/tests_repository_path are resolved",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    checkout = subparsers.add_parser("checkout", help="Clone/fetch/check out the configured repositories")
    checkout.add_argument("--commit-hash", default=None)
    checkout.add_argument("--tests-commit-hash", default=None)
    checkout.add_argument("--json", action="store_true")

    describe = subparsers.add_parser("describe", help="Print the resolved contract as JSON")
    describe.add_argument("--json", action="store_true")

    validate = subparsers.add_parser("validate", aliases=["doctor"], help="Validate the solver contract integration")
    validate.add_argument("--run-script-checks", action="store_true")
    validate.add_argument("--script-check-workspace", default=None)
    validate.add_argument("--json", action="store_true")

    build = subparsers.add_parser("build", help="Run the configured build and optionally collect artifacts")
    build.add_argument("--mode", choices=["production", "instrumentation", "instrumented", "coverage"], required=True)
    build.add_argument("--build-arg", action="append", default=[])
    build.add_argument("--artifacts-dir", default=None)
    build.add_argument("--artifact-archive", default=None)
    build.add_argument("--upload-s3", action="store_true")
    build.add_argument("--s3-bucket", default=None)
    build.add_argument("--s3-prefix", default=None)
    build.add_argument("--json", action="store_true")

    collect = subparsers.add_parser(
        "collect-artifacts",
        help="Collect contract-declared artifacts from an existing workspace and optionally archive them",
    )
    collect.add_argument("--artifacts-dir", required=True)
    collect.add_argument("--artifact-archive", default=None)
    collect.add_argument("--upload-s3", action="store_true")
    collect.add_argument("--s3-bucket", default=None)
    collect.add_argument("--s3-prefix", default=None)
    collect.add_argument("--json", action="store_true")

    discover = subparsers.add_parser("discover-tests", help="Run contract-driven test discovery")
    discover.add_argument("--log-path", default=None)
    discover.add_argument("--json", action="store_true")

    seeds = subparsers.add_parser("prepare-seeds", help="Run tests.sh/tests_command and discover FUZZING_SEEDS")
    seeds.add_argument("--log-path", default=None)
    seeds.add_argument("--json", action="store_true")

    matrix = subparsers.add_parser("matrix", help="Build a commit-fuzzer matrix from contract-driven discovery")
    matrix.add_argument("--limit-tests", type=int, default=None)
    matrix.add_argument("--tests-per-job", type=int, default=None)
    matrix.add_argument("--max-jobs", type=int, default=None)
    matrix.add_argument("--output", default=None)

    coverage_matrix = subparsers.add_parser(
        "coverage-matrix",
        help="Build a coverage-mapping shard matrix from contract-driven discovery",
    )
    coverage_matrix.add_argument("--max-job-time", type=int, default=60)
    coverage_matrix.add_argument("--buffer", type=int, default=10)
    coverage_matrix.add_argument("--avg-test-time", type=float, default=None)
    coverage_matrix.add_argument("--target-jobs", type=int, default=None)
    coverage_matrix.add_argument("--output", default=None)

    coverage_shard = subparsers.add_parser(
        "run-coverage-shard",
        help="Run the contract-declared coverage mapper for one shard",
    )
    coverage_shard.add_argument("--start-index", type=int, required=True)
    coverage_shard.add_argument("--end-index", type=int, required=True)
    coverage_shard.add_argument("--output", default=None)
    coverage_shard.add_argument("--target-binary", default=None)
    coverage_shard.add_argument("--reference-binary", default=None)
    coverage_shard.add_argument("--log-path", default=None)
    coverage_shard.add_argument("--json", action="store_true")

    coverage_join = subparsers.add_parser(
        "join-coverage",
        help="Merge downloaded coverage shard mappings into one mapping artifact",
    )
    coverage_join.add_argument("--mappings-dir", required=True)
    coverage_join.add_argument("--output", default="coverage_mapping.json")
    coverage_join.add_argument("--no-gzip", action="store_true")
    coverage_join.add_argument("--json", action="store_true")

    count_tests = subparsers.add_parser(
        "count-tests",
        help="Count tests through contract-driven discovery and emit metadata",
    )
    count_tests.add_argument("--json", action="store_true")

    reference = subparsers.add_parser("setup-reference", help="Prepare the contract-declared reference binary")
    reference.add_argument("--json", action="store_true")

    harness = subparsers.add_parser("run-harness", help="Run the shared commit harness from the contract")
    harness.add_argument("--tests-json", required=True)
    harness.add_argument("--tests-root", default=None)
    harness.add_argument("--job-id", default=None)
    harness.add_argument("--mode", choices=["production", "instrumentation", "instrumented", "coverage"], default="production")
    harness.add_argument("--target-binary", default=None)
    harness.add_argument("--reference-binary", default=None)
    harness.add_argument("--bugs-folder", default="bugs")
    harness.add_argument("--workers", type=int, default=max(1, os.cpu_count() or 1))
    harness.add_argument("--iterations", type=int, default=250)
    harness.add_argument("--modulo", type=int, default=2)
    harness.add_argument("--time-remaining", type=int, default=None)
    harness.add_argument("--job-start-time", type=float, default=None)
    harness.add_argument("--stop-buffer-minutes", type=int, default=5)
    harness.add_argument("--strict", action="store_true")

    oracle = subparsers.add_parser("oracle", help="Run the contract-declared oracle command")
    oracle.add_argument("--test-file", required=True)
    oracle.add_argument("--mode", choices=["production", "instrumentation", "instrumented", "coverage"], default="production")
    oracle.add_argument("--target-binary", default=None)
    oracle.add_argument("--reference-binary", default=None)
    oracle.add_argument("--log-path", default=None)

    prepare_commit = subparsers.add_parser(
        "prepare-commit",
        help="Run the contract-declared commit preparation command for one commit",
    )
    prepare_commit.add_argument("commit")
    prepare_commit.add_argument("--coverage-json", required=True)
    prepare_commit.add_argument("--compile-commands", default=None)
    prepare_commit.add_argument("--output-matrix", default=None)
    prepare_commit.add_argument("--tests-per-job", type=int, default=1)
    prepare_commit.add_argument("--max-jobs", type=int, default=None)

    prepare_commits = subparsers.add_parser(
        "prepare-commits",
        help="Run the shared commit-preparation history loop for the contract",
    )
    prepare_commits.add_argument("commits_to_analyze", type=int)
    prepare_commits.add_argument("--coverage-json", required=True)
    prepare_commits.add_argument("--commit-hash", default=None)
    prepare_commits.add_argument("--compile-commands", default=None)
    prepare_commits.add_argument("--output-matrix", default=None)
    prepare_commits.add_argument("--tests-per-job", type=int, default=1)
    prepare_commits.add_argument("--max-jobs", type=int, default=None)
    prepare_commits.add_argument("--skip-coverage-enforcement", action="store_true")
    prepare_commits.add_argument("--min-overall-coverage", type=int, default=80)

    regression = subparsers.add_parser("run-regression", help="Run contract-driven regression execution")
    regression.add_argument("--suite", default=None)
    regression.add_argument("--mode", choices=["production", "instrumentation", "instrumented", "coverage"], default="production")
    regression.add_argument("--target-binary", default=None)
    regression.add_argument("--workers", type=int, default=max(1, os.cpu_count() or 1))

    upload = subparsers.add_parser("upload-artifact", help="Upload an existing artifact to S3")
    upload.add_argument("--source", required=True)
    upload.add_argument("--s3-bucket", default=None)
    upload.add_argument("--s3-prefix", default=None)
    upload.add_argument("--step", default="artifact upload")
    upload.add_argument("--json", action="store_true")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        brain = SolverFuzzingBrain(args.contract, workspace_root=args.workspace_root)

        if args.command == "checkout":
            payload = brain.checkout_repositories(
                commit_hash=args.commit_hash,
                tests_commit_hash=args.tests_commit_hash,
            )
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(f"solver_commit_hash={payload.get('solver_commit_hash') or ''}")
                if "tests_commit_hash" in payload:
                    print(f"tests_commit_hash={payload.get('tests_commit_hash') or ''}")
            return 0

        if args.command == "describe":
            payload = {
                "artifact_paths": list(brain.contract.artifact_paths),
                "artifact_s3_bucket": brain.contract.artifact_s3_bucket,
                "artifact_s3_prefix": brain.contract.artifact_s3_prefix,
                "build_command": brain.contract.build_command,
                "build_script": brain.contract.build_script,
                "commit_prepare_command": brain.contract.commit_prepare_command,
                "contract_path": str(brain.contract.contract_path),
                "coverage_average_test_time_seconds": brain.contract.coverage_average_test_time_seconds,
                "coverage_binary_path": brain.contract.coverage_binary_path,
                "coverage_build_command": brain.contract.coverage_build_command,
                "coverage_mapper_command": brain.contract.coverage_mapper_command,
                "coverage_target_job_count": brain.contract.coverage_target_job_count,
                "environment_requirements": {
                    "env": list(brain.contract.environment_requirements.env),
                    "packages": list(brain.contract.environment_requirements.packages),
                },
                "issues_url": brain.contract.resolved_issues_url,
                "oracle_command": brain.contract.oracle_command,
                "production_binary_path": brain.contract.production_binary_path,
                "reference_binary_path": brain.contract.reference_binary_path,
                "reference_contract_path": brain.contract.reference_contract_path,
                "reference_setup_command": brain.contract.reference_setup_command,
                "regression_command": brain.contract.regression_command,
                "regression_environment": list(brain.contract.regression_environment),
                "regression_kind": brain.contract.regression_kind,
                "regression_timeout_seconds": brain.contract.regression_timeout_seconds,
                "regression_working_directory": brain.contract.regression_working_directory,
                "repository_path": brain.contract.repository_path,
                "repository_url": brain.contract.repository_url,
                "solver_name": brain.contract.solver_name,
                "target_commands": list(brain.contract.target_commands),
                "seeds_dir": brain.contract.seeds_dir,
                "test_discovery_command": brain.contract.test_discovery_command,
                "test_root": brain.contract.test_root,
                "tests_command": brain.contract.tests_command,
                "tests_repository_path": brain.contract.tests_repository_path,
                "tests_repository_url": brain.contract.tests_repository_url,
                "tests_script": brain.contract.tests_script,
                "workspace_layout": {
                    "solver_workspace": str(brain.layout.solver_workspace),
                    "tests_workspace": str(brain.layout.tests_workspace),
                    "workspace_root": str(brain.layout.workspace_root),
                },
            }
            print(json.dumps(payload, indent=2 if args.json else None, sort_keys=args.json))
            return 0

        if args.command in {"validate", "doctor"}:
            payload = brain.validate_integration(
                run_script_checks=args.run_script_checks,
                script_check_workspace=args.script_check_workspace,
            )
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(f"✅ {brain.contract.solver_name} contract validation passed")
            return 0

        if args.command == "build":
            result = brain.build(
                mode=args.mode,
                extra_args=args.build_arg,
                artifacts_dir=args.artifacts_dir,
                artifact_archive=args.artifact_archive,
                upload_s3=args.upload_s3,
                s3_bucket=args.s3_bucket,
                s3_prefix=args.s3_prefix,
            )
            payload = {
                "artifact_archive": str(result.artifact_archive) if result.artifact_archive else None,
                "artifact_dir": str(result.artifact_dir) if result.artifact_dir else None,
                "binary_path": str(result.binary_path),
                "commit_hash": result.commit_hash,
                "log_path": str(result.log_path) if result.log_path else None,
                "mode": result.mode,
                "solver_name": brain.contract.solver_name,
                "upload_target": result.upload_target,
                "warnings": list(result.warnings),
            }
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(f"BINARY_PATH={result.binary_path}")
            return 0

        if args.command == "collect-artifacts":
            result = brain.collect_existing_artifacts(
                artifacts_dir=args.artifacts_dir,
                artifact_archive=args.artifact_archive,
                upload_s3=args.upload_s3,
                s3_bucket=args.s3_bucket,
                s3_prefix=args.s3_prefix,
            )
            payload = {
                "artifact_archive": str(result.artifact_archive) if result.artifact_archive else None,
                "artifact_dir": str(result.artifact_dir) if result.artifact_dir else None,
                "solver_name": brain.contract.solver_name,
                "upload_target": result.upload_target,
                "warnings": list(result.warnings),
            }
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            elif result.artifact_archive:
                print(result.artifact_archive)
            return 0

        if args.command == "count-tests":
            payload = brain.count_tests()
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(json.dumps(payload))
            return 0

        if args.command == "setup-reference":
            reference_binary = brain.setup_reference()
            payload = {
                "reference_binary": str(reference_binary) if reference_binary else None,
                "solver_name": brain.contract.solver_name,
            }
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            elif reference_binary:
                print(reference_binary)
            return 0

        if args.command == "discover-tests":
            tests = brain.discover_tests(log_path=args.log_path)
            if args.json:
                print(json.dumps(tests, indent=2))
            else:
                for test in tests:
                    print(test)
            return 0

        if args.command == "prepare-seeds":
            seeds_dir, tests = brain.prepare_seeds(log_path=args.log_path)
            payload = {"seeds_dir": str(seeds_dir), "tests": tests, "total_tests": len(tests)}
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                for test in tests:
                    print(test)
            return 0

        if args.command == "matrix":
            matrix_payload = brain.build_matrix(
                limit_tests=args.limit_tests,
                tests_per_job=args.tests_per_job,
                max_jobs=args.max_jobs,
            )
            if args.output:
                output_path = Path(args.output).resolve()
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(
                    json.dumps(matrix_payload, indent=2, sort_keys=False) + "\n",
                    encoding="utf-8",
                )
                print(f"✅ Matrix written to {output_path}")
            else:
                print(json.dumps(matrix_payload, indent=2))
            return 0

        if args.command == "coverage-matrix":
            matrix_payload = brain.build_coverage_matrix(
                max_job_time_minutes=args.max_job_time,
                buffer_minutes=args.buffer,
                avg_test_time_seconds=args.avg_test_time,
                target_jobs=args.target_jobs,
            )
            if args.output:
                output_path = Path(args.output).resolve()
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(
                    json.dumps(matrix_payload, indent=2, sort_keys=False) + "\n",
                    encoding="utf-8",
                )
                print(f"✅ Matrix written to {output_path}")
            else:
                print(json.dumps(matrix_payload, indent=2))
            return 0

        if args.command == "run-coverage-shard":
            output = brain.run_coverage_shard(
                start_index=args.start_index,
                end_index=args.end_index,
                output_path=args.output,
                target_binary=args.target_binary,
                reference_binary=args.reference_binary,
                log_path=args.log_path,
            )
            payload = {"output_path": str(output)}
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(output)
            return 0

        if args.command == "join-coverage":
            output_path, gzip_path = brain.join_coverage_mappings(
                mappings_dir=args.mappings_dir,
                output_path=args.output,
                gzip_output=not args.no_gzip,
            )
            payload = {
                "output_path": str(output_path),
                "gzip_path": str(gzip_path) if gzip_path else None,
            }
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            elif gzip_path is not None:
                print(gzip_path)
            else:
                print(output_path)
            return 0

        if args.command == "run-harness":
            return brain.run_harness(
                tests=args.tests_json,
                tests_root=args.tests_root,
                target_binary=args.target_binary,
                reference_binary=args.reference_binary,
                mode=args.mode,
                bugs_folder=args.bugs_folder,
                num_workers=args.workers,
                iterations=args.iterations,
                modulo=args.modulo,
                time_remaining=args.time_remaining,
                job_start_time=args.job_start_time,
                stop_buffer_minutes=args.stop_buffer_minutes,
                job_id=args.job_id,
                strict_mode=args.strict,
            )

        if args.command == "oracle":
            result = brain.run_oracle(
                test_file=args.test_file,
                mode=args.mode,
                target_binary=args.target_binary,
                reference_binary=args.reference_binary,
                log_path=args.log_path,
            )
            if result.stdout:
                print(result.stdout, end="")
            if result.stderr:
                print(result.stderr, file=sys.stderr, end="")
            return result.returncode

        if args.command == "prepare-commit":
            result = brain.prepare_commit(
                commit_hash=args.commit,
                coverage_json=args.coverage_json,
                compile_commands=args.compile_commands,
                output_matrix=args.output_matrix,
                tests_per_job=args.tests_per_job,
                max_jobs=args.max_jobs,
            )
            if result.stdout:
                print(result.stdout, end="")
            if result.stderr:
                print(result.stderr, file=sys.stderr, end="")
            return result.returncode

        if args.command == "prepare-commits":
            return brain.prepare_commit_history(
                commits_to_analyze=args.commits_to_analyze,
                coverage_json=args.coverage_json,
                commit_hash=args.commit_hash,
                compile_commands=args.compile_commands,
                output_matrix=args.output_matrix,
                tests_per_job=args.tests_per_job,
                max_jobs=args.max_jobs,
                skip_coverage_enforcement=args.skip_coverage_enforcement,
                min_overall_coverage=args.min_overall_coverage,
            )

        if args.command == "run-regression":
            return brain.run_regression(
                suite=args.suite,
                mode=args.mode,
                target_binary=args.target_binary,
                workers=args.workers,
            )

        if args.command == "upload-artifact":
            upload_target = brain.upload_to_s3(
                args.source,
                bucket=args.s3_bucket or brain.contract.artifact_s3_bucket,
                prefix=args.s3_prefix if args.s3_prefix is not None else brain.contract.artifact_s3_prefix,
                step=args.step,
            )
            if args.json:
                print(json.dumps({"upload_target": upload_target}, indent=2))
            else:
                print(upload_target)
            return 0

    except ContractError as exc:
        print(exc.render(), file=sys.stderr)
        return 1
    except BrainError as exc:
        print(exc.render(), file=sys.stderr)
        return 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
