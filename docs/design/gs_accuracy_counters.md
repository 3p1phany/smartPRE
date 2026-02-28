# GS 预测准确性计数器 - 实现文档

## 概述

本文档描述了为衡量 GS（Global Scoreboarding）行缓冲管理策略预测准确性而添加的性能计数器。这些计数器回答三个核心问题：

1. Timeout precharge 决策是否正确？
2. Row Exclusion Store 保护是否有效？
3. Shadow timeout 仲裁机制的行为如何？

所有计数器均在 SimpleStats 框架中注册，自动以 JSON 和 TXT 两种格式输出到结果文件中，支持 epoch 级别和最终统计两种粒度。

---

## 计数器定义

### A. Timeout Precharge 准确性

| 计数器 | 描述 |
|--------|------|
| `gs_timeout_precharges` | 实际执行的超时触发 precharge 总次数 |
| `gs_timeout_correct` | 超时 precharge 正确：该 bank 的下一次 ACT 目标为**不同**行 |
| `gs_timeout_wrong` | 超时 precharge 错误：该 bank 的下一次 ACT 目标为**相同**行 |
| `gs_timeout_deferred` | 超时到期但因 tRP timing 约束未能立即执行 precharge 的次数 |

**衍生指标：** Timeout 准确率 = `gs_timeout_correct / gs_timeout_precharges`

**不变量：** `gs_timeout_correct + gs_timeout_wrong <= gs_timeout_precharges`

注意：这里是不等式而非等式，因为某些 bank 可能存在未被验证的挂起超时检查（例如仿真结束时，refresh 干扰，或该 bank 上不再有后续 ACT 到达）。

### B. Row Exclusion Store 有效性

| 计数器 | 描述 |
|--------|------|
| `gs_re_insertions` | 插入 RE Store 的条目数量（不含重复项） |
| `gs_re_hits` | RE Store 查找命中并**阻止**了 timeout precharge 的次数 |
| `gs_re_hit_useful` | RE 命中有效：被保护的行确实被再次访问 |
| `gs_re_hit_useless` | RE 命中无效：被保护的行未被再次访问（直到下一次 precharge） |
| `gs_re_hit_cas_served` | RE 命中保护期间，被保护行上服务的 CAS 命令总数 |
| `gs_re_evictions` | 因容量限制（FIFO 淘汰）从 RE Store 中驱逐的条目数量 |

**衍生指标：**
- RE 准确率 = `gs_re_hit_useful / gs_re_hits`
- RE 保护收益 = `gs_re_hit_cas_served`（值越高说明 RE 保护期间利用率越高）

**不变量：** `gs_re_hit_useful + gs_re_hit_useless <= gs_re_hits`

注意：单次 RE 命中事件后，同一 bank 上可能连续发生多次 RE 命中（超时不断被延长）。只有每个 bank 的第一次 RE 命中会设置挂起验证状态；后续命中会递增 `gs_re_hits` 但不会覆盖跟踪状态。

### C. Timeout 仲裁统计

| 计数器 | 描述 |
|--------|------|
| `gs_timeout_switches` | bank 的超时值在仲裁过程中发生切换的次数 |
| `gs_timeout_dist.0` .. `gs_timeout_dist.6` | 每个超时档位在仲裁周期中被选中的次数（索引对应 {50, 100, 150, 200, 300, 400, 800} 周期） |

---

## 实现细节

### 修改的文件

| 文件 | 修改内容 |
|------|----------|
| `dramsim3/src/simple_stats.cc` | 在 `SimpleStats` 构造函数中注册所有 `gs_*` 计数器 |
| `dramsim3/src/command_queue.h` | 在 `RowExclusionDetectState` 结构体中添加跟踪字段 |
| `dramsim3/src/controller.cc` | 在 `ClockTick()` 的 timeout precharge 路径中插桩 |
| `dramsim3/src/command_queue.cc` | 在 `GS_ProcessACT`、`GS_ProcessCAS`、`GS_ArbitrateTimeout`、`RE_AddEntry` 中插桩 |

### SimpleStats 计数器注册

在 `dramsim3/src/simple_stats.cc` 的 `SimpleStats` 构造函数末尾（`average_interarrival` 之后、函数结束之前）添加：

```cpp
// GS accuracy counters (registered for all policies; only incremented under GS/GS_NOHOTROW)
InitStat("gs_timeout_precharges", "counter",
         "GS timeout precharges issued");
InitStat("gs_timeout_correct", "counter",
         "GS timeout precharges verified correct (next ACT targets different row)");
InitStat("gs_timeout_wrong", "counter",
         "GS timeout precharges verified wrong (next ACT targets same row)");
InitStat("gs_timeout_deferred", "counter",
         "GS timeout expired but precharge deferred due to timing constraint");
InitStat("gs_re_insertions", "counter",
         "GS RE store insertions (excluding duplicates)");
InitStat("gs_re_hits", "counter",
         "GS RE store hits (timeout precharge blocked)");
InitStat("gs_re_hit_useful", "counter",
         "GS RE hits verified useful (protected row accessed)");
InitStat("gs_re_hit_useless", "counter",
         "GS RE hits verified useless (protected row not accessed)");
InitStat("gs_re_hit_cas_served", "counter",
         "CAS commands served on RE-protected rows");
InitStat("gs_re_evictions", "counter",
         "GS RE store evictions due to capacity");
InitStat("gs_timeout_switches", "counter",
         "GS timeout value switches during arbitration");
// 长度 7 对应 GS_TIMEOUT_VALUES[] = {50, 100, 150, 200, 300, 400, 800}
// 该常量定义于 command_queue.h 中的 GS_TIMEOUT_COUNT
InitVecStat("gs_timeout_dist", "vec_counter",
            "GS timeout distribution at arbitration", "idx", 7);
```

注意：`simple_stats.cc` 不 include `command_queue.h`，因此 vec 长度 7 需硬编码。
此处通过注释标注其与 `GS_TIMEOUT_COUNT` 的对应关系。

### 每 Bank 跟踪状态

在 `command_queue.h` 中修改 `RowExclusionDetectState`：

```cpp
struct RowExclusionDetectState {
    int prev_row = -1;                   // （已有）上一个打开的行
    bool prev_closed_by_timeout = false;  // （已有）上一行是否被超时关闭
    // === 新增字段 ===
    int timeout_closed_row = -1;          // 被超时关闭的行号，等待验证
    bool pending_timeout_check = false;   // 标志：等待下一次 ACT 来验证超时决策
    bool pending_re_hit_check = false;    // 标志：等待下一次 CAS/ACT 来验证 RE 命中
    int re_hit_row = -1;                  // 被 RE 命中保护的行号，等待验证
};
```

这些是每 bank 的状态（通过 `queue_idx` 索引），与 `re_detect_state_[]` 相同。

### 插桩点与数据流

#### Timeout Precharge 验证流程

```
controller.cc: ClockTick() 超时 precharge 触发
  |
  +--> Increment("gs_timeout_precharges")
  +--> detect.pending_timeout_check = true
  +--> detect.timeout_closed_row = cmd.Row()
  |
  v
command_queue.cc: 同一 bank 上的 GS_ProcessACT()
  |
  +--> if pending_timeout_check:
         new_row == timeout_closed_row? --> Increment("gs_timeout_wrong")
         new_row != timeout_closed_row? --> Increment("gs_timeout_correct")
         清除 pending_timeout_check
```

**在 `controller.cc` 中的位置**（在 `else if (GS/GS_NOHOTROW)` 块内，RE 检查之后、`IssueCommand` 之前）：

```cpp
// GS 准确性：记录 timeout precharge 以待验证
simple_stats_.Increment("gs_timeout_precharges");
cmd_queue_.re_detect_state_[i].pending_timeout_check = true;
cmd_queue_.re_detect_state_[i].timeout_closed_row = cmd.Row();
```

注意：此代码对 GS 和 GS_NOHOTROW 两种策略**都会执行**（位于 `if (row_buf_policy_ == RowBufPolicy::GS)` 块之外）。这是有意为之——timeout precharge 准确性与两种变体都相关。

**在 `command_queue.cc` 中的位置**（`GS_ProcessACT` 顶部，RE 检测之前）：

```cpp
auto& detect = re_detect_state_[queue_idx];
if (detect.pending_timeout_check) {
    if (new_row == detect.timeout_closed_row) {
        simple_stats_.Increment("gs_timeout_wrong");
    } else {
        simple_stats_.Increment("gs_timeout_correct");
    }
    detect.pending_timeout_check = false;
}
```

#### Timeout Deferred 计数

**在 `controller.cc` 中的位置**（在 `else if (GS/GS_NOHOTROW)` 块内，timeout 到期但 timing 不满足时）：

当 `timeout_counter == 0` 但 precharge timing 约束未就绪（`bs.cmd_timing_[PRE] > clk_`）时，precharge 被推迟到下一个 cycle 再尝试。此事件需被记录，因为实际关闭时间可能远晚于预期的 timeout 到期时间。

```cpp
if(cmd_queue_.timeout_ticking[i] && cmd_queue_.timeout_counter[i]==0){
    auto cmd = cmd_queue_.issued_cmd[i];
    cmd.cmd_type=CommandType::PRECHARGE;
    auto& bs=channel_state_.bank_states_[cmd.Rank()][cmd.Bankgroup()][cmd.Bank()];
    if(bs.IsRowOpen() && bs.cmd_timing_[static_cast<int>(CommandType::PRECHARGE)]<=clk_){
        // ... 正常 precharge 路径 ...
    } else if (bs.IsRowOpen()) {
        // Timing 约束未满足，precharge 被推迟
        simple_stats_.Increment("gs_timeout_deferred");
    }
}
```

#### RE 命中记录与验证流程

```
controller.cc: 超时触发，RE_IsInStore() == true
  |
  +--> Increment("gs_re_hits")
  +--> if !pending_re_hit_check:   （只有第一次命中设置跟踪）
         detect.pending_re_hit_check = true
         detect.re_hit_row = cmd.Row()
  +--> 延长超时，继续   （跳过 precharge）
  |
  v  四种可能的结果：
  |
  +--> [路径 1] 同一 bank 上到达 CAS（行仍然打开）
  |    command_queue.cc: GS_ProcessCAS()
  |      Increment("gs_re_hit_useful"), Increment("gs_re_hit_cas_served"), 清除标志
  |
  +--> [路径 2] 同一 bank 上到达 ACT（行因 on-demand precharge 或 refresh 被关闭）
  |    command_queue.cc: GS_ProcessACT()
  |      new_row == re_hit_row? --> Increment("gs_re_hit_useful")
  |      new_row != re_hit_row? --> Increment("gs_re_hit_useless")
  |      清除标志
  |
  +--> [路径 3] 第二次超时触发，RE 此次未命中
  |    controller.cc: ClockTick() timeout precharge 路径，RE 检查失败之后
  |      if pending_re_hit_check:
  |        Increment("gs_re_hit_useless"), 清除标志
  |      然后执行正常的 timeout precharge
  |
  +--> [路径 4] 第二次超时触发，RE 再次命中
       controller.cc: ClockTick() timeout precharge 路径，RE 检查成功
         gs_re_hits 递增（计入新的 RE 命中），但 pending_re_hit_check 保持不变
         （由 if (!pending_re_hit_check) 保护，不覆盖已有跟踪状态）
         超时继续延长
```

**RE 命中记录代码**（`controller.cc`，在 `if (RE_IsInStore(...))` 块内）：

```cpp
if (cmd_queue_.RE_IsInStore(cmd.Rank(), cmd.Bankgroup(), cmd.Bank(), cmd.Row())) {
    // RE 命中计数与跟踪
    simple_stats_.Increment("gs_re_hits");
    auto& detect = cmd_queue_.re_detect_state_[i];
    if (!detect.pending_re_hit_check) {
        detect.pending_re_hit_check = true;
        detect.re_hit_row = cmd.Row();
    }
    cmd_queue_.timeout_counter[i] = cmd_queue_.GetCurrentTimeout(i);
    continue;  // 跳过 precharge
}
```

**路径 1 细节**（`GS_ProcessCAS`）：

```cpp
auto& detect = re_detect_state_[queue_idx];
if (detect.pending_re_hit_check) {
    simple_stats_.Increment("gs_re_hit_useful");
    simple_stats_.Increment("gs_re_hit_cas_served");
    detect.pending_re_hit_check = false;
}
```

注意：`gs_re_hit_useful` 仅在第一次 CAS 时递增并清除标志。`gs_re_hit_cas_served` 需要**持续计数**所有 CAS，因此其递增逻辑应独立于 `pending_re_hit_check`。完整实现如下：

```cpp
auto& detect = re_detect_state_[queue_idx];
// 持续计数 RE 保护期间的 CAS
if (detect.pending_re_hit_check) {
    simple_stats_.Increment("gs_re_hit_cas_served");
}
// 首次 CAS 确认 RE 命中有效
if (detect.pending_re_hit_check) {
    simple_stats_.Increment("gs_re_hit_useful");
    detect.pending_re_hit_check = false;
}
```

简化后等价于：

```cpp
auto& detect = re_detect_state_[queue_idx];
if (detect.pending_re_hit_check) {
    simple_stats_.Increment("gs_re_hit_cas_served");
    simple_stats_.Increment("gs_re_hit_useful");
    detect.pending_re_hit_check = false;
}
```

局限：此写法仅计入第一次 CAS。如需计入保护期间的**所有** CAS，需要额外状态（见"扩展：RE 保护期间完整 CAS 计数"一节）。

**路径 2 细节**（`GS_ProcessACT`，顶部验证区域）：

```cpp
if (detect.pending_re_hit_check) {
    if (new_row == detect.re_hit_row) {
        simple_stats_.Increment("gs_re_hit_useful");
    } else {
        simple_stats_.Increment("gs_re_hit_useless");
    }
    detect.pending_re_hit_check = false;
}
```

**路径 3 细节**（`controller.cc`，在 GS 块内，RE 检查失败之后、执行 precharge 之前）：

```cpp
auto& detect = cmd_queue_.re_detect_state_[i];
if (detect.pending_re_hit_check) {
    simple_stats_.Increment("gs_re_hit_useless");
    detect.pending_re_hit_check = false;
}
```

#### RE Store 插入/驱逐计数

**位置：** `command_queue.cc`，`RE_AddEntry()`：

```cpp
void CommandQueue::RE_AddEntry(const RowExclusionEntry& entry) {
    // 去重检查（已有）-- 直接返回，不计数
    ...
    if (达到容量上限) {
        pop_front();
        simple_stats_.Increment("gs_re_evictions");    // <-- 新增
    }
    simple_stats_.Increment("gs_re_insertions");       // <-- 新增
    push_back(entry);
}
```

#### Timeout 仲裁计数

**位置：** `command_queue.cc`，`GS_ArbitrateTimeout()`，在每 bank 循环内：

```cpp
if (variation_substantial && best_idx != curr_idx && max_gain > 0) {
    state.curr_timeout_idx = best_idx;
    simple_stats_.Increment("gs_timeout_switches");   // <-- 新增
}

// 记录当前超时值分布
simple_stats_.IncrementVec("gs_timeout_dist", state.curr_timeout_idx);  // <-- 新增
```

`gs_timeout_dist` 在**每个仲裁周期（每 `GS_ARBITRATION_PERIOD = 30000` 周期）对每个 bank 递增一次**。索引对应 `GS_TIMEOUT_VALUES[] = {50, 100, 150, 200, 300, 400, 800}`。若需将分布归一化为比例，后处理时除以 `(num_banks × num_arbitration_periods)` 即可。

---

## `GS_ProcessACT` 完整插桩参考

为避免变量遮蔽问题，函数顶部统一获取 `detect` 引用，后续 RE 检测逻辑复用同一引用：

```cpp
void CommandQueue::GS_ProcessACT(int queue_idx, int new_row, uint64_t curr_cycle) {
    auto& detect = re_detect_state_[queue_idx];
    auto& state = gs_shadow_state_[queue_idx];

    // --- 准确性验证（使用 detect）---

    // 1. 验证上一次 timeout precharge 是否正确
    if (detect.pending_timeout_check) {
        if (new_row == detect.timeout_closed_row) {
            simple_stats_.Increment("gs_timeout_wrong");
        } else {
            simple_stats_.Increment("gs_timeout_correct");
        }
        detect.pending_timeout_check = false;
    }

    // 2. 验证上一次 RE 命中是否有效
    if (detect.pending_re_hit_check) {
        if (new_row == detect.re_hit_row) {
            simple_stats_.Increment("gs_re_hit_useful");
        } else {
            simple_stats_.Increment("gs_re_hit_useless");
        }
        detect.pending_re_hit_check = false;
    }

    // --- RE 检测（使用同一 detect 引用）---

    int rank, bankgroup, bank;
    GetBankFromIndex(queue_idx, rank, bankgroup, bank);

    if (top_row_buf_policy_ == RowBufPolicy::GS) {
        if (detect.prev_closed_by_timeout && detect.prev_row == new_row) {
            RowExclusionEntry entry;
            entry.rank = rank;
            entry.bankgroup = bankgroup;
            entry.bank = bank;
            entry.row = new_row;
            entry.caused_conflict = false;
            RE_AddEntry(entry);
        }
        detect.prev_closed_by_timeout = false;
    }

    // --- Shadow simulation ---
    for (int t = 0; t < GS_TIMEOUT_COUNT; t++) {
        // ... 原有逻辑不变 ...
    }

    state.prev_open_row = new_row;
}
```

---

## 扩展：RE 保护期间完整 CAS 计数

基础方案中，`gs_re_hit_cas_served` 仅计入第一次 CAS（因为 `pending_re_hit_check` 在首次 CAS 后即被清除）。若需统计 RE 保护期间服务的**所有** CAS，需增加一个独立的布尔标志：

在 `RowExclusionDetectState` 中添加：

```cpp
bool re_protected = false;  // 当前行正受 RE 保护（独立于验证状态）
```

- **设置时机**：`controller.cc` 中 RE 命中时设为 `true`
- **清除时机**：`GS_ProcessACT` 中（行被关闭并重新打开时）或 `controller.cc` 中 precharge 实际执行时
- **计数**：`GS_ProcessCAS` 中，若 `re_protected == true` 则递增 `gs_re_hit_cas_served`

此扩展为可选项，基础方案已能回答"RE 命中是否有效"的核心问题。

---

## 边界情况与注意事项

1. **GS_NOHOTROW 变体**：在 GS_NOHOTROW 下，所有 RE 相关计数器（`gs_re_*`）均为零，因为 RE 检查位于 `if (row_buf_policy_ == RowBufPolicy::GS)` 条件内。Timeout 计数器（`gs_timeout_precharges/correct/wrong/deferred`）和仲裁计数器（`gs_timeout_switches/dist`）在 GS 和 GS_NOHOTROW 两种策略下均正常工作。

2. **非 GS 策略**：对于 OPEN_PAGE、CLOSE_PAGE、SMART_CLOSE、DPM、ORACLE 策略，所有 `gs_*` 计数器均保持为零。计数器仍会注册（以避免缺少键值导致崩溃），但不会被递增。

3. **同一 bank 上重复 RE 命中**：如果超时反复触发且 RE 每次都命中，`gs_re_hits` 每次都会递增。但 `pending_re_hit_check` 只在**第一次**命中时设置（由 `if (!detect.pending_re_hit_check)` 保护）。这避免了在验证仍挂起时覆盖跟踪行。

4. **仿真结束边界**：如果仿真结束时 `pending_timeout_check` 或 `pending_re_hit_check` 仍为 true，这些事件将永远不会被验证。因此 `correct + wrong <= precharges` 且 `useful + useless <= hits`（不是严格等式）。

5. **Refresh/SREF 干扰**：timeout precharge 路径仅在 `!cmd_issued`（本周期无常规命令发出）时运行，与 refresh precharge 是分开的。因此 `gs_timeout_precharges` 不包括 refresh 引起的 precharge。但 refresh 会干扰 pending 验证状态：
   - 若 refresh 在 `pending_timeout_check = true` 期间发生，refresh precharge 会关闭行，之后的 demand ACT 打开的行可能与 timeout 决策无关。此时 `gs_timeout_correct` 可能被轻微高估（因为 refresh 后的 ACT 大概率指向不同行）。
   - 若 refresh 在 `pending_re_hit_check = true` 期间发生，类似地，RE 验证可能被误判为 useless。
   - **影响程度**：refresh 频率远低于 timeout（典型 tREFI = 3900 cycles 对比 timeout = 50~800 cycles），且仅在 pending 状态与 refresh 时间窗重叠时受影响。对全局统计的偏差可忽略（< 1%）。
   - **已知近似**：当前设计接受此偏差。若后续需要精确处理，可在 `FinishRefresh` 路径中对受影响 bank 清除 pending 状态。

6. **On-demand precharge 与 RE 验证的交互**：当 RE 保护了行 R 后，若同 bank 的一个 conflict 请求触发 on-demand precharge（通过 `ArbitratePrecharge`）关闭行 R，随后 ACT 打开新行 R'，此事件被路径 2 捕获，判定为 `gs_re_hit_useless`（因 `R' != R`）。这一判定不完全准确——RE 保护可能在被抢占前已通过 CAS 服务了有效请求。`gs_re_hit_cas_served` 计数器可辅助区分此类场景。

7. **`gs_timeout_deferred` 的含义**：timeout_counter 归零后，若 precharge timing 约束（`bs.cmd_timing_[PRE] > clk_`）不满足，precharge 被推迟。`timeout_counter` 维持为 0，下个 cycle 重新检查。`gs_timeout_deferred` 统计此推迟次数。若某次 precharge 被连续推迟 N 个 cycle，计数器递增 N 次。高 deferred 计数意味着 timeout 到期时间点与 DRAM timing 约束频繁冲突，实际行关闭时间晚于预期。

8. **交叉验证不变量**：在 GS 策略下，以下关系应成立：
   - `num_pre_for_demand ≈ num_ondemand_pres + gs_timeout_precharges`（近似等式，因 refresh 相关 precharge 已单独计数）
   - `gs_timeout_precharges + gs_re_hits ≈ total timeout expirations`（所有 timeout 到期事件要么被 RE 拦截，要么执行 precharge）
   - `gs_re_insertions >= gs_re_evictions`（只有在 store 满时才有驱逐）

---

## Per-bank 粒度扩展（可选）

当前所有计数器为 per-channel 聚合。对于 32-bank DDR5 配置，不同 bank 的访问模式可能差异很大。若需 per-bank 分析，关键计数器可改为 vec 形式：

```cpp
InitVecStat("gs_timeout_correct_bank", "vec_counter",
            "GS timeout correct per bank", "bank", num_queues);
InitVecStat("gs_timeout_wrong_bank", "vec_counter",
            "GS timeout wrong per bank", "bank", num_queues);
InitVecStat("gs_re_hit_useful_bank", "vec_counter",
            "GS RE hit useful per bank", "bank", num_queues);
InitVecStat("gs_re_hit_useless_bank", "vec_counter",
            "GS RE hit useless per bank", "bank", num_queues);
```

存储开销：32 banks × 4 counters × 8 bytes = 1 KB per channel，可忽略。

此扩展为**可选项**。建议先用 per-channel 聚合验证方案正确性，后续按需启用 per-bank 粒度。
