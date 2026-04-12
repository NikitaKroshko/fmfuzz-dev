#!/usr/bin/env python3
"""Contract parsing and validation for contract-driven solver fuzzing."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


class ContractError(ValueError):
    """Raised when a solver contract is missing data or is malformed."""

    def __init__(
        self,
        message: str,
        *,
        solver_name: Optional[str] = None,
        step: str = "contract validation",
        command: Optional[str] = None,
        hint: Optional[str] = None,
        issues_url: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.solver_name = solver_name
        self.step = step
        self.command = command
        self.hint = hint
        self.issues_url = issues_url

    def render(self) -> str:
        if not self.solver_name and not self.command and not self.hint and not self.issues_url:
            return self.message
        solver = self.solver_name or "<unknown>"
        lines = [f"[solver={solver}][step={self.step}] {self.message}"]
        if self.command:
            lines.append(f"command: {self.command}")
        if self.issues_url:
            lines.append(f"issue tracker: {self.issues_url}")
        if self.hint:
            lines.append(f"hint: {self.hint}")
        return "\n".join(lines)


_INTEGER_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?(?:\d+\.\d*|\d*\.\d+)$")
_GITHUB_REPOSITORY_RE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:)(?P<slug>[^/]+/[^/.]+)(?:\.git)?/?$"
)


@dataclass(frozen=True)
class EnvironmentRequirements:
    packages: Tuple[str, ...] = ()
    env: Tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkspaceLayout:
    workspace_root: Path
    solver_workspace: Path
    tests_workspace: Path


@dataclass(frozen=True)
class SolverContract:
    contract_path: Path
    solver_name: str
    repository_url: str
    repository_path: str
    issues_url: Optional[str]
    tests_repository_url: Optional[str]
    tests_repository_path: Optional[str]
    build_script: Optional[str]
    tests_script: Optional[str]
    build_command: str
    coverage_build_command: str
    production_binary_path: str
    coverage_binary_path: str
    tests_command: Optional[str]
    seeds_dir: Optional[str]
    test_root: Optional[str]
    test_discovery_command: Optional[str]
    target_commands: Tuple[str, ...]
    reference_setup_command: Optional[str]
    reference_binary_path: Optional[str]
    reference_contract_path: Optional[str]
    oracle_command: Optional[str]
    artifact_paths: Tuple[str, ...]
    artifact_s3_bucket: Optional[str]
    artifact_s3_prefix: Optional[str]
    coverage_target_job_count: Optional[int]
    coverage_average_test_time_seconds: Optional[float]
    coverage_mapper_command: Optional[str]
    commit_prepare_command: Optional[str]
    regression_kind: Optional[str]
    regression_command: Optional[str]
    regression_working_directory: Optional[str]
    regression_environment: Tuple[str, ...]
    regression_timeout_seconds: Optional[int]
    environment_requirements: EnvironmentRequirements

    @property
    def resolved_issues_url(self) -> Optional[str]:
        if self.issues_url:
            return self.issues_url
        return derive_github_issues_url(self.repository_url)

    @property
    def uses_split_test_repository(self) -> bool:
        return bool(self.tests_repository_url and self.tests_repository_path)

    def resolve_layout(self, workspace_root: str | Path) -> WorkspaceLayout:
        root = Path(workspace_root).resolve()
        solver_workspace = _resolve_workspace_path(root, self.repository_path)
        if self.uses_split_test_repository:
            tests_workspace = _resolve_workspace_path(root, self.tests_repository_path or "")
        else:
            tests_workspace = solver_workspace
        return WorkspaceLayout(
            workspace_root=root,
            solver_workspace=solver_workspace,
            tests_workspace=tests_workspace,
        )


@dataclass(frozen=True)
class _YamlLine:
    line_no: int
    indent: int
    content: str


def derive_github_issues_url(repository_url: str) -> Optional[str]:
    match = _GITHUB_REPOSITORY_RE.match(repository_url.strip())
    if not match:
        return None
    return f"https://github.com/{match.group('slug')}/issues"


def load_solver_contract(contract_path: str | Path) -> SolverContract:
    path = Path(contract_path).resolve()
    if not path.exists():
        raise ContractError(f"contract file not found: {path}")

    try:
        parsed = _load_yaml_subset(path.read_text(encoding="utf-8"))
    except ContractError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        raise ContractError(f"failed to parse contract {path}: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ContractError(f"contract {path} must contain a top-level mapping")
    return _build_contract(path, parsed)


def _build_contract(contract_path: Path, data: Dict[str, Any]) -> SolverContract:
    solver_name = _required_string(data, "solver_name", contract_path, solver_name=None)
    repository_url = _required_string(data, "repository_url", contract_path, solver_name)
    repository_path = _optional_string(data.get("repository_path")) or solver_name

    build_script = _optional_string(data.get("build_script"))
    build_command = (
        _optional_string(data.get("build_command"))
        or build_script
        or _missing_string("build_command", contract_path, solver_name)
    )
    coverage_build_command = (
        _optional_string(data.get("coverage_build_command"))
        or _optional_string(data.get("instrumented_build_command"))
        or _optional_string(data.get("instrumentation_build_command"))
        or (f"{build_script} --instrumented" if build_script else None)
        or _missing_string("coverage_build_command", contract_path, solver_name)
    )

    binary_path = _optional_string(data.get("binary_path"))
    production_binary_path = (
        _optional_string(data.get("production_binary_path"))
        or binary_path
        or _missing_string("production_binary_path", contract_path, solver_name)
    )
    coverage_binary_path = (
        _optional_string(data.get("coverage_binary_path"))
        or _optional_string(data.get("instrumented_binary_path"))
        or _optional_string(data.get("instrumentation_binary_path"))
        or binary_path
        or production_binary_path
    )

    tests_script = _optional_string(data.get("tests_script"))
    tests_command = _optional_string(data.get("tests_command")) or tests_script
    seeds_dir = _optional_string(data.get("seeds_dir"))
    if tests_command and not seeds_dir:
        seeds_dir = "FUZZING_SEEDS"
    test_root = _optional_string(data.get("test_root"))
    test_discovery_command = _optional_string(data.get("test_discovery_command"))
    if not test_root and not test_discovery_command and not tests_command:
        raise ContractError(
            f"missing `test_discovery_command` or `test_root` for solver "
            f"`{solver_name}` in `{contract_path.name}`",
            solver_name=solver_name,
            hint="provide `tests_script`/`tests_command` for FUZZING_SEEDS, or keep legacy `test_discovery_command`/`test_root`",
            issues_url=derive_github_issues_url(repository_url),
        )

    tests_repository_url = _optional_string(data.get("tests_repository_url"))
    tests_repository_path = _optional_string(data.get("tests_repository_path"))
    if tests_repository_url and not tests_repository_path:
        tests_repository_path = f"{solver_name}-tests"
    if tests_repository_path and not tests_repository_url:
        raise ContractError(
            f"solver `{solver_name}` in `{contract_path.name}` must provide both "
            "`tests_repository_url` and `tests_repository_path` for split-repo mode",
            solver_name=solver_name,
            hint="remove `tests_repository_path` or add `tests_repository_url`",
            issues_url=derive_github_issues_url(repository_url),
        )

    target_commands = tuple(
        _string_list(
            data.get("target_commands"),
            "target_commands",
            contract_path,
            solver_name,
            allow_empty=False,
        )
    )
    artifact_paths = tuple(
        _string_list(
            data.get("artifact_paths", []),
            "artifact_paths",
            contract_path,
            solver_name,
            allow_empty=True,
        )
    )
    issues_url = _optional_string(data.get("issues_url"))
    artifact_s3_bucket = _optional_string(data.get("artifact_s3_bucket"))
    artifact_s3_prefix = _optional_string(data.get("artifact_s3_prefix"))
    reference_setup_command = _optional_string(data.get("reference_setup_command"))
    reference_binary_path = _optional_string(data.get("reference_binary_path"))
    reference_contract_path = _optional_string(data.get("reference_contract_path"))
    oracle_command = _optional_string(data.get("oracle_command"))
    coverage_target_job_count = _optional_int(data.get("coverage_target_job_count"))
    coverage_average_test_time_seconds = _optional_float(
        data.get("coverage_average_test_time_seconds")
    )
    coverage_mapper_command = _optional_string(data.get("coverage_mapper_command"))
    commit_prepare_command = _optional_string(data.get("commit_prepare_command"))
    regression_kind = _optional_string(data.get("regression_kind"))
    regression_command = _optional_string(data.get("regression_command"))
    regression_working_directory = _optional_string(data.get("regression_working_directory"))
    regression_timeout_seconds = _optional_int(data.get("regression_timeout_seconds"))

    if regression_kind and regression_kind not in {"command", "per-test"}:
        raise ContractError(
            f"`regression_kind` for solver `{solver_name}` in `{contract_path.name}` "
            "must be `command` or `per-test`",
            solver_name=solver_name,
            hint="use `command` for one suite command or `per-test` for one command per discovered seed",
            issues_url=derive_github_issues_url(repository_url),
        )
    if regression_kind and not regression_command:
        raise ContractError(
            f"`regression_command` is required when `regression_kind` is set for solver "
            f"`{solver_name}` in `{contract_path.name}`",
            solver_name=solver_name,
            hint="add `regression_command` or remove `regression_kind`",
            issues_url=derive_github_issues_url(repository_url),
        )
    regression_environment = tuple(
        _string_list(
            data.get("regression_environment", []),
            "regression_environment",
            contract_path,
            solver_name,
            allow_empty=True,
        )
    )

    environment_raw = data.get("environment_requirements", {})
    if environment_raw is None:
        environment_raw = {}
    if not isinstance(environment_raw, dict):
        raise ContractError(
            f"`environment_requirements` for solver `{solver_name}` in "
            f"`{contract_path.name}` must be a mapping",
            solver_name=solver_name,
            hint="use `environment_requirements: {packages: [...], env: [...]}`",
            issues_url=derive_github_issues_url(repository_url),
        )
    environment_requirements = EnvironmentRequirements(
        packages=tuple(
            _string_list(
                environment_raw.get("packages", []),
                "environment_requirements.packages",
                contract_path,
                solver_name,
                allow_empty=True,
            )
        ),
        env=tuple(
            _string_list(
                environment_raw.get("env", []),
                "environment_requirements.env",
                contract_path,
                solver_name,
                allow_empty=True,
            )
        ),
    )

    return SolverContract(
        contract_path=contract_path,
        solver_name=solver_name,
        repository_url=repository_url,
        repository_path=repository_path,
        issues_url=issues_url,
        tests_repository_url=tests_repository_url,
        tests_repository_path=tests_repository_path,
        build_script=build_script,
        tests_script=tests_script,
        build_command=build_command,
        coverage_build_command=coverage_build_command,
        production_binary_path=production_binary_path,
        coverage_binary_path=coverage_binary_path,
        tests_command=tests_command,
        seeds_dir=seeds_dir,
        test_root=test_root,
        test_discovery_command=test_discovery_command,
        target_commands=target_commands,
        reference_setup_command=reference_setup_command,
        reference_binary_path=reference_binary_path,
        reference_contract_path=reference_contract_path,
        oracle_command=oracle_command,
        artifact_paths=artifact_paths,
        artifact_s3_bucket=artifact_s3_bucket,
        artifact_s3_prefix=artifact_s3_prefix,
        coverage_target_job_count=coverage_target_job_count,
        coverage_average_test_time_seconds=coverage_average_test_time_seconds,
        coverage_mapper_command=coverage_mapper_command,
        commit_prepare_command=commit_prepare_command,
        regression_kind=regression_kind,
        regression_command=regression_command,
        regression_working_directory=regression_working_directory,
        regression_environment=regression_environment,
        regression_timeout_seconds=regression_timeout_seconds,
        environment_requirements=environment_requirements,
    )


def _required_string(
    data: Dict[str, Any],
    field_name: str,
    contract_path: Path,
    solver_name: Optional[str],
) -> str:
    value = _optional_string(data.get(field_name))
    if value is None:
        return _missing_string(field_name, contract_path, solver_name)
    return value


def _missing_string(field_name: str, contract_path: Path, solver_name: Optional[str]) -> str:
    solver_label = solver_name or "<unknown>"
    raise ContractError(
        f"missing `{field_name}` for solver `{solver_label}` in `{contract_path.name}`",
        solver_name=solver_name,
        hint=f"add `{field_name}` to the solver contract",
    )


def _optional_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip()
        return normalized if normalized else None
    return str(value).strip() or None


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        if _INTEGER_RE.match(normalized):
            return int(normalized)
    raise ContractError(f"expected integer-compatible value, got {value!r}")


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        if _INTEGER_RE.match(normalized) or _FLOAT_RE.match(normalized):
            return float(normalized)
    raise ContractError(f"expected float-compatible value, got {value!r}")


def _string_list(
    value: Any,
    field_name: str,
    contract_path: Path,
    solver_name: str,
    *,
    allow_empty: bool,
) -> List[str]:
    if value is None:
        if allow_empty:
            return []
        raise ContractError(
            f"missing `{field_name}` for solver `{solver_name}` in `{contract_path.name}`"
        )
    if not isinstance(value, list):
        raise ContractError(
            f"`{field_name}` for solver `{solver_name}` in `{contract_path.name}` "
            "must be a list of strings"
        )
    if not allow_empty and not value:
        raise ContractError(
            f"`{field_name}` for solver `{solver_name}` in `{contract_path.name}` "
            "must not be empty"
        )
    items: List[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ContractError(
                f"`{field_name}` for solver `{solver_name}` in `{contract_path.name}` "
                "must contain only strings"
            )
        normalized = item.strip()
        if not normalized:
            raise ContractError(
                f"`{field_name}` for solver `{solver_name}` in `{contract_path.name}` "
                "must not contain empty strings"
            )
        items.append(normalized)
    return items


def _resolve_workspace_path(workspace_root: Path, configured_path: str) -> Path:
    candidate = Path(configured_path)
    if candidate.is_absolute():
        return candidate.resolve()
    return (workspace_root / candidate).resolve()


def _load_yaml_subset(text: str) -> Any:
    lines = _tokenize_yaml(text)
    if not lines:
        return {}
    value, next_index = _parse_block(lines, 0, lines[0].indent)
    if next_index != len(lines):
        extra = lines[next_index]
        raise ContractError(
            f"unexpected trailing content on line {extra.line_no}: {extra.content!r}"
        )
    return value


def _tokenize_yaml(text: str) -> List[_YamlLine]:
    result: List[_YamlLine] = []
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped in {"---", "..."} or stripped.startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if "\t" in raw_line[:indent]:
            raise ContractError(f"tabs are not supported in YAML indentation (line {line_no})")
        result.append(_YamlLine(line_no=line_no, indent=indent, content=raw_line[indent:]))
    return result


def _parse_block(lines: Sequence[_YamlLine], index: int, indent: int) -> Tuple[Any, int]:
    if index >= len(lines):
        raise ContractError("unexpected end of contract while parsing YAML")

    line = lines[index]
    if line.indent < indent:
        raise ContractError(
            f"invalid indentation on line {line.line_no}: expected at least {indent} spaces"
        )
    if line.content.startswith("- "):
        return _parse_sequence(lines, index, indent)
    return _parse_mapping(lines, index, indent)


def _parse_mapping(lines: Sequence[_YamlLine], index: int, indent: int) -> Tuple[Dict[str, Any], int]:
    mapping: Dict[str, Any] = {}
    while index < len(lines):
        line = lines[index]
        if line.indent < indent:
            break
        if line.indent > indent:
            raise ContractError(
                f"unexpected indentation on line {line.line_no}: {line.content!r}"
            )
        if line.content.startswith("- "):
            raise ContractError(
                f"unexpected list item on line {line.line_no}: {line.content!r}"
            )
        if ":" not in line.content:
            raise ContractError(
                f"expected `key: value` on line {line.line_no}: {line.content!r}"
            )

        key, remainder = line.content.split(":", 1)
        key = key.strip()
        if not key:
            raise ContractError(f"empty mapping key on line {line.line_no}")
        remainder = remainder.strip()

        if remainder:
            mapping[key] = _parse_scalar(remainder)
            index += 1
            continue

        index += 1
        if index >= len(lines) or lines[index].indent <= indent:
            mapping[key] = None
            continue
        value, index = _parse_block(lines, index, lines[index].indent)
        mapping[key] = value
    return mapping, index


def _parse_sequence(lines: Sequence[_YamlLine], index: int, indent: int) -> Tuple[List[Any], int]:
    items: List[Any] = []
    while index < len(lines):
        line = lines[index]
        if line.indent < indent:
            break
        if line.indent > indent:
            raise ContractError(
                f"unexpected indentation on line {line.line_no}: {line.content!r}"
            )
        if not line.content.startswith("- "):
            break

        remainder = line.content[2:].strip()
        index += 1
        if remainder:
            items.append(_parse_scalar(remainder))
            continue
        if index >= len(lines) or lines[index].indent <= indent:
            items.append(None)
            continue
        value, index = _parse_block(lines, index, lines[index].indent)
        items.append(value)
    return items, index


def _parse_scalar(value: str) -> Any:
    normalized = value.strip()
    lowered = normalized.lower()
    if lowered in {"null", "~"}:
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if _INTEGER_RE.match(normalized):
        return int(normalized)
    if _FLOAT_RE.match(normalized):
        return float(normalized)
    if normalized in {"[]", "{}"}:
        return ast.literal_eval(normalized)
    if (normalized.startswith('"') and normalized.endswith('"')) or (
        normalized.startswith("'") and normalized.endswith("'")
    ):
        return ast.literal_eval(normalized)
    return normalized
