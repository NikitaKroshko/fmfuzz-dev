#!/usr/bin/env python3
"""Generate an RQ2 coverage matrix through the shared contract-driven brain."""

from __future__ import annotations

import json
import os
import sys
import tarfile
import argparse
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[3]
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


def _download_selected_commits(solver: str) -> list[str]:
    bucket = os.getenv("AWS_S3_BUCKET")
    if not bucket:
        raise RuntimeError("AWS_S3_BUCKET environment variable not set")

    client = boto3.client("s3", region_name=os.getenv("AWS_REGION", "eu-north-1"))
    s3_key = f"evaluation/rq2/{solver}/selected-commits.json"
    try:
        response = client.get_object(Bucket=bucket, Key=s3_key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "NoSuchKey":
            raise RuntimeError(f"Selected commits not found at {s3_key}. Run commit selection first.") from exc
        raise
    return json.loads(response["Body"].read().decode("utf-8"))


def _extract_artifact_archive(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:gz") as archive:
        archive.extractall(destination)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate an RQ2 coverage matrix")
    parser.add_argument("solver", help="Solver name")
    parser.add_argument("max_commits", nargs="?", type=int)
    parser.add_argument("--contract", type=Path, help="Path to the solver contract YAML file")
    parser.add_argument("--workspace-root", type=Path, default=ROOT)
    args = parser.parse_args()

    solver = args.solver
    contract_path = _resolve_contract_path(solver, args.contract)
    if contract_path is None:
        raise RuntimeError(
            f"Unsupported solver without --contract: {solver}. "
            "Pass --contract or add contracts/solvers/<solver>.yml"
        )

    max_commits = args.max_commits
    selected_commits = _download_selected_commits(solver)
    if not selected_commits:
        raise RuntimeError("No commits selected")
    if max_commits and max_commits > 0:
        selected_commits = selected_commits[:max_commits]
        print(f"📝 Limited to {len(selected_commits)} commits", file=sys.stderr)

    brain = SolverFuzzingBrain(contract_path, workspace_root=args.workspace_root)

    first_commit = selected_commits[0]
    print(f"📥 Preparing coverage matrix from {solver} commit {first_commit}", file=sys.stderr)
    brain.checkout_repositories(commit_hash=first_commit)

    bucket = os.getenv("AWS_S3_BUCKET")
    if not bucket:
        raise RuntimeError("AWS_S3_BUCKET environment variable not set")
    client = boto3.client("s3", region_name=os.getenv("AWS_REGION", "eu-north-1"))
    coverage_key = f"evaluation/rq2/{solver}/builds/coverage/{first_commit}.tar.gz"

    artifacts_dir = args.workspace_root / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifacts_dir / "artifacts.tar.gz"
    client.download_file(bucket, coverage_key, str(artifact_path))
    _extract_artifact_archive(artifact_path, brain.layout.solver_workspace)
    print(f"📦 Extracted coverage artifact into {brain.layout.solver_workspace}", file=sys.stderr)

    chunk_matrix = brain.build_coverage_matrix()
    chunks = chunk_matrix["matrix"]["include"]
    print(
        f"📊 Discovered {chunk_matrix['total_tests']} tests, {len(chunks)} chunks",
        file=sys.stderr,
    )

    combined_matrix = []
    for commit in selected_commits:
        for chunk in chunks:
            combined_matrix.append({"commit": commit, "chunk": chunk})

    output = {
        "include": combined_matrix,
        "total_commits": len(selected_commits),
        "total_chunks": len(chunks),
        "chunks_per_commit": len(chunks),
        "repository_path": brain.contract.repository_path,
    }
    print(json.dumps(output, separators=(",", ":")))
    print(
        f"Generated combined matrix: {len(combined_matrix)} jobs "
        f"({len(selected_commits)} commits × {len(chunks)} chunks)",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
