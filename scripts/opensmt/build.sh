#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'USAGE' >&2
Usage: build.sh [--instrumented|--instrumentation|--coverage] [--static]

Builds the checked-out OpenSMT repository from SOLVER_WORKSPACE.
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
ENABLE_STATIC="false"

for arg in "$@"; do
  case "$arg" in
    --instrumented|--instrumentation|--coverage)
      MODE="instrumentation"
      ;;
    --static)
      ENABLE_STATIC="true"
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

SOLVER_ROOT="${SOLVER_WORKSPACE:-$(pwd)/opensmt}"
if [[ ! -d "$SOLVER_ROOT" ]]; then
  echo "Missing solver workspace: $SOLVER_ROOT" >&2
  exit 1
fi

echo "Building OpenSMT in $SOLVER_ROOT" >&2
echo "Mode: $MODE" >&2

cd "$SOLVER_ROOT"
rm -rf build
mkdir -p build
cd build

CMAKE_ARGS=(
  -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
  -DENABLE_LINE_EDITING:BOOL=FALSE
  -DPARALLEL:BOOL=OFF
)

if [[ "$MODE" == "instrumentation" ]]; then
  CMAKE_ARGS+=(-DCMAKE_BUILD_TYPE=Debug)
  CMAKE_ARGS+=(-DCMAKE_C_FLAGS=-O0\ -g\ --coverage)
  CMAKE_ARGS+=(-DCMAKE_CXX_FLAGS=-O0\ -g\ --coverage)
else
  CMAKE_ARGS+=(-DCMAKE_BUILD_TYPE=Release)
fi

if [[ "$ENABLE_STATIC" == "true" ]]; then
  CMAKE_ARGS+=(-DMAXIMALLY_STATIC_BINARY=YES)
fi

cmake "${CMAKE_ARGS[@]}" .. >&2
cmake --build . -j"$(cpu_count)" >&2

mkdir -p bin
if [[ -x "$SOLVER_ROOT/build/opensmt" ]]; then
  cp "$SOLVER_ROOT/build/opensmt" "$SOLVER_ROOT/build/bin/opensmt"
fi

BINARY_PATH="$SOLVER_ROOT/build/bin/opensmt"
if [[ ! -x "$BINARY_PATH" ]]; then
  echo "Expected executable not found: $BINARY_PATH" >&2
  exit 1
fi

printf 'BINARY_PATH=%s\n' "$(cd "$(dirname "$BINARY_PATH")" && pwd)/$(basename "$BINARY_PATH")"
