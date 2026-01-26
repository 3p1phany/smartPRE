# Part 1: Oracle TIMEOUT 数据采集与关联性验证 - 完整实验方案

## 概述

本实验旨在通过"穷举扫描"不同的静态 Timeout 值，采集每个时间窗口的性能数据，找出每个窗口的最优 Timeout，并分析其与 PC Signature 变化的关联性。

---

## Step 1: 代码修改 - 打桩采集 Oracle TIMEOUT 数据

### 1.1 数据采集位置与机制说明

| 数据项 | 采集位置 | 采集机制 |
|--------|----------|----------|
| **Epoch ID** | ChampSim `main.cc` | 每 10K 指令递增的窗口编号 |
| **Hashed PC Signature** | ChampSim `ooo_cpu.cc` 或 `main.cc` | 采集窗口内 retired 指令的 PC，计算 XOR Hash |
| **Row Buffer Hit Rate** | DRAMSim3 `simple_stats.cc` | 使用 `epoch_counters_` 机制，窗口结束时计算并重置 |
| **IPC** | ChampSim `main.cc` | 窗口内 retired 指令数 / 窗口内 cycle 数 |

### 1.2 需要修改的文件

#### 文件 1: `/root/data/smartPRE/champsim-la/src/main.cc`

**修改目的**: 添加 Epoch 统计输出机制

```cpp
// ===== 新增全局变量 (在文件头部，约第 42 行附近) =====
// Epoch logging configuration
static constexpr uint64_t EPOCH_INSTRUCTIONS = 10000;  // 每 10K 指令一个 epoch
uint64_t epoch_id = 0;
uint64_t epoch_start_cycle = 0;
uint64_t epoch_start_instr = 0;
uint64_t epoch_pc_hash = 0;  // XOR hash of all retired PCs in this epoch

// Row Buffer stats (从 DRAMSim3 获取)
extern uint64_t dramsim3_epoch_row_hits;
extern uint64_t dramsim3_epoch_row_misses;

// ===== 新增函数：输出 Epoch 统计 =====
void print_epoch_stats(uint32_t cpu) {
    uint64_t epoch_cycles = ooo_cpu[cpu]->current_cycle - epoch_start_cycle;
    uint64_t epoch_instrs = ooo_cpu[cpu]->num_retired - epoch_start_instr;

    double ipc = (epoch_cycles > 0) ? (double)epoch_instrs / epoch_cycles : 0.0;

    uint64_t total_accesses = dramsim3_epoch_row_hits + dramsim3_epoch_row_misses;
    double rbhr = (total_accesses > 0) ?
                  (double)dramsim3_epoch_row_hits / total_accesses : 0.0;

    // 输出格式: epoch_id, pc_hash, rbhr, ipc
    std::cout << "[EPOCH] " << epoch_id
              << "," << std::hex << epoch_pc_hash << std::dec
              << "," << std::fixed << std::setprecision(4) << rbhr
              << "," << std::fixed << std::setprecision(6) << ipc
              << std::endl;

    // 重置 epoch 状态
    epoch_id++;
    epoch_start_cycle = ooo_cpu[cpu]->current_cycle;
    epoch_start_instr = ooo_cpu[cpu]->num_retired;
    epoch_pc_hash = 0;

    // 通知 DRAMSim3 重置 epoch 计数器
    DRAM.ResetEpochStats();
}

// ===== 在主循环中添加 Epoch 检查 (约第 684-735 行的 CPU 循环内) =====
// 在 heartbeat 检查之后添加：
if (warmup_complete[i] &&
    (ooo_cpu[i]->num_retired - epoch_start_instr) >= EPOCH_INSTRUCTIONS) {
    print_epoch_stats(i);
}
```

#### 文件 2: `/root/data/smartPRE/champsim-la/src/ooo_cpu.cc`

**修改目的**: 采集 Retired 指令的 PC 用于 Hash 计算

```cpp
// 在 retire_rob() 函数中，每条指令 retire 时：
extern uint64_t epoch_pc_hash;

// 在 ROB entry retire 的位置（约在 handle_retired 或 retire_rob 函数内）：
// 假设 ROB[rob_idx].ip 是指令的 PC
epoch_pc_hash ^= ROB[rob_idx].ip;  // XOR 累积
```

#### 文件 3: `/root/data/smartPRE/dramsim3/src/controller.cc` 或 `simple_stats.cc`

**修改目的**: 添加 Row Buffer Hit/Miss 的 Epoch 统计

```cpp
// ===== 新增全局变量 =====
uint64_t dramsim3_epoch_row_hits = 0;
uint64_t dramsim3_epoch_row_misses = 0;

// ===== 在处理 ACT 命令时 (controller.cc 或 command_queue.cc) =====
// 当发生 Row Hit 时:
dramsim3_epoch_row_hits++;

// 当发生 Row Miss (需要 ACT) 时:
dramsim3_epoch_row_misses++;

// ===== 新增重置函数 =====
void ResetEpochStats() {
    dramsim3_epoch_row_hits = 0;
    dramsim3_epoch_row_misses = 0;
}
```

#### 文件 4: `/root/data/smartPRE/champsim-la/inc/dramsim3_wrapper.hpp`

**修改目的**: 添加重置 Epoch 统计的接口

```cpp
// 在 DRAMSim3_DRAM 类中添加:
extern uint64_t dramsim3_epoch_row_hits;
extern uint64_t dramsim3_epoch_row_misses;

void ResetEpochStats() {
    dramsim3_epoch_row_hits = 0;
    dramsim3_epoch_row_misses = 0;
}
```

### 1.3 数据采集原理详解

#### Epoch ID
- **含义**: 时间窗口编号，从 0 开始
- **采集方式**: 全局计数器，每完成 10,000 条指令递增

#### Hashed PC Signature
- **含义**: 代表当前窗口内程序执行"特征"的指纹
- **采集方式**:
  - 每条指令 retire 时，将其 PC (Instruction Pointer) 通过 XOR 累积
  - 公式: `pc_hash ^= current_pc`
  - XOR Hash 的好处：简单、快速、对顺序不敏感但能反映整体分布
- **物理意义**: 不同的 PC Hash 表示程序进入了不同的代码区域（如不同函数、不同算法阶段）

#### Row Buffer Hit Rate (RBHR)
- **含义**: DRAM 行缓冲命中率
- **采集位置**: DRAMSim3 内部
- **计算公式**: `RBHR = row_hits / (row_hits + row_misses)`
- **现有代码位置**:
  - `command_queue.cc` 中 `true_row_hit_count_` 和 `total_command_count_` 已有类似统计
  - 需要在 CAS 命令发出时统计 Hit，在 ACT 命令发出时统计 Miss

#### IPC (Instructions Per Cycle)
- **含义**: 每周期指令数，衡量 CPU 执行效率
- **计算公式**: `IPC = (retired_instr_end - retired_instr_start) / (cycle_end - cycle_start)`
- **采集位置**: ChampSim `main.cc`，利用 `ooo_cpu[i]->num_retired` 和 `ooo_cpu[i]->current_cycle`

---

## Step 2: 穷举扫描 (The Sweep)

### 2.1 Timeout 值集合定义

基于现有 GS 超时值 `{50, 100, 150, 200, 300, 400, 800}` 和硬件典型范围，定义 **16 个 Timeout 值**：

```bash
TIMEOUT_VALUES=(10 20 50 100 150 200 300 400 500 600 800 1000 1200 1600 2000 3200)
```

**设计依据**:
- 覆盖极短超时 (10-50 cycles): 接近 Close-Page
- 覆盖常用范围 (100-400 cycles): GS 默认范围
- 覆盖长超时 (800-3200 cycles): 接近 Open-Page

### 2.2 Trace 列表 (来自 trace_phase.tsv)

```
graph500/s16-e10                    -> /root/data/Trace/LA/graph500/s16-e10/Graph500_s16-e10_0.champsim.trace.xz
ligra/MIS/higgs                     -> /root/data/Trace/LA/ligra/MIS/higgs/ligra_MIS_higgs_200000000.champsim.trace.xz
crono/PageRank/soc-pokec            -> /root/data/Trace/LA/crono/PageRank/soc-pokec/crono_PageRank_soc-pokec.champsim.trace.xz
crono/Connected-Components/higgs    -> /root/data/Trace/LA/crono/Connected-Components/higgs/crono_Connected-Components_higgs_100000000.champsim.trace.xz
hashjoin/hj-8-NPO_st                -> /root/data/Trace/LA/hashjoin/hj-8-NPO_st/hj-8-NPO_st_9090000000.champsim.trace.xz
hpcc/RandAcc                        -> /root/data/Trace/LA/hpcc/RandAcc/hpcc_RandAcc_400000000.champsim.trace.xz
npb/IS                              -> /root/data/Trace/LA/npb/IS/npb_IS_B_2590000000.champsim.trace.xz
spmv/mc2depi                        -> /root/data/Trace/LA/spmv/mc2depi/spmv_mc2depi_100000000.champsim.trace.xz
```

### 2.3 并行执行策略

**总任务数**: 8 traces × 16 timeouts = 128 任务 (刚好匹配 128 线程)

### 2.4 运行脚本

```bash
#!/bin/bash
# File: /root/data/smartPRE/scripts/run_timeout_sweep.sh

set -e

# ===== Configuration =====
CHAMPSIM_BIN="/root/data/smartPRE/champsim-la/bin/champsim"
TRACE_ROOT="/root/data/Trace/LA"
RESULTS_ROOT="/root/data/smartPRE/results/oracle_sweep"
WARMUP=20000000      # 20M warmup instructions
SIM=50000000         # 50M simulation instructions
JOBS=128             # 128 parallel jobs

# Timeout values to sweep (16 values)
TIMEOUT_VALUES=(10 20 50 100 150 200 300 400 500 600 800 1000 1200 1600 2000 3200)

# Trace configurations (from trace_phase.tsv)
declare -A TRACES
TRACES["graph500_s16-e10"]="${TRACE_ROOT}/graph500/s16-e10/Graph500_s16-e10_0.champsim.trace.xz"
TRACES["ligra_MIS_higgs"]="${TRACE_ROOT}/ligra/MIS/higgs/ligra_MIS_higgs_200000000.champsim.trace.xz"
TRACES["crono_PageRank_soc-pokec"]="${TRACE_ROOT}/crono/PageRank/soc-pokec/crono_PageRank_soc-pokec.champsim.trace.xz"
TRACES["crono_CC_higgs"]="${TRACE_ROOT}/crono/Connected-Components/higgs/crono_Connected-Components_higgs_100000000.champsim.trace.xz"
TRACES["hashjoin_hj-8"]="${TRACE_ROOT}/hashjoin/hj-8-NPO_st/hj-8-NPO_st_9090000000.champsim.trace.xz"
TRACES["hpcc_RandAcc"]="${TRACE_ROOT}/hpcc/RandAcc/hpcc_RandAcc_400000000.champsim.trace.xz"
TRACES["npb_IS"]="${TRACE_ROOT}/npb/IS/npb_IS_B_2590000000.champsim.trace.xz"
TRACES["spmv_mc2depi"]="${TRACE_ROOT}/spmv/mc2depi/spmv_mc2depi_100000000.champsim.trace.xz"

# ===== Environment Setup =====
export LD_LIBRARY_PATH="/root/data/smartPRE/dramsim3:${LD_LIBRARY_PATH}"
mkdir -p "${RESULTS_ROOT}"

# ===== Generate Task List =====
TASK_FILE="${RESULTS_ROOT}/tasks.txt"
rm -f "${TASK_FILE}"

for trace_name in "${!TRACES[@]}"; do
    trace_path="${TRACES[$trace_name]}"
    for timeout in "${TIMEOUT_VALUES[@]}"; do
        echo "${trace_name} ${timeout} ${trace_path}" >> "${TASK_FILE}"
    done
done

echo "Generated $(wc -l < ${TASK_FILE}) tasks"

# ===== Run Function =====
run_single_task() {
    local trace_name=$1
    local timeout=$2
    local trace_path=$3

    local out_dir="${RESULTS_ROOT}/${trace_name}/timeout_${timeout}"
    mkdir -p "${out_dir}"

    local log_file="${out_dir}/run.log"
    local epoch_file="${out_dir}/epoch_stats.csv"

    # Set static timeout via config or command line (requires code modification)
    # For now, assume we modify the config file or pass as env var
    export STATIC_TIMEOUT_CYCLES="${timeout}"

    "${CHAMPSIM_BIN}" \
        --warmup_instructions "${WARMUP}" \
        --simulation_instructions "${SIM}" \
        -loongarch \
        "${trace_path}" \
        > "${log_file}" 2>&1

    # Extract epoch stats from log
    grep "^\[EPOCH\]" "${log_file}" | cut -d' ' -f2 > "${epoch_file}"

    echo "${trace_name} timeout=${timeout} done"
}

export -f run_single_task
export CHAMPSIM_BIN RESULTS_ROOT WARMUP SIM

# ===== Parallel Execution =====
echo "Starting parallel sweep with ${JOBS} jobs..."
parallel -j "${JOBS}" --colsep ' ' run_single_task {1} {2} {3} :::: "${TASK_FILE}"

echo "Sweep complete! Results in ${RESULTS_ROOT}"
```

### 2.5 DRAM 配置文件修改

创建支持静态超时的配置文件 `/root/data/smartPRE/dramsim3/configs/DDR4_oracle_sweep.ini`:

```ini
[dram_structure]
protocol = DDR4
bankgroups = 4
banks_per_group = 4
rows = 65536
columns = 1024
device_width = 8
BL = 8

[timing]
tCK = 0.625
tCL = 22
tCWL = 20
tRCD = 22
tRP = 22
tRAS = 52
tRFC = 560
tREFI = 12480
tRRD_S = 6
tRRD_L = 8
tFAW = 48
tWR = 24
tWTR_S = 4
tWTR_L = 12
tRTP = 12
tCCD_S = 4
tCCD_L = 8

[power]
VDD = 1.2
IDD0 = 75
IDD2P = 25
IDD2N = 37
IDD3P = 47
IDD3N = 52
IDD4R = 190
IDD4W = 180
IDD5 = 250

[system]
channel_size = 16384
channels = 4
bus_width = 64
address_mapping = rochrababgco
queue_structure = PER_BANK
row_buf_policy = STATIC_TIMEOUT
cmd_queue_size = 16
trans_queue_size = 32

[other]
epoch_period = 10000
output_level = 1
; Static timeout will be set at runtime
static_timeout_cycles = 100
```

---

## Step 3: 数据合成与分析 (Python 脚本)

### 3.1 数据合成脚本

```python
#!/usr/bin/env python3
"""
Oracle Timeout Data Synthesis and Analysis
File: /root/data/smartPRE/scripts/analyze_oracle_sweep.py
"""

import os
import sys
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from pathlib import Path
from collections import defaultdict

# ===== Configuration =====
RESULTS_ROOT = Path("/root/data/smartPRE/results/oracle_sweep")
OUTPUT_DIR = Path("/root/data/smartPRE/results/analysis")
TIMEOUT_VALUES = [10, 20, 50, 100, 150, 200, 300, 400, 500, 600, 800, 1000, 1200, 1600, 2000, 3200]

def load_epoch_data(trace_name: str) -> dict:
    """Load epoch data for all timeout values of a given trace"""
    data = {}
    trace_dir = RESULTS_ROOT / trace_name

    for timeout in TIMEOUT_VALUES:
        epoch_file = trace_dir / f"timeout_{timeout}" / "epoch_stats.csv"
        if epoch_file.exists():
            # Format: epoch_id,pc_hash,rbhr,ipc
            df = pd.read_csv(epoch_file, header=None,
                           names=['epoch_id', 'pc_hash', 'rbhr', 'ipc'])
            data[timeout] = df
            print(f"  Loaded {trace_name}/timeout_{timeout}: {len(df)} epochs")
        else:
            print(f"  WARNING: Missing {epoch_file}")

    return data

def align_epochs(data: dict) -> pd.DataFrame:
    """
    Align epoch data across all timeout values.
    Ensures all files have the same epoch count.
    """
    # Find minimum epoch count across all timeouts
    min_epochs = min(len(df) for df in data.values())
    print(f"  Aligning to {min_epochs} epochs")

    # Create aligned dataframe
    aligned = pd.DataFrame({'epoch_id': range(min_epochs)})

    for timeout, df in data.items():
        df_aligned = df.head(min_epochs).reset_index(drop=True)
        aligned[f'ipc_{timeout}'] = df_aligned['ipc']
        aligned[f'rbhr_{timeout}'] = df_aligned['rbhr']

        # PC hash only needed once (same trace = same PC sequence)
        if 'pc_hash' not in aligned.columns:
            aligned['pc_hash'] = df_aligned['pc_hash']

    return aligned

def find_best_timeout(aligned: pd.DataFrame) -> pd.DataFrame:
    """
    For each epoch, find the timeout value that gives the best IPC.
    """
    ipc_cols = [f'ipc_{t}' for t in TIMEOUT_VALUES]

    # Find best timeout for each epoch
    best_timeout = []
    best_ipc = []

    for idx, row in aligned.iterrows():
        ipcs = {t: row[f'ipc_{t}'] for t in TIMEOUT_VALUES}
        best_t = max(ipcs, key=ipcs.get)
        best_timeout.append(best_t)
        best_ipc.append(ipcs[best_t])

    aligned['best_timeout'] = best_timeout
    aligned['best_ipc'] = best_ipc

    return aligned

def compute_pc_delta(aligned: pd.DataFrame) -> pd.DataFrame:
    """
    Compute PC signature change rate between consecutive epochs.
    Uses Hamming distance approximation via XOR and popcount.
    """
    pc_hashes = aligned['pc_hash'].apply(lambda x: int(x, 16) if isinstance(x, str) else int(x))

    # Compute delta (XOR then count bits)
    pc_delta = []
    for i in range(len(pc_hashes)):
        if i == 0:
            pc_delta.append(0)
        else:
            xor_result = pc_hashes.iloc[i] ^ pc_hashes.iloc[i-1]
            # Count number of different bits (Hamming distance approximation)
            delta = bin(xor_result).count('1')
            pc_delta.append(delta)

    aligned['pc_delta'] = pc_delta

    # Normalize to 0-1 range for visualization
    max_delta = max(pc_delta) if max(pc_delta) > 0 else 1
    aligned['pc_delta_norm'] = aligned['pc_delta'] / max_delta

    return aligned

def plot_oracle_analysis(trace_name: str, aligned: pd.DataFrame, output_dir: Path):
    """
    Generate the target visualization:
    - X-axis: Instruction count (0 to 50M)
    - Y1 (left): PC Signature change rate
    - Y2 (right): Best Timeout value
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Convert epoch to instruction count (each epoch = 10K instructions)
    instructions = aligned['epoch_id'] * 10000  # In actual instructions
    instructions_M = instructions / 1e6  # In millions

    fig, ax1 = plt.subplots(figsize=(14, 6))

    # Plot PC Delta (left Y-axis)
    color1 = 'tab:blue'
    ax1.set_xlabel('Instructions (Millions)', fontsize=12)
    ax1.set_ylabel('PC Signature Change Rate', color=color1, fontsize=12)
    ax1.plot(instructions_M, aligned['pc_delta_norm'], color=color1,
             linewidth=0.8, alpha=0.8, label='PC Delta')
    ax1.tick_params(axis='y', labelcolor=color1)
    ax1.set_ylim(0, 1.1)

    # Plot Best Timeout (right Y-axis)
    ax2 = ax1.twinx()
    color2 = 'tab:red'
    ax2.set_ylabel('Best Timeout (cycles)', color=color2, fontsize=12)
    ax2.step(instructions_M, aligned['best_timeout'], color=color2,
             linewidth=1.2, where='post', label='Best Timeout')
    ax2.tick_params(axis='y', labelcolor=color2)
    ax2.set_ylim(0, max(TIMEOUT_VALUES) * 1.1)

    # Title and grid
    plt.title(f'Oracle Timeout Analysis: {trace_name}', fontsize=14)
    ax1.grid(True, alpha=0.3)

    # Legend
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc='upper right')

    plt.tight_layout()
    plt.savefig(output_dir / f'{trace_name}_oracle_analysis.png', dpi=150)
    plt.savefig(output_dir / f'{trace_name}_oracle_analysis.pdf')
    plt.close()

    print(f"  Saved plot: {output_dir / f'{trace_name}_oracle_analysis.png'}")

def compute_correlation(aligned: pd.DataFrame, trace_name: str) -> dict:
    """
    Compute correlation between PC delta and timeout changes.
    """
    # Compute timeout change
    timeout_change = aligned['best_timeout'].diff().abs().fillna(0)

    # Pearson correlation
    correlation = aligned['pc_delta'].corr(timeout_change)

    # Timeout distribution
    timeout_dist = aligned['best_timeout'].value_counts().sort_index()

    stats = {
        'trace': trace_name,
        'correlation': correlation,
        'timeout_distribution': timeout_dist.to_dict(),
        'mean_ipc': aligned['best_ipc'].mean(),
        'ipc_variance': aligned[[f'ipc_{t}' for t in TIMEOUT_VALUES]].var(axis=1).mean()
    }

    return stats

def save_oracle_data(trace_name: str, aligned: pd.DataFrame, output_dir: Path):
    """Save the oracle timeout sequence for future use."""
    output_dir.mkdir(parents=True, exist_ok=True)

    oracle_df = aligned[['epoch_id', 'pc_hash', 'best_timeout', 'best_ipc', 'pc_delta']]
    oracle_df.to_csv(output_dir / f'{trace_name}_oracle.csv', index=False)
    print(f"  Saved oracle data: {output_dir / f'{trace_name}_oracle.csv'}")

def main():
    print("=" * 60)
    print("Oracle Timeout Data Synthesis and Analysis")
    print("=" * 60)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Get list of traces
    traces = [d.name for d in RESULTS_ROOT.iterdir() if d.is_dir()]
    print(f"\nFound {len(traces)} traces: {traces}\n")

    all_stats = []

    for trace_name in traces:
        print(f"\nProcessing {trace_name}...")

        # Step 1: Load data
        data = load_epoch_data(trace_name)
        if len(data) < 2:
            print(f"  Skipping {trace_name}: insufficient timeout data")
            continue

        # Step 2: Align epochs
        aligned = align_epochs(data)

        # Step 3: Find best timeout per epoch
        aligned = find_best_timeout(aligned)

        # Step 4: Compute PC delta
        aligned = compute_pc_delta(aligned)

        # Step 5: Generate visualization
        plot_oracle_analysis(trace_name, aligned, OUTPUT_DIR)

        # Step 6: Compute statistics
        stats = compute_correlation(aligned, trace_name)
        all_stats.append(stats)
        print(f"  Correlation(PC_delta, Timeout_change): {stats['correlation']:.4f}")

        # Step 7: Save oracle data
        save_oracle_data(trace_name, aligned, OUTPUT_DIR)

    # Summary report
    print("\n" + "=" * 60)
    print("Summary Statistics")
    print("=" * 60)

    summary_df = pd.DataFrame(all_stats)
    print(summary_df[['trace', 'correlation', 'mean_ipc']].to_string())
    summary_df.to_csv(OUTPUT_DIR / 'summary_stats.csv', index=False)

    print(f"\nAll results saved to: {OUTPUT_DIR}")

if __name__ == "__main__":
    main()
```

### 3.2 汉明距离计算说明

```python
def hamming_distance_approx(hash1: int, hash2: int) -> int:
    """
    计算两个 PC Hash 的汉明距离近似值

    原理：
    1. XOR 操作会在两个值不同的位上产生 1
    2. 统计结果中 1 的个数 = 不同位的数量 = 汉明距离

    示例：
    hash1 = 0xABCD = 1010 1011 1100 1101
    hash2 = 0x1234 = 0001 0010 0011 0100
    XOR   = 0xB9F9 = 1011 1001 1111 1001
    popcount(XOR) = 12 (12个位不同)
    """
    return bin(hash1 ^ hash2).count('1')
```

---

## 预期输出

### 文件结构

```
/root/data/smartPRE/results/
├── oracle_sweep/
│   ├── tasks.txt
│   ├── graph500_s16-e10/
│   │   ├── timeout_10/
│   │   │   ├── run.log
│   │   │   └── epoch_stats.csv
│   │   ├── timeout_20/
│   │   │   └── ...
│   │   └── ... (16 timeout directories)
│   ├── ligra_MIS_higgs/
│   │   └── ...
│   └── ... (8 trace directories)
│
└── analysis/
    ├── graph500_s16-e10_oracle_analysis.png
    ├── graph500_s16-e10_oracle_analysis.pdf
    ├── graph500_s16-e10_oracle.csv
    ├── ligra_MIS_higgs_oracle_analysis.png
    ├── ... (每个 trace 一组文件)
    └── summary_stats.csv
```

### epoch_stats.csv 格式

```csv
0,0xABCDEF12,0.4523,0.234567
1,0x12345678,0.4612,0.241234
2,0xDEADBEEF,0.3891,0.198765
...
```

### 可视化示例 (预期图表)

```
          PC Signature Change Rate                Best Timeout (cycles)
    1.0 |     ^                ^               |                      3200
        |    /|\              /|               |
    0.8 |   / | \            / |               |
        |  /  |  \          /  |               |  ___________
    0.6 | /   |   \        /   |               | |           |        1600
        |/    |    \______/    |               | |           |
    0.4 |     |                |               | |           |_____
        |     |                |               |_|                 |   400
    0.2 |     |                |               |                   |
        |     |                |               |                   |   100
    0.0 +-----+--------+-------+-------+-------+-------------------+
        0    10M      20M     30M     40M     50M    Instructions
```

---

## 执行顺序

1. **修改代码** (Step 1) - 添加 epoch 统计输出
2. **重新编译** - `./scripts/build_with_dramsim3.sh`
3. **运行扫描** (Step 2) - `./scripts/run_timeout_sweep.sh`
4. **数据分析** (Step 3) - `python3 ./scripts/analyze_oracle_sweep.py`

---

## 验证检查点

### 编译后验证
```bash
# 运行单个短测试
./bin/champsim -warmup_instructions 1000000 -simulation_instructions 1000000 \
    -loongarch /path/to/trace.xz 2>&1 | grep "\[EPOCH\]"
# 应该看到类似输出:
# [EPOCH] 0,0xABCD1234,0.4521,0.2341
# [EPOCH] 1,0x5678EFGH,0.4612,0.2456
```

### 扫描完成后验证
```bash
# 检查每个 trace 都有 16 个 timeout 目录
for dir in results/oracle_sweep/*/; do
    echo "$dir: $(ls -1 $dir | wc -l) timeout dirs"
done
```

### 分析完成后验证
```bash
# 检查输出文件
ls -la results/analysis/
# 应该有 8 个 _oracle.csv, 8 个 _oracle_analysis.png, 1 个 summary_stats.csv
```
