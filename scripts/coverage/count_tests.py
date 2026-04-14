#!/usr/bin/env python3
"""Contract-driven test counting for supported solver workspaces."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.solver_fuzzing_brain import SolverFuzzingBrain


def _resolve_contract_path(solver: str | None, explicit_contract: Path | None = None) -> Path | None:
    if explicit_contract is not None:
        return explicit_contract
    if solver is None:
        return None
    candidate = ROOT / "contracts" / "solvers" / f"{solver}.yml"
    return candidate if candidate.exists() else None


def _workspace_root_from_paths(
    *,
    build_dir: Path | None,
    z3test_dir: Path | None,
    opensmt_dir: Path | None,
) -> Path:
    if build_dir is not None:
        return build_dir.resolve().parent.parent
    if z3test_dir is not None:
        return z3test_dir.resolve().parent
    if opensmt_dir is not None:
        return opensmt_dir.resolve().parent
    return ROOT


def count_solver_tests(
    solver: str,
    *,
    contract: Path | None = None,
    workspace_root: Path | None = None,
    build_dir: Path | None = None,
    z3test_dir: Path | None = None,
    opensmt_dir: Path | None = None,
) -> dict:
    contract_path = _resolve_contract_path(solver, contract)
    if contract_path is None:
        raise ValueError(f"no contract found for solver `{solver}`; pass --contract")
    if build_dir is not None and not build_dir.exists():
        raise ValueError("--build-dir must exist")
    if z3test_dir is not None and not z3test_dir.exists():
        raise ValueError("--z3test-dir must exist")
    if opensmt_dir is not None and not opensmt_dir.exists():
        raise ValueError("--opensmt-dir must exist")
    resolved_workspace_root = workspace_root
    if resolved_workspace_root is None:
        resolved_workspace_root = _workspace_root_from_paths(
            build_dir=build_dir,
            z3test_dir=z3test_dir,
            opensmt_dir=opensmt_dir,
        )
    brain = SolverFuzzingBrain(contract_path, workspace_root=resolved_workspace_root)
    return brain.count_tests()


def count_cvc5_tests(build_dir: Path) -> dict:
    return count_solver_tests("cvc5", build_dir=build_dir)


def count_z3_tests(z3test_dir: Path) -> dict:
    return count_solver_tests("z3", z3test_dir=z3test_dir)


def count_opensmt_tests(opensmt_dir: Path) -> dict:
    return count_solver_tests("opensmt", opensmt_dir=opensmt_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description="Count tests through a solver contract")
    parser.add_argument("solver", nargs="?")
    parser.add_argument("--contract", type=Path, help="Path to a solver contract YAML file")
    parser.add_argument("--workspace-root", type=Path, help="Workspace root for repository_path resolution")
    parser.add_argument("--build-dir", type=Path)
    parser.add_argument("--z3test-dir", type=Path)
    parser.add_argument("--opensmt-dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    contract_path = _resolve_contract_path(args.solver, args.contract)
    if contract_path is not None:
        payload = count_solver_tests(
            args.solver or "custom",
            contract=contract_path,
            workspace_root=args.workspace_root,
            build_dir=args.build_dir,
            z3test_dir=args.z3test_dir,
            opensmt_dir=args.opensmt_dir,
        )
    else:
        parser.error("pass --contract or provide a solver name with a matching contract file")

    rendered = json.dumps(payload, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
