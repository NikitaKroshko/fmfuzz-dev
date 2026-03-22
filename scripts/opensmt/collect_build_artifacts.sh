#!/bin/bash
# Collect OpenSMT build artifacts for libclang parsing
# This script collects:
# - All header files (.h, .hpp, .hxx) from the build tree and install prefix
#   with preserved paths
# - The OpenSMT binary
# - compile_commands.json
#
# Usage: ./collect_build_artifacts.sh <build_dir> <output_dir>
# Example: ./collect_build_artifacts.sh opensmt/build artifacts

set -e

BUILD_DIR="${1:-opensmt/build}"
OUTPUT_DIR="${2:-artifacts}"

if [ ! -d "$BUILD_DIR" ]; then
    echo "Error: Build directory not found: $BUILD_DIR"
    exit 1
fi

echo "📦 Collecting build artifacts from $BUILD_DIR"
echo "   Output directory: $OUTPUT_DIR"

mkdir -p "$OUTPUT_DIR/headers"
mkdir -p "$OUTPUT_DIR/bin"

discover_install_prefix() {
    local cache_file="$BUILD_DIR/CMakeCache.txt"
    local install_prefix=""

    if [ -f "$cache_file" ]; then
        install_prefix=$(awk -F= '/^CMAKE_INSTALL_PREFIX:PATH=/{print $2; exit}' "$cache_file")
    fi

    if [ -z "$install_prefix" ]; then
        install_prefix="/usr/local"
    fi

    printf '%s\n' "$install_prefix"
}

collect_headers_from_root() {
    local search_root="$1"
    local prefix_root="$2"
    local label="$3"
    local header_count=0

    if [ ! -d "$search_root" ]; then
        return 0
    fi

    while IFS= read -r -d '' header; do
        local rel_path="${header#$prefix_root/}"
        local target_path="$OUTPUT_DIR/headers/$rel_path"
        mkdir -p "$(dirname "$target_path")"
        cp "$header" "$target_path"
        header_count=$((header_count + 1))
    done < <(find "$search_root" -type f \( -name "*.h" -o -name "*.hpp" -o -name "*.hxx" \) -print0)

    if [ "$header_count" -gt 0 ]; then
        echo "   ✓ Collected $header_count headers from $label"
    fi
}

INSTALL_PREFIX="$(discover_install_prefix)"

echo "🔍 Collecting header files..."

collect_headers_from_root "$BUILD_DIR/include" "$BUILD_DIR" "build/include"
collect_headers_from_root "$BUILD_DIR/src" "$BUILD_DIR" "build/src"
collect_headers_from_root "$BUILD_DIR/deps/include" "$BUILD_DIR" "build/deps/include"
collect_headers_from_root "$BUILD_DIR/deps/src" "$BUILD_DIR" "build/deps/src"

if [ -d "$INSTALL_PREFIX/include/opensmt" ]; then
    collect_headers_from_root "$INSTALL_PREFIX/include/opensmt" "$INSTALL_PREFIX" "install/include/opensmt"
fi

TOTAL_HEADERS=$(find "$OUTPUT_DIR/headers" -type f 2>/dev/null | wc -l || echo "0")
echo "   Total headers collected: $TOTAL_HEADERS"

if [ -f "$BUILD_DIR/bin/opensmt" ]; then
    cp "$BUILD_DIR/bin/opensmt" "$OUTPUT_DIR/bin/opensmt"
    chmod +x "$OUTPUT_DIR/bin/opensmt"
    BINARY_SIZE=$(du -h "$OUTPUT_DIR/bin/opensmt" | cut -f1)
    echo "   ✓ Binary copied ($BINARY_SIZE)"
elif [ -f "$BUILD_DIR/opensmt" ]; then
    cp "$BUILD_DIR/opensmt" "$OUTPUT_DIR/bin/opensmt"
    chmod +x "$OUTPUT_DIR/bin/opensmt"
    BINARY_SIZE=$(du -h "$OUTPUT_DIR/bin/opensmt" | cut -f1)
    echo "   ✓ Binary copied ($BINARY_SIZE)"
elif command -v opensmt >/dev/null 2>&1; then
    cp "$(command -v opensmt)" "$OUTPUT_DIR/bin/opensmt"
    chmod +x "$OUTPUT_DIR/bin/opensmt"
    BINARY_SIZE=$(du -h "$OUTPUT_DIR/bin/opensmt" | cut -f1)
    echo "   ✓ Binary copied ($BINARY_SIZE)"
else
    echo "   ⚠ Warning: Binary not found in build directory or PATH"
fi

if [ -f "$BUILD_DIR/compile_commands.json" ]; then
    cp "$BUILD_DIR/compile_commands.json" "$OUTPUT_DIR/compile_commands.json"
    echo "   ✓ compile_commands.json copied"
else
    echo "   ⚠ Warning: compile_commands.json not found at $BUILD_DIR/compile_commands.json"
fi

echo ""
echo "✅ Artifact collection complete!"
echo "   Headers: $OUTPUT_DIR/headers/"
echo "   Binary: $OUTPUT_DIR/bin/opensmt"
echo "   Compile commands: $OUTPUT_DIR/compile_commands.json"
echo ""
echo "📊 Summary:"
echo "   Total header files: $TOTAL_HEADERS"
if [ -f "$OUTPUT_DIR/bin/opensmt" ]; then
    echo "   Binary: ✓"
else
    echo "   Binary: ✗"
fi
if [ -f "$OUTPUT_DIR/compile_commands.json" ]; then
    echo "   compile_commands.json: ✓"
else
    echo "   compile_commands.json: ✗"
fi
