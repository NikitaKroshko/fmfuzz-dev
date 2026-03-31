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
from scripts.local_commit_fuzzer_matrix import discover_opensmt_tests
from scripts.opensmt.commit_fuzzer import prepare_commit_fuzzer, simple_commit_fuzzer
from scripts.opensmt.commit_fuzzer import run_commit_fuzzer


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

    def test_commit_wrappers_analyze_requested_history(self) -> None:
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

            wrapper_specs = [
                (
                    "cvc5",
                    workspace_root / "scripts" / "cvc5" / "commit_fuzzer" / "run_prepare_commit_fuzzer.sh",
                    "CVC5_COMMIT_HASH",
                ),
                (
                    "opensmt",
                    workspace_root / "scripts" / "opensmt" / "commit_fuzzer" / "run_prepare_commit_fuzzer.sh",
                    "OPENSMT_COMMIT_HASH",
                ),
                (
                    "z3",
                    workspace_root / "scripts" / "z3" / "commit_fuzzer" / "run_prepare_commit_fuzzer.sh",
                    "Z3_COMMIT_HASH",
                ),
            ]

            for name, script_path, _commit_env_var in wrapper_specs:
                invocation_log = workdir / f"{name}-invocations.txt"
                env = os.environ.copy()
                env["INVOCATION_LOG"] = str(invocation_log)
                result = subprocess.run(
                    [
                        "bash",
                        str(script_path),
                        "3",
                        "--python-script",
                        str(stub_script),
                        "--coverage-file",
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
                    msg=f"{name} wrapper failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
                )
                self.assertEqual(invocation_log.read_text(encoding="utf-8").splitlines(), expected_commits)
                self.assertIn(
                    "OVERALL SUMMARY: commits=3; total_functions=3; with_coverage=3; without_coverage=0; overall_coverage=100.0%",
                    result.stdout,
                )

            override_log = workdir / "cvc5-override-invocations.txt"
            override_env = os.environ.copy()
            override_env.update(
                {
                    "INVOCATION_LOG": str(override_log),
                    "CVC5_COMMIT_HASH": expected_commits[1],
                }
            )
            override_result = subprocess.run(
                [
                    "bash",
                    str(wrapper_specs[0][1]),
                    "5",
                    "--python-script",
                    str(stub_script),
                    "--coverage-file",
                    str(coverage_file),
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
                msg=f"cvc5 override wrapper failed\nstdout:\n{override_result.stdout}\nstderr:\n{override_result.stderr}",
            )
            self.assertEqual(override_log.read_text(encoding="utf-8").splitlines(), [expected_commits[1]])
            self.assertIn(
                "OVERALL SUMMARY: commits=1; total_functions=1; with_coverage=1; without_coverage=0; overall_coverage=100.0%",
                override_result.stdout,
            )

    def test_cvc5_coverage_mapper_publishes_final_artifact(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        mapper_text = (repo_root / ".github" / "workflows" / "cvc5-coverage-mapper.yml").read_text(encoding="utf-8")
        analyzer_text = (repo_root / ".github" / "workflows" / "cvc5-commit-analyzer-test.yml").read_text(encoding="utf-8")

        join_block = mapper_text.split("  join-mappings:", 1)[1].split("  update-state:", 1)[0]
        self.assertIn("uses: actions/upload-artifact@v4", join_block)
        self.assertIn("name: coverage-mapping", join_block)
        self.assertIn("path: coverage_mapping.json.gz", join_block)
        self.assertIn("name: coverage-mapping", analyzer_text)
        self.assertIn("gunzip coverage_mapping.json.gz", analyzer_text)

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

    def test_opensmt_simple_commit_fuzzer_runs_to_completion(self) -> None:
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
                "simple_commit_fuzzer.py",
                "--tests-json",
                tests_json,
                "--tests-root",
                "test/regression",
                "--workers",
                "1",
                "--iterations",
                "1",
                "--modulo",
                "1",
                "--bugs-folder",
                "bugs",
                "--opensmt-path",
                "./build/bin/opensmt",
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
                exit_code = simple_commit_fuzzer.main()

            self.assertEqual(exit_code, 0)
            self.assertTrue((workdir / "bugs" / "open-smt-bug.smt2").exists())
            combined_output = stdout.getvalue() + stderr.getvalue()
            self.assertIn("Running fuzzer on 1 test(s)", combined_output)
            self.assertIn("Workers: 1", combined_output)
            self.assertIn("Found 1 bug(s):", combined_output)
            self.assertIn("Bug #1: bugs/open-smt-bug.smt2", combined_output)

    def test_opensmt_simple_commit_fuzzer_counts_collected_bugs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            bin_dir = workdir / "bin"
            bin_dir.mkdir()

            self._write_executable(
                bin_dir / "opensmt",
                """\
                #!/bin/sh
                exit 0
                """,
            )
            self._write_executable(
                bin_dir / "cvc5",
                """\
                #!/bin/sh
                exit 0
                """,
            )

            fuzzer = simple_commit_fuzzer.SimpleCommitFuzzer(
                tests=[],
                tests_root=str(workdir / "tests"),
                bugs_folder=str(workdir / "bugs"),
                num_workers=1,
                iterations=1,
                modulo=1,
                opensmt_path=str(bin_dir / "opensmt"),
                cvc5_path=str(bin_dir / "cvc5"),
            )

            worker_bug_dir = workdir / "bugs" / "worker_1"
            worker_bug_dir.mkdir(parents=True, exist_ok=True)
            (worker_bug_dir / "seed-bug.smt2").write_text("(check-sat)\n", encoding="utf-8")

            fuzzer._move_worker_bug_files()

            self.assertTrue((workdir / "bugs" / "seed-bug.smt2").exists())
            self.assertEqual(fuzzer._get_stat("bugs_found"), 1)

    def test_opensmt_prepare_commit_fuzzer_generates_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            old_cwd = Path.cwd()
            self.addCleanup(os.chdir, old_cwd)
            os.chdir(workdir)

            tests_root = workdir / "test" / "regression"
            (tests_root / "nested").mkdir(parents=True)
            (tests_root / "a.smt2").write_text("(check-sat)\n", encoding="utf-8")
            (tests_root / "nested" / "b.smt").write_text("(check-sat)\n", encoding="utf-8")

            coverage_json = workdir / "coverage_mapping.json"
            coverage_json.write_text("{}\n", encoding="utf-8")
            matrix_path = workdir / "matrix.json"

            argv = [
                "prepare_commit_fuzzer.py",
                "HEAD",
                "--coverage-json",
                str(coverage_json),
                "--output-matrix",
                str(matrix_path),
                "--tests-per-job",
                "1",
                "--max-jobs",
                "2",
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
                    {"job_id": 1, "job_name": "opensmt-job-1", "tests": ["a.smt2"]},
                    {"job_id": 2, "job_name": "opensmt-job-2", "tests": ["nested/b.smt"]},
                ],
            )
            combined_output = stdout.getvalue() + stderr.getvalue()
            self.assertIn("Changed functions: 2; with coverage: 2; without: 0;", combined_output)


if __name__ == "__main__":
    unittest.main()
