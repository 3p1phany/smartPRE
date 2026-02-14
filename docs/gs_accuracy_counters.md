# GS 预测准确性计数器 - 实现文档

## 概述

本文档描述了为衡量 GS（Group Speculation）行缓冲管理策略预测准确性而添加的性能计数器。这些计数器回答三个核心问题：

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

**衍生指标：** Timeout 准确率 = `gs_timeout_correct / gs_timeout_precharges`

**不变量：** `gs_timeout_correct + gs_timeout_wrong <= gs_timeout_precharges`

注意：这里是不等式而非等式，因为某些 bank 可能存在未被验证的挂起超时检查（例如仿真结束时，或该 bank 上不再有后续 ACT 到达）。

### B. Row Exclusion Store 有效性

| 计数器 | 描述 |
|--------|------|
| `gs_re_insertions` | 插入 RE Store 的条目数量（不含重复项） |
| `gs_re_hits` | RE Store 查找命中并**阻止**了 timeout precharge 的次数 |
| `gs_re_hit_useful` | RE 命中有效：被保护的行确实被再次访问 |
| `gs_re_hit_useless` | RE 命中无效：被保护的行未被再次访问 |
| `gs_re_evictions` | 因容量限制（FIFO 淘汰）从 RE Store 中驱逐的条目数量 |

**衍生指标：** RE 准确率 = `gs_re_hit_useful / gs_re_hits`

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

### 每 Bank 跟踪状态

在 `command_queue.h` 中添加到 `RowExclusionDetectState`：

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

#### RE 命中验证流程

```
controller.cc: 超时触发，RE_IsInStore() == true
  |
  +--> Increment("gs_re_hits")
  +--> if !pending_re_hit_check:   （只有第一次命中设置跟踪）
         detect.pending_re_hit_check = true
         detect.re_hit_row = cmd.Row()
  +--> 延长超时，继续   （跳过 precharge）
  |
  v  三种可能的结果：
  |
  +--> [路径 1] 同一 bank 上到达 CAS（行仍然打开）
  |    command_queue.cc: GS_ProcessCAS()
  |      Increment("gs_re_hit_useful"), 清除标志
  |
  +--> [路径 2] 同一 bank 上到达 ACT（行因某种原因被关闭）
  |    command_queue.cc: GS_ProcessACT()
  |      new_row == re_hit_row? --> Increment("gs_re_hit_useful")
  |      new_row != re_hit_row? --> Increment("gs_re_hit_useless")
  |      清除标志
  |
  +--> [路径 3] 第二次超时触发，RE 此次未命中
       controller.cc: ClockTick() timeout precharge 路径，RE 检查之后
         if pending_re_hit_check:
           Increment("gs_re_hit_useless"), 清除标志
         然后执行正常的 timeout precharge
```

**路径 1 细节**（`GS_ProcessCAS`）：
```cpp
auto& detect = re_detect_state_[queue_idx];
if (detect.pending_re_hit_check) {
    simple_stats_.Increment("gs_re_hit_useful");
    detect.pending_re_hit_check = false;
}
```

**路径 3 细节**（`controller.cc`，在 GS 块内，RE 检查失败之后）：
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
        Increment("gs_re_evictions");    // <-- 新增
    }
    Increment("gs_re_insertions");       // <-- 新增
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

`gs_timeout_dist` 在**每个仲裁周期（每 `GS_ARBITRATION_PERIOD = 30000` 周期）对每个 bank 递增一次**。索引对应 `GS_TIMEOUT_VALUES[] = {50, 100, 150, 200, 300, 400, 800}`。

---

## 边界情况与注意事项

1. **GS_NOHOTROW 变体**：在 GS_NOHOTROW 下，所有 RE 相关计数器（`gs_re_*`）均为零，因为 RE 检查位于 `if (row_buf_policy_ == RowBufPolicy::GS)` 条件内。Timeout 计数器（`gs_timeout_precharges/correct/wrong`）和仲裁计数器（`gs_timeout_switches/dist`）在 GS 和 GS_NOHOTROW 两种策略下均正常工作。

2. **非 GS 策略**：对于 OPEN_PAGE、CLOSE_PAGE、SMART_CLOSE、DPM、ORACLE 策略，所有 `gs_*` 计数器均保持为零。计数器仍会注册（以避免缺少键值导致崩溃），但不会被递增。

3. **同一 bank 上重复 RE 命中**：如果超时反复触发且 RE 每次都命中，`gs_re_hits` 每次都会递增。但 `pending_re_hit_check` 只在**第一次**命中时设置（由 `if (!detect.pending_re_hit_check)` 保护）。这避免了在验证仍挂起时覆盖跟踪行。

4. **仿真结束边界**：如果仿真结束时 `pending_timeout_check` 或 `pending_re_hit_check` 仍为 true，这些事件将永远不会被验证。因此 `correct + wrong <= precharges` 且 `useful + useless <= hits`（不是严格等式）。

5. **Refresh/SREF precharge**：timeout precharge 路径仅在 `!cmd_issued`（本周期无常规命令发出）时运行，与 refresh precharge 是分开的。因此 `gs_timeout_precharges` 不包括 refresh 引起的 precharge。

6. **`GS_ProcessACT` 中的 `detect` 变量遮蔽**：`GS_ProcessACT` 中有两处 `auto& detect = re_detect_state_[queue_idx]` 声明——一处在顶部（用于准确性验证），另一处在 `if (GS)` 块内（用于 RE 检测逻辑）。两者引用同一对象；内层声明遮蔽了外层。这是安全的，但可以考虑清理。
