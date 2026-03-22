#!/bin/bash
# Extract OpenSMT build artifacts from artifacts.tar.gz
# This script extracts:
# - All header files to build/ with preserved paths
# - The OpenSMT binary to build/bin/opensmt
# - compile_commands.json to build/
#
# Usage: ./extract_build_artifacts.sh <artifact_file> <build_dir> [extract_headers]
# Example: ./extract_build_artifacts.sh artifacts/artifacts.tar.gz opensmt/build true
#
# If extract_headers is "true" (default), extracts headers. If "false", only extracts binary.

set -e

ARTIFACT_FILE="${1}"
BUILD_DIR="${2:-opensmt/build}"
EXTRACT_HEADERS="${3:-true}"

if [ -z "$ARTIFACT_FILE" ]; then
    echo "Error: Artifact file not specified"
    exit 1
fi

if [ ! -f "$ARTIFACT_FILE" ]; then
    echo "Error: Artifact file not found: $ARTIFACT_FILE"
    exit 1
fi

echo "📦 Extracting build artifacts from $ARTIFACT_FILE"
echo "   Build directory: $BUILD_DIR"
echo "   Extract headers: $EXTRACT_HEADERS"

mkdir -p "$BUILD_DIR"

TMP_DIR=$(mktemp -d)
trap "rm -rf $TMP_DIR" EXIT

echo "Extracting archive..."
tar -xzf "$ARTIFACT_FILE" -C "$TMP_DIR"

if [ -f "$TMP_DIR/bin/opensmt" ]; then
    mkdir -p "$BUILD_DIR/bin"
    cp "$TMP_DIR/bin/opensmt" "$BUILD_DIR/bin/opensmt"
    chmod +x "$BUILD_DIR/bin/opensmt"
    echo "✓ Binary extracted to $BUILD_DIR/bin/opensmt"
else
    echo "⚠ Warning: Binary not found in artifacts"
fi

if [ -f "$TMP_DIR/compile_commands.json" ]; then
    cp "$TMP_DIR/compile_commands.json" "$BUILD_DIR/compile_commands.json"
    echo "✓ compile_commands.json extracted"
else
    echo "⚠ Warning: compile_commands.json not found in artifacts"
fi

if [ "$EXTRACT_HEADERS" = "true" ]; then
    if [ -d "$TMP_DIR/headers" ]; then
        if [ -d "$TMP_DIR/headers/include" ]; then
            mv "$TMP_DIR/headers/include" "$BUILD_DIR/include"
            echo "✓ Headers extracted: include/"
        fi
        if [ -d "$TMP_DIR/headers/src" ]; then
            mv "$TMP_DIR/headers/src" "$BUILD_DIR/src"
            echo "✓ Headers extracted: src/"
        fi
        if [ -d "$TMP_DIR/headers/deps" ]; then
            mv "$TMP_DIR/headers/deps" "$BUILD_DIR/deps"
            echo "✓ Headers extracted: deps/"
        fi

        HEADER_COUNT=$(find "$BUILD_DIR" -type f \( -name "*.h" -o -name "*.hpp" -o -name "*.hxx" \) 2>/dev/null | wc -l || echo "0")
        echo "  Total headers: $HEADER_COUNT"
    else
        echo "⚠ Warning: headers/ directory not found in artifacts"
    fi
fi

if [ -f "$BUILD_DIR/bin/opensmt" ]; then
    "$BUILD_DIR/bin/opensmt" --version > /dev/null 2>&1 && echo "✓ Binary verified" || echo "⚠ Binary verification failed"
fi

echo "✅ Extraction complete!"
