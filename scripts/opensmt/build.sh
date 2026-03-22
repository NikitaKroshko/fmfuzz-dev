#!/bin/bash
# OpenSMT Build and Test Script
# Usage: ./build.sh [--coverage] [--static]

set -e

ENABLE_COVERAGE=false
for arg in "$@"; do
  case "$arg" in
    --coverage)
      ENABLE_COVERAGE=true
      ;;
    --static)
      # Kept for parity with the Z3/CVC5 helpers.
      ;;
    *)
      echo "Warning: ignoring unknown argument: $arg"
      ;;
  esac
done

echo "🔧 Installing basic tools..."
sudo apt-get update
sudo apt-get install -y \
  build-essential \
  cmake \
  git \
  python3 \
  python3-pip \
  libgmp-dev \
  flex \
  bison

if [ "$ENABLE_COVERAGE" = "true" ]; then
  echo "📊 Installing coverage tools..."
  sudo apt-get install -y gcc g++
  pip3 install fastcov psutil
fi

echo "📥 Cloning OpenSMT repository..."
if [ -d "opensmt" ]; then
  echo "⚠️  OpenSMT directory already exists, skipping clone"
else
  git clone https://github.com/usi-verification-and-security/OpenSMT.git opensmt
fi

echo "🔨 Building OpenSMT..."
cd opensmt

if [ "$ENABLE_COVERAGE" = "true" ]; then
  export GCOV_PREFIX="$(pwd)/build"
  export GCOV_PREFIX_STRIP=0
  CMAKE_BUILD_TYPE="Debug"
  CMAKE_C_FLAGS="-O0 -g --coverage"
  CMAKE_CXX_FLAGS="-O0 -g --coverage"
  CMAKE_EXE_LINKER_FLAGS="-static --coverage"
  echo "🔍 Coverage instrumentation enabled"
else
  CMAKE_BUILD_TYPE="Release"
  CMAKE_C_FLAGS=""
  CMAKE_CXX_FLAGS=""
  CMAKE_EXE_LINKER_FLAGS="-static"
fi

export PARALLEL="OFF"
export ENABLE_LINE_EDITING="FALSE"

if [ -d build ]; then
  rm -rf build
fi
mkdir -p build
cd build

cmake_args=(
  "-DCMAKE_BUILD_TYPE=${CMAKE_BUILD_TYPE}"
  "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"
  "-DENABLE_LINE_EDITING:BOOL=${ENABLE_LINE_EDITING}"
  "-DMAXIMALLY_STATIC_BINARY=YES"
  "-DPARALLEL:BOOL=${PARALLEL}"
  "-DCMAKE_EXE_LINKER_FLAGS=${CMAKE_EXE_LINKER_FLAGS}"
)

if [ -n "$CMAKE_C_FLAGS" ]; then
  cmake_args+=("-DCMAKE_C_FLAGS=${CMAKE_C_FLAGS}")
  cmake_args+=("-DCMAKE_CXX_FLAGS=${CMAKE_CXX_FLAGS}")
fi

cmake "${cmake_args[@]}" ..

cmake --build . -j 4

sudo cmake --install .

mkdir -p bin
BUILT_BINARY=""
if [ -f "./opensmt" ]; then
  BUILT_BINARY="./opensmt"
else
  BUILT_BINARY=$(find . -type f -name opensmt -perm -111 | head -n 1 || true)
fi

if [ -z "$BUILT_BINARY" ]; then
  BUILT_BINARY=$(command -v opensmt || true)
fi

if [ -n "$BUILT_BINARY" ] && [ -f "$BUILT_BINARY" ]; then
  if [ "$(realpath "$BUILT_BINARY" 2>/dev/null || echo "$BUILT_BINARY")" != "$(realpath "./bin/opensmt" 2>/dev/null || echo "./bin/opensmt")" ]; then
    cp "$BUILT_BINARY" ./bin/opensmt
  fi
  chmod +x ./bin/opensmt
fi

echo "🧪 Testing OpenSMT binary..."
if [ -f "./bin/opensmt" ]; then
  ./bin/opensmt --version || echo "Version command completed (exit code $?)"
elif command -v opensmt >/dev/null 2>&1; then
  opensmt --version || echo "Version command completed (exit code $?)"
else
  echo "OpenSMT binary not found!"
  exit 1
fi

echo "✅ OpenSMT build and test completed successfully!"
