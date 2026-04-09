from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import subprocess
import textwrap
import unittest
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from scripts.commit_harness_runner import build_opensmt_targets
from scripts.diff_utils import parse_unified_diff
from scripts.local_commit_fuzzer_matrix import discover_opensmt_tests
from scripts.opensmt.commit_fuzzer import prepare_commit_fuzzer, run_commit_fuzzer


class CommitHarnessTests(unittest.TestCase):
    def _write_executable(self, path: Path, content: str) -> None:
        path.write_text(textwrap.dedent(content), encoding="utf-8")
        path.chmod(0o755)

    def _git(self, cwd: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def _import_scheduling_module(self, module_name: str):
        import importlib
        import types

        fake_boto3 = types.ModuleType("boto3")
        fake_boto3.client = lambda *args, **kwargs: object()  # type: ignore[assignment]

        fake_botocore = types.ModuleType("botocore")
        fake_botocore.__path__ = []  # type: ignore[attr-defined]
        fake_botocore_exceptions = types.ModuleType("botocore.exceptions")
        fake_git = types.ModuleType("git")
        fake_git.NULL_TREE = object()

        class BadName(Exception):
            pass

        fake_git.exc = types.SimpleNamespace(BadName=BadName)

        class ClientError(Exception):
            pass

        class NoCredentialsError(Exception):
            pass

        fake_botocore_exceptions.ClientError = ClientError
        fake_botocore_exceptions.NoCredentialsError = NoCredentialsError
        fake_botocore.exceptions = fake_botocore_exceptions  # type: ignore[attr-defined]

        with patch.dict(
            sys.modules,
            {
                "boto3": fake_boto3,
                "botocore": fake_botocore,
                "botocore.exceptions": fake_botocore_exceptions,
                "git": fake_git,
            },
            clear=False,
        ):
            return importlib.import_module(module_name)

    def _import_coverage_state_module(self):
        import importlib
        import types

        fake_boto3 = types.ModuleType("boto3")
        fake_boto3.client = lambda *args, **kwargs: object()  # type: ignore[assignment]

        fake_botocore = types.ModuleType("botocore")
        fake_botocore.__path__ = []  # type: ignore[attr-defined]
        fake_botocore_exceptions = types.ModuleType("botocore.exceptions")

        class ClientError(Exception):
            pass

        fake_botocore_exceptions.ClientError = ClientError
        fake_botocore.exceptions = fake_botocore_exceptions  # type: ignore[attr-defined]

        with patch.dict(
            sys.modules,
            {
                "boto3": fake_boto3,
                "botocore": fake_botocore,
                "botocore.exceptions": fake_botocore_exceptions,
            },
            clear=False,
        ):
            return importlib.import_module("scripts.coverage.coverage_state")

    def test_build_opensmt_targets(self) -> None:
        self.assertEqual(
            build_opensmt_targets("cvc5", "opensmt"),
            [
                "cvc5 --check-models --check-proofs --strings-exp",
                "opensmt",
            ],
        )

    def test_builder_returns_oldest_commit_from_fifo_queue(self) -> None:
        builder = self._import_scheduling_module("scripts.scheduling.builder")

        class FakeManager:
            def _get_versioned_filename(self, base_filename, version=None):  # noqa: ARG002
                return "build-queue-v2.json"

            def read_state(self, filename, default=None):  # noqa: ARG002
                return {"queue": ["oldest", "middle", "newest"]}

        with patch.object(builder, "get_state_manager", return_value=FakeManager()):
            self.assertEqual(builder.get_next_commit_to_build("cvc5"), "oldest")

    def test_manager_leaves_last_checked_commit_unchanged_after_partial_failure(self) -> None:
        scheduling_manager = self._import_scheduling_module("scripts.scheduling.manager")

        class FakeStateManager:
            def __init__(self) -> None:
                self.build_queue = []
                self.fuzz_queue = []
                self.updated_commits = []

            def get_last_checked_commit(self):
                return None

            def add_to_build_queue(self, commit):
                self.build_queue.append(commit)

            def add_to_fuzzing_schedule(self, commit):
                self.fuzz_queue.append(commit)

            def remove_from_fuzzing_schedule(self, commit):  # noqa: ARG002
                return False

            def get_fuzzing_schedule(self):
                return []

            def update_last_checked_commit(self, commit):
                self.updated_commits.append(commit)

        fake_manager = FakeStateManager()

        def fake_detect_cpp_changes(repo_url, commit, token):  # noqa: ARG001
            if commit == "commit-b":
                raise RuntimeError("detection failed")
            if commit == "commit-c":
                return True, ["src/foo.cpp"]
            return False, []

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(scheduling_manager, "get_state_manager", return_value=fake_manager),
            patch.object(scheduling_manager, "get_commits_from_github", return_value=["commit-a", "commit-b", "commit-c"]),
            patch.object(scheduling_manager, "detect_cpp_changes", side_effect=fake_detect_cpp_changes),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            scheduling_manager.run_manager("cvc5", "https://github.com/example/repo.git")

        self.assertEqual(fake_manager.build_queue, ["commit-c"])
        self.assertEqual(fake_manager.fuzz_queue, ["commit-c"])
        self.assertEqual(fake_manager.updated_commits, [])

    def test_prepare_commits_analyzes_requested_history(self) -> None:
        workspace_root = Path(__file__).resolve().parents[1]

        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            repo_root = workdir / "repo"
            repo_root.mkdir()

            self._git(repo_root, "init")
            self._git(repo_root, "config", "user.email", "codex@example.com")
            self._git(repo_root, "config", "user.name", "Codex")

            tracked_file = repo_root / "sample.txt"
            for index in range(4):
                tracked_file.write_text(f"{index}\n", encoding="utf-8")
                self._git(repo_root, "add", "sample.txt")
                self._git(repo_root, "commit", "-m", f"commit {index}")

            expected_commits = self._git(repo_root, "log", "--format=%H", "-n", "3").splitlines()
            coverage_file = workdir / "coverage_mapping.json"
            coverage_file.write_text("{}\n", encoding="utf-8")

            stub_script = workdir / "prepare_commit_fuzzer.py"
            stub_script.write_text(
                "#!/usr/bin/env python3\n"
                "import os\n"
                "import sys\n"
                "from pathlib import Path\n\n"
                "def main() -> int:\n"
                "    log_path = Path(os.environ[\"INVOCATION_LOG\"])\n"
                "    with log_path.open(\"a\", encoding=\"utf-8\") as handle:\n"
                "        handle.write(sys.argv[1] + \"\\n\")\n"
                "    print(\"Changed functions: 1; with coverage: 1; without: 0;\")\n"
                "    return 0\n\n"
                "if __name__ == \"__main__\":\n"
                "    raise SystemExit(main())\n",
                encoding="utf-8",
            )
            stub_script.chmod(0o755)
            contract_path = workdir / "solver.yml"
            contract_path.write_text(
                textwrap.dedent(
                    f"""\
                    solver_name: demo
                    repository_url: https://github.com/example/demo.git
                    repository_path: repo
                    build_command: /bin/true
                    coverage_build_command: /bin/true
                    production_binary_path: build/demo
                    coverage_binary_path: build/demo
                    test_root: .
                    test_discovery_command: null
                    target_commands:
                      - "{{target_binary}}"
                    oracle_command: null
                    commit_prepare_command: {stub_script}
                    environment_requirements:
                      packages: []
                      env: []
                    artifact_paths: []
                    artifact_s3_bucket: null
                    artifact_s3_prefix: null
                    """
                ),
                encoding="utf-8",
            )

            invocation_log = workdir / "invocations.txt"
            env = os.environ.copy()
            env["INVOCATION_LOG"] = str(invocation_log)
            result = subprocess.run(
                [
                    "python3",
                    str(workspace_root / "scripts" / "solver_fuzzing_brain.py"),
                    "--contract",
                    str(contract_path),
                    "--workspace-root",
                    str(workdir),
                    "prepare-commits",
                    "3",
                    "--coverage-json",
                    str(coverage_file),
                ],
                cwd=repo_root,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(
                result.returncode,
                0,
                msg=f"prepare-commits failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertEqual(invocation_log.read_text(encoding="utf-8").splitlines(), expected_commits)
            self.assertIn(
                "OVERALL SUMMARY: commits=3; total_functions=3; with_coverage=3; without_coverage=0; overall_coverage=100.0%",
                result.stdout,
            )

            override_log = workdir / "override-invocations.txt"
            override_env = os.environ.copy()
            override_env["INVOCATION_LOG"] = str(override_log)
            override_result = subprocess.run(
                [
                    "python3",
                    str(workspace_root / "scripts" / "solver_fuzzing_brain.py"),
                    "--contract",
                    str(contract_path),
                    "--workspace-root",
                    str(workdir),
                    "prepare-commits",
                    "5",
                    "--coverage-json",
                    str(coverage_file),
                    "--commit-hash",
                    expected_commits[1],
                ],
                cwd=repo_root,
                env=override_env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                override_result.returncode,
                0,
                msg=f"prepare-commits override failed\nstdout:\n{override_result.stdout}\nstderr:\n{override_result.stderr}",
            )
            self.assertEqual(override_log.read_text(encoding="utf-8").splitlines(), [expected_commits[1]])
            self.assertIn(
                "OVERALL SUMMARY: commits=1; total_functions=1; with_coverage=1; without_coverage=0; overall_coverage=100.0%",
                override_result.stdout,
            )

    def test_parse_unified_diff_tracks_delete_only_hunks(self) -> None:
        diff_text = """\
diff --git a/src/foo.cpp b/src/foo.cpp
index 1111111..2222222 100644
--- a/src/foo.cpp
+++ b/src/foo.cpp
@@ -10,2 +10,0 @@
-  remove_me();
-  remove_me_too();
"""

        parsed = parse_unified_diff(diff_text)
        self.assertEqual(set(parsed), {"src/foo.cpp"})
        file_diff = parsed["src/foo.cpp"]
        self.assertEqual(file_diff.old_path, "src/foo.cpp")
        self.assertEqual(file_diff.new_path, "src/foo.cpp")
        self.assertTrue(file_diff.has_delete_only_overlap(10, 11))
        self.assertTrue(file_diff.overlaps_before(10, 11))
        self.assertFalse(file_diff.overlaps_after(1, 100))

    def test_coverage_state_rebuilds_when_commit_changes_without_test_count_change(self) -> None:
        coverage_state = self._import_coverage_state_module()
        manager = coverage_state.CoverageStateManager(bucket="bucket", solver="z3")

        with patch.object(
            manager,
            "get_state",
            return_value={
                "test_count": 42,
                "last_build_timestamp": "2026-03-01T00:00:00Z",
                "commit_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "solver_version": "main",
            },
        ):
            decision = manager.should_rebuild(
                42,
                current_commit_hash="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            )

        self.assertTrue(decision["should_rebuild"])
        self.assertIn("Commit changed", decision["reason"])

    def test_cvc5_coverage_mapper_publishes_final_artifact(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        mapper_text = (repo_root / ".github" / "workflows" / "cvc5-coverage-mapper.yml").read_text(encoding="utf-8")
        analyzer_text = (repo_root / ".github" / "workflows" / "cvc5-commit-analyzer-test.yml").read_text(encoding="utf-8")

        join_block = mapper_text.split("  join-mappings:", 1)[1].split("  update-state:", 1)[0]
        self.assertIn("uses: actions/upload-artifact@v4", join_block)
        self.assertIn("name: coverage-mapping", join_block)
        self.assertIn("path: coverage_mapping.json.gz", join_block)
        self.assertIn("contracts/solvers/cvc5.yml", mapper_text)
        self.assertIn("scripts/solver_fuzzing_brain.py", mapper_text)
        self.assertIn("name: coverage-mapping", analyzer_text)
        self.assertIn("gunzip coverage_mapping.json.gz", analyzer_text)
        self.assertIn("contracts/solvers/cvc5.yml", analyzer_text)
        self.assertIn("scripts/solver_fuzzing_brain.py", analyzer_text)

    def test_opensmt_coverage_mapper_publishes_final_artifact(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        workflow_text = (repo_root / ".github" / "workflows" / "opensmt-coverage-mapper.yml").read_text(encoding="utf-8")

        join_block = workflow_text.split("  join-mappings:", 1)[1].split("  update-state:", 1)[0]
        self.assertIn("uses: actions/upload-artifact@v4", join_block)
        self.assertIn("name: coverage-mapping", join_block)
        self.assertIn("path: coverage_mapping.json.gz", join_block)

    def test_build_workflows_delegate_to_shared_contract_driven_build_workflow(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        reusable_text = (repo_root / ".github" / "workflows" / "solver-build.yml").read_text(encoding="utf-8")

        self.assertIn('python3 scripts/scheduling/check_build_queue.py "${{ inputs.solver_name }}"', reusable_text)
        self.assertIn("scripts/solver_fuzzing_brain.py", reusable_text)
        self.assertNotIn("tail -1", reusable_text)

        for workflow_name, solver in [("z3.yml", "z3"), ("cvc5.yml", "cvc5"), ("opensmt.yml", "opensmt")]:
            workflow_text = (repo_root / ".github" / "workflows" / workflow_name).read_text(encoding="utf-8")
            self.assertIn("uses: ./.github/workflows/solver-build.yml", workflow_text)
            self.assertIn(f"solver_name: {solver}", workflow_text)
            self.assertIn(f"contract_path: contracts/solvers/{solver}.yml", workflow_text)

    def test_coverage_mapper_workflows_propagate_resolved_commit_hash(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]

        workflow_expectations = {
            "z3-coverage-mapper.yml": '--commit-hash "${{ needs.discover-tests.outputs.commit_hash }}"',
            "cvc5-coverage-mapper.yml": '--commit-hash "${{ needs.discover-tests.outputs.commit_hash }}"',
            "opensmt-coverage-mapper.yml": '--commit-hash "${{ needs.discover-tests.outputs.commit_hash }}"',
        }

        for workflow_name, replay_step in workflow_expectations.items():
            workflow_text = (repo_root / ".github" / "workflows" / workflow_name).read_text(encoding="utf-8")
            self.assertIn("commit_hash: ${{ steps.resolve-commit.outputs.commit_hash }}", workflow_text)
            self.assertIn(replay_step, workflow_text)
            self.assertIn('COMMIT_HASH="${{ needs.discover-tests.outputs.commit_hash }}"', workflow_text)
            self.assertNotIn("steps.get-commit.outputs.commit_hash", workflow_text)

    def test_cvc5_workflows_use_contract_brain_and_drop_legacy_helpers(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]

        workflow_names = [
            "cvc5-coverage-mapper.yml",
            "cvc5-coverage-daily-check.yml",
            "cvc5-commit-fuzzer.yml",
            "cvc5-regression.yml",
            "cvc5-evaluation-rq2-build.yml",
            "cvc5-commit-analyzer-test.yml",
        ]
        for workflow_name in workflow_names:
            workflow_text = (repo_root / ".github" / "workflows" / workflow_name).read_text(encoding="utf-8")
            self.assertIn("contracts/solvers/cvc5.yml", workflow_text)
            self.assertIn("scripts/solver_fuzzing_brain.py", workflow_text)

        rq2_build_text = (repo_root / ".github" / "workflows" / "cvc5-evaluation-rq2-build.yml").read_text(encoding="utf-8")
        rq2_coverage_text = (repo_root / ".github" / "workflows" / "cvc5-evaluation-rq2-coverage-mapping.yml").read_text(encoding="utf-8")
        self.assertIn("--artifact-archive cvc5/build/artifacts-production.tar.gz", rq2_build_text)
        self.assertNotIn("scripts/cvc5/collect_build_artifacts.sh", rq2_build_text)
        self.assertNotIn("scripts/cvc5/extract_build_artifacts.sh", rq2_coverage_text)
        self.assertIn("tar -xzf artifacts/artifacts.tar.gz -C cvc5", rq2_coverage_text)

    def test_commit_fuzzer_workflows_use_shared_harness(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]

        for solver in ("cvc5", "z3", "opensmt"):
            commit_fuzzer_text = (
                repo_root / ".github" / "workflows" / f"{solver}-commit-fuzzer.yml"
            ).read_text(encoding="utf-8")
            self.assertIn("scripts/solver_fuzzing_brain.py", commit_fuzzer_text)
            self.assertIn(f"contracts/solvers/{solver}.yml", commit_fuzzer_text)
            self.assertIn("run-harness", commit_fuzzer_text)
            self.assertNotIn("simple_commit_fuzzer.py", commit_fuzzer_text)
            self.assertNotIn("run_commit_fuzzer.py", commit_fuzzer_text)
            self.assertNotIn(f"scripts/{solver}/commit_fuzzer", commit_fuzzer_text)

    def test_opensmt_regression_workflow_uses_contract_brain(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]

        regression_text = (repo_root / ".github" / "workflows" / "opensmt-regression.yml").read_text(encoding="utf-8")
        self.assertIn("run-regression", regression_text)

    def test_deleted_legacy_helpers_are_absent(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        deleted_paths = [
            "scripts/cvc5/coverage/generate_matrix.py",
            "scripts/cvc5/coverage/join_coverage_mappings.py",
            "scripts/cvc5/coverage/run_coverage_builder.sh",
            "scripts/z3/coverage/generate_matrix.py",
            "scripts/z3/coverage/join_coverage_mappings.py",
            "scripts/z3/coverage/run_coverage_builder.sh",
            "scripts/opensmt/coverage/generate_matrix.py",
            "scripts/opensmt/coverage/join_coverage_mappings.py",
            "scripts/opensmt/coverage/run_coverage_builder.sh",
            "scripts/cvc5/commit_fuzzer/simple_commit_fuzzer.py",
            "scripts/cvc5/commit_fuzzer/run_prepare_commit_fuzzer.sh",
            "scripts/z3/commit_fuzzer/run_prepare_commit_fuzzer.sh",
            "scripts/opensmt/commit_fuzzer/run_prepare_commit_fuzzer.sh",
            "scripts/cvc5/run_regression_tests.sh",
            "scripts/opensmt/run_regression_tests.sh",
            "scripts/opensmt/run_regression_tests.py",
        ]
        for relative_path in deleted_paths:
            self.assertFalse((repo_root / relative_path).exists(), msg=relative_path)

    def test_workflows_do_not_contain_empty_env_blocks(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]

        for workflow_path in sorted((repo_root / ".github" / "workflows").glob("*.yml")):
            lines = workflow_path.read_text(encoding="utf-8").splitlines()
            for index, line in enumerate(lines):
                if line.strip() != "env:":
                    continue
                env_indent = len(line) - len(line.lstrip(" "))
                for next_line in lines[index + 1 :]:
                    if not next_line.strip():
                        continue
                    next_indent = len(next_line) - len(next_line.lstrip(" "))
                    self.assertGreater(
                        next_indent,
                        env_indent,
                        msg=f"{workflow_path.relative_to(repo_root)} has an empty env block at line {index + 1}",
                    )
                    break

    def test_z3_and_opensmt_rq2_workflows_drop_legacy_artifact_helpers(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]

        z3_build_text = (repo_root / ".github" / "workflows" / "z3-evaluation-rq2-build.yml").read_text(encoding="utf-8")
        z3_coverage_text = (repo_root / ".github" / "workflows" / "z3-evaluation-rq2-coverage-mapping.yml").read_text(encoding="utf-8")
        opensmt_build_text = (repo_root / ".github" / "workflows" / "opensmt-evaluation-rq2-build.yml").read_text(encoding="utf-8")
        opensmt_coverage_text = (repo_root / ".github" / "workflows" / "opensmt-evaluation-rq2-coverage-mapping.yml").read_text(encoding="utf-8")

        self.assertIn("contracts/solvers/z3.yml", z3_build_text)
        self.assertIn("--artifact-archive z3/build/artifacts-production.tar.gz", z3_build_text)
        self.assertNotIn("scripts/z3/collect_build_artifacts.sh", z3_build_text)
        self.assertNotIn("scripts/z3/extract_build_artifacts.sh", z3_coverage_text)
        self.assertIn("tar -xzf artifacts/artifacts.tar.gz -C z3", z3_coverage_text)

        self.assertIn("contracts/solvers/opensmt.yml", opensmt_build_text)
        self.assertIn("--artifact-archive artifacts-production.tar.gz", opensmt_build_text)
        self.assertNotIn("scripts/opensmt/collect_build_artifacts.sh", opensmt_build_text)
        self.assertNotIn("scripts/opensmt/extract_build_artifacts.sh", opensmt_coverage_text)
        self.assertIn("tar -xzf artifacts/artifacts.tar.gz -C opensmt", opensmt_coverage_text)

    def test_cache_backed_upstream_workflows_persist_sha_after_successful_build(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]

        expectations = {
            "q3b.yml": ("Build Q3B", ".cache/q3b_last_sha"),
            "bitwuzla.yml": ("Build Bitwuzla", ".cache/bitwuzla_last_sha"),
            "stp.yml": ("Build STP", ".cache/stp_last_sha"),
            "smtrat.yml": ("Build SMT-RAT", ".cache/smtrat_last_sha"),
        }

        for workflow_name, (build_step, cache_file) in expectations.items():
            workflow_text = (repo_root / ".github" / "workflows" / workflow_name).read_text(encoding="utf-8")
            self.assertIn("Persist built SHA", workflow_text)
            self.assertIn(cache_file, workflow_text)
            self.assertLess(workflow_text.index(build_step), workflow_text.index("Persist built SHA"))

    def test_daily_coverage_checks_include_commit_hash_in_rebuild_decision(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]

        for workflow_name in [
            "z3-coverage-daily-check.yml",
            "cvc5-coverage-daily-check.yml",
            "opensmt-coverage-daily-check.yml",
        ]:
            workflow_text = (repo_root / ".github" / "workflows" / workflow_name).read_text(encoding="utf-8")
            self.assertIn("--commit-hash ${{ steps.count-tests.outputs.commit_hash }}", workflow_text)

    def test_discover_opensmt_tests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "opensmt"
            seeds_root = repo_root / "test" / "regression"
            (seeds_root / "nested").mkdir(parents=True)
            (seeds_root / "a.smt2").write_text("(check-sat)\n", encoding="utf-8")
            (seeds_root / "nested" / "b.smt").write_text("(check-sat)\n", encoding="utf-8")
            (seeds_root / "splitting" / "patches").mkdir(parents=True)
            (seeds_root / "splitting" / "patches" / "ignored.smt2").write_text("(check-sat)\n", encoding="utf-8")
            (seeds_root / "ignore.txt").write_text("not a seed\n", encoding="utf-8")

            self.assertEqual(
                discover_opensmt_tests(str(repo_root)),
                ["a.smt2", "nested/b.smt"],
            )

    def test_discover_opensmt_tests_prioritizes_supported_array_families(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "opensmt"
            seeds_root = repo_root / "test" / "regression"
            (seeds_root / "base" / "QF_AUFLIRA").mkdir(parents=True)
            (seeds_root / "base" / "QF_AX").mkdir(parents=True)
            (seeds_root / "base" / "QF_BV").mkdir(parents=True)
            (seeds_root / "misc").mkdir(parents=True)

            (seeds_root / "base" / "QF_AUFLIRA" / "mixed.smt2").write_text("(check-sat)\n", encoding="utf-8")
            (seeds_root / "base" / "QF_AX" / "array.smt2").write_text("(check-sat)\n", encoding="utf-8")
            (seeds_root / "base" / "QF_BV" / "bv.smt2").write_text("(check-sat)\n", encoding="utf-8")
            (seeds_root / "misc" / "plain.smt2").write_text("(check-sat)\n", encoding="utf-8")

            self.assertEqual(
                discover_opensmt_tests(str(repo_root)),
                [
                    "base/QF_AUFLIRA/mixed.smt2",
                    "base/QF_AX/array.smt2",
                    "misc/plain.smt2",
                    "base/QF_BV/bv.smt2",
                ],
            )

    def test_opensmt_commit_fuzzer_runs_to_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            old_cwd = Path.cwd()
            self.addCleanup(os.chdir, old_cwd)
            os.chdir(workdir)

            tests_root = workdir / "test" / "regression"
            tests_root.mkdir(parents=True)
            (tests_root / "seed.smt2").write_text("(check-sat)\n", encoding="utf-8")

            bin_dir = workdir / "bin"
            bin_dir.mkdir()
            opensmt_build_bin = workdir / "build" / "bin"
            opensmt_build_bin.mkdir(parents=True)

            self._write_executable(
                bin_dir / "typefuzz",
                """\
                #!/usr/bin/env python3
                import sys
                from pathlib import Path

                def main() -> int:
                    args = sys.argv[1:]
                    bugs_dir = None
                    for index, value in enumerate(args):
                        if value == "--bugs" and index + 1 < len(args):
                            bugs_dir = Path(args[index + 1])
                            break
                    if bugs_dir is None:
                        return 2

                    bugs_dir.mkdir(parents=True, exist_ok=True)
                    sentinel = bugs_dir / ".seen"
                    if sentinel.exists():
                        return 3

                    sentinel.write_text("seen\\n", encoding="utf-8")
                    (bugs_dir / "open-smt-bug.smt2").write_text("(check-sat)\\n", encoding="utf-8")
                    return 10

                if __name__ == "__main__":
                    raise SystemExit(main())
                """,
            )
            self._write_executable(
                bin_dir / "cvc5",
                """\
                #!/bin/sh
                exit 0
                """,
            )
            self._write_executable(
                opensmt_build_bin / "opensmt",
                """\
                #!/bin/sh
                exit 0
                """,
            )

            path_env = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
            tests_json = json.dumps(["seed.smt2"])
            argv = [
                "run_commit_fuzzer.py",
                "--tests-json",
                tests_json,
                "--tests-root",
                "test/regression",
                "--workers",
                "1",
                "--time-remaining",
                "3",
                "--iterations",
                "1",
                "--modulo",
                "1",
                "--bugs-folder",
                "bugs",
                "--cvc5-path",
                "cvc5",
            ]

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch.dict(os.environ, {"PATH": path_env}, clear=False),
                patch.object(sys, "argv", argv),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = run_commit_fuzzer.main()

            self.assertEqual(exit_code, 0)
            self.assertTrue((workdir / "bugs" / "open-smt-bug.smt2").exists())
            combined_output = stdout.getvalue() + stderr.getvalue()
            self.assertIn("[WORKER 1] ✓ Exit code 10: Found 1 bug(s) on seed.smt2", combined_output)

    def test_opensmt_prepare_commit_fuzzer_uses_commit_diff_and_matched_tests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            old_cwd = Path.cwd()
            self.addCleanup(os.chdir, old_cwd)
            os.chdir(workdir)

            self._git(workdir, "init")
            self._git(workdir, "config", "user.email", "tests@example.com")
            self._git(workdir, "config", "user.name", "Tests")

            source_dir = workdir / "src"
            source_dir.mkdir(parents=True)
            source_file = source_dir / "example.cpp"
            source_file.write_text(
                textwrap.dedent(
                    """\
                    int stable() {
                      return 1;
                    }

                    int changed(int x) {
                      return x + 1;
                    }
                    """
                ),
                encoding="utf-8",
            )

            tests_root = workdir / "test" / "regression"
            (tests_root / "family_a").mkdir(parents=True)
            (tests_root / "family_b").mkdir(parents=True)
            (tests_root / "family_c").mkdir(parents=True)
            (tests_root / "family_a" / "alpha.smt2").write_text("(check-sat)\n", encoding="utf-8")
            (tests_root / "family_b" / "beta.smt2").write_text("(check-sat)\n", encoding="utf-8")
            (tests_root / "family_c" / "gamma.smt2").write_text("(check-sat)\n", encoding="utf-8")

            self._git(workdir, "add", ".")
            self._git(workdir, "commit", "-m", "initial")

            source_file.write_text(
                textwrap.dedent(
                    """\
                    int stable() {
                      return 1;
                    }

                    int changed(int x) {
                      return x + 2;
                    }
                    """
                ),
                encoding="utf-8",
            )
            self._git(workdir, "add", "src/example.cpp")
            self._git(workdir, "commit", "-m", "change function")
            commit_hash = self._git(workdir, "rev-parse", "HEAD")

            all_tests = discover_opensmt_tests(str(workdir))
            self.assertEqual(len(all_tests), 3)
            matched_tests = all_tests[:2]

            coverage_json = workdir / "coverage_mapping.json"
            coverage_json.write_text(
                json.dumps(
                    {
                        "src/example.cpp:changed(int):5": matched_tests,
                        "src/example.cpp:stable()": [all_tests[2]],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            matrix_path = workdir / "matrix.json"

            argv = [
                "prepare_commit_fuzzer.py",
                commit_hash,
                "--coverage-json",
                str(coverage_json),
                "--output-matrix",
                str(matrix_path),
                "--tests-per-job",
                "1",
                "--max-jobs",
                "2",
                "--opensmt-dir",
                str(workdir),
            ]

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch.object(sys, "argv", argv),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = prepare_commit_fuzzer.main()

            self.assertEqual(exit_code, 0)
            matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
            self.assertEqual(matrix["total_tests"], 2)
            self.assertEqual(matrix["total_jobs"], 2)
            self.assertEqual(matrix["tests_per_job"], 1)
            self.assertEqual(
                matrix["matrix"]["include"],
                [
                    {"job_id": 1, "job_name": "opensmt-job-1", "tests": [matched_tests[0]]},
                    {"job_id": 2, "job_name": "opensmt-job-2", "tests": [matched_tests[1]]},
                ],
            )
            combined_output = stdout.getvalue() + stderr.getvalue()
            self.assertIn(
                "Changed functions: 1; with coverage: 1; without: 0; unique tests: 2; coverage: 100.0%",
                combined_output,
            )
            self.assertIn("Functions selected from commit:", combined_output)
            self.assertIn("src/example.cpp:changed(int):5", combined_output)

    def test_opensmt_prepare_commit_fuzzer_falls_back_to_all_tests_when_coverage_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            old_cwd = Path.cwd()
            self.addCleanup(os.chdir, old_cwd)
            os.chdir(workdir)

            self._git(workdir, "init")
            self._git(workdir, "config", "user.email", "tests@example.com")
            self._git(workdir, "config", "user.name", "Tests")

            source_dir = workdir / "src"
            source_dir.mkdir(parents=True)
            source_file = source_dir / "example.cpp"
            source_file.write_text(
                textwrap.dedent(
                    """\
                    int stable() {
                      return 1;
                    }

                    int changed(int x) {
                      return x + 1;
                    }
                    """
                ),
                encoding="utf-8",
            )

            tests_root = workdir / "test" / "regression"
            (tests_root / "family_a").mkdir(parents=True)
            (tests_root / "family_b").mkdir(parents=True)
            (tests_root / "family_c").mkdir(parents=True)
            (tests_root / "family_a" / "alpha.smt2").write_text("(check-sat)\n", encoding="utf-8")
            (tests_root / "family_b" / "beta.smt2").write_text("(check-sat)\n", encoding="utf-8")
            (tests_root / "family_c" / "gamma.smt2").write_text("(check-sat)\n", encoding="utf-8")

            self._git(workdir, "add", ".")
            self._git(workdir, "commit", "-m", "initial")

            source_file.write_text(
                textwrap.dedent(
                    """\
                    // comment-only change
                    int stable() {
                      return 1;
                    }

                    int changed(int x) {
                      return x + 1;
                    }
                    """
                ),
                encoding="utf-8",
            )
            self._git(workdir, "add", "src/example.cpp")
            self._git(workdir, "commit", "-m", "comment-only change")
            no_functions_commit = self._git(workdir, "rev-parse", "HEAD")

            source_file.write_text(
                textwrap.dedent(
                    """\
                    // comment-only change
                    int stable() {
                      return 1;
                    }

                    int changed(int x) {
                      return x + 2;
                    }
                    """
                ),
                encoding="utf-8",
            )
            self._git(workdir, "add", "src/example.cpp")
            self._git(workdir, "commit", "-m", "change function")
            changed_function_commit = self._git(workdir, "rev-parse", "HEAD")

            all_tests = discover_opensmt_tests(str(workdir))
            self.assertEqual(all_tests, ["family_a/alpha.smt2", "family_b/beta.smt2", "family_c/gamma.smt2"])

            coverage_with_unrelated_tests = workdir / "coverage_unrelated.json"
            coverage_with_unrelated_tests.write_text(
                json.dumps({"src/other.cpp:unrelated()": all_tests}, indent=2),
                encoding="utf-8",
            )

            empty_coverage_json = workdir / "coverage_empty.json"
            empty_coverage_json.write_text("{}\n", encoding="utf-8")

            analyzer = prepare_commit_fuzzer.PrepareCommitAnalyzer(".", opensmt_dir=str(workdir))

            no_functions_result = analyzer.analyze_commit_coverage(no_functions_commit, str(coverage_with_unrelated_tests))
            self.assertTrue(no_functions_result["summary"]["fallback_to_all_tests"])
            self.assertEqual(no_functions_result["changed_functions"], [])
            self.assertEqual(no_functions_result["files_with_no_functions"], ["src/example.cpp"])
            self.assertEqual(no_functions_result["covering_tests"], all_tests)
            self.assertEqual(no_functions_result["summary"]["total_covering_tests"], len(all_tests))
            self.assertEqual(no_functions_result["summary"]["functions_with_tests"], 0)

            unrelated_match_result = analyzer.analyze_commit_coverage(
                changed_function_commit,
                str(coverage_with_unrelated_tests),
            )
            self.assertTrue(unrelated_match_result["summary"]["fallback_to_all_tests"])
            self.assertTrue(unrelated_match_result["changed_functions"])
            self.assertEqual(unrelated_match_result["covering_tests"], all_tests)
            self.assertEqual(unrelated_match_result["summary"]["total_covering_tests"], len(all_tests))
            self.assertEqual(unrelated_match_result["summary"]["functions_with_tests"], 0)

            empty_coverage_result = analyzer.analyze_commit_coverage(changed_function_commit, str(empty_coverage_json))
            self.assertTrue(empty_coverage_result["summary"]["fallback_to_all_tests"])
            self.assertEqual(empty_coverage_result["covering_tests"], [])
            self.assertEqual(empty_coverage_result["summary"]["total_covering_tests"], 0)
            self.assertEqual(empty_coverage_result["summary"]["functions_with_tests"], 0)


if __name__ == "__main__":
    unittest.main()
