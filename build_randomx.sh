#!/usr/bin/env bash
# Build librandomx.so and librxbatch.so (native batch miner).
# Run this once before starting the miner.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SCRIPT_DIR/RandomX"
BUILD="$SRC/build"

if [ ! -d "$SRC" ]; then
  echo "Cloning RandomX source..."
  git clone --depth=1 https://github.com/tevador/RandomX.git "$SRC"
fi

echo "Building librandomx..."
mkdir -p "$BUILD"
cd "$BUILD"
cmake .. -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON
make -j"$(nproc)"

echo ""
echo "Building librxbatch (native batch miner)..."
cd "$SCRIPT_DIR"
gcc -O3 -march=native -shared -fPIC \
    -I RandomX/src \
    rx_batch.c \
    -L RandomX/build \
    -Wl,-rpath,'$ORIGIN/RandomX/build' \
    -lrandomx \
    -o librxbatch.so

echo ""
echo "Build complete:"
echo "  $BUILD/librandomx.so"
echo "  $SCRIPT_DIR/librxbatch.so"
echo ""
echo "You can now run the miner with: python3 miner/main.py"
