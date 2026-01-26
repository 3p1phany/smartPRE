# smartPRE

A research framework for studying **smart DRAM Row Buffer Precharge** strategies based on program behavior characteristics.

## Overview

smartPRE investigates the correlation between program execution patterns (PC signatures) and optimal DRAM row buffer timeout values. By analyzing this relationship, we aim to develop dynamic timeout policies that adapt to changing memory access patterns.

### Key Research Questions

- Can program counter (PC) signatures predict optimal row buffer timeout?
- How does timeout affect performance (IPC) and row buffer hit rate (RBHR)?
- Is there a correlation between PC pattern changes and optimal timeout transitions?

## Architecture

```
smartPRE/
├── champsim-la/          # ChampSim CPU simulator (LoongArch port)
├── dramsim3/             # Modified DRAMSim3 with epoch statistics
└── scripts/              # Experiment and analysis scripts
```

### Components

| Component | Description | Repository |
|-----------|-------------|------------|
| **champsim-la** | ChampSim with LoongArch support and epoch instrumentation | [mychampsim](https://github.com/3p1phany/mychampsim) |
| **dramsim3** | DRAMSim3 with row buffer statistics and static timeout policy | [myDRAMsim](https://github.com/3p1phany/myDRAMsim) |

## Getting Started

### Prerequisites

- GCC/G++ with C++17 support
- GNU Make
- Python 3.8+ with numpy, pandas, matplotlib
- GNU Parallel (optional, for parallel experiments)

### Clone with Submodules

```bash
git clone --recursive https://github.com/3p1phany/smartPRE.git
cd smartPRE
```

Or if already cloned:

```bash
git submodule update --init --recursive
```

### Build

```bash
# Build DRAMSim3
cd dramsim3
make -j$(nproc)
cd ..

# Build ChampSim (normal mode)
cd champsim-la
make -j$(nproc)
cd ..

# Or build with epoch statistics enabled (for experiments)
./scripts/build_with_epoch_stats.sh
```

## Experiments

### Oracle Timeout Sweep

The main experiment sweeps through multiple static timeout values to find the optimal timeout for each execution epoch.

#### Collected Metrics

| Metric | Description | Collection Point |
|--------|-------------|------------------|
| **Epoch ID** | Time window number (per 10K instructions) | `main.cc` |
| **PC Hash** | XOR hash of memory access PCs | `dramsim3_wrapper.hpp` |
| **RBHR** | Row Buffer Hit Rate | `controller.cc` |
| **IPC** | Instructions Per Cycle | `main.cc` |

#### Running the Experiment

```bash
# Set trace directory
export TRACE_ROOT=/path/to/traces

# Run sweep (8 traces x 20 timeout values = 160 tasks)
./scripts/run_timeout_sweep.sh

# Analyze results
python3 ./scripts/analyze_oracle_sweep.py
```

#### Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `WARMUP` | 20M | Warmup instructions |
| `SIM` | 50M | Simulation instructions |
| `JOBS` | 128 | Parallel jobs |
| `TIMEOUT_VALUES` | 20-400 (step 20) | Timeout values to sweep |

### Output Structure

```
results/
├── oracle_sweep/
│   ├── configs/                    # Generated DRAM configs
│   ├── <trace_name>/
│   │   └── timeout_<N>/
│   │       ├── run.log             # Full simulation log
│   │       └── epoch_stats.csv     # Epoch data
│   └── tasks.txt
└── analysis/
    ├── <trace>_oracle_analysis.png # Visualization
    ├── <trace>_oracle.csv          # Oracle timeout sequence
    └── summary_stats.csv           # Correlation statistics
```

## Methodology

### Epoch Statistics

Each epoch (10K instructions) records:

```
[EPOCH] epoch_id,pc_hash,rbhr,ipc
[EPOCH] 0,0x120000efc,0.0000,3.171672
[EPOCH] 1,0x3c,0.9905,1.271872
```

### PC Hash Calculation

The PC hash captures memory access patterns by XOR-accumulating the program counters of all instructions that reach DRAM:

```cpp
// At DRAM entry point
epoch_pc_hash ^= packet->ip;
```

### Oracle Analysis

For each epoch, the analysis script:
1. Compares IPC across all timeout values
2. Identifies the best-performing timeout
3. Computes PC signature change rate (Hamming distance)
4. Calculates correlation between PC changes and timeout transitions

## Benchmarks

The framework includes traces from memory-intensive workloads:

- **Graph500** - BFS on scale-16 graph
- **Ligra** - MIS algorithm on Higgs dataset
- **CRONO** - PageRank and Connected Components
- **HashJoin** - Database join operations
- **HPCC** - Random Access benchmark
- **NPB** - Integer Sort (Class B)
- **SpMV** - Sparse matrix-vector multiplication

## References

- [ChampSim](https://github.com/ChampSim/ChampSim) - CPU trace simulator
- [DRAMSim3](https://github.com/umd-memsys/DRAMsim3) - DRAM timing simulator

## License

This project is for academic research purposes.
