# Smart Strategy - Staged Implementation Plan

This document provides a step-by-step implementation plan with verification at each stage.

---

## Stage 1: Add SMART Policy Enum and Basic Framework

### Goal
Add the SMART policy type and create the basic infrastructure without any actual logic.

### Implementation

**File: `dramsim3/src/common.h`**
```cpp
// Add SMART to RowBufPolicy enum
enum class RowBufPolicy {
    OPEN_PAGE, CLOSE_PAGE, ORACLE, SMART_CLOSE, DPM, GS, STATIC_TIMEOUT,
    SMART,  // <-- Add this
    SIZE
};
```

**File: `dramsim3/src/configuration.cc`**
```cpp
// Add SMART to policy string mapping
else if (row_buf_policy == "SMART") {
    row_buf_policy_ = RowBufPolicy::SMART;
}
```

**File: `dramsim3/src/controller.cc`**
```cpp
// Add placeholder in ClockTick() after GS handling
else if (row_buf_policy_ == RowBufPolicy::SMART) {
    // TODO: Smart policy logic
}
```

### Verification
```bash
# Build the project
cd /root/data/smartPRE/dramsim3
mkdir -p build && cd build
cmake .. && make -j

# Run a simple test with SMART policy (should behave like OPEN_PAGE for now)
# Create a test config with row_buf_policy = SMART
# Run one short trace and verify it completes without errors
```

### Expected Result
- Build succeeds
- Simulation runs without crashes
- Behavior identical to OPEN_PAGE (since no logic added yet)

---

## Stage 2: Per-Bank Reuse Distance Statistics (Online)

### Goal
Adapt existing reuse distance statistics to be per-bank and accessible at runtime.

### Implementation

**File: `dramsim3/src/command_queue.h`**
```cpp
// Add new structure for per-bank reuse distance tracking
struct SmartReuseStats {
    static constexpr int NUM_BINS = 12;
    static constexpr int BIN_BOUNDARIES[NUM_BINS] = {
        16, 48, 112, 240, 496, 1008, 2032, 4080, 8176, 9360, 20000, INT_MAX
    };

    uint64_t bin_counts[NUM_BINS] = {0};
    uint64_t total_samples = 0;

    // Last access timestamp per row (limited history)
    static constexpr size_t MAX_ROW_HISTORY = 64;
    struct RowRecord {
        int row = -1;
        uint64_t timestamp = 0;
    };
    std::array<RowRecord, MAX_ROW_HISTORY> row_history;
    size_t history_head = 0;
    size_t history_count = 0;

    void RecordAccess(int row, uint64_t cycle);
    int GetBinIndex(int distance) const;
    double GetCDF(int distance) const;
    double GetPDF(int distance) const;
};

// In CommandQueue class, add:
std::vector<SmartReuseStats> smart_reuse_stats_;  // per-bank
```

**File: `dramsim3/src/command_queue.cc`**
```cpp
// Implement SmartReuseStats methods
void SmartReuseStats::RecordAccess(int row, uint64_t cycle) {
    // Check history for same row
    for (size_t i = 0; i < history_count; i++) {
        if (row_history[i].row == row) {
            int distance = static_cast<int>(cycle - row_history[i].timestamp);
            int bin = GetBinIndex(distance);
            bin_counts[bin]++;
            total_samples++;
            row_history[i].timestamp = cycle;
            return;
        }
    }

    // New row, add to history
    row_history[history_head] = {row, cycle};
    history_head = (history_head + 1) % MAX_ROW_HISTORY;
    if (history_count < MAX_ROW_HISTORY) history_count++;
}

int SmartReuseStats::GetBinIndex(int distance) const {
    for (int i = 0; i < NUM_BINS; i++) {
        if (distance < BIN_BOUNDARIES[i]) return i;
    }
    return NUM_BINS - 1;
}

double SmartReuseStats::GetCDF(int distance) const {
    if (total_samples == 0) return 0.0;
    uint64_t count = 0;
    int bin = GetBinIndex(distance);
    for (int i = 0; i <= bin; i++) {
        count += bin_counts[i];
    }
    return static_cast<double>(count) / total_samples;
}
```

**Integration point in Controller::AddTransaction or CommandQueue::AddCommand:**
```cpp
// When a new request arrives, record to smart_reuse_stats_
if (row_buf_policy_ == RowBufPolicy::SMART) {
    int bank_idx = GetBankIndex(cmd);
    smart_reuse_stats_[bank_idx].RecordAccess(cmd.Row(), clk_);
}
```

### Verification
```bash
# Run with SMART policy on a single trace
# Add debug output to print reuse_stats at end of simulation
# Compare distribution with existing row_hit_distance.tsv results
```

### Expected Result
- Per-bank reuse distance distributions collected
- Distributions roughly match existing offline statistics

---

## Stage 3: Marginal Analysis Timeout Computation

### Goal
Implement the timeout computation based on marginal analysis: `f(T) × C = 1 - F(T)`

### Implementation

**File: `dramsim3/src/command_queue.h`**
```cpp
// Add to SmartReuseStats or as separate function
int ComputeOptimalTimeout(int C) const;  // C = tRP + tRCD

// Add per-bank timeout state
struct SmartBankState {
    int current_timeout = 100;  // initial default
    uint64_t last_timeout_update_cycle = 0;
};
std::vector<SmartBankState> smart_bank_state_;
```

**File: `dramsim3/src/command_queue.cc`**
```cpp
int SmartReuseStats::ComputeOptimalTimeout(int C) const {
    if (total_samples < 100) {
        return 100;  // Default when insufficient samples
    }

    // Search for T where f(T) * C ≈ 1 - F(T)
    // Use bin midpoints as candidate T values
    for (int i = 0; i < NUM_BINS - 1; i++) {
        int T = (i == 0) ? BIN_BOUNDARIES[i] / 2 :
                (BIN_BOUNDARIES[i-1] + BIN_BOUNDARIES[i]) / 2;

        double F_T = GetCDF(T);
        double f_T = GetPDF(T);  // approximate as bin density

        double marginal_benefit = f_T * C;
        double marginal_cost = 1.0 - F_T;

        if (marginal_benefit <= marginal_cost) {
            return T;
        }
    }
    return BIN_BOUNDARIES[NUM_BINS - 2];  // Max reasonable timeout
}

double SmartReuseStats::GetPDF(int distance) const {
    if (total_samples == 0) return 0.0;
    int bin = GetBinIndex(distance);
    int bin_width = (bin == 0) ? BIN_BOUNDARIES[0] :
                    BIN_BOUNDARIES[bin] - BIN_BOUNDARIES[bin-1];
    return static_cast<double>(bin_counts[bin]) / (total_samples * bin_width);
}
```

**Periodic timeout update (e.g., every 10000 cycles):**
```cpp
void CommandQueue::UpdateSmartTimeouts(uint64_t curr_cycle) {
    static constexpr uint64_t UPDATE_PERIOD = 10000;
    int C = config_.tRP + config_.tRCD;

    for (int i = 0; i < num_queues_; i++) {
        auto& state = smart_bank_state_[i];
        if (curr_cycle - state.last_timeout_update_cycle >= UPDATE_PERIOD) {
            state.current_timeout = smart_reuse_stats_[i].ComputeOptimalTimeout(C);
            state.last_timeout_update_cycle = curr_cycle;
        }
    }
}
```

### Verification
```bash
# Run with debug output showing computed timeout values
# Verify timeout values change over time and vary by bank
# Check that timeouts are reasonable (not 0, not extremely large)
```

### Expected Result
- Timeout values computed per-bank
- Values adapt based on workload characteristics
- Different benchmarks show different timeout distributions

---

## Stage 4: Basic Timeout Timer and Precharge Logic

### Goal
Implement the timeout timer that triggers precharge when expired.

### Implementation

**File: `dramsim3/src/command_queue.h`**
```cpp
// Reuse existing timeout_counter and timeout_ticking vectors
// Add for SMART:
std::vector<int> smart_timeout_open_row_;  // row being waited on
```

**File: `dramsim3/src/controller.cc`**
```cpp
// In ClockTick(), add SMART timeout handling (similar to GS):
else if (row_buf_policy_ == RowBufPolicy::SMART) {
    for (int i = 0; i < cmd_queue_.num_queues_; i++) {
        // Decrement timer
        if (cmd_queue_.timeout_ticking[i] && cmd_queue_.timeout_counter[i] > 0) {
            cmd_queue_.timeout_counter[i]--;
        }

        // Timer expired -> issue precharge
        if (cmd_queue_.timeout_ticking[i] && cmd_queue_.timeout_counter[i] == 0) {
            Command cmd = CreatePrechargeCommand(i);
            if (channel_state_.IsReady(cmd, clk_)) {
                IssueCommand(cmd);
                cmd_queue_.timeout_ticking[i] = false;
            }
        }
    }

    // Update timeouts periodically
    cmd_queue_.UpdateSmartTimeouts(clk_);
}
```

**File: `dramsim3/src/command_queue.cc`**
```cpp
// When last row-hit R/W command is issued, start timer:
void CommandQueue::StartSmartTimeout(int queue_idx, int row, uint64_t cycle) {
    timeout_counter[queue_idx] = smart_bank_state_[queue_idx].current_timeout;
    timeout_ticking[queue_idx] = true;
    smart_timeout_open_row_[queue_idx] = row;
}

// When new row-hit request arrives, reset timer:
void CommandQueue::ResetSmartTimeout(int queue_idx) {
    timeout_counter[queue_idx] = smart_bank_state_[queue_idx].current_timeout;
}

// When row-conflict request arrives, stop timer (will be handled in Stage 5)
```

### Verification
```bash
# Run and check that precharge commands are issued after timeout
# Verify timer resets on row hits
# Compare num_pre_cmds with OPEN_PAGE and GS
```

### Expected Result
- Precharge commands issued based on timeout
- Timer behavior correct (start, reset, expire)
- Performance between OPEN_PAGE and GS

---

## Stage 5: Early Termination Conditions

### Goal
Implement conditions that trigger immediate precharge.

### Implementation

**File: `dramsim3/src/command_queue.h`**
```cpp
struct SmartBankState {
    // ... existing fields ...

    // For early termination
    int queued_conflict_count = 0;
    uint64_t oldest_conflict_arrival = 0;
    int consecutive_timeout_failures = 0;
};
```

**File: `dramsim3/src/command_queue.cc`**
```cpp
bool CommandQueue::ShouldEarlyTerminate(int queue_idx, uint64_t curr_cycle) {
    auto& state = smart_bank_state_[queue_idx];

    // Condition 1: Queue full
    if (queues_[queue_idx].size() >= queue_size_) {
        return true;
    }

    // Condition 2: Too many conflict requests
    if (state.queued_conflict_count >= config_.smart_conflict_count_threshold) {
        return true;
    }

    // Condition 3: Conflict request waited too long
    if (state.oldest_conflict_arrival > 0 &&
        curr_cycle - state.oldest_conflict_arrival >= config_.smart_conflict_wait_threshold) {
        return true;
    }

    return false;
}

// When row-conflict command is added to queue:
void CommandQueue::OnConflictAdded(int queue_idx, uint64_t arrival_cycle) {
    auto& state = smart_bank_state_[queue_idx];
    state.queued_conflict_count++;
    if (state.oldest_conflict_arrival == 0) {
        state.oldest_conflict_arrival = arrival_cycle;
    }
}

// When timeout expires without row hit:
void CommandQueue::OnTimeoutFailure(int queue_idx) {
    auto& state = smart_bank_state_[queue_idx];
    state.consecutive_timeout_failures++;

    // Condition 4: Consecutive failures -> short-term timeout reduction
    if (state.consecutive_timeout_failures >= config_.smart_failure_threshold) {
        state.current_timeout = std::max(20, state.current_timeout / 2);
        state.consecutive_timeout_failures = 0;
    }
}

// On row hit, reset failure counter:
void CommandQueue::OnRowHit(int queue_idx) {
    smart_bank_state_[queue_idx].consecutive_timeout_failures = 0;
}
```

### Verification
```bash
# Add counters for each early termination reason
# Run and verify each condition triggers appropriately
# Check that starvation is prevented (no request waits forever)
```

### Expected Result
- Early precharge triggered by each condition
- No request starvation
- Improved performance on conflict-heavy workloads

---

## Stage 6: Hot Row Tracking

### Goal
Track frequently accessed rows and compute individual timeouts for them.

### Implementation

**File: `dramsim3/src/command_queue.h`**
```cpp
struct HotRowEntry {
    int row = -1;
    int access_count = 0;
    int individual_timeout = 100;
    uint64_t last_access_cycle = 0;
};

struct SmartBankState {
    // ... existing fields ...
    std::vector<HotRowEntry> hot_rows;  // size = hot_row_max_count

    int GetHotRowTimeout(int row) const;
    void UpdateHotRow(int row, uint64_t cycle, int default_timeout);
};
```

**File: `dramsim3/src/command_queue.cc`**
```cpp
void SmartBankState::UpdateHotRow(int row, uint64_t cycle, int default_timeout) {
    // Find existing entry
    for (auto& entry : hot_rows) {
        if (entry.row == row) {
            entry.access_count++;
            // Update individual timeout based on recent intervals
            if (entry.last_access_cycle > 0) {
                int interval = cycle - entry.last_access_cycle;
                // Exponential moving average
                entry.individual_timeout = (entry.individual_timeout * 3 + interval) / 4;
            }
            entry.last_access_cycle = cycle;
            return;
        }
    }

    // Check if qualifies as hot row
    // (would need a separate counter for recent accesses)
    // For now, add if there's space or replace least accessed
    for (auto& entry : hot_rows) {
        if (entry.row == -1) {
            entry = {row, 1, default_timeout, cycle};
            return;
        }
    }

    // Replace entry with lowest count
    auto min_it = std::min_element(hot_rows.begin(), hot_rows.end(),
        [](const HotRowEntry& a, const HotRowEntry& b) {
            return a.access_count < b.access_count;
        });
    if (min_it->access_count < config_.smart_hot_row_freq_threshold / 2) {
        *min_it = {row, 1, default_timeout, cycle};
    }
}

int SmartBankState::GetHotRowTimeout(int row) const {
    for (const auto& entry : hot_rows) {
        if (entry.row == row &&
            entry.access_count >= config_.smart_hot_row_freq_threshold) {
            return entry.individual_timeout;
        }
    }
    return -1;  // Not a hot row
}
```

**Usage in timeout selection:**
```cpp
int CommandQueue::GetSmartTimeout(int queue_idx, int row) {
    auto& state = smart_bank_state_[queue_idx];

    int hot_timeout = state.GetHotRowTimeout(row);
    if (hot_timeout > 0) {
        return hot_timeout;
    }
    return state.current_timeout;
}
```

### Verification
```bash
# Add debug output for hot row table contents
# Run on workloads with known hot rows (e.g., PageRank)
# Verify hot rows are identified and get different timeouts
```

### Expected Result
- Hot rows correctly identified
- Individual timeouts computed for hot rows
- Improved performance on workloads with access locality

---

## Stage 7: Fallback to Open Page

### Goal
Implement conditions that trigger fallback to open page policy.

### Implementation

**File: `dramsim3/src/command_queue.h`**
```cpp
struct SmartBankState {
    // ... existing fields ...

    // Fallback tracking
    bool fallback_active = false;
    uint64_t row_hits = 0;
    uint64_t row_accesses = 0;
    uint64_t queue_occupancy_sum = 0;
    uint64_t queue_samples = 0;

    bool ShouldFallback(const Config& config) const;
    void ResetFallbackStats();
};
```

**File: `dramsim3/src/command_queue.cc`**
```cpp
bool SmartBankState::ShouldFallback(const Config& config, uint64_t total_samples) const {
    // Condition 1: High row hit rate
    if (row_accesses > 100) {
        double hit_rate = static_cast<double>(row_hits) / row_accesses;
        if (hit_rate >= config.smart_hit_rate_fallback) {
            return true;
        }
    }

    // Condition 2: Insufficient samples
    if (total_samples < config.smart_sample_fallback) {
        return true;
    }

    // Condition 3: Low memory pressure
    if (queue_samples > 100) {
        double avg_occupancy = static_cast<double>(queue_occupancy_sum) / queue_samples;
        double occupancy_ratio = avg_occupancy / config.cmd_queue_size;
        if (occupancy_ratio < config.smart_queue_occupancy_fallback) {
            return true;
        }
    }

    return false;
}
```

**Integration:**
```cpp
// In Controller::ClockTick() or timeout handling:
if (row_buf_policy_ == RowBufPolicy::SMART) {
    for (int i = 0; i < cmd_queue_.num_queues_; i++) {
        auto& state = cmd_queue_.smart_bank_state_[i];

        // Check fallback conditions periodically
        state.fallback_active = state.ShouldFallback(config_,
            cmd_queue_.smart_reuse_stats_[i].total_samples);

        if (state.fallback_active) {
            // Behave like OPEN_PAGE: no timeout precharge
            continue;
        }

        // Normal SMART timeout logic...
    }
}
```

### Verification
```bash
# Run on high-locality workload (should trigger hit rate fallback)
# Run short simulation (should trigger sample fallback)
# Run low-pressure workload (should trigger occupancy fallback)
# Verify fallback activates and deactivates correctly
```

### Expected Result
- Fallback triggers under appropriate conditions
- Performance on edge cases matches or exceeds open page
- Smooth transitions between modes

---

## Stage 8: Lookahead Detection in Transaction Buffer

### Goal
Detect row conflicts/hits at transaction buffer level for earlier response.

### Implementation

**File: `dramsim3/src/controller.cc`**
```cpp
// When transaction is added to read_queue_ or write_buffer_:
void Controller::OnTransactionAdded(const Transaction& trans) {
    if (row_buf_policy_ != RowBufPolicy::SMART) return;

    Address addr = config_.AddressMapping(trans.addr);
    int queue_idx = GetQueueIndex(addr);

    // Get currently open row for this bank
    int open_row = channel_state_.GetOpenRow(addr.rank, addr.bankgroup, addr.bank);

    if (open_row == -1) {
        // Bank closed, no action needed
        return;
    }

    if (addr.row == open_row) {
        // Row hit incoming -> reset timeout, prioritize scheduling
        cmd_queue_.ResetSmartTimeout(queue_idx);
        // Optionally: mark for priority scheduling
    } else {
        // Row conflict incoming -> consider early precharge
        if (cmd_queue_.timeout_ticking[queue_idx]) {
            // Trigger early termination check
            if (cmd_queue_.ShouldEarlyTerminate(queue_idx, clk_)) {
                // Issue precharge immediately
                cmd_queue_.timeout_ticking[queue_idx] = false;
                cmd_queue_.timeout_counter[queue_idx] = 0;
            }
        }
    }
}
```

### Verification
```bash
# Add counters for lookahead-triggered actions
# Verify row conflicts trigger early precharge
# Verify row hits reset timeout timer
# Compare latency with and without lookahead
```

### Expected Result
- Earlier response to incoming conflicts
- Reduced average latency for conflict requests
- Slight improvement in overall IPC

---

## Stage 9: Configuration Parameters and Final Integration

### Goal
Add all configuration parameters and finalize integration.

### Implementation

**File: `dramsim3/src/configuration.h`**
```cpp
// Add SMART parameters
int smart_window_size_ = 10000;
int smart_conflict_wait_threshold_ = 100;
int smart_conflict_count_threshold_ = 4;
int smart_failure_threshold_ = 3;
int smart_hot_row_freq_threshold_ = 20;
int smart_hot_row_max_count_ = 8;
double smart_hit_rate_fallback_ = 0.95;
int smart_sample_fallback_ = 100;
double smart_queue_occupancy_fallback_ = 0.20;
```

**File: `dramsim3/src/configuration.cc`**
```cpp
// Parse from config file
smart_window_size_ = reader.GetInteger("system", "smart_window_size", 10000);
// ... similar for all parameters
```

**File: DRAMsim3 config file template**
```ini
[system]
row_buf_policy = SMART
smart_window_size = 10000
smart_conflict_wait_threshold = 100
smart_conflict_count_threshold = 4
smart_failure_threshold = 3
smart_hot_row_freq_threshold = 20
smart_hot_row_max_count = 8
smart_hit_rate_fallback = 0.95
smart_sample_fallback = 100
smart_queue_occupancy_fallback = 0.20
```

### Verification
```bash
# Test with various parameter combinations
# Verify parameters are read correctly
# Run full benchmark suite with default parameters
# Compare against all baselines
```

### Expected Result
- All parameters configurable
- Full integration complete
- Ready for parameter tuning experiments

---

## Summary: Implementation Checklist

| Stage | Description | Files Modified | Verification |
|-------|-------------|----------------|--------------|
| 1 | Add SMART enum | common.h, configuration.cc, controller.cc | Build + run |
| 2 | Per-bank reuse stats | command_queue.h/cc | Compare with offline stats |
| 3 | Timeout computation | command_queue.h/cc | Debug output |
| 4 | Timer + precharge | controller.cc, command_queue.cc | Count precharges |
| 5 | Early termination | command_queue.h/cc | Trigger counters |
| 6 | Hot row tracking | command_queue.h/cc | Hot row debug output |
| 7 | Fallback logic | command_queue.h/cc, controller.cc | Fallback triggers |
| 8 | Lookahead detection | controller.cc | Action counters |
| 9 | Config parameters | configuration.h/cc | Full benchmark run |

Each stage should be implemented, tested, and committed before proceeding to the next.
