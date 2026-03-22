from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.commit_harness_runner import build_cvc5_opensmt_targets
from scripts.local_commit_fuzzer_matrix import discover_opensmt_tests
from scripts.opensmt.commit_fuzzer import run_commit_fuzzer


class CommitHarnessTests(unittest.TestCase):
    def _write_executable(self, path: Path, content: str) -> None:
        path.write_text(textwrap.dedent(content), encoding="utf-8")
        path.chmod(0o755)

    def test_build_cvc5_opensmt_targets(self) -> None:
        self.assertEqual(
            build_cvc5_opensmt_targets("cvc5", "opensmt"),
            [
                "cvc5 --check-models --check-proofs --strings-exp",
                "opensmt",
            ],
        )

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
                bin_dir / "opensmt",
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
                "--iterations",
                "1",
                "--modulo",
                "1",
                "--bugs-folder",
                "bugs",
                "--opensmt-path",
                "opensmt",
                "--cvc5-path",
                "cvc5",
            ]

            with patch.dict(os.environ, {"PATH": path_env}, clear=False), patch.object(sys, "argv", argv):
                exit_code = run_commit_fuzzer.main()

            self.assertEqual(exit_code, 0)
            self.assertTrue((workdir / "bugs" / "open-smt-bug.smt2").exists())


if __name__ == "__main__":
    unittest.main()
