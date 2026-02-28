# Phase 1 实现方案：RE-driven Timeout Escalation

**日期：** 2026-02-26
**基于：** `re_driven_timeout_escalation.md` 机制设计

---

## 修改涉及 4 个文件

| 文件 | 改动类型 |
|------|---------|
| `dramsim3/src/command_queue.h` | 新增常量、数据结构、成员变量/函数声明 |
| `dramsim3/src/command_queue.cc` | 核心逻辑：escalation 触发、de-escalation、GetCurrentTimeout 修改 |
| `dramsim3/src/controller.cc` | ClockTick RE 命中分支增加 escalation 触发逻辑 |
| `dramsim3/src/simple_stats.cc` | 注册新计数器 |

---

## 1. `command_queue.h` — 数据结构与常量

### 1.1 扩展 timeout 候选集

将原有 7 个候选值扩展为 9 个（Phase 1 不启用 open-page，不加 `INT_MAX`）：

```cpp
// 替换原有常量
static constexpr int GS_TIMEOUT_COUNT = 9;
static constexpr int GS_TIMEOUT_VALUES[GS_TIMEOUT_COUNT] =
    {50, 100, 150, 200, 300, 400, 800, 1600, 3200};
```

> **关键影响**：`GSShadowState` 中的 `hits[]`、`conflicts[]`、`next_cas_state[]` 数组大小由 `GS_TIMEOUT_COUNT` 控制，自动扩展。`GS_ArbitrateTimeout` 中的循环也自动适配。`gs_timeout_dist` 向量从 7 扩展到 9。

### 1.2 Escalation 参数（编译期常量，通过 `-D` 切换配置）

```cpp
// ===== RE-driven Escalation Constants =====
#ifndef GS_ESC_HIT_THRESHOLD
#define GS_ESC_HIT_THRESHOLD 5       // N: 连续 RE 命中阈值
#endif
#ifndef GS_ESC_STEP
#define GS_ESC_STEP 2                // STEP: 每次提升跳过的档位数
#endif
static constexpr int GS_ESC_DECAY_PERIODS = 5;     // Phase 1 固定
static constexpr int GS_ESC_CONFLICT_THR = 3;       // Phase 1 固定
```

用 `-D` 宏的好处：8 种配置只需在 Makefile 中传不同的 `-DGS_ESC_HIT_THRESHOLD=3 -DGS_ESC_STEP=1`，无需改源码。

### 1.3 RowExclusionEntry 增加命中计数

```cpp
struct RowExclusionEntry {
    int rank;
    int bankgroup;
    int bank;
    int row;
    bool caused_conflict = false;
    int consecutive_hits = 0;      // 新增：连续 RE 命中计数
    // operator== 不变
};
```

### 1.4 新增 BankEscalationState 结构

```cpp
struct BankEscalationState {
    bool escalated = false;           // 是否处于 escalation 状态
    int escalated_timeout_idx = -1;   // escalation 后的 timeout index
    int original_timeout_idx = -1;    // escalation 前 shadow simulation 的选择
    uint64_t escalation_cycle = 0;    // escalation 发生时刻
    int decay_counter = 0;            // 衰减计数器
};
```

### 1.5 CommandQueue 新增成员

```cpp
// ===== RE-driven Escalation Members =====
std::vector<BankEscalationState> bank_escalation_state_;  // per bank

// RE-driven Escalation helper
RowExclusionEntry* RE_FindEntry(int rank, int bankgroup, int bank, int row);
```

---

## 2. `command_queue.cc` — 核心逻辑

### 2.1 初始化（构造函数中，紧接 `re_detect_state_.resize()`）

```cpp
bank_escalation_state_.resize(num_queues_);
```

### 2.2 新增 `RE_FindEntry` — 返回可修改的指针

```cpp
RowExclusionEntry* CommandQueue::RE_FindEntry(int rank, int bankgroup, int bank, int row) {
    for (auto& entry : row_exclusion_store_) {
        if (entry.rank == rank && entry.bankgroup == bankgroup &&
            entry.bank == bank && entry.row == row) {
            return &entry;
        }
    }
    return nullptr;
}
```

现有的 `RE_IsInStore` 是 const 查询，返回 bool。escalation 需要修改 `consecutive_hits`，所以新增一个返回指针的版本。

### 2.3 修改 `GetCurrentTimeout` — 尊重 escalation 状态

```cpp
int CommandQueue::GetCurrentTimeout(int queue_idx) const {
    if (bank_escalation_state_[queue_idx].escalated) {
        return GS_TIMEOUT_VALUES[bank_escalation_state_[queue_idx].escalated_timeout_idx];
    }
    return GS_TIMEOUT_VALUES[gs_shadow_state_[queue_idx].curr_timeout_idx];
}
```

### 2.4 修改 `GS_ArbitrateTimeout` — 增加 de-escalation 逻辑

在每个 bank 的仲裁循环末尾（`state.hits[t] = 0` 之前）插入：

```cpp
// ===== De-escalation Logic =====
auto& esc = bank_escalation_state_[q];
if (esc.escalated) {
    int esc_idx = esc.escalated_timeout_idx;

    // 路径 1: Conflict 驱动快降级
    // 用 escalated_timeout_idx 对应的 conflict 数判断
    if (state.conflicts[esc_idx] > GS_ESC_CONFLICT_THR) {
        esc.escalated = false;
        esc.decay_counter = 0;
        simple_stats_.Increment("gs_re_escalation_demotions");
    } else {
        // 路径 2: Decay 驱动慢降级
        esc.decay_counter++;
        if (esc.decay_counter >= GS_ESC_DECAY_PERIODS) {
            // 降一档
            esc.escalated_timeout_idx = std::max(esc.escalated_timeout_idx - 1,
                                                  esc.original_timeout_idx);
            esc.decay_counter = 0;
            if (esc.escalated_timeout_idx <= esc.original_timeout_idx) {
                esc.escalated = false;  // 完全恢复
                simple_stats_.Increment("gs_re_escalation_demotions");
            }
        }
    }
}
```

在仲裁循环 **外部**（`for(q)` 结束后），统计 escalated bank 数：

```cpp
int escalated_count = 0;
for (int q = 0; q < num_queues_; q++) {
    if (bank_escalation_state_[q].escalated) escalated_count++;
}
simple_stats_.IncrementBy("gs_re_escalation_active_banks", escalated_count);
```

### 2.5 timeout 分布计数器长度适配

原有代码 `simple_stats_.IncrementVec("gs_timeout_dist", state.curr_timeout_idx)` 不变，但需在 `simple_stats.cc` 中将向量长度从 7 改为 9。

---

## 3. `controller.cc` — ClockTick 中的 Escalation 触发

修改位置：`controller.cc:142-155`，即 RE 命中分支。

原代码（第 142-155 行）：

```cpp
if (cmd_queue_.RE_IsInStore(...)) {
    simple_stats_.Increment("gs_re_hits");
    // ...track for verification...
    cmd_queue_.timeout_counter[i] = cmd_queue_.GetCurrentTimeout(i);
    continue;
}
```

替换为：

```cpp
if (auto* re_entry = cmd_queue_.RE_FindEntry(
        cmd.Rank(), cmd.Bankgroup(), cmd.Bank(), cmd.Row())) {
    simple_stats_.Increment("gs_re_hits");
    auto& detect = cmd_queue_.re_detect_state_[i];
    if (!detect.pending_re_hit_check) {
        detect.pending_re_hit_check = true;
        detect.re_hit_row = cmd.Row();
    }

    // ===== Escalation trigger =====
    re_entry->consecutive_hits++;
    if (re_entry->consecutive_hits >= GS_ESC_HIT_THRESHOLD) {
        auto& esc = cmd_queue_.bank_escalation_state_[i];
        auto& shadow = cmd_queue_.gs_shadow_state_[i];
        int current_idx = esc.escalated ? esc.escalated_timeout_idx
                                        : shadow.curr_timeout_idx;
        int target_idx = std::min(current_idx + GS_ESC_STEP,
                                  GS_TIMEOUT_COUNT - 1);

        esc.escalated = true;
        esc.escalated_timeout_idx = target_idx;
        if (!esc.escalated || esc.original_timeout_idx < 0) {
            esc.original_timeout_idx = shadow.curr_timeout_idx;
        }
        esc.escalation_cycle = clk_;
        esc.decay_counter = 0;

        // 释放 RE 条目，腾出容量
        cmd_queue_.RE_RemoveEntry(cmd.Rank(), cmd.Bankgroup(),
                                   cmd.Bank(), cmd.Row());
        simple_stats_.Increment("gs_re_escalation_triggers");
        simple_stats_.Increment("gs_re_freed_by_escalation");
        simple_stats_.IncrementVec("gs_escalated_timeout_dist", target_idx);
    }
    // ===== End escalation trigger =====

    // 用 escalation-aware timeout 重置计数器
    cmd_queue_.timeout_counter[i] = cmd_queue_.GetCurrentTimeout(i);
    continue;
}
```

**核心逻辑解读**：

- `RE_FindEntry` 替代 `RE_IsInStore`，返回条目指针以便修改 `consecutive_hits`
- 达到阈值 N 后：计算 target_idx（当前 idx + STEP，不超过最大 idx）；设置 escalation 状态；释放 RE 条目
- 未达阈值时：行为与原来完全一致（重置 timeout，保留 RE 条目）

---

## 4. `simple_stats.cc` — 注册新计数器

在 `gs_timeout_switches` 之后增加：

```cpp
// RE-driven Escalation counters
InitStat("gs_re_escalation_triggers", "counter",
         "RE-driven timeout escalation triggers");
InitStat("gs_re_escalation_demotions", "counter",
         "RE-driven timeout de-escalation events");
InitStat("gs_re_freed_by_escalation", "counter",
         "RE entries freed by escalation");
InitStat("gs_re_escalation_active_banks", "counter",
         "Cumulative escalated bank count at arbitration");
InitVecStat("gs_escalated_timeout_dist", "vec_counter",
            "Escalated timeout index distribution", "idx", 9);
```

同时将原有 `gs_timeout_dist` 长度从 7 改为 9：

```cpp
InitVecStat("gs_timeout_dist", "vec_counter",
            "GS timeout distribution at arbitration", "idx", 9);
```

---

## 5. 构建配置：8 种参数组合

在 Makefile 或构建脚本中通过 `-D` 传参：

| 标签 | 编译选项 |
|------|---------|
| `GS_esc_N3_S1` | `-DGS_ESC_HIT_THRESHOLD=3 -DGS_ESC_STEP=1` |
| `GS_esc_N3_S2` | `-DGS_ESC_HIT_THRESHOLD=3 -DGS_ESC_STEP=2` |
| `GS_esc_N5_S1` | `-DGS_ESC_HIT_THRESHOLD=5 -DGS_ESC_STEP=1` |
| `GS_esc_N5_S2` | `-DGS_ESC_HIT_THRESHOLD=5 -DGS_ESC_STEP=2` |
| `GS_esc_N5_S3` | `-DGS_ESC_HIT_THRESHOLD=5 -DGS_ESC_STEP=3` |
| `GS_esc_N8_S1` | `-DGS_ESC_HIT_THRESHOLD=8 -DGS_ESC_STEP=1` |
| `GS_esc_N8_S2` | `-DGS_ESC_HIT_THRESHOLD=8 -DGS_ESC_STEP=2` |
| `GS_esc_N12_S2` | `-DGS_ESC_HIT_THRESHOLD=12 -DGS_ESC_STEP=2` |

每种配置编译一个独立的 `libdramsim3.so`，再链接 ChampSim 生成 8 个不同的 `bin/champsim`。

---

## 6. 关键设计决策说明

| 决策 | 理由 |
|------|------|
| **escalation 在 controller.cc 而非 command_queue.cc 触发** | RE 命中判断发生在 `controller.cc` ClockTick 的 per-bank 循环中，这里已有 `cmd` 上下文和 `clk_`，是最自然的插入点 |
| **用 `RE_FindEntry` 返回指针替代 `RE_IsInStore`** | 需要在同一次查找中既判断存在性又修改 `consecutive_hits`，避免两次遍历 |
| **escalation 后立即释放 RE 条目** | 核心设计目标——腾出 RE 容量。条目的保护职责已转移到更高的 timeout 上 |
| **de-escalation 放在 `GS_ArbitrateTimeout` 中** | 与 shadow simulation 同周期执行（30k cycles），可直接使用该 period 的 conflict 统计 |
| **`GS_TIMEOUT_COUNT` 从 7 扩展到 9** | shadow simulation 自动覆盖新候选值 {1600, 3200}，即使没有 escalation，shadow 也可能自主选择更高档位——这本身就是有意义的改进 |
| **`original_timeout_idx` 记录 escalation 前的选择** | de-escalation 慢降级路径需要知道"回到哪里"，避免降到比 shadow 当前选择更低的档位 |

---

## 7. 验证清单

实现完成后，需验证：

1. **编译正确性**：8 种配置都能正常编译链接
2. **Baseline 不受影响**：`GS_NOHOTROW` 策略不触发 escalation（因为不进入 RE 分支）
3. **计数器自洽**：`gs_re_escalation_triggers` ≤ `gs_re_hits`；`gs_re_freed_by_escalation` = `gs_re_escalation_triggers`
4. **负反馈闭环**：lbm 上观察到 escalation 后 `gs_re_hits` 和 `gs_re_insertions` 下降
5. **无 regression**：hpcc/RandomAccess 上 escalation 触发数极低（RE 准确率仅 10.7%，很少有连续 N 次命中）
