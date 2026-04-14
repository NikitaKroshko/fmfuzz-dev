#!/usr/bin/env python3
"""Prepare OpenSMT commit-fuzzer jobs from real commit coverage analysis."""

from __future__ import annotations

import argparse
import gzip
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

try:
    import clang.cindex as clang_cindex
except Exception:
    clang_cindex = None

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.diff_utils import FileDiff, parse_unified_diff  # noqa: E402
from scripts.local_commit_fuzzer_matrix import build_jobs, discover_opensmt_tests  # noqa: E402


CONTROL_PREFIX_RE = re.compile(
    r"^(?:if|for|while|switch|catch|return|case|break|continue|goto|do)\b"
)
FUNCTION_NAME_RE = re.compile(
    r"(operator\s*(?:\[\]|\(\)|<<|>>|->\*|->|new(?:\[\])?|delete(?:\[\])?|[^\s(]+)|[~A-Za-z_][\w:~<>]*)$"
)


@dataclass(frozen=True)
class FunctionInfo:
    signature: str
    start: int
    end: int
    file: str


class GitHelper:
    def __init__(self, repo_path: Path):
        self.repo_path = repo_path

    def get_commit_info(self, commit_hash: str) -> Optional[Dict[str, str]]:
        result = subprocess.run(
            [
                "git",
                "show",
                "-s",
                "--format=%H%x00%an%x00%ae%x00%ad%x00%s%x00%B",
                commit_hash,
            ],
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            print(f"Error getting commit info for {commit_hash}: {result.stderr.strip()}")
            return None

        parts = result.stdout.split("\x00", 5)
        if len(parts) < 6:
            return None

        return {
            "hash": parts[0].strip(),
            "author_name": parts[1].strip(),
            "author_email": parts[2].strip(),
            "date": parts[3].strip(),
            "message": parts[5].strip(),
            "summary": parts[4].strip(),
        }

    def get_commit_diff(self, commit_hash: str) -> str:
        result = subprocess.run(
            ["git", "show", "-U0", "--no-color", "--no-ext-diff", commit_hash],
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            print(f"Error getting commit diff for {commit_hash}: {result.stderr.strip()}")
            return ""
        return result.stdout

    def get_changed_files(self, diff_text: str) -> Dict[str, FileDiff]:
        return parse_unified_diff(diff_text)

    def get_file_text_at_commit(self, rev: Optional[str], path: str) -> Optional[str]:
        if not rev or not path or path == "/dev/null":
            return None

        result = subprocess.run(
            ["git", "show", f"{rev}:{path}"],
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout


class Matcher:
    def __init__(self, coverage_map: Dict[str, Sequence[str]]):
        self.by_path_sig_line: Dict[Tuple[str, str, int], Set[str]] = {}
        self.by_path_sig: Dict[Tuple[str, str], Set[str]] = {}
        self.by_sig: Dict[str, Set[str]] = {}
        self.sig_examples: Dict[str, List[str]] = {}

        for raw_key, tests in coverage_map.items():
            path, signature, line = self._split_key(raw_key)
            normalized_signature = self.normalize_signature(signature)
            test_set = set(tests if isinstance(tests, (list, set, tuple)) else [tests])

            self.by_path_sig_line.setdefault((path, normalized_signature, line), set()).update(test_set)
            self.by_path_sig.setdefault((path, normalized_signature), set()).update(test_set)
            self.by_sig.setdefault(normalized_signature, set()).update(test_set)
            self.sig_examples.setdefault(normalized_signature, []).append(raw_key)

    @staticmethod
    def _strip_line_suffix(value: str) -> Tuple[str, int]:
        if ":" not in value:
            return value, -1
        base, last = value.rsplit(":", 1)
        if last.isdigit():
            return base, int(last)
        return value, -1

    def _split_key(self, key: str) -> Tuple[str, str, int]:
        without_line, line = self._strip_line_suffix(key)
        if ":" in without_line:
            path, signature = without_line.split(":", 1)
        else:
            path, signature = "", without_line
        return path, signature, line

    @staticmethod
    def _split_top_level_params(params: str) -> List[str]:
        chunks: List[str] = []
        buffer: List[str] = []
        angle_depth = 0
        paren_depth = 0
        bracket_depth = 0

        for char in params:
            if char == "<":
                angle_depth += 1
            elif char == ">":
                angle_depth = max(0, angle_depth - 1)
            elif char == "(":
                paren_depth += 1
            elif char == ")":
                paren_depth = max(0, paren_depth - 1)
            elif char == "[":
                bracket_depth += 1
            elif char == "]":
                bracket_depth = max(0, bracket_depth - 1)

            if char == "," and angle_depth == 0 and paren_depth == 0 and bracket_depth == 0:
                chunk = "".join(buffer).strip()
                if chunk:
                    chunks.append(chunk)
                buffer = []
            else:
                buffer.append(char)

        if buffer:
            chunk = "".join(buffer).strip()
            if chunk:
                chunks.append(chunk)

        return chunks

    @classmethod
    def _normalize_param(cls, param: str) -> str:
        param = re.sub(r"\s+", " ", param).strip()
        if not param:
            return param
        if param == "...":
            return param

        if "=" in param:
            param = param.split("=", 1)[0].strip()

        # Remove trailing parameter names while keeping the type portion intact.
        param = re.sub(r"(\b[\w:<>*&\s]+?)\s+([A-Za-z_][A-Za-z0-9_]*)$", r"\1", param)

        leading_const = param.startswith("const ")
        if leading_const:
            param = param[len("const "):].strip()

        match = re.match(r"^(.*?)(\s*[&*]+)$", param)
        if match:
            base = match.group(1).strip()
            suffix = match.group(2).replace(" ", "")
        else:
            base = param
            suffix = ""

        if leading_const:
            param = f"{base} const{suffix}"
        else:
            param = f"{base}{suffix}"

        param = re.sub(r"\s*::\s*", "::", param)
        param = re.sub(r"\s+([&*])", r"\1", param)
        param = re.sub(r"<\s*", "<", param)
        param = param.replace(">>", "> >")
        return param.strip()

    def normalize_signature(self, signature: str) -> str:
        try:
            head = signature.strip()

            # Keep any trailing :line suffix intact if one is present.
            line_part = ""
            if ":" in head:
                base, last = head.rsplit(":", 1)
                if last.isdigit():
                    head = base
                    line_part = f":{last}"

            head = re.sub(r"\[abi:[^\]]+\]", "", head)
            head = re.sub(r"\s*::\s*", "::", head)

            open_idx = head.find("(")
            if open_idx == -1:
                return re.sub(r"\s+", " ", head).strip() + line_part

            depth = 0
            close_idx = -1
            for index in range(open_idx, len(head)):
                char = head[index]
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        close_idx = index
                        break
            if close_idx == -1:
                return re.sub(r"\s+", " ", head).strip() + line_part

            prefix = head[:open_idx + 1]
            params = head[open_idx + 1 : close_idx]
            suffix = head[close_idx:]

            normalized_params = ", ".join(
                self._normalize_param(param) for param in self._split_top_level_params(params)
            )
            normalized = f"{prefix}{normalized_params}{suffix}"
            normalized = re.sub(r"\s+", " ", normalized)
            normalized = re.sub(r"\s*::\s*", "::", normalized)
            normalized = re.sub(r",\s*", ", ", normalized)
            normalized = re.sub(r"\s+([&*])", r"\1", normalized)
            normalized = normalized.replace(">>", "> >")
            return normalized.strip() + line_part
        except Exception:
            return signature.strip()

    def match(self, functions: Sequence[FunctionInfo]) -> Dict[str, object]:
        all_covering_tests: Set[str] = set()
        function_test_counts: Dict[str, int] = {}
        test_function_counts: Dict[str, int] = {}
        function_matches: Dict[str, Dict[str, object]] = {}
        match_type_counts: Dict[str, int] = {}
        functions_with_tests = 0
        functions_without_tests = 0
        direct_matches = 0
        path_removed_matches = 0
        signature_only_matches = 0

        for function in functions:
            normalized_signature = self.normalize_signature(function.signature)
            normalized_path = Path(function.file).as_posix()
            full_key = f"{normalized_path}:{normalized_signature}:{function.start}"

            matching_tests: Set[str] = set()
            match_type = "none"

            exact_key = (normalized_path, normalized_signature, function.start)
            path_key = (normalized_path, normalized_signature)

            if exact_key in self.by_path_sig_line:
                matching_tests.update(self.by_path_sig_line[exact_key])
                direct_matches += 1
                match_type = "direct"
            elif path_key in self.by_path_sig:
                matching_tests.update(self.by_path_sig[path_key])
                path_removed_matches += 1
                match_type = "path_removed"
            elif normalized_signature in self.by_sig:
                matching_tests.update(self.by_sig[normalized_signature])
                signature_only_matches += 1
                match_type = "signature_only"
            else:
                best_candidate = (None, 0.0)
                for candidate_sig in self.by_sig:
                    ratio = SequenceMatcher(None, normalized_signature, candidate_sig).ratio()
                    if ratio > best_candidate[1]:
                        best_candidate = (candidate_sig, ratio)
                candidate_sig, ratio = best_candidate
                if candidate_sig is not None and ratio >= 0.9:
                    match_type = f"fuzzy_candidate:{ratio:.2f}"
                    examples = self.sig_examples.get(candidate_sig, [])
                    if examples:
                        print(
                            f"DEBUG_FUZZY_MAP our={normalized_signature} matched_sig={candidate_sig} examples={examples}"
                        )

            if matching_tests:
                functions_with_tests += 1
                all_covering_tests.update(matching_tests)
                function_test_counts[full_key] = len(matching_tests)
                for test_name in matching_tests:
                    test_function_counts[test_name] = test_function_counts.get(test_name, 0) + 1
            else:
                functions_without_tests += 1
                function_test_counts[full_key] = 0

            function_matches[full_key] = {
                "tests": sorted(matching_tests),
                "match_type": match_type,
            }
            match_type_counts[match_type] = match_type_counts.get(match_type, 0) + 1

        return {
            "all_covering_tests": all_covering_tests,
            "functions_with_tests": functions_with_tests,
            "functions_without_tests": functions_without_tests,
            "total_tests": len(all_covering_tests),
            "function_test_counts": function_test_counts,
            "test_function_counts": test_function_counts,
            "direct_matches": direct_matches,
            "path_removed_matches": path_removed_matches,
            "signature_only_matches": signature_only_matches,
            "function_matches": function_matches,
            "match_type_counts": match_type_counts,
        }


class PrepareCommitAnalyzer:
    def __init__(
        self,
        repo_path: str = ".",
        opensmt_dir: Optional[str] = None,
        compile_commands: Optional[str] = None,
    ):
        self.repo_path = Path(repo_path)
        self.opensmt_dir = Path(opensmt_dir or repo_path)
        self.git = GitHelper(self.repo_path)
        self.coverage_map: Optional[Dict[str, Sequence[str]]] = None
        self.corpus_order: Optional[List[str]] = None
        self.compdb = None
        self.compdb_dir: Optional[str] = None

        if compile_commands:
            self._init_compilation_database(compile_commands)

    @staticmethod
    def _strip_inline_comment(line: str) -> str:
        return re.sub(r"//.*", "", line)

    @classmethod
    def _is_control_line(cls, stripped: str) -> bool:
        if CONTROL_PREFIX_RE.match(stripped):
            return True
        if stripped.startswith(("class ", "struct ", "enum ", "union ", "namespace ", "using ", "typedef ")):
            return True
        if stripped in {"public:", "private:", "protected:"}:
            return True
        return False

    def _looks_like_function_candidate(self, lines: Sequence[str], start_index: int) -> bool:
        stripped = self._strip_inline_comment(lines[start_index]).strip()
        if not stripped:
            return False
        if stripped.startswith(("#", "//", "/*", "*", "}")):
            return False
        if stripped.startswith("[") or "[](" in stripped or "](" in stripped:
            return False
        if self._is_control_line(stripped):
            return False
        if "(" in stripped:
            return True
        if stripped.startswith("template"):
            return True

        for lookahead in lines[start_index + 1 : start_index + 3]:
            candidate = self._strip_inline_comment(lookahead).strip()
            if not candidate or candidate.startswith(("#", "//", "/*", "*", "}")):
                continue
            if self._is_control_line(candidate):
                continue
            if "(" in candidate:
                return True
        return False

    def _extract_function_signature(self, header_text: str) -> Optional[str]:
        text = header_text.split("{", 1)[0]
        open_idx = text.find("(")
        if open_idx == -1:
            return None

        depth = 0
        close_idx = -1
        for index in range(open_idx, len(text)):
            char = text[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    close_idx = index
                    break
        if close_idx == -1:
            return None

        before = re.sub(r"\s+", " ", text[:open_idx]).strip()
        params = text[open_idx + 1 : close_idx].strip()
        if not before:
            return None

        name_match = FUNCTION_NAME_RE.search(before)
        if name_match:
            name = re.sub(r"\s+", " ", name_match.group(1)).strip()
        else:
            name = before.split()[-1]

        raw_signature = f"{name}({params})"
        return Matcher({}).normalize_signature(raw_signature)

    def _find_function_end(self, lines: Sequence[str], open_brace_line: int, open_brace_col: int) -> Optional[int]:
        depth = 0
        in_block_comment = False
        in_string: Optional[str] = None
        started = False

        for line_index in range(open_brace_line, len(lines)):
            line = lines[line_index]
            col = open_brace_col if line_index == open_brace_line else 0

            while col < len(line):
                char = line[col]
                next_char = line[col + 1] if col + 1 < len(line) else ""

                if in_block_comment:
                    if char == "*" and next_char == "/":
                        in_block_comment = False
                        col += 2
                        continue
                    col += 1
                    continue

                if in_string:
                    if char == "\\":
                        col += 2
                        continue
                    if char == in_string:
                        in_string = None
                    col += 1
                    continue

                if char == "/" and next_char == "/":
                    break
                if char == "/" and next_char == "*":
                    in_block_comment = True
                    col += 2
                    continue
                if char in {'"', "'"}:
                    in_string = char
                    col += 1
                    continue

                if char == "{":
                    depth += 1
                    started = True
                elif char == "}":
                    depth -= 1
                    if started and depth == 0:
                        return line_index

                col += 1

        return None

    def _init_compilation_database(self, compile_commands: str) -> None:
        if clang_cindex is None:
            return

        try:
            cc_path = Path(compile_commands)
            cc_dir = cc_path if cc_path.is_dir() else cc_path.parent
            db = clang_cindex.CompilationDatabase.fromDirectory(str(cc_dir))  # type: ignore[attr-defined]
            _ = db.getAllCompileCommands()  # type: ignore[attr-defined]
            self.compdb = db
            self.compdb_dir = str(cc_dir)
        except Exception:
            self.compdb = None
            self.compdb_dir = None

    def _extract_args_from_compile_command(self, cmd) -> List[str]:
        args: List[str] = []
        try:
            raw = list(getattr(cmd, "arguments", None) or getattr(cmd, "commandLine", []))
            skip_next = False
            src = str(getattr(cmd, "filename", ""))
            for index, value in enumerate(raw):
                if skip_next:
                    skip_next = False
                    continue
                if index == 0:
                    continue
                if value == src or value.endswith(src):
                    continue
                if value in {"-c"}:
                    continue
                if value in {"-o", "/Fo"}:
                    skip_next = True
                    continue
                args.append(value)
        except Exception:
            return []

        if "-x" not in args:
            args = ["-x", "c++"] + args
        return args

    def _clang_resource_dir(self) -> Optional[str]:
        try:
            result = subprocess.run(["clang", "-print-resource-dir"], capture_output=True, text=True, check=False)
            if result.returncode == 0:
                value = result.stdout.strip()
                if value:
                    return value
        except Exception:
            pass
        return None

    def _build_clang_args(self) -> List[str]:
        args = ["-x", "c++", "-std=c++17"]
        for candidate in (
            self.opensmt_dir / "src",
            self.opensmt_dir / "include",
            self.opensmt_dir / "build",
            self.opensmt_dir / "build" / "src",
            self.opensmt_dir / "build" / "include",
        ):
            if candidate.exists():
                args.extend(["-I", str(candidate)])

        resource_dir = self._clang_resource_dir()
        if resource_dir:
            args.extend(["-resource-dir", resource_dir])
        return args

    def _get_clang_args_for_file(self, file_path: str) -> List[str]:
        if clang_cindex is None:
            return self._build_clang_args()

        if self.compdb:
            try:
                abs_path = str((self.repo_path / file_path).resolve()) if not Path(file_path).is_absolute() else str(Path(file_path).resolve())
                compile_commands = self.compdb.getCompileCommands(abs_path)  # type: ignore[attr-defined]
                if compile_commands and len(compile_commands) > 0:
                    args = self._extract_args_from_compile_command(compile_commands[0])
                    resource_dir = self._clang_resource_dir()
                    if resource_dir and "-resource-dir" not in args:
                        args.extend(["-resource-dir", resource_dir])
                    return args
            except Exception:
                pass

        return self._build_clang_args()

    def _demangle_with_cxxfilt(self, mangled_name: Optional[str]) -> Optional[str]:
        if not mangled_name:
            return None

        try:
            result = subprocess.run(["c++filt"], input=str(mangled_name), capture_output=True, text=True, check=False)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            pass
        return None

    def _normalize_source_path(self, source_path: Optional[str]) -> Optional[str]:
        if not source_path:
            return None

        candidate = Path(source_path)
        try:
            resolved = candidate.resolve()
        except Exception:
            resolved = candidate

        for root in (self.repo_path, self.opensmt_dir):
            try:
                return resolved.relative_to(Path(root).resolve()).as_posix()
            except Exception:
                continue

        return candidate.as_posix().lstrip("./")

    def _get_qualified_name(self, cursor) -> str:
        parts: List[str] = []
        current = cursor

        while current is not None:
            try:
                kind = current.kind
            except Exception:
                break

            if kind in {
                getattr(clang_cindex.CursorKind, "NAMESPACE", None),
                getattr(clang_cindex.CursorKind, "CLASS_DECL", None),
                getattr(clang_cindex.CursorKind, "STRUCT_DECL", None),
                getattr(clang_cindex.CursorKind, "UNION_DECL", None),
                getattr(clang_cindex.CursorKind, "FUNCTION_DECL", None),
                getattr(clang_cindex.CursorKind, "CXX_METHOD", None),
                getattr(clang_cindex.CursorKind, "CONSTRUCTOR", None),
                getattr(clang_cindex.CursorKind, "DESTRUCTOR", None),
            }:
                spelling = getattr(current, "spelling", "")
                if spelling and spelling not in parts:
                    parts.append(spelling)
            current = getattr(current, "semantic_parent", None)

        parts.reverse()
        return "::".join(parts)

    def _get_function_signature(self, cursor) -> Optional[str]:
        try:
            line = getattr(cursor.location, "line", 0)
            mangled_name = getattr(cursor, "mangled_name", None)
            demangled = self._demangle_with_cxxfilt(mangled_name)
            if demangled:
                return Matcher({}).normalize_signature(f"{demangled}:{line}")

            display_name = getattr(cursor, "displayname", "") or ""
            if display_name:
                return Matcher({}).normalize_signature(f"{display_name}:{line}")

            qualified_name = self._get_qualified_name(cursor)
            if not qualified_name:
                return None

            params: List[str] = []
            for child in cursor.get_children():
                if clang_cindex is not None and child.kind == clang_cindex.CursorKind.PARM_DECL:
                    type_spelling = ""
                    try:
                        type_spelling = child.type.spelling or child.type.get_canonical().spelling
                    except Exception:
                        type_spelling = ""
                    params.append(type_spelling)

            signature = f"{qualified_name}({', '.join(param for param in params if param)})"
            return Matcher({}).normalize_signature(f"{signature}:{line}")
        except Exception:
            return None

    def _parse_functions_with_clang(self, file_path: str, source_text: Optional[str]) -> List[FunctionInfo]:
        if source_text is None or clang_cindex is None:
            return []

        try:
            index = clang_cindex.Index.create()  # type: ignore[attr-defined]
            args = self._get_clang_args_for_file(file_path)
            abs_path = str((self.repo_path / file_path).resolve()) if not Path(file_path).is_absolute() else str(Path(file_path).resolve())
            translation_unit = index.parse(abs_path, args=args, unsaved_files=[(abs_path, source_text)])

            functions: List[FunctionInfo] = []
            seen: Set[Tuple[str, str, int, int]] = set()
            function_kinds = {
                getattr(clang_cindex.CursorKind, "FUNCTION_DECL", None),
                getattr(clang_cindex.CursorKind, "CXX_METHOD", None),
                getattr(clang_cindex.CursorKind, "CONSTRUCTOR", None),
                getattr(clang_cindex.CursorKind, "DESTRUCTOR", None),
                getattr(clang_cindex.CursorKind, "FUNCTION_TEMPLATE", None),
            }

            def visit(node) -> None:
                try:
                    kind = node.kind
                except Exception:
                    return

                if kind in function_kinds:
                    try:
                        is_definition = node.is_definition()
                    except Exception:
                        is_definition = False

                    if is_definition:
                        signature = self._get_function_signature(node)
                        node_file = getattr(getattr(node, "location", None), "file", None)
                        normalized_file = self._normalize_source_path(str(node_file) if node_file else None)
                        if signature and normalized_file and normalized_file.startswith("src/"):
                            key = (normalized_file, signature, node.extent.start.line, node.extent.end.line)
                            if key not in seen:
                                seen.add(key)
                                functions.append(
                                    FunctionInfo(
                                        signature=signature,
                                        start=node.extent.start.line,
                                        end=node.extent.end.line,
                                        file=normalized_file,
                                    )
                                )

                try:
                    for child in node.get_children():
                        visit(child)
                except Exception:
                    return

            visit(translation_unit.cursor)
            return functions
        except Exception:
            return []

    def parse_functions_from_text(self, file_path: str, source_text: Optional[str]) -> List[FunctionInfo]:
        if source_text is None:
            return []

        clang_functions = self._parse_functions_with_clang(file_path, source_text)
        if clang_functions:
            return clang_functions

        lines = source_text.splitlines()
        functions: List[FunctionInfo] = []
        max_header_lines = 8
        index = 0

        while index < len(lines):
            if not self._looks_like_function_candidate(lines, index):
                index += 1
                continue

            header_lines = [lines[index]]
            brace_line: Optional[int] = None
            brace_col = -1
            saw_open_paren = "(" in self._strip_inline_comment(lines[index])

            for lookahead in range(index, min(len(lines), index + max_header_lines)):
                current = lines[lookahead]
                if lookahead != index:
                    header_lines.append(current)

                cleaned = self._strip_inline_comment(current)
                if "(" in cleaned:
                    saw_open_paren = True
                if "{" in cleaned and saw_open_paren:
                    brace_line = lookahead
                    brace_col = cleaned.index("{")
                    break
                if saw_open_paren and ";" in cleaned and "{" not in cleaned:
                    brace_line = None
                    break

            if brace_line is None:
                index += 1
                continue

            header_text = "\n".join(header_lines)
            signature = self._extract_function_signature(header_text)
            if not signature:
                index += 1
                continue

            end_line = self._find_function_end(lines, brace_line, brace_col)
            if end_line is None:
                index += 1
                continue

            functions.append(
                FunctionInfo(
                    signature=signature,
                    start=index + 1,
                    end=end_line + 1,
                    file=Path(file_path).as_posix(),
                )
            )
            index = end_line + 1

        return functions

    @staticmethod
    def _normalize_code(code: str) -> str:
        code = re.sub(r"//.*", "", code)
        code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
        code = re.sub(r"\s+", " ", code).strip()
        return code

    @staticmethod
    def _canonicalize_path(path: str) -> str:
        return Path(path).as_posix().lstrip("./")

    def _normalize_params_for_body_compare(self, function: FunctionInfo, source_text: str) -> str:
        lines = source_text.splitlines()
        start = max(0, function.start - 1)
        end = min(len(lines), function.end)
        return self._normalize_code("\n".join(lines[start:end]))

    def get_commit_functions(self, commit_hash: str) -> Tuple[List[FunctionInfo], List[str]]:
        commit_info = self.git.get_commit_info(commit_hash)
        if not commit_info:
            raise ValueError(f"Unknown commit: {commit_hash}")

        diff_text = self.git.get_commit_diff(commit_hash)
        changed_files = self.git.get_changed_files(diff_text)

        parent_hash: Optional[str] = None
        result = subprocess.run(
            ["git", "rev-list", "--parents", "-n", "1", commit_hash],
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            parts = result.stdout.strip().split()
            if len(parts) >= 2:
                parent_hash = parts[1]

        changed_functions: List[FunctionInfo] = []
        files_with_no_functions: List[str] = []

        for file_path, file_diff in changed_files.items():
            canonical_path = Path(file_path).as_posix()
            if not canonical_path.startswith("src/"):
                continue
            if not canonical_path.endswith((".cpp", ".cc", ".cxx", ".c", ".h", ".hh", ".hpp", ".hxx", ".ipp")):
                continue

            after_path = file_diff.new_path if file_diff.new_path != "/dev/null" else None
            before_path = file_diff.old_path if file_diff.old_path != "/dev/null" else None

            after_src = self.git.get_file_text_at_commit(commit_hash, after_path or canonical_path)
            before_src = self.git.get_file_text_at_commit(parent_hash, before_path or canonical_path)

            after_funcs = self.parse_functions_from_text(after_path or canonical_path, after_src)
            before_funcs = self.parse_functions_from_text(before_path or canonical_path, before_src)

            selected: Dict[Tuple[str, str], FunctionInfo] = {}
            after_by_key = {(func.file, func.signature): func for func in after_funcs}
            before_by_key = {(func.file, func.signature): func for func in before_funcs}

            if after_src is not None:
                for func in after_funcs:
                    if file_diff.overlaps_after(func.start, func.end):
                        selected[(func.file, func.signature)] = func

            if before_src is not None:
                for func in before_funcs:
                    key = (func.file, func.signature)
                    if key in selected:
                        continue
                    if file_diff.overlaps_before(func.start, func.end):
                        selected[key] = func

            if not selected and canonical_path.endswith((".cpp", ".cc", ".cxx", ".c", ".h", ".hh", ".hpp", ".hxx", ".ipp")):
                files_with_no_functions.append(canonical_path)

            for key, func in selected.items():
                after_func = after_by_key.get(key)
                before_func = before_by_key.get(key)
                if after_src is not None and before_src is not None and after_func and before_func:
                    if self._normalize_code(self._normalize_params_for_body_compare(after_func, after_src)) == self._normalize_code(
                        self._normalize_params_for_body_compare(before_func, before_src)
                    ):
                        continue
                changed_functions.append(func)

        changed_functions.sort(key=lambda func: (func.file, func.start, func.signature))
        files_with_no_functions.sort()
        return (changed_functions, files_with_no_functions)

    def _resolve_coverage_path(self, coverage_json_path: str) -> Path:
        path = Path(coverage_json_path)
        if path.exists():
            return path

        if path.suffix == ".gz":
            return path

        gz_candidate = Path(f"{coverage_json_path}.gz")
        if gz_candidate.exists():
            return gz_candidate
        return path

    def load_coverage_mapping(self, coverage_json_path: str) -> None:
        path = self._resolve_coverage_path(coverage_json_path)
        if not path.exists():
            raise FileNotFoundError(f"Coverage JSON file not found: {coverage_json_path}")

        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                self.coverage_map = json.load(handle)
        else:
            with path.open("r", encoding="utf-8") as handle:
                self.coverage_map = json.load(handle)

    def get_all_tests_from_coverage(self) -> Set[str]:
        if not self.coverage_map:
            return set()

        tests: Set[str] = set()
        for value in self.coverage_map.values():
            if isinstance(value, str):
                tests.add(value)
            elif isinstance(value, (list, tuple, set)):
                tests.update(str(item) for item in value)
        return tests

    def cleanup_coverage_mapping(self) -> None:
        self.coverage_map = None

    def _corpus_order_map(self) -> Dict[str, int]:
        if self.corpus_order is None:
            self.corpus_order = discover_opensmt_tests(str(self.opensmt_dir))
        return {test_name: index for index, test_name in enumerate(self.corpus_order)}

    def _order_tests(self, tests: Sequence[str]) -> List[str]:
        order_map = self._corpus_order_map()
        normalized = {Path(test).as_posix() for test in tests}
        return sorted(
            normalized,
            key=lambda test: (order_map.get(test, len(order_map)), test),
        )

    def find_tests_for_functions(self, functions: Sequence[FunctionInfo]) -> Dict[str, object]:
        if not self.coverage_map:
            return {
                "all_covering_tests": set(),
                "functions_with_tests": 0,
                "functions_without_tests": 0,
                "total_tests": 0,
                "function_test_counts": {},
                "test_function_counts": {},
                "direct_matches": 0,
                "path_removed_matches": 0,
                "signature_only_matches": 0,
                "function_matches": {},
                "match_type_counts": {},
            }

        matcher = Matcher(self.coverage_map)
        return matcher.match(functions)

    def analyze_commit_coverage(self, commit_hash: str, coverage_json_path: str) -> Dict[str, object]:
        print(f"Analyzing commit {commit_hash}...")

        changed_functions, files_with_no_functions = self.get_commit_functions(commit_hash)
        self.load_coverage_mapping(coverage_json_path)

        try:
            all_tests = self.get_all_tests_from_coverage()
            ordered_all_tests = self._order_tests(all_tests) if all_tests else []

            if not changed_functions:
                if files_with_no_functions:
                    print(
                        f"Warning: {len(files_with_no_functions)} source file(s) changed but no functions were detected:"
                    )
                    for file_name in files_with_no_functions:
                        print(f"  - {file_name}")
                    print("Including all tests from coverage mapping as fallback.")
                    summary = {
                        "total_functions": 0,
                        "functions_with_tests": 0,
                        "functions_without_tests": 0,
                        "total_covering_tests": len(ordered_all_tests),
                        "coverage_percentage": 0.0,
                        "fallback_to_all_tests": True,
                    }
                    return {
                        "commit": commit_hash,
                        "changed_functions": [],
                        "files_with_no_functions": files_with_no_functions,
                        "covering_tests": ordered_all_tests,
                        "function_matches": {},
                        "function_test_counts": {},
                        "test_function_counts": {},
                        "match_type_counts": {},
                        "summary": summary,
                    }

                print("No functions found in commit")
                summary = {
                    "total_functions": 0,
                    "functions_with_tests": 0,
                    "functions_without_tests": 0,
                    "total_covering_tests": 0,
                    "coverage_percentage": 0.0,
                    "fallback_to_all_tests": False,
                }
                return {
                    "commit": commit_hash,
                    "changed_functions": [],
                    "files_with_no_functions": files_with_no_functions,
                    "covering_tests": [],
                    "function_matches": {},
                    "function_test_counts": {},
                    "test_function_counts": {},
                    "match_type_counts": {},
                    "summary": summary,
                }

            test_results = self.find_tests_for_functions(changed_functions)
            ordered_tests = (
                self._order_tests(test_results["all_covering_tests"])
                if test_results["all_covering_tests"]
                else []
            )

            total_functions = len(changed_functions)
            functions_with_tests = int(test_results["functions_with_tests"])
            functions_without_tests = int(test_results["functions_without_tests"])
            total_covering_tests = len(ordered_tests)
            coverage_percentage = (functions_with_tests / total_functions * 100.0) if total_functions else 0.0
            should_fallback = False
            fallback_reason = ""

            if test_results["direct_matches"] == 0 and files_with_no_functions:
                should_fallback = True
                fallback_reason = f"{len(files_with_no_functions)} source file(s) changed but no functions were detected"
            elif functions_with_tests == 0 and changed_functions:
                should_fallback = True
                fallback_reason = f"No tests mapped to {len(changed_functions)} changed function(s) (coverage = 0%)"
            elif test_results["total_tests"] == 0:
                should_fallback = True
                fallback_reason = "No tests found in coverage mapping"

            if should_fallback:
                print(f"Warning: {fallback_reason}")
                if files_with_no_functions:
                    for file_name in files_with_no_functions:
                        print(f"  - {file_name}")
                print(f"No direct matches found ({test_results['direct_matches']} direct matches).")
                print("Including all tests from coverage mapping as fallback.")

                summary = {
                    "total_functions": total_functions,
                    "functions_with_tests": functions_with_tests,
                    "functions_without_tests": functions_without_tests,
                    "total_covering_tests": len(ordered_all_tests),
                    "coverage_percentage": coverage_percentage,
                    "fallback_to_all_tests": True,
                }

                return {
                    "commit": commit_hash,
                    "changed_functions": [f"{function.file}:{function.signature}:{function.start}" for function in changed_functions],
                    "files_with_no_functions": files_with_no_functions,
                    "covering_tests": ordered_all_tests,
                    "function_matches": test_results.get("function_matches", {}),
                    "function_test_counts": test_results.get("function_test_counts", {}),
                    "test_function_counts": test_results.get("test_function_counts", {}),
                    "match_type_counts": test_results.get("match_type_counts", {}),
                    "summary": summary,
                }

            if files_with_no_functions:
                if changed_functions:
                    print(
                        f"Warning: {len(files_with_no_functions)} source file(s) changed but no functions were detected:"
                    )
                else:
                    print(f"Warning: {len(files_with_no_functions)} source file(s) changed but no functions were detected:")
                for file_name in files_with_no_functions:
                    print(f"  - {file_name}")

            if total_functions and functions_with_tests == 0:
                print(f"Warning: No tests mapped to {total_functions} changed function(s)")

            print(
                f"Changed functions: {total_functions}; "
                f"with coverage: {functions_with_tests}; "
                f"without: {functions_without_tests}; "
                f"unique tests: {total_covering_tests}; "
                f"coverage: {coverage_percentage:.1f}%"
            )

            if changed_functions:
                print("\nFunctions selected from commit:")
                for function in changed_functions:
                    full_key = f"{function.file}:{function.signature}:{function.start}"
                    match = test_results.get("function_matches", {}).get(full_key, {})
                    tests = match.get("tests", [])
                    match_type = match.get("match_type", "none")
                    print(f"  {full_key} -> {match_type} (tests={len(tests)})")

            match_type_counts = test_results.get("match_type_counts", {})
            if match_type_counts:
                print("\nMatch breakdown:")
                for key in sorted(match_type_counts):
                    print(f"  {key}: {match_type_counts[key]}")

            summary = {
                "total_functions": total_functions,
                "functions_with_tests": functions_with_tests,
                "functions_without_tests": functions_without_tests,
                "total_covering_tests": total_covering_tests,
                "coverage_percentage": coverage_percentage,
                "fallback_to_all_tests": False,
            }

            return {
                "commit": commit_hash,
                "changed_functions": [f"{function.file}:{function.signature}:{function.start}" for function in changed_functions],
                "files_with_no_functions": files_with_no_functions,
                "covering_tests": ordered_tests,
                "function_matches": test_results.get("function_matches", {}),
                "function_test_counts": test_results.get("function_test_counts", {}),
                "test_function_counts": test_results.get("test_function_counts", {}),
                "match_type_counts": match_type_counts,
                "summary": summary,
            }
        finally:
            self.cleanup_coverage_mapping()


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze commit coverage using coverage mapping")
    parser.add_argument("commit", help="Commit hash to analyze")
    parser.add_argument(
        "--coverage-json",
        default="coverage_mapping.json",
        help="Path to coverage mapping JSON file",
    )
    parser.add_argument(
        "--compile-commands",
        default=None,
        help="Path to compile_commands.json or its directory (used when libclang is available)",
    )
    parser.add_argument(
        "--opensmt-dir",
        default=".",
        help="OpenSMT repository root used for corpus discovery",
    )
    parser.add_argument(
        "--output-matrix",
        help="Output matrix to JSON file instead of console",
    )
    parser.add_argument(
        "--tests-per-job",
        type=int,
        default=1,
        help="Number of tests to group per job (default: 1)",
    )
    parser.add_argument(
        "--max-jobs",
        type=int,
        default=None,
        help="Maximum number of jobs to create (default: unlimited)",
    )

    args = parser.parse_args()

    compile_commands = args.compile_commands
    if not compile_commands:
        build_dir = Path(args.opensmt_dir) / "build"
        if (build_dir / "compile_commands.json").exists():
            compile_commands = str(build_dir / "compile_commands.json")
        elif build_dir.exists():
            compile_commands = str(build_dir)

    analyzer = PrepareCommitAnalyzer(".", opensmt_dir=args.opensmt_dir, compile_commands=compile_commands)
    try:
        result = analyzer.analyze_commit_coverage(args.commit, args.coverage_json)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}")
        return 1

    unique_tests = list(result.get("covering_tests", []))
    jobs, tests_per_job = build_jobs(unique_tests, args.tests_per_job, args.max_jobs, "opensmt")

    if args.output_matrix:
        matrix_data = {
            "matrix": {"include": jobs},
            "total_tests": len(unique_tests),
            "total_jobs": len(jobs),
            "tests_per_job": tests_per_job,
            "matched_tests": unique_tests,
            "summary": result["summary"],
        }
        with open(args.output_matrix, "w", encoding="utf-8") as handle:
            json.dump(matrix_data, handle, indent=2)

        if unique_tests:
            print(
                f"Matrix written to {args.output_matrix} with {len(unique_tests)} unique tests in {len(jobs)} jobs"
            )
        else:
            print(f"No matched tests found; wrote empty matrix to {args.output_matrix}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
