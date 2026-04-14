#!/usr/bin/env python3
"""Helpers for parsing unified diffs and matching changed line ranges."""

from __future__ import annotations

from dataclasses import dataclass, field
import re


_HUNK_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


@dataclass(frozen=True)
class DiffHunk:
    """A unified-diff hunk with old/new ranges."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int

    @property
    def is_delete_only(self) -> bool:
        return self.old_count > 0 and self.new_count == 0

    @property
    def is_insert_only(self) -> bool:
        return self.old_count == 0 and self.new_count > 0

    @property
    def is_replace(self) -> bool:
        return self.old_count > 0 and self.new_count > 0

    def overlaps_old(self, start_line: int, end_line: int) -> bool:
        return _range_overlaps(self.old_start, self.old_count, start_line, end_line)

    def overlaps_new(self, start_line: int, end_line: int) -> bool:
        return _range_overlaps(self.new_start, self.new_count, start_line, end_line)


@dataclass
class FileDiff:
    """Parsed hunks for one changed file."""

    old_path: str | None = None
    new_path: str | None = None
    hunks: list[DiffHunk] = field(default_factory=list)

    @property
    def canonical_path(self) -> str | None:
        if self.new_path and self.new_path != "/dev/null":
            return self.new_path
        if self.old_path and self.old_path != "/dev/null":
            return self.old_path
        return None

    def overlaps_before(self, start_line: int, end_line: int) -> bool:
        return any(hunk.overlaps_old(start_line, end_line) for hunk in self.hunks if hunk.old_count > 0)

    def overlaps_after(self, start_line: int, end_line: int) -> bool:
        return any(hunk.overlaps_new(start_line, end_line) for hunk in self.hunks if hunk.new_count > 0)

    def has_delete_only_overlap(self, start_line: int, end_line: int) -> bool:
        return any(hunk.overlaps_old(start_line, end_line) for hunk in self.hunks if hunk.is_delete_only)


def _normalize_path(raw_path: str) -> str:
    if raw_path in {"a/dev/null", "b/dev/null", "/dev/null"}:
        return "/dev/null"
    if raw_path.startswith("a/") or raw_path.startswith("b/"):
        return raw_path[2:]
    return raw_path


def _range_overlaps(range_start: int, range_count: int, start_line: int, end_line: int) -> bool:
    if range_count <= 0:
        return False
    range_end = range_start + range_count - 1
    return not (end_line < range_start or start_line > range_end)


def parse_unified_diff(diff_text: str) -> dict[str, FileDiff]:
    """Parse a unified diff into per-file old/new hunk ranges."""
    parsed: dict[str, FileDiff] = {}
    current: FileDiff | None = None

    for raw_line in diff_text.splitlines():
        if raw_line.startswith("diff --git "):
            if current and current.canonical_path:
                parsed[current.canonical_path] = current
            current = FileDiff()
            continue

        if current is None:
            continue

        if raw_line.startswith("--- "):
            current.old_path = _normalize_path(raw_line[4:].strip())
            continue

        if raw_line.startswith("+++ "):
            current.new_path = _normalize_path(raw_line[4:].strip())
            continue

        if raw_line.startswith("@@ "):
            match = _HUNK_RE.match(raw_line)
            if not match:
                continue
            current.hunks.append(
                DiffHunk(
                    old_start=int(match.group("old_start")),
                    old_count=int(match.group("old_count") or "1"),
                    new_start=int(match.group("new_start")),
                    new_count=int(match.group("new_count") or "1"),
                )
            )

    if current and current.canonical_path:
        parsed[current.canonical_path] = current

    return parsed
