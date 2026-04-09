#!/usr/bin/env python3
"""Generate an RQ2 coverage matrix through the shared contract-driven brain."""

from __future__ import annotations

import json
import os
import sys
import tarfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.solver_contract import load_solver_contract
from scripts.solver_fuzzing_brain import SolverFuzzingBrain


CONTRACTS = {
    "cvc5": ROOT / "contracts" / "solvers" / "cvc5.yml",
    "z3": ROOT / "contracts" / "solvers" / "z3.yml",
    "opensmt": ROOT / "contracts" / "solvers" / "opensmt.yml",
}


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
    solver = sys.argv[1]
    if solver not in CONTRACTS:
        raise RuntimeError(f"Unsupported solver: {solver}")

    max_commits = int(sys.argv[2]) if len(sys.argv) > 2 else None
    selected_commits = _download_selected_commits(solver)
    if not selected_commits:
        raise RuntimeError("No commits selected")
    if max_commits and max_commits > 0:
        selected_commits = selected_commits[:max_commits]
        print(f"📝 Limited to {len(selected_commits)} commits", file=sys.stderr)

    contract = load_solver_contract(CONTRACTS[solver])
    brain = SolverFuzzingBrain(CONTRACTS[solver], workspace_root=ROOT)

    first_commit = selected_commits[0]
    print(f"📥 Preparing coverage matrix from {solver} commit {first_commit}", file=sys.stderr)
    brain.checkout_repositories(commit_hash=first_commit)

    bucket = os.getenv("AWS_S3_BUCKET")
    if not bucket:
        raise RuntimeError("AWS_S3_BUCKET environment variable not set")
    client = boto3.client("s3", region_name=os.getenv("AWS_REGION", "eu-north-1"))
    coverage_key = f"evaluation/rq2/{solver}/builds/coverage/{first_commit}.tar.gz"

    artifacts_dir = ROOT / "artifacts"
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
        "repository_path": contract.repository_path,
    }
    print(json.dumps(output, separators=(",", ":")))
    print(
        f"Generated combined matrix: {len(combined_matrix)} jobs "
        f"({len(selected_commits)} commits × {len(chunks)} chunks)",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
