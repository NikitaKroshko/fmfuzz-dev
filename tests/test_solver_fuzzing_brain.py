from __future__ import annotations

import json
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.solver_contract import ContractError, derive_github_issues_url, load_solver_contract
from scripts.solver_fuzzing_brain import BrainError, SolverFuzzingBrain


class SolverFuzzingBrainTests(unittest.TestCase):
    def _write_file(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content), encoding="utf-8")

    def _write_executable(self, path: Path, content: str) -> None:
        self._write_file(path, content)
        path.chmod(0o755)

    def _write_contract(self, path: Path, content: str) -> Path:
        self._write_file(path, content)
        return path

    def test_contract_loader_derives_issue_url_and_split_repo_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = self._write_contract(
                root / "solver.yml",
                """\
                solver_name: demo
                repository_url: https://github.com/example/demo.git
                repository_path: solver
                build_command: /bin/true
                coverage_build_command: /bin/true --instrumentation
                production_binary_path: build/demo
                coverage_binary_path: build/demo-inst
                test_root: .
                test_discovery_command: null
                target_commands:
                  - "{target_binary}"
                oracle_command: null
                tests_repository_url: https://github.com/example/demo-tests.git
                tests_repository_path: tests
                environment_requirements:
                  packages:
                    - cmake
                  env:
                    - DEMO_ENV
                artifact_paths:
                  - build/demo
                artifact_s3_bucket: null
                artifact_s3_prefix: null
                """,
            )

            parsed = load_solver_contract(contract)
            self.assertEqual(parsed.resolved_issues_url, "https://github.com/example/demo/issues")
            layout = parsed.resolve_layout(root)
            self.assertEqual(layout.solver_workspace, (root / "solver").resolve())
            self.assertEqual(layout.tests_workspace, (root / "tests").resolve())
            self.assertTrue(parsed.uses_split_test_repository)

    def test_contract_loader_fails_with_precise_missing_field_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contract = self._write_contract(
                Path(tmp) / "broken.yml",
                """\
                solver_name: broken
                repository_url: https://github.com/example/broken.git
                repository_path: solver
                build_command: /bin/true
                coverage_build_command: /bin/true --instrumentation
                coverage_binary_path: build/demo-inst
                test_root: .
                target_commands:
                  - "{target_binary}"
                oracle_command: null
                artifact_paths: []
                artifact_s3_bucket: null
                artifact_s3_prefix: null
                """,
            )

            with self.assertRaises(ContractError) as ctx:
                load_solver_contract(contract)

            self.assertEqual(
                str(ctx.exception),
                "missing `production_binary_path` for solver `broken` in `broken.yml`",
            )

    def test_build_runs_production_and_instrumentation_commands_and_parses_binary_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            solver_root = root / "solver"
            solver_root.mkdir()
            build_script = root / "build.sh"
            self._write_executable(
                build_script,
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                printf '%s\\n' "$*" >> "${SOLVER_WORKSPACE}/invocations.log"
                mkdir -p "${SOLVER_WORKSPACE}/bin" "${SOLVER_WORKSPACE}/artifacts"
                if [[ "${1:-}" == "--instrumentation" ]]; then
                  target="${SOLVER_WORKSPACE}/bin/demo-inst"
                else
                  target="${SOLVER_WORKSPACE}/bin/demo"
                fi
                printf '#!/bin/sh\\nexit 0\\n' > "$target"
                chmod +x "$target"
                printf 'artifact\\n' > "${SOLVER_WORKSPACE}/artifacts/output.txt"
                printf 'BINARY_PATH=%s\\n' "$target"
                """,
            )
            contract = self._write_contract(
                root / "solver.yml",
                f"""\
                solver_name: demo
                repository_url: https://github.com/example/demo.git
                repository_path: solver
                build_command: {build_script}
                coverage_build_command: {build_script} --instrumentation
                production_binary_path: bin/demo
                coverage_binary_path: bin/demo-inst
                test_root: .
                test_discovery_command: null
                target_commands:
                  - "{{target_binary}}"
                oracle_command: null
                environment_requirements:
                  packages: []
                  env: []
                artifact_paths:
                  - bin
                  - artifacts/output.txt
                artifact_s3_bucket: null
                artifact_s3_prefix: null
                """,
            )

            brain = SolverFuzzingBrain(contract, workspace_root=root)
            production = brain.build(mode="production", artifacts_dir=root / "artifacts" / "production")
            instrumentation = brain.build(
                mode="instrumentation",
                artifacts_dir=root / "artifacts" / "instrumentation",
            )

            self.assertEqual(production.binary_path, (solver_root / "bin" / "demo").resolve())
            self.assertEqual(
                instrumentation.binary_path,
                (solver_root / "bin" / "demo-inst").resolve(),
            )
            self.assertTrue((root / "artifacts" / "production" / "bin" / "demo").exists())
            self.assertTrue(
                (root / "artifacts" / "instrumentation" / "artifacts" / "output.txt").exists()
            )
            self.assertEqual(
                (solver_root / "invocations.log").read_text(encoding="utf-8").splitlines(),
                ["", "--instrumentation"],
            )

    def test_build_fails_when_binary_is_not_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            solver_root = root / "solver"
            solver_root.mkdir()
            build_script = root / "build.sh"
            self._write_executable(
                build_script,
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                mkdir -p "${SOLVER_WORKSPACE}/bin"
                target="${SOLVER_WORKSPACE}/bin/demo"
                printf 'not executable\\n' > "$target"
                printf 'BINARY_PATH=%s\\n' "$target"
                """,
            )
            contract = self._write_contract(
                root / "solver.yml",
                f"""\
                solver_name: demo
                repository_url: https://github.com/example/demo.git
                repository_path: solver
                build_command: {build_script}
                coverage_build_command: {build_script} --instrumentation
                production_binary_path: bin/demo
                coverage_binary_path: bin/demo
                test_root: .
                test_discovery_command: null
                target_commands:
                  - "{{target_binary}}"
                oracle_command: null
                environment_requirements:
                  packages: []
                  env: []
                artifact_paths: []
                artifact_s3_bucket: null
                artifact_s3_prefix: null
                """,
            )

            brain = SolverFuzzingBrain(contract, workspace_root=root)
            with self.assertRaises(BrainError) as ctx:
                brain.build(mode="production")

            self.assertIn("resolved binary is not executable", ctx.exception.render())

    def test_discovery_command_takes_precedence_over_test_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            solver_root = root / "solver"
            tests_root = solver_root / "tests"
            tests_root.mkdir(parents=True)
            (tests_root / "fallback.smt2").write_text("(check-sat)\n", encoding="utf-8")
            discover_script = root / "discover.sh"
            self._write_executable(
                discover_script,
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                printf 'preferred.smt2\\n'
                """,
            )
            contract = self._write_contract(
                root / "solver.yml",
                f"""\
                solver_name: demo
                repository_url: https://github.com/example/demo.git
                repository_path: solver
                build_command: /bin/true
                coverage_build_command: /bin/true --instrumentation
                production_binary_path: bin/demo
                coverage_binary_path: bin/demo-inst
                test_root: tests
                test_discovery_command: {discover_script}
                target_commands:
                  - "{{target_binary}}"
                oracle_command: null
                environment_requirements:
                  packages: []
                  env: []
                artifact_paths: []
                artifact_s3_bucket: null
                artifact_s3_prefix: null
                """,
            )

            brain = SolverFuzzingBrain(contract, workspace_root=root)
            self.assertEqual(brain.discover_tests(), ["preferred.smt2"])

    def test_test_root_discovery_returns_sorted_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests_root = root / "solver" / "tests"
            (tests_root / "b").mkdir(parents=True)
            (tests_root / "a.smt2").write_text("(check-sat)\n", encoding="utf-8")
            (tests_root / "b" / "c.smt").write_text("(check-sat)\n", encoding="utf-8")
            contract = self._write_contract(
                root / "solver.yml",
                """\
                solver_name: demo
                repository_url: https://github.com/example/demo.git
                repository_path: solver
                build_command: /bin/true
                coverage_build_command: /bin/true --instrumentation
                production_binary_path: bin/demo
                coverage_binary_path: bin/demo-inst
                test_root: tests
                test_discovery_command: null
                target_commands:
                  - "{target_binary}"
                oracle_command: null
                environment_requirements:
                  packages: []
                  env: []
                artifact_paths: []
                artifact_s3_bucket: null
                artifact_s3_prefix: null
                """,
            )

            brain = SolverFuzzingBrain(contract, workspace_root=root)
            self.assertEqual(brain.discover_tests(), ["a.smt2", "b/c.smt"])

    def test_oracle_command_templates_reference_target_and_test_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            solver_root = root / "solver"
            solver_root.mkdir()
            oracle_script = root / "oracle.sh"
            args_log = root / "oracle-args.txt"
            self._write_executable(
                oracle_script,
                f"""\
                #!/usr/bin/env bash
                set -euo pipefail
                printf '%s\\n' "$*" > "{args_log}"
                printf 'sat\\n'
                """,
            )
            contract = self._write_contract(
                root / "solver.yml",
                f"""\
                solver_name: demo
                repository_url: https://github.com/example/demo.git
                repository_path: solver
                build_command: /bin/true
                coverage_build_command: /bin/true --instrumentation
                production_binary_path: bin/demo
                coverage_binary_path: bin/demo-inst
                test_root: .
                test_discovery_command: null
                target_commands:
                  - "{{target_binary}}"
                oracle_command: {oracle_script} {{reference_binary}} {{target_binary}} {{test_file}}
                environment_requirements:
                  packages: []
                  env: []
                artifact_paths: []
                artifact_s3_bucket: null
                artifact_s3_prefix: null
                """,
            )

            brain = SolverFuzzingBrain(contract, workspace_root=root)
            result = brain.run_oracle(
                test_file="seed.smt2",
                target_binary="/bin/true",
                reference_binary="/bin/false",
            )

            self.assertEqual(result.returncode, 0)
            self.assertEqual(
                args_log.read_text(encoding="utf-8").strip(),
                "/bin/false /bin/true seed.smt2",
            )

    def test_collect_artifacts_supports_solver_tests_and_workspace_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "solver" / "build").mkdir(parents=True)
            (root / "solver" / "build" / "solver.txt").write_text("solver\n", encoding="utf-8")
            (root / "tests" / "case.txt").parent.mkdir(parents=True)
            (root / "tests" / "case.txt").write_text("tests\n", encoding="utf-8")
            (root / "shared.txt").write_text("workspace\n", encoding="utf-8")
            contract = self._write_contract(
                root / "solver.yml",
                """\
                solver_name: demo
                repository_url: https://github.com/example/demo.git
                repository_path: solver
                tests_repository_url: https://github.com/example/tests.git
                tests_repository_path: tests
                build_command: /bin/true
                coverage_build_command: /bin/true --instrumentation
                production_binary_path: bin/demo
                coverage_binary_path: bin/demo-inst
                test_root: .
                test_discovery_command: null
                target_commands:
                  - "{target_binary}"
                oracle_command: null
                environment_requirements:
                  packages: []
                  env: []
                artifact_paths:
                  - solver:build/solver.txt
                  - tests:case.txt
                  - workspace:shared.txt
                  - solver:missing.txt
                artifact_s3_bucket: null
                artifact_s3_prefix: null
                """,
            )

            brain = SolverFuzzingBrain(contract, workspace_root=root)
            warnings = brain.collect_artifacts(root / "collected")

            self.assertTrue((root / "collected" / "build" / "solver.txt").exists())
            self.assertTrue((root / "collected" / "case.txt").exists())
            self.assertTrue((root / "collected" / "shared.txt").exists())
            self.assertEqual(warnings, ["missing artifact path: solver:missing.txt"])

    def test_upload_to_s3_uses_contract_or_override_without_requiring_real_boto3(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_file = root / "artifact.tar.gz"
            source_file.write_text("artifact\n", encoding="utf-8")
            contract = self._write_contract(
                root / "solver.yml",
                """\
                solver_name: demo
                repository_url: https://github.com/example/demo.git
                repository_path: solver
                build_command: /bin/true
                coverage_build_command: /bin/true --instrumentation
                production_binary_path: bin/demo
                coverage_binary_path: bin/demo-inst
                test_root: .
                test_discovery_command: null
                target_commands:
                  - "{target_binary}"
                oracle_command: null
                environment_requirements:
                  packages: []
                  env: []
                artifact_paths: []
                artifact_s3_bucket: null
                artifact_s3_prefix: null
                """,
            )

            uploads: list[tuple[str, str, str]] = []

            class FakeClient:
                def upload_file(self, source: str, bucket: str, key: str) -> None:
                    uploads.append((source, bucket, key))

            fake_boto3 = types.ModuleType("boto3")
            fake_boto3.client = lambda *_args, **_kwargs: FakeClient()  # type: ignore[assignment]

            brain = SolverFuzzingBrain(contract, workspace_root=root)
            with patch.dict("sys.modules", {"boto3": fake_boto3}, clear=False):
                upload_target = brain.upload_to_s3(
                    source_file,
                    bucket="bucket",
                    prefix="prefix/path",
                    step="artifact upload",
                )

            self.assertEqual(upload_target, "s3://bucket/prefix/path/artifact.tar.gz")
            self.assertEqual(
                uploads,
                [(str(source_file.resolve()), "bucket", "prefix/path/artifact.tar.gz")],
            )

    def test_builtin_solver_contracts_load(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        for contract_name in ["cvc5.yml", "z3.yml", "opensmt.yml"]:
            contract = load_solver_contract(repo_root / "contracts" / "solvers" / contract_name)
            self.assertTrue(contract.target_commands)
            self.assertTrue(contract.build_command.endswith("build.sh"))

    def test_issue_url_override_beats_derivation(self) -> None:
        self.assertEqual(
            derive_github_issues_url("git@github.com:example/project.git"),
            "https://github.com/example/project/issues",
        )


if __name__ == "__main__":
    unittest.main()
