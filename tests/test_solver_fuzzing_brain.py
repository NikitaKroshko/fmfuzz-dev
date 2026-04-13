from __future__ import annotations

import json
import os
import subprocess
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
            root = Path(tmp)
            cases = [
                (
                    "production_binary_path",
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
                    "missing `production_binary_path` for solver `broken` in `broken.yml`",
                ),
                (
                    "target_commands",
                    """\
                    solver_name: broken
                    repository_url: https://github.com/example/broken.git
                    repository_path: solver
                    build_command: /bin/true
                    coverage_build_command: /bin/true --instrumentation
                    production_binary_path: build/demo
                    coverage_binary_path: build/demo-inst
                    test_root: .
                    oracle_command: null
                    artifact_paths: []
                    artifact_s3_bucket: null
                    artifact_s3_prefix: null
                    """,
                    "missing `target_commands` for solver `broken` in `broken.yml`",
                ),
            ]

            for field_name, content, expected_message in cases:
                with self.subTest(field=field_name):
                    contract = self._write_contract(root / "broken.yml", content)
                    with self.assertRaises(ContractError) as ctx:
                        load_solver_contract(contract)

                    self.assertEqual(str(ctx.exception), expected_message)
                    rendered = ctx.exception.render()
                    self.assertIn("[solver=broken][step=contract validation]", rendered)
                    self.assertIn("repository url: https://github.com/example/broken.git", rendered)
                    self.assertIn("issue tracker: https://github.com/example/broken/issues", rendered)

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

    def test_build_uses_final_binary_path_line_from_stdout(self) -> None:
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
                first="${SOLVER_WORKSPACE}/bin/wrong"
                target="${SOLVER_WORKSPACE}/bin/right"
                printf '#!/bin/sh\\nexit 0\\n' > "$first"
                printf '#!/bin/sh\\nexit 0\\n' > "$target"
                chmod +x "$first" "$target"
                printf 'BINARY_PATH=%s\\n' "$first"
                printf 'status: intermediate\\n'
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
                coverage_build_command: {build_script} --instrumented
                production_binary_path: bin/right
                coverage_binary_path: bin/right
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
            production = brain.build(mode="production")

            self.assertEqual(production.binary_path, (solver_root / "bin" / "right").resolve())

    def test_user_contract_build_script_tests_script_and_fuzzing_seeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            solver_root = root / "solver"
            solver_root.mkdir()
            build_script = root / "build.sh"
            tests_script = root / "tests.sh"
            self._write_executable(
                build_script,
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                mkdir -p "${SOLVER_WORKSPACE}/bin"
                case "${1:-}" in
                  "" ) target="${SOLVER_WORKSPACE}/bin/demo" ;;
                  --instrumented|--instrumentation|--coverage ) target="${SOLVER_WORKSPACE}/bin/demo-inst" ;;
                  * ) echo "bad mode: $1" >&2; exit 2 ;;
                esac
                printf '#!/bin/sh\\nexit 0\\n' > "$target"
                chmod +x "$target"
                printf 'BINARY_PATH=%s\\n' "$target"
                """,
            )
            self._write_executable(
                tests_script,
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                mkdir -p "${FUZZING_SEEDS}/nested"
                printf '(check-sat)\\n' > "${FUZZING_SEEDS}/a.smt2"
                printf '(check-sat)\\n' > "${FUZZING_SEEDS}/nested/b.smt"
                printf 'ignore\\n' > "${FUZZING_SEEDS}/ignored.txt"
                """,
            )
            contract = self._write_contract(
                root / "solver.yml",
                f"""\
                solver_name: demo
                repository_url: https://github.com/example/demo.git
                repository_path: solver
                build_script: {build_script}
                production_binary_path: bin/demo
                coverage_binary_path: bin/demo-inst
                tests_script: {tests_script}
                seeds_dir: FUZZING_SEEDS
                target_commands:
                  - "{{target_binary}}"
                environment_requirements:
                  packages: []
                  env: []
                artifact_paths: []
                """,
            )

            brain = SolverFuzzingBrain(contract, workspace_root=root)
            validation = brain.validate_integration()
            self.assertEqual(validation["status"], "ok")

            production = brain.build(mode="production")
            instrumentation = brain.build(mode="instrumentation")
            self.assertEqual(production.binary_path, (solver_root / "bin" / "demo").resolve())
            self.assertEqual(instrumentation.binary_path, (solver_root / "bin" / "demo-inst").resolve())

            seeds_dir, tests = brain.prepare_seeds()
            self.assertEqual(seeds_dir, (solver_root / "FUZZING_SEEDS").resolve())
            self.assertEqual(tests, ["a.smt2", "nested/b.smt"])
            matrix = brain.build_matrix(max_jobs=1)
            self.assertEqual(matrix["total_tests"], 2)
            self.assertEqual(matrix["matrix"]["include"][0]["tests"], ["a.smt2", "nested/b.smt"])

    def test_doctor_can_run_lightweight_script_checks_in_fake_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_script = root / "build.sh"
            tests_script = root / "tests.sh"
            self._write_executable(
                build_script,
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                mkdir -p "${SOLVER_WORKSPACE}/bin"
                target="${SOLVER_WORKSPACE}/bin/demo"
                if [[ "${1:-}" == "--instrumented" ]]; then
                  target="${SOLVER_WORKSPACE}/bin/demo-inst"
                fi
                printf '#!/bin/sh\\nexit 0\\n' > "$target"
                chmod +x "$target"
                printf 'BINARY_PATH=%s\\n' "$target"
                """,
            )
            self._write_executable(
                tests_script,
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                mkdir -p "$FUZZING_SEEDS"
                printf '(check-sat)\\n' > "$FUZZING_SEEDS/seed.smt2"
                """,
            )
            contract = self._write_contract(
                root / "solver.yml",
                f"""\
                solver_name: demo
                repository_url: https://github.com/example/demo.git
                build_script: {build_script}
                binary_path: bin/demo
                instrumented_binary_path: bin/demo-inst
                tests_script: {tests_script}
                target_commands:
                  - "{{target_binary}}"
                artifact_paths: []
                """,
            )

            brain = SolverFuzzingBrain(contract, workspace_root=root)
            payload = brain.validate_integration(
                run_script_checks=True,
                script_check_workspace=root / "fake-workspace",
            )
            self.assertEqual(payload["script_checks"]["seeds"]["test_count"], 1)

    def test_checkout_failure_is_structured_brain_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = self._write_contract(
                root / "solver.yml",
                """\
                solver_name: demo
                repository_url: file:///definitely/missing/demo.git
                repository_path: solver
                build_command: /bin/true
                coverage_build_command: /bin/true --instrumented
                production_binary_path: bin/demo
                coverage_binary_path: bin/demo
                test_root: .
                target_commands:
                  - "{target_binary}"
                artifact_paths: []
                """,
            )

            brain = SolverFuzzingBrain(contract, workspace_root=root)
            with self.assertRaises(BrainError) as ctx:
                brain.checkout_repositories()

            rendered = ctx.exception.render()
            self.assertIn("[solver=demo][step=solver checkout]", rendered)
            self.assertIn("git command failed", rendered)
            self.assertIn("command: git clone file:///definitely/missing/demo.git", rendered)
            self.assertIn("repository url: file:///definitely/missing/demo.git", rendered)
            self.assertIn("issue tracker:", rendered)
            self.assertIn("stderr:", rendered)

    def test_validate_integration_rejects_unresolved_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_script = root / "build.sh"
            self._write_executable(
                build_script,
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                mkdir -p "${SOLVER_WORKSPACE}/bin"
                target="${SOLVER_WORKSPACE}/bin/demo"
                printf '#!/bin/sh\\nexit 0\\n' > "$target"
                chmod +x "$target"
                printf 'BINARY_PATH=%s\\n' "$target"
                """,
            )
            contract = self._write_contract(
                root / "solver.yml",
                f"""\
                solver_name: demo
                repository_url: https://github.com/example/demo.git
                repository_path: solver
                build_script: {build_script}
                production_binary_path: bin/demo
                coverage_binary_path: bin/demo
                tests_script: tests.sh
                target_commands:
                  - "definitely-not-installed-command-12345 --flag"
                artifact_paths: []
                """,
            )
            self._write_executable(
                root / "tests.sh",
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                mkdir -p "$FUZZING_SEEDS"
                printf '(check-sat)\\n' > "$FUZZING_SEEDS/seed.smt2"
                """,
            )

            brain = SolverFuzzingBrain(contract, workspace_root=root)
            with self.assertRaises(BrainError) as ctx:
                brain.validate_integration()

            rendered = ctx.exception.render()
            self.assertIn("definitely-not-installed-command-12345", rendered)
            self.assertIn("repository url: https://github.com/example/demo.git", rendered)
            self.assertIn("issue tracker: https://github.com/example/demo/issues", rendered)

    def test_count_tests_accepts_generic_contract(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "solver").mkdir()
            tests_script = root / "tests.sh"
            self._write_executable(
                tests_script,
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                mkdir -p "$FUZZING_SEEDS"
                printf '(check-sat)\\n' > "$FUZZING_SEEDS/one.smt2"
                printf '(check-sat)\\n' > "$FUZZING_SEEDS/two.smt"
                """,
            )
            contract = self._write_contract(
                root / "solver.yml",
                f"""\
                solver_name: demo
                repository_url: https://github.com/example/demo.git
                repository_path: solver
                build_command: /bin/true
                coverage_build_command: /bin/true --instrumented
                production_binary_path: bin/demo
                coverage_binary_path: bin/demo
                tests_script: {tests_script}
                target_commands:
                  - "{{target_binary}}"
                artifact_paths: []
                """,
            )
            output = root / "count.json"

            result = subprocess.run(
                [
                    "python3",
                    str(repo_root / "scripts" / "coverage" / "count_tests.py"),
                    "--contract",
                    str(contract),
                    "--workspace-root",
                    str(root),
                    "--output",
                    str(output),
                ],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["test_count"], 2)

    def test_coverage_matrix_is_built_from_fuzzing_seeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "solver").mkdir()
            tests_script = root / "tests.sh"
            self._write_executable(
                tests_script,
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                mkdir -p "${FUZZING_SEEDS}/nested"
                printf '(check-sat)\\n' > "${FUZZING_SEEDS}/alpha.smt2"
                printf '(check-sat)\\n' > "${FUZZING_SEEDS}/nested/beta.smt"
                """,
            )
            contract = self._write_contract(
                root / "solver.yml",
                f"""\
                solver_name: demo
                repository_url: https://github.com/example/demo.git
                repository_path: solver
                build_command: /bin/true
                coverage_build_command: /bin/true --instrumented
                production_binary_path: bin/demo
                coverage_binary_path: bin/demo
                tests_script: {tests_script}
                target_commands:
                  - "{{target_binary}}"
                artifact_paths: []
                """,
            )

            brain = SolverFuzzingBrain(contract, workspace_root=root)
            matrix = brain.build_coverage_matrix(
                max_job_time_minutes=60,
                buffer_minutes=10,
                target_jobs=2,
                avg_test_time_seconds=1.0,
            )

            self.assertEqual(matrix["total_tests"], 2)
            self.assertEqual(matrix["total_jobs"], 2)
            self.assertEqual(matrix["tests_per_job"], 1)
            self.assertEqual(
                matrix["matrix"]["include"],
                [
                    {"job_name": "demo-coverage-1", "start_index": 1, "end_index": 1},
                    {"job_name": "demo-coverage-2", "start_index": 2, "end_index": 2},
                ],
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

    def test_tests_script_takes_precedence_over_legacy_discovery_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            solver_root = root / "solver"
            tests_root = solver_root / "tests"
            tests_root.mkdir(parents=True)
            (tests_root / "fallback.smt2").write_text("(check-sat)\n", encoding="utf-8")
            discover_script = root / "discover.sh"
            tests_script = root / "tests.sh"
            self._write_executable(
                discover_script,
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                printf 'preferred.smt2\\n'
                """,
            )
            self._write_executable(
                tests_script,
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                mkdir -p "${FUZZING_SEEDS}/nested"
                printf '(check-sat)\\n' > "${FUZZING_SEEDS}/alpha.smt2"
                printf '(check-sat)\\n' > "${FUZZING_SEEDS}/nested/beta.smt"
                """,
            )
            contract = self._write_contract(
                root / "solver.yml",
                f"""\
                solver_name: demo
                repository_url: https://github.com/example/demo.git
                repository_path: solver
                build_command: /bin/true
                coverage_build_command: /bin/true --instrumented
                production_binary_path: bin/demo
                coverage_binary_path: bin/demo-inst
                tests_script: {tests_script}
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
            self.assertEqual(brain.discover_tests(), ["alpha.smt2", "nested/beta.smt"])

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

    def test_builtin_discovery_scripts_do_not_emit_status_lines_as_tests(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cvc5_build = root / "cvc5" / "build"
            cvc5_build.mkdir(parents=True)
            fake_bin = root / "bin"
            fake_ctest = fake_bin / "ctest"
            fake_pythonpath = root / "pythonpath"
            self._write_file(fake_pythonpath / "psutil.py", "")
            self._write_executable(
                fake_ctest,
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                if [[ "${1:-}" == "--show-only" ]]; then
                  printf 'Test #1: regress0/a.smt2\\n'
                  printf 'Test #2: regress0/b.smt2\\n'
                  exit 0
                fi
                exit 2
                """,
            )

            cvc5_brain = SolverFuzzingBrain(
                repo_root / "contracts" / "solvers" / "cvc5.yml",
                workspace_root=root,
            )
            with patch.dict(
                os.environ,
                {
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                    "PYTHONPATH": f"{fake_pythonpath}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
                },
                clear=False,
            ):
                cvc5_tests = cvc5_brain.discover_tests()

            self.assertEqual(cvc5_tests, ["regress0/a.smt2", "regress0/b.smt2"])

            (root / "z3").mkdir()
            z3_tests_root = root / "z3test" / "regressions" / "smt2"
            z3_tests_root.mkdir(parents=True)
            (z3_tests_root / "a.smt2").write_text("(check-sat)\n", encoding="utf-8")
            (z3_tests_root / "b.smt").write_text("(check-sat)\n", encoding="utf-8")

            z3_brain = SolverFuzzingBrain(
                repo_root / "contracts" / "solvers" / "z3.yml",
                workspace_root=root,
            )
            with patch.dict(
                os.environ,
                {"PYTHONPATH": f"{fake_pythonpath}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"},
                clear=False,
            ):
                z3_tests = z3_brain.discover_tests()

            self.assertEqual(z3_tests, ["regressions/smt2/a.smt2", "regressions/smt2/b.smt"])

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

    def test_collect_existing_artifacts_can_archive_without_running_a_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "solver" / "build" / "bin").mkdir(parents=True)
            (root / "solver" / "build" / "bin" / "demo").write_text("binary\n", encoding="utf-8")
            (root / "solver" / "build" / "compile_commands.json").write_text("[]\n", encoding="utf-8")
            contract = self._write_contract(
                root / "solver.yml",
                """\
                solver_name: demo
                repository_url: https://github.com/example/demo.git
                repository_path: solver
                build_command: /bin/true
                coverage_build_command: /bin/true --instrumentation
                production_binary_path: build/bin/demo
                coverage_binary_path: build/bin/demo
                test_root: .
                test_discovery_command: null
                target_commands:
                  - "{target_binary}"
                oracle_command: null
                environment_requirements:
                  packages: []
                  env: []
                artifact_paths:
                  - build/bin/demo
                  - build/compile_commands.json
                artifact_s3_bucket: null
                artifact_s3_prefix: null
                """,
            )

            brain = SolverFuzzingBrain(contract, workspace_root=root)
            result = brain.collect_existing_artifacts(
                artifacts_dir=root / "collected",
                artifact_archive=root / "archive.tar.gz",
            )

            self.assertTrue((root / "collected" / "build" / "bin" / "demo").exists())
            self.assertTrue((root / "collected" / "build" / "compile_commands.json").exists())
            self.assertEqual(result.warnings, ())
            self.assertEqual(result.artifact_archive, (root / "archive.tar.gz").resolve())
            self.assertTrue(result.artifact_archive.exists())

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

    def test_builtin_solver_contracts_expose_script_surface(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]

        cvc5_contract = load_solver_contract(repo_root / "contracts" / "solvers" / "cvc5.yml")
        self.assertTrue(cvc5_contract.target_commands)
        self.assertTrue(cvc5_contract.build_command.endswith("build.sh"))
        self.assertTrue(cvc5_contract.test_discovery_command.endswith("test.sh --discover"))

        z3_contract = load_solver_contract(repo_root / "contracts" / "solvers" / "z3.yml")
        self.assertTrue(z3_contract.target_commands)
        self.assertEqual(z3_contract.build_script, "scripts/z3/build.sh")
        self.assertEqual(z3_contract.tests_script, "scripts/z3/tests.sh")
        self.assertEqual(z3_contract.seeds_dir, "FUZZING_SEEDS")
        self.assertEqual(z3_contract.tests_repository_url, "https://github.com/z3prover/z3test.git")

        opensmt_contract = load_solver_contract(repo_root / "contracts" / "solvers" / "opensmt.yml")
        self.assertTrue(opensmt_contract.target_commands)
        self.assertEqual(opensmt_contract.build_script, "scripts/opensmt/build.sh")
        self.assertEqual(opensmt_contract.tests_script, "scripts/opensmt/tests.sh")
        self.assertEqual(opensmt_contract.seeds_dir, "FUZZING_SEEDS")
        self.assertIsNone(opensmt_contract.tests_repository_url)

    def test_bad_reference_contract_fails_during_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "solver").mkdir()
            reference_contract = root / "reference.yml"
            self._write_contract(
                reference_contract,
                """\
                solver_name: reference
                repository_url: https://github.com/example/reference.git
                build_command: python3
                coverage_build_command: python3
                production_binary_path: build/reference
                coverage_binary_path: build/reference
                test_root: .
                artifact_paths: []
                """,
            )
            contract = self._write_contract(
                root / "solver.yml",
                """\
                solver_name: demo
                repository_url: https://github.com/example/demo.git
                repository_path: solver
                build_command: python3
                coverage_build_command: python3
                production_binary_path: build/demo
                coverage_binary_path: build/demo
                test_root: .
                reference_contract_path: contract:reference.yml
                target_commands:
                  - "{target_binary}"
                artifact_paths: []
                """,
            )

            brain = SolverFuzzingBrain(contract, workspace_root=root)
            with self.assertRaises(ContractError) as ctx:
                brain.validate_integration()

            rendered = ctx.exception.render()
            self.assertIn("missing `target_commands`", rendered)
            self.assertIn("repository url: https://github.com/example/reference.git", rendered)
            self.assertIn("issue tracker: https://github.com/example/reference/issues", rendered)

    def test_z3_and_opensmt_validate_and_prepare_seeds_from_tests_script(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]

        for solver_name, source_setup, expected_tests in [
            (
                "z3",
                lambda workspace_root: (
                    (workspace_root / "z3").mkdir(),
                    (workspace_root / "z3test" / "regressions" / "smt2" / "nested").mkdir(parents=True),
                    (workspace_root / "z3test" / "regressions" / "smt2" / "a.smt2").write_text(
                        "(check-sat)\n",
                        encoding="utf-8",
                    ),
                    (workspace_root / "z3test" / "regressions" / "smt2" / "nested" / "b.smt").write_text(
                        "(check-sat)\n",
                        encoding="utf-8",
                    ),
                    (workspace_root / "z3test" / "regressions" / "smt2" / "ignored.txt").write_text(
                        "ignore\n",
                        encoding="utf-8",
                    ),
                ),
                ["regressions/smt2/a.smt2", "regressions/smt2/nested/b.smt"],
            ),
            (
                "opensmt",
                lambda workspace_root: (
                    (workspace_root / "opensmt").mkdir(),
                    (workspace_root / "opensmt" / "test" / "regression" / "nested").mkdir(parents=True),
                    (workspace_root / "opensmt" / "test" / "regression" / "alpha.smt2").write_text(
                        "(check-sat)\n",
                        encoding="utf-8",
                    ),
                    (workspace_root / "opensmt" / "test" / "regression" / "nested" / "beta.smt").write_text(
                        "(check-sat)\n",
                        encoding="utf-8",
                    ),
                    (workspace_root / "opensmt" / "test" / "regression" / "ignored.txt").write_text(
                        "ignore\n",
                        encoding="utf-8",
                    ),
                ),
                ["alpha.smt2", "nested/beta.smt"],
            ),
        ]:
            with self.subTest(solver=solver_name):
                with tempfile.TemporaryDirectory() as tmp:
                    workspace_root = Path(tmp)
                    source_setup(workspace_root)

                    validate = subprocess.run(
                        [
                            "python3",
                            str(repo_root / "scripts" / "solver_fuzzing_brain.py"),
                            "--contract",
                            str(repo_root / "contracts" / "solvers" / f"{solver_name}.yml"),
                            "validate",
                            "--json",
                        ],
                        cwd=repo_root,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(validate.returncode, 0, msg=validate.stderr)
                    self.assertEqual(json.loads(validate.stdout)["status"], "ok")

                    prepare = subprocess.run(
                        [
                            "python3",
                            str(repo_root / "scripts" / "solver_fuzzing_brain.py"),
                            "--contract",
                            str(repo_root / "contracts" / "solvers" / f"{solver_name}.yml"),
                            "--workspace-root",
                            str(workspace_root),
                            "prepare-seeds",
                            "--json",
                        ],
                        cwd=repo_root,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(prepare.returncode, 0, msg=prepare.stderr)
                    payload = json.loads(prepare.stdout)
                    self.assertEqual(payload["tests"], expected_tests)
                    self.assertEqual(payload["total_tests"], len(expected_tests))
                    brain = SolverFuzzingBrain(
                        repo_root / "contracts" / "solvers" / f"{solver_name}.yml",
                        workspace_root=workspace_root,
                    )
                    self.assertEqual(payload["seeds_dir"], str(brain.resolve_seeds_dir()))
                    self.assertEqual(brain.discover_tests(), expected_tests)

    def test_reference_contract_path_defaults_to_repo_root(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            brain = SolverFuzzingBrain(
                repo_root / "contracts" / "solvers" / "cvc5.yml",
                workspace_root=Path(tmp),
            )

            resolved = brain._resolve_reference_contract_path(step="contract validation")

            self.assertEqual(resolved, repo_root / "contracts" / "solvers" / "z3.yml")

    def test_issue_url_override_beats_derivation(self) -> None:
        self.assertEqual(
            derive_github_issues_url("git@github.com:example/project.git"),
            "https://github.com/example/project/issues",
        )


if __name__ == "__main__":
    unittest.main()
