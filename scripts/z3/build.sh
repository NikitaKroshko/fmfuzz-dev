#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'USAGE' >&2
Usage: build.sh [--instrumentation|--coverage] [--static]

Builds the checked-out z3 repository from SOLVER_WORKSPACE.
The final stdout line is always: BINARY_PATH=/abs/path/to/binary
USAGE
}

cpu_count() {
  if command -v nproc >/dev/null 2>&1; then
    nproc
  else
    sysctl -n hw.logicalcpu
  fi
}

MODE="production"

for arg in "$@"; do
  case "$arg" in
    --instrumentation|--coverage)
      MODE="instrumentation"
      ;;
    --static)
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      usage
      exit 2
      ;;
  esac
done

SOLVER_ROOT="${SOLVER_WORKSPACE:-$(pwd)/z3}"
if [[ ! -d "$SOLVER_ROOT" ]]; then
  echo "Missing solver workspace: $SOLVER_ROOT" >&2
  exit 1
fi

echo "Building z3 in $SOLVER_ROOT" >&2
echo "Mode: $MODE" >&2

cd "$SOLVER_ROOT"
rm -rf build
mkdir -p build
cd build

COMMON_FLAGS=(
  -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
  -DZ3_BUILD_LIBZ3_SHARED=OFF
  -DZ3_BUILD_EXECUTABLE=ON
  -DZ3_BUILD_TEST_EXECUTABLES=OFF
  -G
  "Unix Makefiles"
)

if [[ "$MODE" == "instrumentation" ]]; then
  CFLAGS="-O0 -g --coverage" \
  CXXFLAGS="-O0 -g --coverage" \
    cmake -DCMAKE_BUILD_TYPE=Debug "${COMMON_FLAGS[@]}" ..
else
  cmake -DCMAKE_BUILD_TYPE=Release "${COMMON_FLAGS[@]}" ..
fi

make -j"$(cpu_count)" >&2

BINARY_PATH="$SOLVER_ROOT/build/z3"
if [[ ! -x "$BINARY_PATH" ]]; then
  echo "Expected executable not found: $BINARY_PATH" >&2
  exit 1
fi

printf 'BINARY_PATH=%s\n' "$(cd "$(dirname "$BINARY_PATH")" && pwd)/$(basename "$BINARY_PATH")"
