#!/usr/bin/env python3
"""
Simple script to join OpenSMT coverage mapping files.
"""

from __future__ import annotations

import json
import os
import sys


def main() -> int:
    mapping_files = []
    for root, _, files in os.walk("coverage-mappings"):
        for file in files:
            if file.startswith("coverage_mapping_") and file.endswith(".json"):
                mapping_files.append(os.path.join(root, file))

    if not mapping_files:
        print("No coverage mapping files found, writing empty mapping")
        with open("coverage_mapping.json", "w", encoding="utf-8") as f:
            json.dump({}, f, separators=(",", ":"))
        os.system("gzip -kf coverage_mapping.json")
        return 0

    mapping_files.sort()
    print(f"Found {len(mapping_files)} mapping files")

    merged_mapping = {}
    for file_path in mapping_files:
        print(f"Processing {os.path.basename(file_path)}")
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            for func, tests in data.items():
                merged_mapping.setdefault(func, []).extend(tests)

    for func in merged_mapping:
        merged_mapping[func] = sorted(set(merged_mapping[func]))

    with open("coverage_mapping.json", "w", encoding="utf-8") as f:
        json.dump(merged_mapping, f, separators=(",", ":"))

    original_size = os.path.getsize("coverage_mapping.json")
    print(f"Original size: {original_size:,} bytes")

    os.system("gzip -kf coverage_mapping.json")
    compressed_size = os.path.getsize("coverage_mapping.json.gz")
    compression_ratio = (compressed_size / original_size) * 100 if original_size else 0
    print(f"Compressed size: {compressed_size:,} bytes ({compression_ratio:.1f}% of original)")
    print(f"Functions: {len(merged_mapping)}")
    print("Merged coverage mapping saved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
