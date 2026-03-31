#!/usr/bin/env python3
"""Compatibility entrypoint for the standalone OpenSMT commit fuzzer."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.opensmt.commit_fuzzer.simple_commit_fuzzer import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
