#!/usr/bin/env python3
"""Shared contract-driven build, discovery, harness, and artifact logic."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.local_commit_fuzzer_matrix import (  # noqa: E402
    build_jobs,
    maybe_limit_tests,
)
from scripts.solver_contract import (  # noqa: E402
    ContractError,
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
        self.issues_url = issues_url
        self.hint = hint
        self.category = category

    def render(self) -> str:
        lines = [f"[solver={self.solver_name}][step={self.step}] {self.message}"]
        if self.category:
            lines.append(f"category: {self.category}")
        if self.command:
            lines.append(f"command: {self.command}")
        if self.exit_code is not None:
            lines.append(f"exit code: {self.exit_code}")
        lines.append(f"repo: {self.repository_url}")
        if self.issues_url:
            lines.append(f"issue tracker: {self.issues_url}")
        if self.log_path:
            lines.append(f"log: {self.log_path}")
        if self.hint:
            lines.append(f"hint: {self.hint}")
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


def ensure_command_available(command: str, label: str) -> None:
    """Fail fast if a required command cannot be resolved."""
    executable = shlex.split(command)[0]
    if os.path.sep in executable or executable.startswith("."):
        if not Path(executable).exists():
            raise ValueError(f"{label} not found at: {executable}")
        return
    if shutil.which(executable) is None:
        raise ValueError(f"{label} not found in PATH: {executable}")


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
            result = self._run_command(
                step=f"{normalized_mode} build",
                command_template=command_template,
                context=self._command_context(mode=normalized_mode),
                log_path=log_path,
                category="build problem",
                hint=(
                    f"{normalized_mode}_binary_path did not resolve to an executable"
                    if normalized_mode == "instrumentation"
                    else "production_binary_path did not resolve to an executable"
                ),
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

    def discover_tests(self, *, log_path: Optional[str | Path] = None) -> List[str]:
        self._ensure_workspace_exists()
        optional_log = Path(log_path).resolve() if log_path else None

        if self.contract.test_discovery_command:
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

    def resolve_test_root(self) -> Path:
        if not self.contract.test_root:
            return self.layout.tests_workspace
        return self._resolve_path_value(self.contract.test_root, self.layout.tests_workspace)

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
            subprocess.run(
                ["git", "fetch", "--all", "--tags"],
                cwd=destination,
                check=True,
                capture_output=True,
                text=True,
            )
        elif destination.exists() and any(destination.iterdir()):
            raise BrainError(
                solver_name=self.contract.solver_name,
                repository_url=self.contract.repository_url,
                step=f"{label} checkout",
                message=f"destination exists but is not a git checkout: {destination}",
                issues_url=self.contract.resolved_issues_url,
                hint="remove the directory or point the contract to an empty checkout path",
                category="environment problem",
            )
        else:
            subprocess.run(
                ["git", "clone", repository_url, str(destination)],
                check=True,
                capture_output=True,
                text=True,
            )

        if commit_hash:
            subprocess.run(
                ["git", "checkout", commit_hash],
                cwd=destination,
                check=True,
                capture_output=True,
                text=True,
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

    def _resolve_path_value(self, value: str, base: Path) -> Path:
        candidate = Path(value)
        if candidate.is_absolute():
            return candidate.resolve()
        return (base / candidate).resolve()

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
        if os.path.sep in token or token.startswith("."):
            brain_candidate = (self.brain_root / path_candidate).resolve()
            if brain_candidate.exists():
                return str(brain_candidate)
        return token

    def _execute_command(
        self,
        *,
        step: str,
        command_template: str,
        context: Dict[str, str],
        log_path: Optional[Path],
    ) -> CommandExecutionResult:
        argv = self._parse_command_template(
            command_template,
            context,
            step=step,
            hint=f"fix `{step}` command in `{self.contract.contract_path.name}`",
        )
        environment = os.environ.copy()
        environment.update(
            {
                "BRAIN_WORKSPACE": str(self.brain_root),
                "WORKSPACE_ROOT": str(self.layout.workspace_root),
                "SOLVER_WORKSPACE": str(self.layout.solver_workspace),
                "TESTS_WORKSPACE": str(self.layout.tests_workspace),
                "PYTHONPATH": self._pythonpath_with_root(os.environ.get("PYTHONPATH")),
            }
        )

        result = subprocess.run(
            argv,
            cwd=self.brain_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if log_path:
            self._write_command_log(
                log_path=log_path,
                argv=argv,
                cwd=self.brain_root,
                stdout=result.stdout,
                stderr=result.stderr,
            )

        return CommandExecutionResult(
            argv=argv,
            cwd=self.brain_root,
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
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
    ) -> CommandExecutionResult:
        result = self._execute_command(
            step=step,
            command_template=command_template,
            context=context,
            log_path=log_path,
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

    build = subparsers.add_parser("build", help="Run the configured build and optionally collect artifacts")
    build.add_argument("--mode", choices=["production", "instrumentation"], required=True)
    build.add_argument("--artifacts-dir", default=None)
    build.add_argument("--artifact-archive", default=None)
    build.add_argument("--upload-s3", action="store_true")
    build.add_argument("--s3-bucket", default=None)
    build.add_argument("--s3-prefix", default=None)
    build.add_argument("--json", action="store_true")

    discover = subparsers.add_parser("discover-tests", help="Run contract-driven test discovery")
    discover.add_argument("--log-path", default=None)
    discover.add_argument("--json", action="store_true")

    matrix = subparsers.add_parser("matrix", help="Build a commit-fuzzer matrix from contract-driven discovery")
    matrix.add_argument("--limit-tests", type=int, default=None)
    matrix.add_argument("--tests-per-job", type=int, default=None)
    matrix.add_argument("--max-jobs", type=int, default=None)
    matrix.add_argument("--output", default=None)

    harness = subparsers.add_parser("run-harness", help="Run the shared commit harness from the contract")
    harness.add_argument("--tests-json", required=True)
    harness.add_argument("--tests-root", default=None)
    harness.add_argument("--job-id", default=None)
    harness.add_argument("--mode", choices=["production", "instrumentation"], default="production")
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
    oracle.add_argument("--mode", choices=["production", "instrumentation"], default="production")
    oracle.add_argument("--target-binary", default=None)
    oracle.add_argument("--reference-binary", default=None)
    oracle.add_argument("--log-path", default=None)

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
                "contract_path": str(brain.contract.contract_path),
                "coverage_binary_path": brain.contract.coverage_binary_path,
                "coverage_build_command": brain.contract.coverage_build_command,
                "environment_requirements": {
                    "env": list(brain.contract.environment_requirements.env),
                    "packages": list(brain.contract.environment_requirements.packages),
                },
                "issues_url": brain.contract.resolved_issues_url,
                "oracle_command": brain.contract.oracle_command,
                "production_binary_path": brain.contract.production_binary_path,
                "repository_path": brain.contract.repository_path,
                "repository_url": brain.contract.repository_url,
                "solver_name": brain.contract.solver_name,
                "target_commands": list(brain.contract.target_commands),
                "test_discovery_command": brain.contract.test_discovery_command,
                "test_root": brain.contract.test_root,
                "tests_repository_path": brain.contract.tests_repository_path,
                "tests_repository_url": brain.contract.tests_repository_url,
                "workspace_layout": {
                    "solver_workspace": str(brain.layout.solver_workspace),
                    "tests_workspace": str(brain.layout.tests_workspace),
                    "workspace_root": str(brain.layout.workspace_root),
                },
            }
            print(json.dumps(payload, indent=2 if args.json else None, sort_keys=args.json))
            return 0

        if args.command == "build":
            result = brain.build(
                mode=args.mode,
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

        if args.command == "discover-tests":
            tests = brain.discover_tests(log_path=args.log_path)
            if args.json:
                print(json.dumps(tests, indent=2))
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
        print(str(exc), file=sys.stderr)
        return 1
    except BrainError as exc:
        print(exc.render(), file=sys.stderr)
        return 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
