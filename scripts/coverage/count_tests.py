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


CONTRACTS = {
    "cvc5": ROOT / "contracts" / "solvers" / "cvc5.yml",
    "z3": ROOT / "contracts" / "solvers" / "z3.yml",
    "opensmt": ROOT / "contracts" / "solvers" / "opensmt.yml",
}


def _default_contract_for_solver(solver: str) -> Path | None:
    if solver in CONTRACTS:
        return CONTRACTS[solver]
    candidate = ROOT / "contracts" / "solvers" / f"{solver}.yml"
    return candidate if candidate.exists() else None


def _workspace_root_for_solver(
    solver: str,
    *,
    build_dir: Path | None,
    z3test_dir: Path | None,
    opensmt_dir: Path | None,
) -> Path:
    if solver == "cvc5":
        if build_dir is None:
            raise ValueError("--build-dir is required for cvc5")
        return build_dir.resolve().parent.parent
    if solver == "z3":
        if z3test_dir is None:
            raise ValueError("--z3test-dir is required for z3")
        return z3test_dir.resolve().parent
    if opensmt_dir is None:
        raise ValueError("--opensmt-dir is required for opensmt")
    return opensmt_dir.resolve().parent


def count_solver_tests(
    solver: str,
    *,
    contract: Path | None = None,
    workspace_root: Path | None = None,
    build_dir: Path | None = None,
    z3test_dir: Path | None = None,
    opensmt_dir: Path | None = None,
) -> dict:
    contract_path = contract or _default_contract_for_solver(solver)
    if contract_path is None:
        raise ValueError(f"no contract found for solver `{solver}`; pass --contract")
    resolved_workspace_root = workspace_root
    if resolved_workspace_root is None:
        if solver in CONTRACTS and (build_dir or z3test_dir or opensmt_dir):
            resolved_workspace_root = _workspace_root_for_solver(
                solver,
                build_dir=build_dir,
                z3test_dir=z3test_dir,
                opensmt_dir=opensmt_dir,
            )
        else:
            resolved_workspace_root = ROOT
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

    derived_contract = args.contract or (_default_contract_for_solver(args.solver) if args.solver else None)
    if args.contract or (args.solver not in CONTRACTS and derived_contract is not None):
        solver = args.solver or "custom"
        payload = count_solver_tests(
            solver,
            contract=derived_contract,
            workspace_root=args.workspace_root,
            build_dir=args.build_dir,
            z3test_dir=args.z3test_dir,
            opensmt_dir=args.opensmt_dir,
        )
    elif args.solver == "cvc5":
        if not args.build_dir or not args.build_dir.exists():
            parser.error("--build-dir is required for cvc5 and must exist")
        payload = count_cvc5_tests(args.build_dir)
    elif args.solver == "z3":
        if not args.z3test_dir or not args.z3test_dir.exists():
            parser.error("--z3test-dir is required for z3 and must exist")
        payload = count_z3_tests(args.z3test_dir)
    elif args.solver == "opensmt":
        if not args.opensmt_dir or not args.opensmt_dir.exists():
            parser.error("--opensmt-dir is required for opensmt and must exist")
        payload = count_opensmt_tests(args.opensmt_dir)
    else:
        parser.error("provide one of cvc5, z3, opensmt, or pass --contract")

    rendered = json.dumps(payload, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
