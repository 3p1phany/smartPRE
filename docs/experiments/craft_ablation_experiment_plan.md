# CRAFT Enhanced: Ablation Experiment Plan

## 1. Overview

This document describes the ablation experiment design for five CRAFT enhancements implemented simultaneously in DRAMSim3. All five enhancements are controlled by compile-time `constexpr bool` flags in `command_queue.h`, enabling systematic ablation by toggling individual features.

### 1.1 Enhancement Summary

| ID | Name | Flag | Key Mechanism | Code Changes |
|----|------|------|---------------|--------------|
| PR | Phase Reset | `CRAFT_PHASE_RESET_ENABLED` | 3-bit `conflict_streak` counter; fast reset to INIT_TIMEOUT on PHASE_THRESHOLD consecutive conflicts | `command_queue.h` +2 fields, `command_queue.cc` AddCommand +10 lines, ProcessACT +2 lines |
| QDSD | Queue-Depth Scaled De-escalation | `CRAFT_QDSD_ENABLED` | Scale conflict de-escalation step by `min(queue_depth, QDSD_SCALE_CAP=4)` | `command_queue.cc` AddCommand +5 lines |
| RS | Right Streak Gentle De-escalation | `CRAFT_RIGHT_STREAK_ENABLED` | 3-bit `right_streak` counter; gentle de-escalation (half conflict step) on RIGHT_THRESHOLD consecutive right precharges | `command_queue.h` +1 field, `command_queue.cc` ProcessACT +8 lines, AddCommand +1 line |
| RW | Read/Write Cost Differentiation | `CRAFT_RW_ENABLED` | Write wrong precharge: half escalation step; Read conflict: double de-escalation step | `command_queue.cc` ProcessACT +6 lines, AddCommand +5 lines, GetFirstReadyInQueue +5 lines |
| SD | Streak Decay | `CRAFT_STREAK_DECAY_ENABLED` | Decay `reopen_streak` by 1 on each right precharge | `command_queue.cc` ProcessACT +1 line |

### 1.2 Feature Interaction Map

```
                    ┌─ PR (Phase Reset)     - targets: phase change benchmarks
                    │
Conflict path ──────┼─ QDSD (Queue Depth)   - targets: high bank contention benchmarks
                    │
                    └─ RW (Read/Write)      - targets: read-dominant low-locality benchmarks
                         (de-escalation side)

                    ┌─ SD (Streak Decay)    - targets: oscillating locality benchmarks
                    │
Right precharge ────┼─ RS (Right Streak)    - targets: high timeout_high_pct benchmarks
path                │
                    └─ RW (Read/Write)      - targets: write-heavy workloads
                         (escalation side)
```

PR, QDSD, and RW-deesc operate on the conflict path.
SD, RS, and RW-esc operate on the right/wrong precharge path.
The two paths are triggered by different events, so cross-path interactions are indirect (through timeout_value changes).

---

## 2. Ablation Experiment Matrix

### 2.1 Core Ablation (Individual Feature Contribution)

Each experiment enables exactly ONE enhancement over the CRAFT baseline.

| Exp | Label | PR | QDSD | RS | RW | SD | Purpose |
|-----|-------|----|------|----|----|----|---------|
| A0 | `CRAFT_1c` | off | off | off | off | off | Baseline (existing results) |
| A1 | `CRAFT_PR_1c` | **on** | off | off | off | off | Phase Reset only |
| A2 | `CRAFT_QDSD_1c` | off | **on** | off | off | off | QDSD only |
| A3 | `CRAFT_RS_1c` | off | off | **on** | off | off | Right Streak only |
| A4 | `CRAFT_RW_1c` | off | off | off | **on** | off | Read/Write Cost only |
| A5 | `CRAFT_SD_1c` | off | off | off | off | **on** | Streak Decay only |
| A6 | `CRAFT_ALL_1c` | **on** | **on** | **on** | **on** | **on** | All enhancements |

**Total: 7 configurations** (A0 existing, 5 individual + 1 combined = 6 new builds)

### 2.2 Interaction Analysis (Pairwise & Group Combinations)

Based on Section 1.2 feature interaction map, the following subsets test important combinations:

| Exp | Label | PR | QDSD | RS | RW | SD | Purpose |
|-----|-------|----|------|----|----|----|---------|
| B1 | `CRAFT_PR_SD_1c` | on | off | off | off | on | Conflict-path + precharge-path complementarity |
| B2 | `CRAFT_RW_QDSD_1c` | off | on | off | on | off | Two conflict-path enhancements interaction |
| B3 | `CRAFT_RS_SD_1c` | off | off | on | off | on | Two precharge-path enhancements interaction |
| B4 | `CRAFT_CONFLICT_1c` | on | on | off | on | off | All conflict-path enhancements |
| B5 | `CRAFT_PRECHARGE_1c` | off | off | on | on | on | All precharge-path enhancements (RW affects both paths) |

**Total: 5 additional configurations**

### 2.3 Parameter Sensitivity

| Exp | Label | Parameter | Values | Purpose |
|-----|-------|-----------|--------|---------|
| S1 | `CRAFT_PR3_1c` | PHASE_THRESHOLD | 3 | More aggressive phase detection |
| S2 | `CRAFT_PR6_1c` | PHASE_THRESHOLD | 6 | More conservative phase detection |
| S3 | `CRAFT_QDSD_C2_1c` | QDSD_SCALE_CAP | 2 | Conservative queue scaling |
| S4 | `CRAFT_QDSD_C8_1c` | QDSD_SCALE_CAP | 8 | Aggressive queue scaling |
| S5 | `CRAFT_RS3_1c` | RIGHT_THRESHOLD | 3 | More aggressive right streak |
| S6 | `CRAFT_RS6_1c` | RIGHT_THRESHOLD | 6 | More conservative right streak |

**Total: 6 sensitivity configurations**

---

## 3. Implementation Workflow

### 3.1 How to Toggle Features

All feature flags are `static constexpr bool` in `dramsim3/src/command_queue.h`:

```cpp
static constexpr bool CRAFT_PHASE_RESET_ENABLED = true;
static constexpr bool CRAFT_QDSD_ENABLED = true;
static constexpr bool CRAFT_RIGHT_STREAK_ENABLED = true;
static constexpr bool CRAFT_RW_ENABLED = true;
static constexpr bool CRAFT_STREAK_DECAY_ENABLED = true;
```

To run experiment A1 (PR only), set all to `false` except `CRAFT_PHASE_RESET_ENABLED = true`, then rebuild.

### 3.2 Build & Run Procedure

For each experiment configuration:

```bash
# 1. Edit feature flags in command_queue.h per experiment table
vim /root/data/smartPRE/dramsim3/src/command_queue.h

# 2. Rebuild DRAMSim3
cd /root/data/smartPRE/dramsim3/build && make -j8

# 3. Rebuild ChampSim (links against libdramsim3.so)
cd /root/data/smartPRE/champsim-la && make -j8

# 4. Run benchmarks
export LD_LIBRARY_PATH=/root/data/smartPRE/dramsim3:$LD_LIBRARY_PATH
cd /root/data/smartPRE/champsim-la
TRACE_ROOT=/root/data/Trace/LA scripts/run_selected_slices.sh
# Enter label: <experiment_label>
```

### 3.3 Recommended Execution Order

**Phase 1: Core ablation (highest priority)**
1. A6 (`CRAFT_ALL_1c`) - Full enhancement, check if overall improvement exists
2. A1-A5 (individual features) - Identify which features contribute most

**Phase 2: Interaction analysis (if Phase 1 shows promise)**
3. B1-B5 - Understand feature interactions

**Phase 3: Sensitivity (if specific features show significant impact)**
4. S1-S6 - Optimize parameters for the most impactful features

---

## 4. Evaluation Metrics

### 4.1 Performance Metrics (per benchmark)

| Metric | Source | Description |
|--------|--------|-------------|
| IPC | ChampSim output | Per-benchmark IPC and improvement vs baseline |
| GEOMEAN speedup | Derived | Geometric mean across all benchmarks |

**Report IPC improvement for EVERY benchmark individually, not just GEOMEAN.**

### 4.2 DRAM Behavior Metrics (per benchmark, from ddr.json)

| Metric | Source | Description |
|--------|--------|-------------|
| `craft_phase_resets` | New counter | [PR] Phase reset trigger count |
| `craft_qdsd_scale_dist` | New vec counter | [QDSD] Scale factor distribution (0-4) |
| `craft_gentle_deescalations` | New counter | [RS] Gentle de-escalation trigger count |
| `craft_wrong_read` / `craft_wrong_write` | New counters | [RW] Read/write wrong precharge split |
| `craft_conflict_read` / `craft_conflict_write` | New counters | [RW] Read/write conflict split |
| `craft_reopen_streak_dist` | Existing vec counter | [SD] Streak distribution shift |
| `craft_timeout_value_sum` | Existing histogram | Average timeout value |
| `craft_conflicts` | Existing counter | Total conflicts |
| `craft_escalations` | Existing counter | Total escalations |
| `craft_deescalations` | Existing counter | Total de-escalations |
| `craft_timeout_wrong` | Existing counter | Total wrong precharges |
| `craft_timeout_correct` | Existing counter | Total right precharges |
| `average_read_latency` | Existing calculated | Average DRAM read latency |

### 4.3 Derived Metrics

| Metric | Formula | Purpose |
|--------|---------|---------|
| Phase reset rate | `craft_phase_resets / craft_conflicts` | [PR] How often phase change is detected |
| QDSD avg scale | `weighted_avg(craft_qdsd_scale_dist)` | [QDSD] Average queue contention level |
| Gentle deesc rate | `craft_gentle_deescalations / craft_timeout_correct` | [RS] Right streak trigger rate |
| R/W wrong ratio | `craft_wrong_read / craft_wrong_write` | [RW] Read vs write wrong precharge balance |
| Avg timeout | `histo_avg(craft_timeout_value_sum)` | Average timeout value |
| Timeout high pct | `craft_timeout_value_sum[high bins] / total` | Fraction of timeout values near T_MAX |
| Streak shift | `delta(craft_reopen_streak_dist)` | [SD] How much streak distribution changed |

---

## 5. Benchmark Classification and Expected Impact

### 5.1 Per-Feature Target Benchmarks

| Feature | Primary targets | Expected impact | Low/no impact |
|---------|----------------|-----------------|---------------|
| **PR** | PageRank (higgs, soc-pokec), hashjoin, cactusADM | Phase change benchmarks: +0.1%~+1.0% | Steady-state: lbm, RandAcc |
| **QDSD** | mcf, omnetpp, PageRank (graph algo with high contention) | High bank contention: +0.1%~+0.5% | Low contention: fotonik3d, sphinx3 |
| **RS** | BFS-Bitvector/soc-pokec, Triangle/roadNet, npb/CG | High timeout_high_pct (>50%): +0.0%~+0.3% | Low timeout_high_pct: PageRank/higgs |
| **RW** | mcf, omnetpp, xalancbmk (read-dominant low locality) | Read-heavy + low locality: +0.1%~+0.5% | High locality: lbm, fotonik3d |
| **SD** | cactusADM, zeusmp, cactuBSSN, GemsFDTD, CF | Oscillating locality: +0.05%~+0.3% | Phase change or steady-state |

### 5.2 Expected Combined Impact (A6 = ALL)

| Comparison | Expected GEOMEAN | Reasoning |
|------------|-----------------|-----------|
| CRAFT_ALL vs CRAFT | +0.2% ~ +1.0% | Sum of individual improvements, minus overlap |
| CRAFT_ALL vs GS | -0.1% ~ +0.3% | Close gap or surpass GS (original: -0.28%) |
| CRAFT_ALL vs open_page | +1.3% ~ +2.0% | Maintain advantage over open_page |

---

## 6. Result Analysis Procedure

### 6.1 IPC Comparison Commands

```bash
cd /root/data/smartPRE/champsim-la

# A0 vs A6: Overall improvement
python3 scripts/compare_ipc.py --a results/CRAFT_1c --b results/CRAFT_ALL_1c \
  --a-label CRAFT --b-label CRAFT_ALL --out results/compare_CRAFT_vs_CRAFT_ALL.tsv

# Individual feature contribution (example for PR)
python3 scripts/compare_ipc.py --a results/CRAFT_1c --b results/CRAFT_PR_1c \
  --a-label CRAFT --b-label CRAFT_PR --out results/compare_CRAFT_vs_CRAFT_PR.tsv

# Against GS baseline
python3 scripts/compare_ipc.py --a results/GS_1c --b results/CRAFT_ALL_1c \
  --a-label GS --b-label CRAFT_ALL --out results/compare_GS_vs_CRAFT_ALL.tsv

# Against open_page baseline
python3 scripts/compare_ipc.py --a results/open_page_1c --b results/CRAFT_ALL_1c \
  --a-label OPEN_PAGE --b-label CRAFT_ALL --out results/compare_open_vs_CRAFT_ALL.tsv
```

### 6.2 Behavior Analysis

```bash
# CRAFT behavior table for each experiment
for exp in PR QDSD RS RW SD ALL; do
  python3 scripts/craft_behavior_table.py \
    --craft-dir results/CRAFT_${exp}_1c \
    --baseline-dir results/CRAFT_1c \
    --manifest benchmarks_selected.tsv \
    --out results/craft_${exp}_behavior.tsv
done
```

### 6.3 Key Analysis Questions

For each experiment, answer:

1. **Did the feature trigger?** Check feature-specific counters (phase_resets, qdsd_scale_dist, gentle_deescalations, wrong_read/write)
2. **Did it trigger on the right benchmarks?** Compare trigger counts with the expected target benchmarks in Section 5.1
3. **Did IPC improve on target benchmarks?** Check per-benchmark IPC delta
4. **Did any non-target benchmark degrade?** Check for unexpected IPC drops
5. **Is the average timeout more reasonable?** Compare craft_timeout_value_sum histograms

---

## 7. Result Presentation Format

### 7.1 Main Ablation Table (for paper)

| Benchmark | CRAFT | +PR | +QDSD | +RS | +RW | +SD | ALL | Category |
|-----------|-------|-----|-------|-----|-----|-----|-----|----------|
| (IPC improvement vs CRAFT baseline for each) |

### 7.2 Feature Contribution Summary

| Feature | Target benchmarks improved | Non-target degraded | Net GEOMEAN impact |
|---------|--------------------------|--------------------|--------------------|
| PR | | | |
| QDSD | | | |
| RS | | | |
| RW | | | |
| SD | | | |
| ALL | | | |

### 7.3 Interaction Analysis Table

| Benchmark | PR alone | SD alone | PR+SD | Expected (PR+SD) | Interaction |
|-----------|----------|----------|-------|-------------------|-------------|
| (Check if combined effect > sum of individual effects → synergy; < sum → overlap) |

### 7.4 Comparison vs State-of-the-Art

| Benchmark | open_page | GS | CRAFT | CRAFT_ALL | Category |
|-----------|-----------|-----|-------|-----------|----------|
| (Absolute IPC for each, with improvement % in parentheses) |

---

## 8. Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| QDSD + RW double-amplification on read conflicts | Medium | QDSD*RW could make step too large; monitor timeout_value hitting T_MIN frequently |
| PR reset competing with QDSD/RW de-escalation | Low | PR fires before QDSD/RW (goto skips normal deesc); no double-counting |
| RS gentle deesc + SD streak decay + RW write-half-step triple interaction | Low | All three are mild; worst case = slightly lower timeout than optimal |
| Compiler dead-code eliminates disabled features | None | `constexpr bool` ensures zero overhead when disabled |

---

## 9. Hardware Cost Summary

| Feature | Additional storage per bank | Additional logic |
|---------|---------------------------|-----------------|
| PR | 3 bits (conflict_streak) | Saturating counter + comparator |
| QDSD | 0 (uses existing queue occupancy) | 2-bit shift-add multiplier |
| RS | 3 bits (right_streak) | Saturating counter + comparator |
| RW | 0 (uses existing cmd_type) | 1 MUX + 1 shifter |
| SD | 0 (modifies existing reopen_streak update) | 1 saturating decrementer |
| **Total** | **6 bits/bank** | Minimal combinational logic |

For 32 banks: 6 bits * 32 = 24 bytes/channel additional storage.
Total CRAFT overhead: ~180B (base) + 24B (enhancements) = **204B/channel**.
Compare: GS = 1,042B, DYMPL = 3,390B, RL_PAGE = 4,140B.
