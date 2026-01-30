# Smart Row Buffer Management Strategy - Experiment Plan

## 1. Strategy Overview

**Smart** is an adaptive row buffer management strategy that uses runtime statistics to determine optimal timeout values, with multiple safeguards against starvation and fallback mechanisms for edge cases.

### 1.1 Core Mechanisms

#### 1.1.1 Reuse Distance Based Timeout
- Collect per-bank reuse distance distribution (time between accesses to the same row)
- Use marginal analysis to compute optimal timeout T*:
  ```
  f(T) × C = 1 - F(T)
  ```
  Where:
  - f(T): PDF of reuse distance at T
  - F(T): CDF of reuse distance at T
  - C = tRP + tRCD (cycles saved by a row hit)

#### 1.1.2 Hot Row Tracking
- Per-bank tracking of frequently accessed rows
- Hot row identification: access count exceeds threshold within statistics window
- Hot rows get individually computed timeout values

#### 1.1.3 Early Termination Conditions
Immediately precharge when any of the following conditions is met:
1. Command queue is full
2. Number of row conflict requests exceeds threshold
3. Any row conflict request has waited too long
4. Consecutive timeout failures on the same bank → short-term timeout reduction (overrides long-term computed value)

#### 1.1.4 Fallback to Open Page
Switch to open page policy when any condition is met:
1. Row hit rate is extremely high (e.g., >95%)
2. Insufficient statistics samples (e.g., <100 samples)
3. Low memory pressure (e.g., queue occupancy <20%)

#### 1.1.5 Removed Mechanism
- Fixed "4 consecutive row hits trigger precharge" limit is removed
- Starvation protection is handled by early termination conditions 2 and 3

### 1.2 Lookahead Detection
- When a row conflict request enters transaction buffer → trigger early precharge evaluation
- When a row hit request enters transaction buffer → prioritize scheduling to command queue

---

## 2. Implementation Architecture

### 2.1 Code Locations

| Component | File | Description |
|-----------|------|-------------|
| Reuse distance statistics | `dram_system.cc/h` | Existing offline implementation, needs online adaptation |
| Timeout computation | `command_queue.cc/h` | New: marginal analysis logic |
| Hot row tracking | `command_queue.cc/h` | New: per-bank hot row table |
| Early termination | `command_queue.cc/h`, `controller.cc` | New: condition checking |
| Fallback logic | `controller.cc` | New: policy switching |
| Lookahead detection | `controller.cc` | New: transaction buffer monitoring |

### 2.2 Data Structures

```cpp
// Per-bank statistics (in CommandQueue)
struct SmartBankState {
    // Reuse distance histogram (sliding window)
    std::map<int, uint64_t> reuse_distance_hist;
    uint64_t total_samples = 0;
    uint64_t window_start_cycle = 0;

    // Computed timeout
    int current_timeout = 100;  // initial value

    // Hot row table
    struct HotRowEntry {
        int row;
        int access_count;
        int timeout;
    };
    std::vector<HotRowEntry> hot_rows;  // max 8 entries

    // Short-term adaptation
    int consecutive_timeout_failures = 0;
    int short_term_timeout_override = -1;  // -1 means not active

    // Fallback state
    bool fallback_to_open_page = false;

    // Statistics for fallback decision
    uint64_t row_hits = 0;
    uint64_t row_accesses = 0;
    uint64_t queue_occupancy_sum = 0;
    uint64_t queue_sample_count = 0;
};
```

---

## 3. Experiment Design

### 3.1 Parameter Space

| Parameter | Initial | Search Range | Description |
|-----------|---------|--------------|-------------|
| `window_size` | 10000 | 1K, 5K, 10K, 50K, 100K | Statistics window (requests) |
| `conflict_wait_threshold` | 100 | 50, 100, 200, 400 | Max wait cycles for conflict request |
| `conflict_count_threshold` | 4 | 2, 4, 8, 16 | Max queued conflict requests |
| `timeout_failure_threshold` | 3 | 2, 3, 5, 8 | Consecutive failures for short-term adjustment |
| `hot_row_freq_threshold` | 20 | 5, 10, 20, 50, 100 | Access count to be considered hot |
| `hot_row_max_count` | 8 | 4, 8, 16, 32 | Max hot rows per bank |
| `hit_rate_fallback` | 95% | 90%, 95%, 98% | Row hit rate for fallback |
| `sample_fallback` | 100 | 50, 100, 200 | Min samples before using computed timeout |
| `queue_occupancy_fallback` | 20% | 10%, 20%, 30% | Queue occupancy for fallback |

### 3.2 Tuning Phases

**Phase 1: Core timeout mechanism**
1. `window_size`
2. `conflict_wait_threshold`

**Phase 2: Early termination conditions**
3. `conflict_count_threshold`
4. `timeout_failure_threshold`

**Phase 3: Hot row mechanism**
5. `hot_row_freq_threshold`
6. `hot_row_max_count`

**Phase 4: Fallback conditions**
7. `hit_rate_fallback`
8. `sample_fallback`
9. `queue_occupancy_fallback`

### 3.3 Evaluation Metrics

**Primary:**
- IPC (Instructions Per Cycle)

**Secondary:**
- Row hit rate: `num_read_row_hits / num_read_cmds`
- Average read latency: `average_read_latency`
- Bandwidth utilization: `average_bandwidth`
- Precharge count: `num_pre_cmds`

### 3.4 Baselines

| Policy | Description | Results Location |
|--------|-------------|------------------|
| OPEN_PAGE | Keep row open until conflict | `results/open_page_1c/` |
| CLOSE_PAGE | Close row after each access | (to be run if needed) |
| GS | Goodput-sensitive timeout | `results/GS_1c/` |
| SMART_CLOSE | Adaptive close based on hit rate | `results/smart_close_1c/` |
| DPM | Dynamic page management | `results/dpdm_1c/` |
| ORACLE | Perfect knowledge (upper bound) | `results/oracle_1c/` |

### 3.5 Workloads

- 62 benchmarks, 799 trace slices
- Source: `/root/data/smartPRE/champsim-la/benchmarks_selected.tsv`
- Categories: crono, graph500, hashjoin, hpcc, ligra, npb, spec06, spec17, spmv

---

## 4. Experiment Workflow

### 4.1 Implementation Phase
1. Implement smart strategy incrementally (see staged implementation plan)
2. Verify each stage with simple tests

### 4.2 Parameter Tuning Phase
For each phase:
1. Fix other parameters at current best values
2. Run experiments with all candidate values for current parameter(s)
3. Select best value based on geometric mean IPC across benchmarks
4. Proceed to next phase

### 4.3 Final Evaluation Phase
1. Run smart strategy with optimized parameters on all benchmarks
2. Compare against all baselines
3. Analyze results by benchmark category
4. Generate performance comparison charts

---

## 5. Expected Outputs

### 5.1 Per-experiment Outputs
- `ddr.json`: Detailed statistics including all metrics
- `ddr.txt`: Human-readable statistics
- `run.log`: Simulation log

### 5.2 Summary Outputs
- `summary.tsv`: Per-benchmark weighted IPC
- Parameter sensitivity analysis charts
- Comparison charts against baselines

---

## 6. Existing Code to Reuse

### 6.1 Reuse Distance Statistics
Location: `dramsim3/src/dram_system.cc`
- `BankRowHistory`: Per-bank row access history (64 entries)
- `row_hit_distance_histogram_`: Global histogram
- `RecordRowAccess()`: Update history on each access

**Needed modifications:**
- Change from global to per-bank histogram
- Add sliding window support
- Make it accessible for online timeout computation

### 6.2 GS Timeout Framework
Location: `dramsim3/src/command_queue.cc`
- `GSShadowState`: Per-bank state tracking
- `timeout_counter`, `timeout_ticking`: Timer management
- `GS_ArbitrateTimeout()`: Periodic timeout adjustment

**Can reference for:**
- Timer management structure
- Command queue integration points
