#!/bin/bash
# Build ChampSim and DRAMSim3 with ENABLE_EPOCH_STATS
# Usage: ./scripts/build_with_epoch_stats.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
CHAMPSIM_ROOT="${PROJECT_ROOT}/champsim-la"
DRAMSIM3_ROOT="${PROJECT_ROOT}/dramsim3"

echo "=========================================="
echo "Building with ENABLE_EPOCH_STATS"
echo "=========================================="

# Build DRAMSim3
echo ""
echo "[1/2] Building DRAMSim3..."
cd "${DRAMSIM3_ROOT}"
make clean
make EPOCH_STATS=1 -j$(nproc)

# Build ChampSim - need to add flag to CXXFLAGS directly
echo ""
echo "[2/2] Building ChampSim..."
cd "${CHAMPSIM_ROOT}"
make clean
python3 config.sh champsim_config.json

# Add EPOCH_STATS flag by modifying Makefile and compiling immediately
# Use a subshell to ensure modification persists during make
(
    sed -i 's/^CXXFLAGS := -Wall/CXXFLAGS := -DENABLE_EPOCH_STATS -Wall/' Makefile
    make -j$(nproc)
    sed -i 's/^CXXFLAGS := -DENABLE_EPOCH_STATS -Wall/CXXFLAGS := -Wall/' Makefile
)

echo ""
echo "=========================================="
echo "Build complete!"
echo "Binary: ${CHAMPSIM_ROOT}/bin/champsim"
echo ""
echo "To run:"
echo "  export LD_LIBRARY_PATH=${DRAMSIM3_ROOT}:\$LD_LIBRARY_PATH"
echo "  ${CHAMPSIM_ROOT}/bin/champsim -loongarch <trace>"
echo "=========================================="
