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
    build_dir: Path | None = None,
    z3test_dir: Path | None = None,
    opensmt_dir: Path | None = None,
) -> dict:
    workspace_root = _workspace_root_for_solver(
        solver,
        build_dir=build_dir,
        z3test_dir=z3test_dir,
        opensmt_dir=opensmt_dir,
    )
    brain = SolverFuzzingBrain(CONTRACTS[solver], workspace_root=workspace_root)
    return brain.count_tests()


def count_cvc5_tests(build_dir: Path) -> dict:
    return count_solver_tests("cvc5", build_dir=build_dir)


def count_z3_tests(z3test_dir: Path) -> dict:
    return count_solver_tests("z3", z3test_dir=z3test_dir)


def count_opensmt_tests(opensmt_dir: Path) -> dict:
    return count_solver_tests("opensmt", opensmt_dir=opensmt_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description="Count tests for cvc5, z3, or opensmt")
    parser.add_argument("solver", choices=sorted(CONTRACTS))
    parser.add_argument("--build-dir", type=Path)
    parser.add_argument("--z3test-dir", type=Path)
    parser.add_argument("--opensmt-dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.solver == "cvc5":
        if not args.build_dir or not args.build_dir.exists():
            parser.error("--build-dir is required for cvc5 and must exist")
        payload = count_cvc5_tests(args.build_dir)
    elif args.solver == "z3":
        if not args.z3test_dir or not args.z3test_dir.exists():
            parser.error("--z3test-dir is required for z3 and must exist")
        payload = count_z3_tests(args.z3test_dir)
    else:
        if not args.opensmt_dir or not args.opensmt_dir.exists():
            parser.error("--opensmt-dir is required for opensmt and must exist")
        payload = count_opensmt_tests(args.opensmt_dir)

    rendered = json.dumps(payload, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
