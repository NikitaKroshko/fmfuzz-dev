#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'USAGE' >&2
Usage: build.sh [--instrumented|--instrumentation|--coverage] [--static]

Builds the checked-out cvc5 repository from SOLVER_WORKSPACE.
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

SOLVER_ROOT="${SOLVER_WORKSPACE:-$(pwd)/cvc5}"
if [[ ! -d "$SOLVER_ROOT" ]]; then
  echo "Missing solver workspace: $SOLVER_ROOT" >&2
  exit 1
fi

if [[ ! -x "$SOLVER_ROOT/configure.sh" ]]; then
  echo "Missing cvc5 configure script: $SOLVER_ROOT/configure.sh" >&2
  exit 1
fi

echo "Building cvc5 in $SOLVER_ROOT" >&2
echo "Mode: $MODE" >&2

cd "$SOLVER_ROOT"
rm -rf build

CONFIGURE_ARGS=(--auto-download)
if [[ "$ENABLE_STATIC" == "true" ]]; then
  CONFIGURE_ARGS+=(--static --static-binary)
fi

if [[ "$MODE" == "instrumentation" ]]; then
  ./configure.sh debug --coverage --assertions "${CONFIGURE_ARGS[@]}" >&2
else
  ./configure.sh production "${CONFIGURE_ARGS[@]}" >&2
fi

make -C build -j"$(cpu_count)" >&2

BINARY_PATH="$SOLVER_ROOT/build/bin/cvc5"
if [[ ! -x "$BINARY_PATH" ]]; then
  echo "Expected executable not found: $BINARY_PATH" >&2
  exit 1
fi

printf 'BINARY_PATH=%s\n' "$(cd "$(dirname "$BINARY_PATH")" && pwd)/$(basename "$BINARY_PATH")"
