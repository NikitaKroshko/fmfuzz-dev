from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import subprocess

from scripts.local_commit_fuzzer_matrix import discover_opensmt_tests
from scripts.coverage.count_tests import count_opensmt_tests
from scripts.opensmt.coverage.generate_matrix import generate_matrix


class OpenSMTCoverageTests(unittest.TestCase):
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
