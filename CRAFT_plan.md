# CRAFT (Cost-Aware Feedback-driven Adaptive Timeout) Row Buffer Management Strategy

## Goal Description

Implement CRAFT, a new per-bank adaptive row buffer management policy in DRAMSim3, within the smartPRE simulation platform. CRAFT uses three event-driven feedback signals (wrong precharge, conflict, right precharge) to continuously adjust per-bank timeout values in the range [50, 3200] cycles. Escalation uses exponential backoff via a reopen streak counter (replacing GS's RE Store), while de-escalation uses a fixed cost-aware step on conflict events. The implementation modifies 6 files in `dramsim3/src/`, reuses existing timeout infrastructure (`timeout_counter`, `timeout_ticking`), and adds CRAFT-specific per-bank state and statistics counters. The policy must be selectable via the `row_buf_policy = CRAFT` configuration string and produce correct adaptive behavior when simulated with ChampSim-LA traces.

## Acceptance Criteria

Following TDD philosophy, each criterion includes positive and negative tests for deterministic verification.

- AC-1: CRAFT policy enum and configuration parsing
  - Positive Tests (expected to PASS):
    - Setting `row_buf_policy = CRAFT` in a DRAM config `.ini` file results in `RowBufPolicy::CRAFT` being selected at runtime
    - The `CRAFT` enum value exists in `RowBufPolicy` in `common.h`
  - Negative Tests (expected to FAIL):
    - Setting `row_buf_policy = CRAFT_INVALID` does not select CRAFT (falls through to default OPEN_PAGE)
    - Compiling without the CRAFT enum value causes a build error if any code references `RowBufPolicy::CRAFT`

- AC-2: CRAFT per-bank state structure and initialization
  - Positive Tests (expected to PASS):
    - `CraftBankState` struct contains fields: `timeout_value` (int), `reopen_streak` (int), `prev_row` (int), `prev_closed_by_timeout` (bool)
    - `craft_state_` vector in `CommandQueue` is sized to `num_queues_` and initialized with `timeout_value = CRAFT_INIT_TIMEOUT (200)`, `reopen_streak = 0`, `prev_row = -1`, `prev_closed_by_timeout = false`
    - Constants defined: `CRAFT_T_MIN=50`, `CRAFT_T_MAX=3200`, `CRAFT_BASE_STEP=50`, `CRAFT_INIT_TIMEOUT=200`
  - Negative Tests (expected to FAIL):
    - Accessing `craft_state_[i]` with `i >= num_queues_` is out of bounds
    - Initializing `timeout_value` to a value outside [T_MIN, T_MAX] is invalid

- AC-3: CRAFT escalation on wrong precharge (ACT event processing)
  - Positive Tests (expected to PASS):
    - When ACT arrives for `new_row == prev_row` and `prev_closed_by_timeout == true`: `timeout_value` increases by `BASE_STEP << min(reopen_streak, 5)`, `reopen_streak` increments (saturating at 7), `prev_closed_by_timeout` resets to false
    - Consecutive wrong precharges produce exponentially growing steps: +50, +100, +200, +400, +800, +1600
    - `timeout_value` is clamped at `T_MAX (3200)` and never exceeds it
    - `reopen_streak` saturates at 7 and does not overflow
    - Stat counter `craft_timeout_wrong` and `craft_escalations` are incremented
  - Negative Tests (expected to FAIL):
    - When `prev_closed_by_timeout == false`, no escalation occurs regardless of row match
    - When ACT arrives for `new_row != prev_row` and `prev_closed_by_timeout == true` (right precharge), no escalation occurs — only `craft_timeout_correct` stat increments
  - AC-3.1: Right precharge does NOT trigger de-escalation
    - Positive: Right precharge (different row after timeout close) only increments `craft_timeout_correct`, leaves `timeout_value` and `reopen_streak` unchanged
    - Negative: Right precharge must not decrease `timeout_value`

- AC-4: CRAFT de-escalation on conflict (AddCommand event processing)
  - Positive Tests (expected to PASS):
    - When a command arrives with `cmd.Row() != open_row` while `timeout_ticking == true`: `timeout_value` decreases by `CONFLICT_STEP`, `reopen_streak` resets to 0, `timeout_counter` is set to 0 (triggering immediate precharge)
    - `CONFLICT_STEP` is computed as `BASE_STEP * tRP / (tRP + tRCD)` from the DRAM configuration (25 for DDR5-4800 with tRP=tRCD=40)
    - `timeout_value` is clamped at `T_MIN (50)` and never goes below it
    - Stat counters `craft_conflicts` and `craft_deescalations` are incremented
  - Negative Tests (expected to FAIL):
    - When `timeout_ticking == false`, no de-escalation occurs on a different-row command
    - When `cmd.Row() == open_row` (row hit during timeout), de-escalation does NOT occur — instead the timer is reset

- AC-5: Row hit handling during timeout
  - Positive Tests (expected to PASS):
    - When a command arrives with `cmd.Row() == open_row` while `timeout_ticking == true`: `timeout_counter` is reset to `craft_state_[bank].timeout_value`, `timeout_ticking` is set to false
    - The row remains open for the new request
  - Negative Tests (expected to FAIL):
    - A row hit during timeout must not modify `timeout_value` or `reopen_streak`

- AC-6: Timeout expiry and precharge issuance in controller
  - Positive Tests (expected to PASS):
    - When `timeout_counter` reaches 0 and timing allows precharge: PRECHARGE command is issued, `craft_state_[bank].prev_closed_by_timeout` is set to true, `craft_state_[bank].prev_row` records the closed row, `timeout_ticking` is set to false
    - Stat counter `craft_timeout_precharges` is incremented
    - When timing does not allow precharge (bank timing constraint), the precharge is deferred (counter stays at 0, ticking remains true)
  - Negative Tests (expected to FAIL):
    - No RE Store lookup occurs for CRAFT (unlike GS) — the precharge path must not reference row exclusion logic
    - Timeout precharge must not fire when `timeout_ticking == false`

- AC-7: Timeout startup on bank queue empty
  - Positive Tests (expected to PASS):
    - When the last command in a bank queue is issued (queue becomes empty): `timeout_ticking[bank]` is set to true, `timeout_counter[bank]` is set to `craft_state_[bank].timeout_value`
    - `GetCurrentTimeout()` returns the current CRAFT per-bank timeout value when policy is CRAFT
  - Negative Tests (expected to FAIL):
    - Timeout must not start while the bank queue still has pending commands

- AC-8: Statistics counters registration and output
  - Positive Tests (expected to PASS):
    - The following counters are registered in `simple_stats.cc`: `craft_timeout_precharges`, `craft_timeout_wrong`, `craft_timeout_correct`, `craft_conflicts`, `craft_escalations`, `craft_deescalations`, `craft_timeout_value_sum`
    - Vector counter `craft_reopen_streak_dist` with length 8 is registered
    - All counters appear in simulation output files after a CRAFT-policy run
  - Negative Tests (expected to FAIL):
    - CRAFT counters are not incremented when running under a different policy (e.g., OPEN_PAGE, GS)

- AC-9: Build and smoke test
  - Positive Tests (expected to PASS):
    - DRAMSim3 compiles without errors with CRAFT code added (`make -j8` in `dramsim3/build/`)
    - ChampSim-LA compiles and links against the updated `libdramsim3.so`
    - A short simulation (warmup + 1M instructions) with `row_buf_policy = CRAFT` completes without crash or assertion failure
  - Negative Tests (expected to FAIL):
    - A simulation with CRAFT policy must not produce zero `craft_timeout_precharges` (the timeout mechanism must be active)

- AC-10: Behavioral verification (directional sanity checks)
  - Positive Tests (expected to PASS):
    - For a workload with high row locality (e.g., lbm-like): average `timeout_value` across banks trends upward from the initial 200, driven by reopen streak escalation
    - For a workload with low row locality (e.g., hpcc-like): average `timeout_value` across banks trends downward toward T_MIN, driven by frequent conflicts
    - For mixed workloads: different banks exhibit different `timeout_value`s (not all converge to the same value)
  - Negative Tests (expected to FAIL):
    - `timeout_value` must never be observed outside [T_MIN, T_MAX] = [50, 3200]
    - `reopen_streak` must never be observed outside [0, 7]

## Path Boundaries

Path boundaries define the acceptable range of implementation quality and choices.

### Upper Bound (Maximum Acceptable Scope)

The implementation includes all CRAFT mechanism logic (escalation, de-escalation, timeout expiry, timeout startup, row hit handling), the `CraftBankState` structure, all 8 statistics counters (including the vector distribution), `CONFLICT_STEP` auto-computed from DRAM timing parameters, a CRAFT-specific DDR5 config `.ini` file for easy testing, and verified correct behavior on at least two representative benchmarks (one high-locality, one low-locality).

### Lower Bound (Minimum Acceptable Scope)

The implementation includes the `CRAFT` enum value, `CraftBankState` structure, the three core event handlers (escalation on ACT, de-escalation on conflict, timeout expiry precharge), timeout startup, configuration parsing, and at least the scalar statistics counters. The implementation compiles, links, and runs without crash on a short trace.

### Allowed Choices

The design is highly deterministic as specified in the draft. The following choices are fixed:

- **Timeout range**: [50, 3200] — fixed per draft
- **Escalation formula**: `BASE_STEP << min(reopen_streak, 5)` — fixed per draft
- **De-escalation formula**: `timeout_value - CONFLICT_STEP` — fixed per draft
- **CONFLICT_STEP computation**: `BASE_STEP * tRP / (tRP + tRCD)` — fixed per draft
- **Initial timeout**: 200 cycles — fixed per draft
- **Reopen streak**: 3-bit saturating counter (0-7) — fixed per draft
- **Right precharge**: No action (no de-escalation) — fixed per draft

Implementation-level choices that are flexible:
- Can use: integer arithmetic for CONFLICT_STEP computation (truncating division is acceptable)
- Can use: existing `GetCurrentTimeout()` dispatch pattern or direct access to `craft_state_`
- Can place: `CraftBankState` struct definition in `command_queue.h` (following GS pattern) or in a separate header
- Cannot use: floating-point arithmetic in the hot path (CRAFT must be hardware-realistic)
- Cannot use: shadow simulation, epoch-based arbitration, or RE Store — these are GS mechanisms that CRAFT explicitly replaces

## Feasibility Hints and Suggestions

> **Note**: This section is for reference and understanding only. These are conceptual suggestions, not prescriptive requirements.

### Conceptual Approach

CRAFT follows the same structural pattern as the existing GS implementation but with dramatically simpler logic:

1. **Data structures**: Add `CraftBankState` struct (4 fields) and `craft_state_` vector to `CommandQueue`, mirroring `GSShadowState` / `gs_shadow_state_` pattern
2. **ACT hook**: Add `CRAFT_ProcessACT()` call alongside existing `GS_ProcessACT()` in `GetFirstReadyInQueue()` where ACT commands are detected
3. **AddCommand hook**: Add CRAFT branch alongside GS branch for conflict/row-hit handling during timeout
4. **Controller precharge**: Add CRAFT branch in the timeout expiry loop in `Controller::ClockTick()`, simpler than GS (no RE Store check)
5. **Timeout startup**: Extend `GetCurrentTimeout()` to return `craft_state_[queue_idx].timeout_value` for CRAFT policy
6. **Config**: Add `"CRAFT"` string case to the policy parsing chain in `Controller` constructor

The CONFLICT_STEP can be computed once during `CommandQueue` construction from `config_.tRP` and `config_.tRCD`.

### Relevant References

- `dramsim3/src/common.h` — `RowBufPolicy` enum definition (add CRAFT here)
- `dramsim3/src/command_queue.h` — `GSShadowState` struct and `gs_shadow_state_` vector (pattern to follow for `CraftBankState`)
- `dramsim3/src/command_queue.cc` — `GS_ProcessACT()` (pattern for `CRAFT_ProcessACT()`), `AddCommand()` conflict/hit handling, `GetCommandToIssue()` timeout startup, `GetFirstReadyInQueue()` ACT detection
- `dramsim3/src/controller.cc` — `ClockTick()` timeout countdown and precharge issuance loop, `Controller` constructor policy string parsing
- `dramsim3/src/configuration.cc` — DRAM timing parameter access (`tRP`, `tRCD`)
- `dramsim3/src/simple_stats.h` / `simple_stats.cc` — `InitStat()` and `InitVecStat()` for counter registration

## Dependencies and Sequence

### Milestones

1. **Foundation**: Enum, data structures, and configuration
   - Add `CRAFT` to `RowBufPolicy` enum in `common.h`
   - Define `CraftBankState` struct and constants in `command_queue.h`
   - Add `craft_state_` vector to `CommandQueue`, initialize in constructor
   - Add `"CRAFT"` parsing in `Controller` constructor
   - Compute and store `CRAFT_CONFLICT_STEP` from timing parameters

2. **Core Mechanism**: Event handlers
   - Implement `CRAFT_ProcessACT()` in `command_queue.cc` (escalation + right precharge detection)
   - Add CRAFT branch in `AddCommand()` for conflict de-escalation and row hit timer reset
   - Add CRAFT branch in `Controller::ClockTick()` for timeout expiry precharge
   - Extend `GetCurrentTimeout()` for CRAFT policy
   - Ensure timeout startup in `GetCommandToIssue()` works with CRAFT (via `GetCurrentTimeout()`)

3. **Instrumentation**: Statistics and verification
   - Register all `craft_*` counters in `simple_stats.cc`
   - Add `Increment()` / `IncrementVec()` calls at each event point
   - Add `craft_timeout_value_sum` accumulation for average timeout computation
   - Create or modify a DDR5 config `.ini` with `row_buf_policy = CRAFT`

4. **Validation**: Build and test
   - Build DRAMSim3, then ChampSim-LA
   - Run smoke test with short trace
   - Verify behavioral correctness on representative workloads
   - Compare IPC against baseline policies

Milestone 1 must complete before Milestone 2 (data structures needed by event handlers). Milestone 3 can partially overlap with Milestone 2 (counter registration is independent of handler logic, but increment calls require the handlers to exist). Milestone 4 depends on all prior milestones.

## Implementation Notes

### Code Style Requirements
- Implementation code and comments must NOT contain plan-specific terminology such as "AC-", "Milestone", "Step", "Phase", or similar workflow markers
- These terms are for plan documentation only, not for the resulting codebase
- Use descriptive, domain-appropriate naming in code instead (e.g., `CRAFT_ProcessACT`, `craft_timeout_precharges`, not "AC3_escalation_handler")
- Follow existing codebase conventions: use `snake_case` for variables, `PascalCase` for types, prefix CRAFT-specific members with `craft_`
- Comments should explain DRAM-domain rationale (e.g., "exponential backoff for hot row protection") not plan references

--- Original Design Draft Start ---

# 全新行缓冲管理策略：Cost-Aware Feedback-driven Adaptive Timeout (CRAFT)

## Context

GS的核心机制（shadow simulation + RE Store + epoch arbitration）在论文中过于显眼，无法避免与GS对比。本方案从零设计一个全新策略，保留"自适应timeout"和"热行保护"的核心目标，但通过完全不同的机制实现：

| 特征 | GS | CRAFT |
|------|-----|--------|
| Timeout选择 | 7个固定候选值的shadow simulation | **连续timeout空间 + 事件驱动反馈调整** |
| 热行保护 | 独立的RE Store (64-entry FIFO CAM) | **per-bank reopen streak计数器，内嵌于timeout调整** |
| 仲裁频率 | 30K周期epoch-based | **每次反馈事件即时更新** |
| 效用函数 | gain = Δhits - Δconflicts (等权) | **非对称步长，cost ratio隐含在escalation/de-escalation速率中** |
| 候选空间 | 离散 {50,100,150,200,300,400,800} | **连续 [T_MIN, T_MAX] = [50, 3200]** |

**论文定位**：与FAPS、DYMPL、RL_PAGE、open-page、close-page对比。无需与GS对比——机制完全不同。

---

## 核心机制设计

### 三个反馈信号

per-bank timeout的调整由三种事件驱动：

| 事件 | 含义 | 检测位置 | 动作 |
|------|------|----------|------|
| **Wrong precharge** | timeout关了行，同行又被打开 → timeout太短 | ACT时检查 prev_closed_by_timeout && same_row | **Escalate**（指数退避） |
| **Conflict** | timeout还没到期，不同行请求到达，打断timeout → timeout太长 | AddCommand时检查 timeout_ticking && diff_row | **De-escalate** |
| **Right precharge** | timeout关了行，不同行被打开 → timeout合适 | ACT时检查 prev_closed_by_timeout && diff_row | **无动作** |

**关键设计决策**：right precharge不触发de-escalation。"timeout正确"只说明timeout够用，不说明timeout过大。**真正的"timeout过大"信号是conflict**——行还开着就被不同行请求打断，说明timeout让行白白占用了bank。

### 1. Escalation：指数退避（替代RE Store）

```
reopen_streak: 3-bit saturating counter per bank

Wrong precharge:
  step = BASE_STEP << min(reopen_streak, 5)
  timeout_value = min(timeout_value + step, T_MAX)
  reopen_streak = min(reopen_streak + 1, 7)

  streak=0: +50    (首次错误，温和)
  streak=1: +100
  streak=2: +200
  streak=3: +400
  streak=4: +800   (连续5次，激进跳跃)
  streak=5: +1600  (接近直接到上限)
```

RE Store的功能被完全吸收：
- RE检测"同行被关了又重开" → reopen_streak做同样的事
- RE保护热行 → 高streak导致大timeout，根本不会触发precharge
- RE的FIFO替换 → streak自然归零（conflict时reset）

### 2. De-escalation：conflict驱动

```
Conflict事件（timeout ticking时不同行请求到达）:
  timeout_value = max(timeout_value - CONFLICT_STEP, T_MIN)
  reopen_streak = 0  // conflict说明当前行不再"长寿"
```

**Cost-aware不对称性**：
- Escalation步长随streak指数增长（miss代价大：tRP + tRCD）
- De-escalation步长固定为CONFLICT_STEP（conflict代价相对小：多等了一些cycle）
- CONFLICT_STEP的选择反映cost ratio：`CONFLICT_STEP = BASE_STEP * tRP / (tRP + tRCD)`
- 对DDR5-4800 (tRP=tRCD=40): CONFLICT_STEP = 50 * 40/80 = 25 cycles

即：**一次conflict减少25c，一次首次wrong增加50c（2:1），连续wrong还会指数增长**。这隐式编码了hit/conflict的成本不对称。

### 3. Conflict时的timeout处理

当conflict发生时（AddCommand中检测到timeout_ticking && diff_row）：
- `timeout_counter = 0` （立即触发precharge，与GS相同——这是基本的DRAM控制逻辑）
- `timeout_value -= CONFLICT_STEP` （CRAFT独有：同时调小未来的timeout）
- `reopen_streak = 0` （conflict说明行模式已变化）

---

## Per-Bank State (硬件开销)

| 字段 | 位宽 | 说明 |
|------|------|------|
| `timeout_value` | 12 bits | 当前timeout值 (范围50-3200) |
| `reopen_streak` | 3 bits | 连续wrong precharge次数 (饱和至7) |
| `prev_row` | 16 bits | 上次timeout关闭的行号 |
| `prev_closed_by_timeout` | 1 bit | timeout关闭标志 |
| `timeout_counter` | 12 bits | 当前倒计时 |
| `timeout_ticking` | 1 bit | 计时中标志 |
| **Per-bank总计** | **45 bits (5.6 B)** | |
| **32 banks总计** | **1440 bits (180 B)** | |

**与其他策略对比**：

| 策略 | Per-Channel存储 | 相对CRAFT |
|------|----------------|-----------|
| **CRAFT** | **180 B** | 1x |
| FAPS | 152 B | 0.84x |
| GS | 1,042 B | 5.8x |
| DYMPL | 3,390 B | 18.8x |
| RL_PAGE | 4,140 B | 23.0x |

---

## 完整伪代码

```
Constants:
  T_MIN = 50
  T_MAX = 3200
  BASE_STEP = 50
  CONFLICT_STEP = BASE_STEP * tRP / (tRP + tRCD)  // 25 for DDR5-4800
  INIT_TIMEOUT = 200

Per-bank state:
  timeout_value = INIT_TIMEOUT
  reopen_streak = 0
  prev_row = -1
  prev_closed_by_timeout = false
  timeout_counter, timeout_ticking

=== Timeout到期 (controller.cc, 每cycle检查) ===
for each bank i:
  if timeout_ticking[i] && timeout_counter[i] > 0:
    timeout_counter[i]--
  if timeout_ticking[i] && timeout_counter[i] == 0:
    if timing_ok_for_precharge:
      record prev_closed_by_timeout = true, prev_row = open_row
      issue PRECHARGE
      timeout_ticking = false
      stat: "craft_timeout_precharges"

=== ACT到达 → Escalation判断 (command_queue.cc) ===
CRAFT_ProcessACT(bank_idx, new_row):
  if prev_closed_by_timeout[bank_idx]:
    if new_row == prev_row[bank_idx]:
      // WRONG: timeout太短 → escalate
      step = BASE_STEP << min(reopen_streak, 5)
      timeout_value = min(timeout_value + step, T_MAX)
      reopen_streak = min(reopen_streak + 1, 7)
      stat: "craft_timeout_wrong", "craft_escalations"
    else:
      // RIGHT: timeout合适，无动作
      stat: "craft_timeout_correct"
    prev_closed_by_timeout = false

=== Conflict到达 → De-escalation (command_queue.cc AddCommand) ===
On AddCommand(cmd) when timeout_ticking[bank] && cmd.Row() != open_row:
  // Conflict: timeout太长 → de-escalate
  timeout_value = max(timeout_value - CONFLICT_STEP, T_MIN)
  reopen_streak = 0
  timeout_counter = 0  // 立即precharge
  stat: "craft_conflicts", "craft_deescalations"

On AddCommand(cmd) when timeout_ticking[bank] && cmd.Row() == open_row:
  // Row hit during timeout → reset timer, continue
  timeout_counter = timeout_value
  timeout_ticking = false

=== Timeout启动 (command_queue.cc, bank队列变空时) ===
On bank queue empty after issuing command:
  timeout_ticking[bank] = true
  timeout_counter[bank] = timeout_value[bank]
```

---

## 论文故事线

1. **Motivation**: 现有自适应行缓冲策略要么用粗粒度的open/close二元切换（FAPS），要么需要昂贵的学习硬件（DYMPL: 3.4KB, RL_PAGE: 4.1KB per channel）。能否用极小的硬件开销实现细粒度的连续timeout自适应？

2. **Key Insight**: Timeout正确性有两个方向的即时反馈信号：wrong precharge说明timeout太短，conflict说明timeout太长。通过非对称的反馈步长（反映DRAM timing的成本比），可以在连续timeout空间中快速收敛到cost-optimal均衡点。

3. **Contributions**:
   - 事件驱动反馈控制：无需shadow simulation或epoch-based arbitration
   - 指数退避热行保护：无需独立的热行保护表
   - 成本非对称步长：自动适配不同DRAM配置的timing参数
   - 仅180B/channel，接近最简单的FAPS，远低于DYMPL/RL_PAGE

4. **对比对象**: open-page, close-page, FAPS, DYMPL, RL_PAGE

---

## Implementation Steps

### Step 1: 新增RowBufPolicy枚举值 (`common.h`)
- 添加 `CRAFT` 到 `RowBufPolicy` enum

### Step 2: 数据结构 (`command_queue.h`)
- 新增常量: `CRAFT_T_MIN=50`, `CRAFT_T_MAX=3200`, `CRAFT_BASE_STEP=50`, `CRAFT_INIT_TIMEOUT=200`
- 新增 `CraftBankState` 结构体:
  ```cpp
  struct CraftBankState {
      int timeout_value = CRAFT_INIT_TIMEOUT;
      int reopen_streak = 0;
      int prev_row = -1;
      bool prev_closed_by_timeout = false;
  };
  ```
- CommandQueue新增: `std::vector<CraftBankState> craft_state_`
- 注：`timeout_counter` 和 `timeout_ticking` 已存在于CommandQueue中，复用

### Step 3: ACT事件处理 (`command_queue.cc`)
- 新增 `CRAFT_ProcessACT(int queue_idx, int new_row)` 函数
- 在现有ACT处理路径中，当policy==CRAFT时调用此函数

### Step 4: Conflict + Row hit处理 (`command_queue.cc` AddCommand)
- 在AddCommand中，当policy==CRAFT时:
  - diff_row + timeout_ticking → de-escalate + timeout_counter=0
  - same_row + timeout_ticking → reset timer

### Step 5: Timeout到期处理 (`controller.cc`)
- 新增CRAFT分支（比GS简单得多：无RE查找，直接precharge并记录状态）

### Step 6: Timeout启动 (`command_queue.cc`)
- bank队列变空后，设timeout_counter = craft_state_[bank].timeout_value

### Step 7: 统计计数器 (`simple_stats.cc`)
- `craft_timeout_precharges`, `craft_timeout_wrong`, `craft_timeout_correct`
- `craft_conflicts`, `craft_escalations`, `craft_deescalations`
- `craft_timeout_value_sum` (用于计算平均timeout)
- `craft_reopen_streak_dist` (vec, len=8)

### Step 8: 配置支持 (`configuration.cc`)
- 解析 `"CRAFT"` 策略字符串
- CONFLICT_STEP 从config的tRP和tRCD计算

---

## Files to Modify

| File | Changes |
|------|---------|
| `dramsim3/src/common.h` | RowBufPolicy新增CRAFT |
| `dramsim3/src/command_queue.h` | CraftBankState, 常量, CommandQueue新增成员和方法 |
| `dramsim3/src/command_queue.cc` | CRAFT_ProcessACT, AddCommand中CRAFT分支, timeout启动 |
| `dramsim3/src/controller.cc` | CRAFT timeout到期分支 |
| `dramsim3/src/configuration.cc` | 解析"CRAFT"策略 |
| `dramsim3/src/simple_stats.cc` | 新增统计计数器 |

---

## Verification

1. **编译**: 构建dramsim3和champsim-la
2. **Smoke test**: 短trace验证CRAFT不crash
3. **行为验证**:
   - lbm: timeout_value应自动增长到>800（通过reopen_streak指数退避）
   - hpcc: timeout_value应保持在50-100（大量conflict驱动de-escalation）
   - 混合workload: 不同bank应有不同的timeout_value
4. **性能对比**: `scripts/compare_ipc.py` 与 open-page, close-page, FAPS, DYMPL, RL_PAGE
5. **统计分析**: escalation/de-escalation频率、reopen_streak分布、per-benchmark平均timeout

--- Original Design Draft End ---
