from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
import unittest
import textwrap
from pathlib import Path
import subprocess
from unittest.mock import patch

from scripts.local_commit_fuzzer_matrix import discover_opensmt_tests
from scripts.coverage.count_tests import count_opensmt_tests
from scripts.opensmt.coverage.generate_matrix import generate_matrix


class OpenSMTCoverageTests(unittest.TestCase):
    def _load_module(self, module_name: str, module_path: Path):
        module_dir = str(module_path.parent)
        original_sys_path = list(sys.path)
        original_coverage_mapper = sys.modules.get("coverage_mapper")
        original_psutil = sys.modules.get("psutil")
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), original_sys_path))
        self.addCleanup(
            lambda: (
                sys.modules.__setitem__("coverage_mapper", original_coverage_mapper)
                if original_coverage_mapper is not None
                else sys.modules.pop("coverage_mapper", None)
            )
        )
        self.addCleanup(
            lambda: (
                sys.modules.__setitem__("psutil", original_psutil)
                if original_psutil is not None
                else sys.modules.pop("psutil", None)
            )
        )
        sys.path.insert(0, module_dir)
        sys.modules.pop("coverage_mapper", None)
        sys.modules["psutil"] = types.SimpleNamespace(Process=lambda: None)

        spec = importlib.util.spec_from_file_location(module_name, module_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(module_name, None)
        return module

    def test_generate_matrix_preserves_order_and_avoids_empty_shards(self) -> None:
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

            matrix = generate_matrix(opensmt_dir=str(repo_root))
            self.assertEqual(matrix["total_tests"], 4)
            self.assertEqual(matrix["total_jobs"], 4)
            self.assertEqual(
                matrix["matrix"]["include"],
                [
                    {"job_name": "opensmt-part1", "start_index": 1, "end_index": 1},
                    {"job_name": "opensmt-part2", "start_index": 2, "end_index": 2},
                    {"job_name": "opensmt-part3", "start_index": 3, "end_index": 3},
                    {"job_name": "opensmt-part4", "start_index": 4, "end_index": 4},
                ],
            )

    def test_generate_matrix_returns_empty_matrix_for_empty_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "opensmt"
            (repo_root / "test" / "regression").mkdir(parents=True)

            matrix = generate_matrix(opensmt_dir=str(repo_root))
            self.assertEqual(matrix, {"matrix": {"include": []}, "total_tests": 0, "total_jobs": 0})

    def test_generate_matrix_reports_actual_job_count_for_non_even_corpora(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "opensmt"
            seeds_root = repo_root / "test" / "regression"

            (seeds_root / "family_a").mkdir(parents=True)
            (seeds_root / "family_b").mkdir(parents=True)
            (seeds_root / "family_c").mkdir(parents=True)

            for name in ["a.smt2", "b.smt2", "c.smt2", "d.smt2", "e.smt2"]:
                target_dir = seeds_root / f"family_{name[0]}"
                target_dir.mkdir(parents=True, exist_ok=True)
                (target_dir / name).write_text("(check-sat)\n", encoding="utf-8")

            matrix = generate_matrix(opensmt_dir=str(repo_root))

            self.assertEqual(matrix["total_tests"], 5)
            self.assertEqual(matrix["total_jobs"], len(matrix["matrix"]["include"]))
            self.assertEqual(matrix["total_jobs"], 3)
            for shard in matrix["matrix"]["include"]:
                self.assertLessEqual(shard["start_index"], shard["end_index"])
                self.assertGreaterEqual(shard["start_index"], 1)
                self.assertLessEqual(shard["end_index"], matrix["total_tests"])

    def test_coverage_mapper_writes_function_to_test_mapping_end_to_end(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            opensmt_root = tmp_path / "opensmt"
            build_dir = opensmt_root / "build"
            tests_root = opensmt_root / "test" / "regression"
            bin_dir = build_dir / "bin"
            output_file = build_dir / "coverage_mapping.json"

            tests_root.mkdir(parents=True)
            bin_dir.mkdir(parents=True)

            (tests_root / "alpha.smt2").write_text("(check-sat)\n", encoding="utf-8")
            (tests_root / "nested").mkdir(parents=True)
            (tests_root / "nested" / "beta.smt").write_text("(check-sat)\n", encoding="utf-8")

            opensmt_binary = bin_dir / "opensmt"
            opensmt_binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            opensmt_binary.chmod(0o755)

            module = self._load_module(
                "test_opensmt_coverage_mapper",
                repo_root / "scripts" / "opensmt" / "coverage" / "coverage_mapper.py",
            )
            mapper = module.CoverageMapper(
                build_dir=str(build_dir),
                opensmt_dir=str(opensmt_root),
                opensmt_path=str(opensmt_binary),
            )

            tests = mapper.get_opensmt_tests()
            self.assertEqual(
                tests,
                [
                    (1, "alpha.smt2"),
                    (2, "nested/beta.smt"),
                ],
            )

            def fake_coverage(test_name: str) -> dict:
                return {
                    "test_name": test_name,
                    "functions": [f"src/{Path(test_name).stem}.cpp:run():1"],
                }

            with (
                patch.object(mapper, "reset_coverage_counters"),
                patch.object(
                    mapper,
                    "_run_solver",
                    return_value=subprocess.CompletedProcess(args=["opensmt"], returncode=0, stdout="", stderr=""),
                ),
                patch.object(mapper, "extract_coverage_data", side_effect=fake_coverage),
            ):
                written_path = mapper.process_tests(tests, output_file)

            self.assertEqual(written_path, str(output_file))
            mapping = json.loads(output_file.read_text(encoding="utf-8"))
            self.assertEqual(
                mapping,
                {
                    "src/alpha.cpp:run():1": ["alpha.smt2"],
                    "src/beta.cpp:run():1": ["nested/beta.smt"],
                },
            )

    def test_prepare_commit_fuzzer_auto_detects_compile_commands(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            opensmt_root = tmp_path / "opensmt"
            build_dir = opensmt_root / "build"
            build_dir.mkdir(parents=True)
            (build_dir / "compile_commands.json").write_text("[]\n", encoding="utf-8")

            coverage_json = tmp_path / "coverage.json"
            coverage_json.write_text(
                json.dumps({"src/example.cpp:run()": ["test-a", "test-b"]}),
                encoding="utf-8",
            )
            matrix_path = tmp_path / "matrix.json"

            module = self._load_module(
                "test_opensmt_prepare_commit_fuzzer",
                repo_root / "scripts" / "opensmt" / "commit_fuzzer" / "prepare_commit_fuzzer.py",
            )

            with patch.object(module, "PrepareCommitAnalyzer") as analyzer_cls:
                analyzer = analyzer_cls.return_value
                analyzer.analyze_commit_coverage.return_value = {
                    "covering_tests": ["test-a", "test-b"],
                    "summary": {
                        "total_functions": 1,
                        "functions_with_tests": 1,
                        "functions_without_tests": 0,
                        "total_covering_tests": 2,
                        "coverage_percentage": 100.0,
                        "fallback_to_all_tests": False,
                    },
                }

                argv = [
                    "prepare_commit_fuzzer.py",
                    "deadbeef",
                    "--coverage-json",
                    str(coverage_json),
                    "--opensmt-dir",
                    str(opensmt_root),
                    "--output-matrix",
                    str(matrix_path),
                ]

                with patch.object(sys, "argv", argv):
                    exit_code = module.main()

            self.assertEqual(exit_code, 0)
            analyzer_cls.assert_called_once_with(
                ".",
                opensmt_dir=str(opensmt_root),
                compile_commands=str(build_dir / "compile_commands.json"),
            )

            matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
            self.assertEqual(matrix["total_tests"], 2)
            self.assertEqual(matrix["total_jobs"], 2)
            self.assertEqual(
                matrix["matrix"]["include"],
                [
                    {"job_id": 1, "job_name": "opensmt-job-1", "tests": ["test-a"]},
                    {"job_id": 2, "job_name": "opensmt-job-2", "tests": ["test-b"]},
                ],
            )

    def test_opensmt_regression_runner_executes_tests_in_discovery_order(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            opensmt_root = tmp_path / "opensmt"
            tests_root = opensmt_root / "test" / "regression"
            build_dir = opensmt_root / "build"
            bin_dir = build_dir / "bin"
            log_file = tmp_path / "invocations.txt"

            (tests_root / "nested").mkdir(parents=True)
            tests_root.mkdir(parents=True, exist_ok=True)
            (tests_root / "alpha.smt2").write_text("(check-sat)\n", encoding="utf-8")
            (tests_root / "nested" / "beta.smt").write_text("(check-sat)\n", encoding="utf-8")
            bin_dir.mkdir(parents=True)

            opensmt_binary = bin_dir / "opensmt"
            opensmt_binary.write_text(
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env python3
                    import sys
                    from pathlib import Path

                    log_path = Path(r"{log_file}")
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    with log_path.open("a", encoding="utf-8") as handle:
                        handle.write(sys.argv[-1] + "\\n")
                    sys.exit(0)
                    """
                ),
                encoding="utf-8",
            )
            opensmt_binary.chmod(0o755)

            module = self._load_module(
                "test_opensmt_regression_runner",
                repo_root / "scripts" / "opensmt" / "run_regression_tests.py",
            )
            runner = module.OpenSMTRegressionRunner(
                build_dir=str(build_dir),
                opensmt_dir=str(opensmt_root),
                opensmt_path=str(opensmt_binary),
            )

            exit_code = runner.run()

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                log_file.read_text(encoding="utf-8").splitlines(),
                [
                    str((tests_root / "alpha.smt2").resolve()),
                    str((tests_root / "nested" / "beta.smt").resolve()),
                ],
            )

    def test_z3_generate_matrix_caps_job_count_and_avoids_empty_shards(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        module = self._load_module(
            "test_z3_generate_matrix",
            repo_root / "scripts" / "z3" / "coverage" / "generate_matrix.py",
        )

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(module, "CoverageMapper") as mapper_cls,
            patch.object(module, "filter_tests", side_effect=lambda tests, _z3dir: tests),
        ):
            mapper_cls.return_value.get_smt2_tests.return_value = [
                (1, "regressions/smt2/a.smt2"),
                (2, "regressions/smt2/b.smt2"),
                (3, "regressions/smt2/c.smt2"),
            ]
            matrix = module.generate_matrix(z3test_dir=tmp)

        self.assertEqual(matrix["total_tests"], 3)
        self.assertEqual(matrix["total_jobs"], 3)
        self.assertEqual(
            matrix["matrix"]["include"],
            [
                {"job_name": "z3-part1", "start_index": 1, "end_index": 1},
                {"job_name": "z3-part2", "start_index": 2, "end_index": 2},
                {"job_name": "z3-part3", "start_index": 3, "end_index": 3},
            ],
        )

    def test_cvc5_generate_matrix_caps_job_count_and_handles_tiny_time_budget(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        module = self._load_module(
            "test_cvc5_generate_matrix",
            repo_root / "scripts" / "cvc5" / "coverage" / "generate_matrix.py",
        )

        total_jobs, tests_per_job = module.calculate_jobs(
            total_tests=2,
            target_jobs=6,
            max_job_time_minutes=11,
            buffer_minutes=10,
            avg_test_time_seconds=120.0,
        )
        self.assertEqual((total_jobs, tests_per_job), (2, 1))

        with patch.object(module, "CoverageMapper") as mapper_cls:
            mapper_cls.return_value.get_ctest_tests.return_value = [
                (1, "suite/a"),
                (2, "suite/b"),
                (3, "suite/c"),
            ]
            matrix = module.generate_matrix(build_dir="ignored")

        self.assertEqual(matrix["total_tests"], 3)
        self.assertEqual(matrix["total_jobs"], 3)
        self.assertEqual(
            matrix["matrix"]["include"],
            [
                {"job_name": "regress0a", "start_index": 1, "end_index": 1},
                {"job_name": "regress0b", "start_index": 2, "end_index": 2},
                {"job_name": "regress0c", "start_index": 3, "end_index": 3},
            ],
        )

    def test_count_tests_reports_count_and_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "opensmt"
            seeds_root = repo_root / "test" / "regression"
            seeds_root.mkdir(parents=True)
            (seeds_root / "seed.smt2").write_text("(check-sat)\n", encoding="utf-8")

            subprocess.run(["git", "init"], cwd=repo_root, capture_output=True, text=True, check=True)
            subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=repo_root, check=True)
            subprocess.run(["git", "config", "user.name", "Tests"], cwd=repo_root, check=True)
            subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=repo_root, capture_output=True, text=True, check=True)

            result = count_opensmt_tests(repo_root)
            head_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

            self.assertEqual(result["test_count"], 1)
            self.assertEqual(result["commit_hash"], head_commit)

    def test_collect_build_artifacts_includes_installed_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_dir = tmp_path / "opensmt" / "build"
            install_prefix = tmp_path / "install"
            output_dir = tmp_path / "artifacts"

            build_header_dir = build_dir / "include" / "opensmt"
            install_header_dir = install_prefix / "include" / "opensmt"
            build_header_dir.mkdir(parents=True)
            install_header_dir.mkdir(parents=True)
            (build_header_dir / "build_only.h").write_text("// build\n", encoding="utf-8")
            (install_header_dir / "install_only.h").write_text("// install\n", encoding="utf-8")

            bin_dir = build_dir / "bin"
            bin_dir.mkdir(parents=True)
            binary = bin_dir / "opensmt"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)

            (build_dir / "compile_commands.json").write_text("[]\n", encoding="utf-8")
            (build_dir / "CMakeCache.txt").write_text(
                f"CMAKE_INSTALL_PREFIX:PATH={install_prefix}\n",
                encoding="utf-8",
            )

            script = Path(__file__).resolve().parents[1] / "scripts" / "opensmt" / "collect_build_artifacts.sh"
            subprocess.run(
                ["bash", str(script), str(build_dir), str(output_dir)],
                capture_output=True,
                text=True,
                check=True,
            )

            self.assertTrue((output_dir / "headers" / "include" / "opensmt" / "build_only.h").exists())
            self.assertTrue((output_dir / "headers" / "include" / "opensmt" / "install_only.h").exists())
            self.assertTrue((output_dir / "bin" / "opensmt").exists())
            self.assertTrue((output_dir / "compile_commands.json").exists())


if __name__ == "__main__":
    unittest.main()
