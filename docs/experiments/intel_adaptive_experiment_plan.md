# Intel Adaptive Page Policy Experiment Plan

## 1. Overview

This document describes the experiment design for implementing the **Intel Adaptive Open Page Policy** on the smartPRE codebase (DRAMSim3 + ChampSim-LA), based on the description in:

> Ghasempour et al., "HAPPY: Hybrid Address-based Page Policy in DRAMs," MEMSYS 2016.

The Intel Adaptive policy was originally deployed in Intel Xeon X5650 processors. It is a **time-based** page closure predictor that adaptively adjusts the timeout duration for closing an open row, using a per-bank Mistake Counter (MC) to learn from prediction errors. This experiment implements the algorithm as described in the paper (Section 3.3, Figure 6), and compares it against existing page policies (CRAFT, GS, open_page, close_page) already implemented in the codebase.

### 1.1 Motivation

| Policy | Approach | Feedback | Granularity | Adjustment |
|--------|----------|----------|-------------|------------|
| **GS** | Fixed timeout per bank | Offline (config) | Per-bank | None (static timeout) |
| **CRAFT** | Adaptive timeout | Per-event (immediate) | Per-bank | Exponential escalation / fixed de-escalation |
| **Intel Adaptive** | Adaptive timeout | Batched via MC | Per-bank | Periodic +-step to TR |
| **ABP (existing)** | Access-count prediction | Per-ACT | Per-row (table) | +-1 to predicted count |

Intel Adaptive occupies a unique design point between GS (static) and CRAFT (aggressive per-event feedback). It batches feedback through a Mistake Counter and adjusts timeout periodically, making it:
- **More stable** than CRAFT (less oscillation from noisy events)
- **More adaptive** than GS (learns from runtime behavior)
- **Lower hardware cost** than ABP (no per-row prediction table; only 2 counters + 1 register per bank)

Comparing Intel Adaptive against CRAFT isolates the value of **feedback granularity** (periodic vs. per-event) and **step function** (linear vs. exponential), which strengthens the argument for CRAFT's design choices.

### 1.2 Algorithm (from HAPPY Paper Section 3.3, Figure 6)

Per-bank hardware:
- **Timeout Counter (TC)**: Countdown timer (cycles). Starts at TR when last command to a row is issued.
- **Timeout Register (TR)**: Current timeout value (cycles). Determines how long a row stays open.
- **Mistake Counter (MC)**: 4-bit saturating counter (0--15). Tracks prediction quality.

```
On last RD/WR to open row (row_hit_count drops to 1):
    TC = TR                          // start countdown
    timeout_ticking = true

Each cycle (while timeout_ticking):
    TC--
    if TC == 0:
        issue PRECHARGE              // timeout-based close
        record prev_row, prev_closed_by_timeout = true

On ACTIVATE (new access to bank):
    if prev_closed_by_timeout:
        if new_row == prev_row:
            MC++                     // wrong precharge: should have kept open
        // Note: if new_row != prev_row, prediction was correct, no update
    prev_closed_by_timeout = false

On CONFLICT (new request during countdown targets different row):
    MC--                             // over-kept: should have closed sooner
    // Force early timeout (TC = 0)

Every CHECK_INTERVAL accesses to this bank:
    if MC > HIGH_THRESHOLD:
        TR = min(TR + TR_STEP, TR_MAX)   // keep rows open longer
    else if MC < LOW_THRESHOLD:
        TR = max(TR - TR_STEP, TR_MIN)   // close rows sooner
    MC = MC_INIT                         // reset MC to midpoint
    access_counter = 0
```

### 1.3 Key Differences from CRAFT

| Aspect | CRAFT | Intel Adaptive |
|--------|-------|----------------|
| Escalation trigger | Wrong precharge (same row reopened) | MC > HIGH_THRESH at check interval |
| De-escalation trigger | Conflict (different row during countdown) | MC < LOW_THRESH at check interval |
| Escalation step | Exponential backoff: `BASE_STEP << min(streak, CAP)` | Linear: `+TR_STEP` |
| De-escalation step | Fixed: `BASE_STEP * tRP/(tRP+tRCD)` | Linear: `-TR_STEP` |
| Feedback timing | Immediate (every event) | Batched (every CHECK_INTERVAL accesses) |
| State per bank | timeout_value + reopen_streak + conflict_streak + right_streak = ~27 bits | MC(4) + TR(12) + TC(12) + access_counter(8) = 36 bits |
| Right precharge feedback | Enhancements: RS, SD | No-op (correct prediction, no update) |

---

## 2. Data Structures

### 2.1 Per-Bank State (`command_queue.h`)

```cpp
// Intel Adaptive page policy per-bank state
struct IntelAdaptiveBankState {
    int timeout_register;       // TR: current timeout value (cycles)
    int mistake_counter;        // MC: 4-bit saturating counter (0-15)
    int access_counter;         // accesses since last MC check
    int prev_row;               // last row closed by timeout
    bool prev_closed_by_timeout; // whether last precharge was timeout-driven
};
```

### 2.2 Constants (`command_queue.h`)

```cpp
// Intel Adaptive page policy constants
static constexpr int INTAP_MC_BITS = 4;                      // MC width
static constexpr int INTAP_MC_MAX = (1 << INTAP_MC_BITS) - 1; // 15
static constexpr int INTAP_MC_INIT = INTAP_MC_MAX / 2;        // 7 (midpoint)
static constexpr int INTAP_HIGH_THRESHOLD = 10;               // MC > 10 => open longer
static constexpr int INTAP_LOW_THRESHOLD = 5;                 // MC < 5 => close sooner
static constexpr int INTAP_CHECK_INTERVAL = 128;              // bank accesses per MC check
static constexpr int INTAP_TR_INIT = 200;                     // initial TR (cycles), same as CRAFT
static constexpr int INTAP_TR_STEP = 50;                      // TR adjustment step (cycles), same as CRAFT BASE_STEP
static constexpr int INTAP_TR_MIN = 50;                       // minimum TR
static constexpr int INTAP_TR_MAX = 3200;                     // maximum TR
```

**Design rationale**: TR range [50, 3200] and initial value 200 match CRAFT defaults for fair comparison. TR_STEP = 50 matches CRAFT_BASE_STEP. The MC thresholds (5/10 out of 15) create a "dead zone" where no adjustment occurs, providing stability.

---

## 3. Implementation Plan

### 3.1 Files to Modify

| File | Changes |
|------|---------|
| `dramsim3/src/common.h` | Add `INTEL_ADAPTIVE` to `RowBufPolicy` enum |
| `dramsim3/src/command_queue.h` | Add `IntelAdaptiveBankState` struct, constants, state vector, method declarations |
| `dramsim3/src/command_queue.cc` | Core logic: initialization, GetCommandToIssue (start TC), AddCommand (conflict handling), INTAP_ProcessACT (right/wrong feedback + periodic check) |
| `dramsim3/src/controller.cc` | Add string-to-enum mapping `"INTEL_ADAPTIVE"`, add timeout countdown logic (same pattern as CRAFT) |
| `dramsim3/src/simple_stats.cc` | Register INTEL_ADAPTIVE-specific counters |
| `dramsim3/configs/*_system.ini` | Add config example with `row_buf_policy = INTEL_ADAPTIVE` |

### 3.2 Code Changes (Detailed)

#### 3.2.1 `common.h` -- Add enum value

```cpp
enum class RowBufPolicy {
    OPEN_PAGE, CLOSE_PAGE, ORACLE, SMART_CLOSE, DPM, GS, GS_NOHOTROW,
    DYMPL, FAPS, RL_PAGE, CRAFT, ABP, INTEL_ADAPTIVE, SIZE
};
```

#### 3.2.2 `command_queue.h` -- State and declarations

Add after CRAFT state definitions:

```cpp
struct IntelAdaptiveBankState {
    int timeout_register = INTAP_TR_INIT;
    int mistake_counter  = INTAP_MC_INIT;
    int access_counter   = 0;
    int prev_row         = -1;
    bool prev_closed_by_timeout = false;
};

// In CommandQueue class:
std::vector<IntelAdaptiveBankState> intap_state_;  // per bank
void INTAP_ProcessACT(int bank_idx, int new_row);
```

#### 3.2.3 `command_queue.cc` -- Core logic

**Initialization** (constructor):
```cpp
if (top_row_buf_policy_ == RowBufPolicy::INTEL_ADAPTIVE) {
    pp = RowBufPolicy::OPEN_PAGE;  // start as open-page with adaptive timeout
}
// ...
intap_state_.resize(num_banks);
```

**GetCommandToIssue** -- Start timeout on last access (row_hit_count == 1):
```cpp
else if (top_row_buf_policy_ == RowBufPolicy::INTEL_ADAPTIVE) {
    auto& istate = intap_state_[queue_idx_];
    controller_->timeout_counter[queue_idx_] = istate.timeout_register;
    controller_->timeout_ticking[queue_idx_] = true;
    controller_->issued_cmd[queue_idx_] = cmd;
}
```

**AddCommand** -- Conflict detection (new request to different row during countdown):
```cpp
else if (top_row_buf_policy_ == RowBufPolicy::INTEL_ADAPTIVE) {
    auto& istate = intap_state_[bank_idx];
    // Conflict: MC-- (should have closed sooner)
    if (istate.mistake_counter > 0) {
        istate.mistake_counter--;
    }
    simple_stats_.Increment("intap_conflicts");
    // Force early timeout
    controller_->timeout_counter[bank_idx] = 0;
}
```

**INTAP_ProcessACT** -- Feedback on ACTIVATE:
```cpp
void CommandQueue::INTAP_ProcessACT(int bank_idx, int new_row) {
    auto& istate = intap_state_[bank_idx];

    // --- Wrong/Right precharge feedback ---
    if (istate.prev_closed_by_timeout) {
        if (new_row == istate.prev_row) {
            // Wrong precharge: should have kept open => MC++
            if (istate.mistake_counter < INTAP_MC_MAX) {
                istate.mistake_counter++;
            }
            simple_stats_.Increment("intap_wrong_precharges");
        } else {
            // Right precharge: correct prediction => no MC update
            simple_stats_.Increment("intap_right_precharges");
        }
        istate.prev_closed_by_timeout = false;
    }

    // --- Periodic TR adjustment ---
    istate.access_counter++;
    if (istate.access_counter >= INTAP_CHECK_INTERVAL) {
        if (istate.mistake_counter > INTAP_HIGH_THRESHOLD) {
            // Too many wrong precharges => open longer
            istate.timeout_register = std::min(istate.timeout_register + INTAP_TR_STEP,
                                                INTAP_TR_MAX);
            simple_stats_.Increment("intap_tr_increments");
        } else if (istate.mistake_counter < INTAP_LOW_THRESHOLD) {
            // Too many conflicts => close sooner
            istate.timeout_register = std::max(istate.timeout_register - INTAP_TR_STEP,
                                                INTAP_TR_MIN);
            simple_stats_.Increment("intap_tr_decrements");
        }
        // Reset MC and access counter
        istate.mistake_counter = INTAP_MC_INIT;
        istate.access_counter = 0;
        simple_stats_.Increment("intap_checks");
    }

    // Record current row for next feedback
    istate.prev_row = new_row;
}
```

#### 3.2.4 `controller.cc` -- String mapping and timeout tick

Add to policy string-to-enum mapping:
```cpp
config.row_buf_policy == "INTEL_ADAPTIVE" ? RowBufPolicy::INTEL_ADAPTIVE :
```

Add timeout tick logic (parallel to existing CRAFT block at lines 194-218):
```cpp
else if (row_buf_policy_ == RowBufPolicy::INTEL_ADAPTIVE) {
    // Same timeout countdown mechanism as CRAFT
    for (int i = 0; i < num_banks; i++) {
        if (cmd_queue_.timeout_ticking[i]) {
            cmd_queue_.timeout_counter[i]--;
            if (cmd_queue_.timeout_counter[i] <= 0) {
                // Issue timeout precharge
                cmd_queue_.timeout_ticking[i] = false;
                auto& istate = cmd_queue_.intap_state_[i];
                istate.prev_closed_by_timeout = true;
                // Issue PRECHARGE command (same as CRAFT pattern)
                ...
            }
        }
    }
}
```

#### 3.2.5 `simple_stats.cc` -- Statistics

```cpp
// Intel Adaptive counters
simple_stats_.InitStat("intap_conflicts", "counter",
                        "Intel Adaptive: conflicts (MC--)");
simple_stats_.InitStat("intap_wrong_precharges", "counter",
                        "Intel Adaptive: wrong precharges (MC++)");
simple_stats_.InitStat("intap_right_precharges", "counter",
                        "Intel Adaptive: right precharges");
simple_stats_.InitStat("intap_tr_increments", "counter",
                        "Intel Adaptive: TR incremented (open longer)");
simple_stats_.InitStat("intap_tr_decrements", "counter",
                        "Intel Adaptive: TR decremented (close sooner)");
simple_stats_.InitStat("intap_checks", "counter",
                        "Intel Adaptive: MC check events");
simple_stats_.InitStat("intap_timeout_precharges", "counter",
                        "Intel Adaptive: total timeout precharges");
// Histogram: TR value distribution over time
simple_stats_.InitStat("intap_tr_value_sum", "histogram",
                        "Intel Adaptive: TR value distribution", 0, INTAP_TR_MAX, 32);
```

---

## 4. Experiment Matrix

### 4.1 Primary Comparison (vs Existing Policies)

| Exp | Label | Policy | Purpose |
|-----|-------|--------|---------|
| E0 | `open_page_1c` | OPEN_PAGE | Static baseline (always open) |
| E1 | `close_page_1c` | CLOSE_PAGE | Static baseline (always close) |
| E2 | `GS_1c` | GS | Fixed timeout baseline |
| E3 | `CRAFT_1c` | CRAFT | Adaptive timeout (per-event feedback) |
| E4 | `CRAFT_ALL_1c` | CRAFT + all enhancements | Best CRAFT variant |
| E5 | `ABP_1c` | ABP | Access-count prediction |
| **E6** | **`INTAP_1c`** | **INTEL_ADAPTIVE** | **This experiment: Intel Adaptive** |

**Total: 1 new build** (E0--E5 should have existing results from prior experiments)

### 4.2 Parameter Sensitivity: MC Thresholds

The dead zone between LOW_THRESHOLD and HIGH_THRESHOLD controls stability vs. responsiveness.

| Exp | Label | LOW_THRESH | HIGH_THRESH | Dead Zone Width | Character |
|-----|-------|------------|-------------|-----------------|-----------|
| E6 | `INTAP_1c` | 5 | 10 | 5 | Balanced (default) |
| S1 | `INTAP_narrow_1c` | 6 | 9 | 3 | Responsive (adjusts more often) |
| S2 | `INTAP_wide_1c` | 3 | 12 | 9 | Stable (adjusts rarely) |
| S3 | `INTAP_noDZ_1c` | 7 | 8 | 1 | Very responsive (almost always adjusts) |

### 4.3 Parameter Sensitivity: Check Interval

How often MC is evaluated affects adaptation speed.

| Exp | Label | CHECK_INTERVAL | Character |
|-----|-------|----------------|-----------|
| E6 | `INTAP_1c` | 128 | Default |
| S4 | `INTAP_ci64_1c` | 64 | Fast adaptation |
| S5 | `INTAP_ci256_1c` | 256 | Slow adaptation |
| S6 | `INTAP_ci512_1c` | 512 | Very slow adaptation |

### 4.4 Parameter Sensitivity: TR Step Size

How aggressively TR changes per adjustment.

| Exp | Label | TR_STEP | Character |
|-----|-------|---------|-----------|
| E6 | `INTAP_1c` | 50 | Default (= CRAFT BASE_STEP) |
| S7 | `INTAP_step25_1c` | 25 | Fine-grained |
| S8 | `INTAP_step100_1c` | 100 | Coarse-grained |

### 4.5 Parameter Sensitivity: Initial TR

| Exp | Label | TR_INIT | Character |
|-----|-------|---------|-----------|
| E6 | `INTAP_1c` | 200 | Default (= CRAFT INIT_TIMEOUT) |
| S9 | `INTAP_init50_1c` | 50 | Start conservative (close quickly) |
| S10 | `INTAP_init800_1c` | 800 | Start aggressive (keep open long) |

**Total sensitivity: 10 configurations (S1--S10)**

---

## 5. Implementation Workflow

### 5.1 Build & Run Procedure

For each experiment configuration:

```bash
# 1. Edit constants in command_queue.h per experiment table
vim /root/data/smartPRE/dramsim3/src/command_queue.h

# 2. Set policy in DRAMSim3 config
#    Edit the [system] section of the active .ini config:
#    row_buf_policy = INTEL_ADAPTIVE
vim /root/data/smartPRE/dramsim3/configs/DDR5_64GB_4ch_4800_system.ini

# 3. Rebuild DRAMSim3
cd /root/data/smartPRE/dramsim3/build && make -j8

# 4. Rebuild ChampSim (links against libdramsim3.so)
cd /root/data/smartPRE/champsim-la && make -j8

# 5. Run benchmarks
export LD_LIBRARY_PATH=/root/data/smartPRE/dramsim3:$LD_LIBRARY_PATH
cd /root/data/smartPRE/champsim-la
TRACE_ROOT=/root/data/Trace/LA scripts/run_selected_slices.sh
# Enter label: <experiment_label>
```

### 5.2 Recommended Execution Order

**Phase 1: Primary comparison (highest priority)**
1. E6 (`INTAP_1c`) -- Default Intel Adaptive, compare vs E0--E5

**Phase 2: Sensitivity on most impactful parameters**
2. S1--S3 (threshold sensitivity) -- Determine optimal dead zone
3. S4--S6 (check interval) -- Determine optimal feedback frequency

**Phase 3: Remaining sensitivity (if Phase 2 reveals interesting behavior)**
4. S7--S8 (TR step) -- Fine-tune adjustment granularity
5. S9--S10 (initial TR) -- Check sensitivity to cold start

---

## 6. Evaluation Metrics

### 6.1 Performance Metrics (per benchmark)

| Metric | Source | Description |
|--------|--------|-------------|
| IPC | ChampSim output | Per-benchmark IPC and improvement vs baselines |
| GEOMEAN speedup | Derived | Geometric mean across all benchmarks |

**Report IPC improvement for EVERY benchmark individually, not just GEOMEAN.**

### 6.2 DRAM Behavior Metrics (per benchmark, from ddr.json)

| Metric | Source | Description |
|--------|--------|-------------|
| `intap_conflicts` | New counter | Conflicts during countdown (MC--) |
| `intap_wrong_precharges` | New counter | Wrong precharges: same row reopened (MC++) |
| `intap_right_precharges` | New counter | Right precharges: different row (no MC update) |
| `intap_tr_increments` | New counter | TR increased at check interval |
| `intap_tr_decrements` | New counter | TR decreased at check interval |
| `intap_checks` | New counter | Total MC check events |
| `intap_timeout_precharges` | New counter | Total timeout precharges |
| `intap_tr_value_sum` | Histogram | TR value distribution (32 bins, 0--3200) |
| `average_read_latency` | Existing | Average DRAM read latency |

### 6.3 Derived Metrics

| Metric | Formula | Purpose |
|--------|---------|---------|
| Wrong rate | `intap_wrong_precharges / intap_timeout_precharges` | Prediction error rate |
| Conflict rate | `intap_conflicts / (intap_conflicts + intap_timeout_precharges)` | How often conflict pre-empts timeout |
| Avg TR | `histo_avg(intap_tr_value_sum)` | Average learned timeout value |
| TR stability | `intap_tr_increments / (intap_tr_increments + intap_tr_decrements)` | Direction bias (>0.5 = tends to open longer) |
| Adaptation speed | `intap_checks / total_bank_accesses` | How many check intervals occurred |
| MC effective rate | `(intap_tr_increments + intap_tr_decrements) / intap_checks` | Fraction of checks that triggered adjustment |

---

## 7. Benchmark Classification and Expected Behavior

### 7.1 Expected Per-Workload Behavior

| Workload Type | Example Benchmarks | Expected Intel Adaptive Behavior | Expected vs CRAFT |
|---------------|-------------------|----------------------------------|-------------------|
| **High locality (streaming)** | lbm, fotonik3d, GemsFDTD | TR drifts to high values; few conflicts; mostly right precharges | Similar: both learn high timeout |
| **Low locality (random)** | mcf, omnetpp, xalancbmk | TR drifts to low values; frequent conflicts; MC stays low | Slower to adapt; CRAFT de-escalates faster per-event |
| **Phase-changing** | PageRank, cactusADM, hashjoin | TR oscillates; periodic MC checks may lag behind phase changes | Worse: batched feedback misses rapid transitions |
| **Moderate locality** | sphinx3, soplex, bzip2 | TR stabilizes at intermediate value | Similar: both converge to reasonable timeout |
| **Mixed R/W** | cactuBSSN, zeusmp | TR settles on average; no R/W differentiation | CRAFT-RW advantage lost (no R/W awareness in Intel Adaptive) |

### 7.2 Theoretical Prediction: Intel Adaptive vs CRAFT

**Where Intel Adaptive may win:**
- Workloads with very noisy but overall stable access patterns (Intel Adaptive's batching filters noise)
- Workloads where CRAFT's exponential backoff overshoots (Intel Adaptive uses linear +-step)

**Where CRAFT should win:**
- Phase-changing workloads (CRAFT reacts immediately; Intel Adaptive needs CHECK_INTERVAL accesses to detect)
- Workloads with strong read/write asymmetry (CRAFT-RW differentiates; Intel Adaptive does not)
- Workloads with bursty conflicts (CRAFT de-escalates per-event; Intel Adaptive accumulates in MC)

**Expected overall**: CRAFT outperforms Intel Adaptive on GEOMEAN, primarily due to faster adaptation. The gap should be most visible on phase-changing and high-contention benchmarks.

---

## 8. Result Analysis Procedure

### 8.1 IPC Comparison Commands

```bash
cd /root/data/smartPRE/champsim-la

# Intel Adaptive vs baselines
python3 scripts/compare_ipc.py --a results/open_page_1c --b results/INTAP_1c \
  --a-label OPEN_PAGE --b-label INTAP --out results/compare_open_vs_INTAP.tsv

python3 scripts/compare_ipc.py --a results/close_page_1c --b results/INTAP_1c \
  --a-label CLOSE_PAGE --b-label INTAP --out results/compare_close_vs_INTAP.tsv

python3 scripts/compare_ipc.py --a results/GS_1c --b results/INTAP_1c \
  --a-label GS --b-label INTAP --out results/compare_GS_vs_INTAP.tsv

# Intel Adaptive vs CRAFT (key comparison)
python3 scripts/compare_ipc.py --a results/INTAP_1c --b results/CRAFT_1c \
  --a-label INTAP --b-label CRAFT --out results/compare_INTAP_vs_CRAFT.tsv

python3 scripts/compare_ipc.py --a results/INTAP_1c --b results/CRAFT_ALL_1c \
  --a-label INTAP --b-label CRAFT_ALL --out results/compare_INTAP_vs_CRAFT_ALL.tsv

# Intel Adaptive vs ABP
python3 scripts/compare_ipc.py --a results/INTAP_1c --b results/ABP_1c \
  --a-label INTAP --b-label ABP --out results/compare_INTAP_vs_ABP.tsv
```

### 8.2 Key Analysis Questions

For the primary experiment (E6):

1. **How does Intel Adaptive compare to open_page / close_page?**
   - If worse than both on some benchmarks => the adaptive mechanism is mislearning
   - Expected: better than both on GEOMEAN (paper reports 5% and 14% better)

2. **How does Intel Adaptive compare to GS?**
   - GS has a well-tuned static timeout; Intel Adaptive should match or exceed
   - If GS wins => Intel Adaptive's periodic check is too slow to converge

3. **How does Intel Adaptive compare to CRAFT?**
   - CRAFT's per-event feedback should be superior on volatile workloads
   - Quantify the gap per benchmark to argue for CRAFT's design choice
   - Key question: is the gap from feedback granularity or step function?

4. **What does the TR distribution look like?**
   - Compare `intap_tr_value_sum` histogram with CRAFT's `craft_timeout_value_sum`
   - Do both policies converge to similar timeout values? If so, the difference is in convergence speed.

5. **How often does MC actually trigger TR adjustment?**
   - `MC effective rate` near 0 => thresholds too extreme, always in dead zone
   - `MC effective rate` near 1 => thresholds too close, always adjusting (no filtering benefit)

For sensitivity experiments:

6. **Does narrowing the dead zone (S1, S3) help or hurt?**
   - If helps: Intel Adaptive is under-reacting with default thresholds
   - If hurts: the filtering/stability is valuable

7. **Does faster check interval (S4) close the gap with CRAFT?**
   - If so: confirms that feedback frequency is the key differentiator
   - If not: the issue is step function (linear vs exponential), not frequency

---

## 9. Result Presentation Format

### 9.1 Main Comparison Table (for paper)

| Benchmark | open_page | close_page | GS | ABP | INTAP | CRAFT | CRAFT_ALL | Category |
|-----------|-----------|------------|-----|-----|-------|-------|-----------|----------|
| benchmark1 | IPC | IPC | IPC | IPC | IPC | IPC | IPC | type |
| ... | | | | | | | | |
| **GEOMEAN** | | | | | | | | |

### 9.2 Intel Adaptive Behavior Table

| Benchmark | TR_avg | Wrong% | Conflict% | TR_inc | TR_dec | MC_eff_rate | IPC vs CRAFT |
|-----------|--------|--------|-----------|--------|--------|-------------|--------------|
| (per benchmark behavior analysis) |

### 9.3 Sensitivity Summary

| Parameter | Value | GEOMEAN IPC | vs Default | Observation |
|-----------|-------|-------------|------------|-------------|
| DZ width=5 (default) | 5/10 | | baseline | |
| DZ width=3 (S1) | 6/9 | | delta | |
| DZ width=9 (S2) | 3/12 | | delta | |
| ... | | | | |

### 9.4 CRAFT vs Intel Adaptive Deep Dive

| Benchmark | INTAP IPC | CRAFT IPC | Delta | INTAP TR_avg | CRAFT timeout_avg | Convergence gap | Explanation |
|-----------|-----------|-----------|-------|-------------|-------------------|----------------|-------------|
| (Focus on benchmarks where the two diverge most) |

---

## 10. Hardware Cost Comparison

| Policy | Storage per bank | Total (32 banks) | Logic complexity |
|--------|-----------------|-------------------|-----------------|
| GS | 12 bits (timeout counter) | 48B | Counter + comparator |
| **Intel Adaptive** | **36 bits** (MC:4 + TR:12 + TC:12 + access_cnt:8) | **144B** | 2 comparators + add/sub |
| CRAFT (base) | ~27 bits (timeout:12 + streak:3 + prev_row:12) | ~108B | Shifter + comparator + MUX |
| CRAFT (all enhancements) | ~33 bits (+conflict_streak:3 + right_streak:3) | ~132B | Additional counters + comparators |
| ABP | 256 entries * 21 bits | ~672B | CAM lookup + LRU |

Intel Adaptive and CRAFT have comparable hardware cost. The key differentiator is design philosophy, not area.

---

## 11. Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| MC always in dead zone (never adjusts) | Medium | Monitor `MC effective rate`; tune thresholds if < 10% |
| TR saturates at TR_MIN or TR_MAX early | Medium | Monitor histogram; increase TR_STEP if stuck |
| CHECK_INTERVAL too long for short traces | Low | Ensure at least 10+ checks per benchmark; reduce interval if needed |
| Conflict pre-empts timeout too often | Low | Track `conflict rate`; high rate means Intel Adaptive behaves like close-page |
| Code integration breaks existing policies | Low | Compile-test all existing policies before and after changes |

---

## 12. Extension: HAPPY Encoding (Optional, Phase 2)

If the basic Intel Adaptive experiment yields interesting results, a follow-up can implement the HAPPY enhancement:

**HAPPY-Intel-Adaptive** (paper Section 3.3, Figure 7):
- Replace per-bank MC+TR with per-address-bit Monitoring Units
- Each Monitoring Unit has its own MC and TR
- The effective timeout for a given address is aggregated from all participating address bits
- This changes the prediction from per-bank to per-address, improving accuracy for diverse workloads

This is a larger implementation effort (new address-bit extraction, aggregation logic) and should only be pursued if the basic Intel Adaptive shows promise compared to CRAFT.

---

## 13. Summary: What This Experiment Answers

| Question | Answered by |
|----------|-------------|
| Is periodic feedback (Intel Adaptive) competitive with per-event feedback (CRAFT)? | E6 vs E3/E4 |
| How much does feedback frequency matter? | S4--S6 (check interval sensitivity) |
| How much does the dead zone / stability matter? | S1--S3 (threshold sensitivity) |
| Is linear step competitive with exponential backoff? | E6 vs E3 (different step functions, similar timeout range) |
| Where does Intel Adaptive fail that CRAFT succeeds? | Per-benchmark analysis of E6 vs E3 |
| Is Intel Adaptive a useful baseline for the CRAFT paper? | Overall comparison across all benchmarks |
