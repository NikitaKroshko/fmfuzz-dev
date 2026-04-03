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


_INTEGER_RE = re.compile(r"^-?\d+$")
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
    build_command: str
    coverage_build_command: str
    production_binary_path: str
    coverage_binary_path: str
    test_root: Optional[str]
    test_discovery_command: Optional[str]
    target_commands: Tuple[str, ...]
    oracle_command: Optional[str]
    artifact_paths: Tuple[str, ...]
    artifact_s3_bucket: Optional[str]
    artifact_s3_prefix: Optional[str]
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
    repository_path = _required_string(data, "repository_path", contract_path, solver_name)
    build_command = _required_string(data, "build_command", contract_path, solver_name)
    coverage_build_command = _required_string(
        data, "coverage_build_command", contract_path, solver_name
    )
    production_binary_path = _required_string(
        data, "production_binary_path", contract_path, solver_name
    )
    coverage_binary_path = _required_string(
        data, "coverage_binary_path", contract_path, solver_name
    )

    test_root = _optional_string(data.get("test_root"))
    test_discovery_command = _optional_string(data.get("test_discovery_command"))
    if not test_root and not test_discovery_command:
        raise ContractError(
            f"missing `test_discovery_command` or `test_root` for solver "
            f"`{solver_name}` in `{contract_path.name}`"
        )

    tests_repository_url = _optional_string(data.get("tests_repository_url"))
    tests_repository_path = _optional_string(data.get("tests_repository_path"))
    if bool(tests_repository_url) != bool(tests_repository_path):
        raise ContractError(
            f"solver `{solver_name}` in `{contract_path.name}` must provide both "
            "`tests_repository_url` and `tests_repository_path` for split-repo mode"
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
    oracle_command = _optional_string(data.get("oracle_command"))

    environment_raw = data.get("environment_requirements", {})
    if environment_raw is None:
        environment_raw = {}
    if not isinstance(environment_raw, dict):
        raise ContractError(
            f"`environment_requirements` for solver `{solver_name}` in "
            f"`{contract_path.name}` must be a mapping"
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
        build_command=build_command,
        coverage_build_command=coverage_build_command,
        production_binary_path=production_binary_path,
        coverage_binary_path=coverage_binary_path,
        test_root=test_root,
        test_discovery_command=test_discovery_command,
        target_commands=target_commands,
        oracle_command=oracle_command,
        artifact_paths=artifact_paths,
        artifact_s3_bucket=artifact_s3_bucket,
        artifact_s3_prefix=artifact_s3_prefix,
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
        solver_label = solver_name or "<unknown>"
        raise ContractError(
            f"missing `{field_name}` for solver `{solver_label}` in `{contract_path.name}`"
        )
    return value


def _optional_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip()
        return normalized if normalized else None
    return str(value).strip() or None


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
    if normalized in {"[]", "{}"}:
        return ast.literal_eval(normalized)
    if (normalized.startswith('"') and normalized.endswith('"')) or (
        normalized.startswith("'") and normalized.endswith("'")
    ):
        return ast.literal_eval(normalized)
    return normalized
