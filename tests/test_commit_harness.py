#!/usr/bin/env python3
from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from scripts.commit_harness_orchestrator import CommitHarnessRunner
from scripts.commit_harness_runner import (
    TYPEFUZZ_HARNESS_TEMPLATE,
    build_cvc5_opensmt_targets,
    build_z3_cvc5_targets,
    run_commit_harness,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


class CommitHarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._old_cwd = os.getcwd()
        os.chdir(self._tmpdir.name)
        self.addCleanup(os.chdir, self._old_cwd)

    def _seed_test_tree(self) -> tuple[Path, Path]:
        tests_root = Path(self._tmpdir.name) / "tests-root"
        tests_root.mkdir(parents=True, exist_ok=True)
        test_path = tests_root / "seed.smt2"
        test_path.write_text("(check-sat)\n", encoding="utf-8")
        return tests_root, test_path

    def test_shared_target_helpers_build_expected_solver_pairs(self) -> None:
        self.assertEqual(
            build_z3_cvc5_targets("z3", "./build/bin/cvc5"),
            [
                "z3 smt.threads=1 memory_max_size=2048 model_validate=true",
                "./build/bin/cvc5 --check-models --check-proofs --strings-exp",
            ],
        )
        self.assertEqual(
            build_cvc5_opensmt_targets("cvc5", "opensmt"),
            [
                "cvc5 --check-models --check-proofs --strings-exp",
                "opensmt",
            ],
        )

    def test_run_commit_harness_uses_default_template_and_runner(self) -> None:
        with mock.patch("scripts.commit_harness_runner.CommitHarnessRunner") as runner_cls:
            runner_instance = runner_cls.return_value
            runner_instance.run.return_value = 27

            exit_code = run_commit_harness(
                tests='["seed.smt2"]',
                tests_root="/tmp/tests-root",
                bugs_folder="/tmp/bugs",
                num_workers=2,
                iterations=9,
                modulo=3,
                time_remaining=120,
                job_start_time=123.4,
                stop_buffer_minutes=7,
                targets=["z3", "cvc5"],
                job_id="job-7",
                strict_mode=True,
            )

        self.assertEqual(exit_code, 27)
        runner_cls.assert_called_once()
        called_kwargs = runner_cls.call_args.kwargs
        self.assertEqual(called_kwargs["tests"], ["seed.smt2"])
        self.assertEqual(called_kwargs["tests_root"], "/tmp/tests-root")
        self.assertEqual(called_kwargs["bugs_folder"], "/tmp/bugs")
        self.assertEqual(called_kwargs["num_workers"], 2)
        self.assertEqual(called_kwargs["iterations"], 9)
        self.assertEqual(called_kwargs["modulo"], 3)
        self.assertEqual(called_kwargs["time_remaining"], 120)
        self.assertEqual(called_kwargs["job_start_time"], 123.4)
        self.assertEqual(called_kwargs["stop_buffer_minutes"], 7)
        self.assertEqual(called_kwargs["targets"], ["z3", "cvc5"])
        self.assertEqual(called_kwargs["job_id"], "job-7")
        self.assertTrue(called_kwargs["strict_mode"])
        self.assertEqual(called_kwargs["harness"], TYPEFUZZ_HARNESS_TEMPLATE)
        runner_instance.run.assert_called_once()

    def test_orchestrator_persists_bug_files_end_to_end(self) -> None:
        tests_root, test_path = self._seed_test_tree()
        bugs_folder = Path(self._tmpdir.name) / "bugs"
        harness_script = textwrap.dedent(
            """
            import sys
            from pathlib import Path

            bugs_dir = Path(sys.argv[1])
            target_clis = sys.argv[2]
            test_path = sys.argv[3]

            bugs_dir.mkdir(parents=True, exist_ok=True)
            (bugs_dir / "bug.smt2").write_text(
                f"{target_clis}\\n{test_path}\\n(assert false)\\n",
                encoding="utf-8",
            )
            sys.exit(10)
            """
        ).strip()

        runner = CommitHarnessRunner(
            tests=["seed.smt2"],
            tests_root=str(tests_root),
            bugs_folder=str(bugs_folder),
            num_workers=1,
            iterations=4,
            modulo=2,
            targets=build_z3_cvc5_targets("z3", "cvc5"),
            harness=[
                "python3",
                "-c",
                harness_script,
                "{bugs_dir}",
                "{target_clis}",
                "{test_path}",
            ],
            strict_mode=True,
        )

        exit_code = runner.run()

        self.assertEqual(exit_code, 10)
        self.assertTrue((bugs_folder / "bug.smt2").exists())
        self.assertEqual(
            (bugs_folder / "bug.smt2").read_text(encoding="utf-8"),
            "z3 smt.threads=1 memory_max_size=2048 model_validate=true;"
            "cvc5 --check-models --check-proofs --strings-exp\n"
            f"{test_path}\n"
            "(assert false)\n",
        )

    def test_workflows_and_registry_reference_the_new_orchestrator(self) -> None:
        z3_workflow = (REPO_ROOT / ".github/workflows/z3-commit-fuzzer.yml").read_text(
            encoding="utf-8"
        )
        cvc5_workflow = (
            REPO_ROOT / ".github/workflows/cvc5-commit-fuzzer.yml"
        ).read_text(encoding="utf-8")
        opensmt_workflow = (
            REPO_ROOT / ".github/workflows/opensmt-commit-fuzzer.yml"
        ).read_text(encoding="utf-8")
        s3_state = (REPO_ROOT / "scripts/scheduling/s3_state.py").read_text(
            encoding="utf-8"
        )
        commit_selection = (
            REPO_ROOT / "scripts/evaluation/rq2/commit_selection.py"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "scripts/z3/commit_fuzzer/run_commit_fuzzer.py",
            z3_workflow,
        )
        self.assertIn(
            "scripts/cvc5/commit_fuzzer/run_commit_fuzzer.py",
            cvc5_workflow,
        )
        self.assertIn(
            "scripts/opensmt/commit_fuzzer/run_commit_fuzzer.py",
            opensmt_workflow,
        )
        self.assertNotIn("simple_commit_fuzzer.py", z3_workflow)
        self.assertNotIn("simple_commit_fuzzer.py", cvc5_workflow)
        self.assertIn('SUPPORTED_SOLVERS = ("z3", "cvc5", "opensmt")', s3_state)
        self.assertIn(
            "from scheduling.s3_state import SUPPORTED_SOLVERS",
            commit_selection,
        )
        self.assertIn("choices=SUPPORTED_SOLVERS", commit_selection)


if __name__ == "__main__":
    unittest.main()
