# CRAFT Phase Change Fast Reset 实验方案

## 1. 问题分析

### 1.1 现有缺陷

CRAFT 基础反馈回路在稳态下表现良好，但面对 workload phase 突变时收敛过慢。具体场景：

当 workload 从高局部性阶段（如流式访问，timeout 被 escalation 推高到 ~3200）突变为低局部性阶段（如随机访问），timeout 需要通过 conflict 事件逐步下降：

```
最坏情况收敛时间 = (T_MAX - T_MIN) / CONFLICT_STEP
                  = (3200 - 50) / 25
                  = 126 次 conflict
```

每次 conflict 需要等待当前 timeout 倒计时到 0 或被新请求打断，假设平均每次 conflict 间隔 ~200-500 cycles（取决于访问密度），最坏情况需要 25,000-63,000 cycles 才能收敛到合适的 timeout 值。

### 1.2 现有数据中的线索

从 CRAFT vs GS 对比数据中，以下 benchmark 可能受此问题影响（CRAFT 落后 GS 较多）：

| Benchmark | CRAFT vs GS | 特征 |
|-----------|-------------|------|
| crono/PageRank/higgs | -2.07% | 图算法，迭代式访问模式，有明显相变 |
| crono/PageRank/soc-pokec | -1.87% | 同上，更大的图 |
| ligra/PageRank/soc-pokec | -2.10% | 同类workload |
| ligra/CF/soc-pokec | -1.28% | 协同过滤，有散射/聚集模式切换 |
| spec06/cactusADM/ref | -1.98% | 科学计算，可能有网格遍历相变 |
| spec06/zeusmp/ref | -1.66% | 天体物理，网格计算 |
| hashjoin/hj-2-NPO_st | -1.60% | hash join 有 build/probe 两个明确阶段 |

这些 benchmark 的共同特征是存在不同访问模式的阶段切换。GS 的 epoch-based shadow simulation（30K cycle 评估一次）反而能更快地在相变时切换 timeout 级别，因为 GS 直接从 7 个候选值中选最优，而 CRAFT 只能逐步线性调整。

### 1.3 Phase Change Fast Reset 机制

添加 per-bank `conflict_streak` 计数器（3-bit 饱和计数器），追踪连续 conflict 次数：

```
conflict_streak: 3-bit saturating counter per bank [0-7]

On conflict:
  conflict_streak++
  if conflict_streak >= PHASE_THRESHOLD:
    timeout_value = INIT_TIMEOUT   // 直接跳回初始值
    conflict_streak = 0
    reopen_streak = 0
  else:
    timeout_value -= CONFLICT_STEP  // 正常降阶

On wrong precharge (escalation) or right precharge:
  conflict_streak = 0  // 任何非 conflict 事件打断 streak
```

**设计要点**：
- Fast reset 目标值选 `INIT_TIMEOUT (200)` 而非 `T_MIN (50)`：相变后访问模式未知，200 是中性起点，后续反馈会快速调整到合适值
- 同时清零 `reopen_streak`：相变意味着之前的热行信息已过时
- 阈值 `PHASE_THRESHOLD` 不宜过小（避免正常 de-escalation 中误触发）也不宜过大（否则失去快速响应能力）

**最坏情况收敛时间改善**：
- 改进前：O(T_MAX / CONFLICT_STEP) = O(126)
- 改进后：O(PHASE_THRESHOLD) = O(4)，相变后仅需 4 次 conflict 即可重置

---

## 2. 代码修改

### 2.1 `dramsim3/src/command_queue.h`

#### 2.1a 新增常量（line 95 之后，CRAFT_SHIFT_CAP 之后）

```cpp
static constexpr int CRAFT_PHASE_THRESHOLD = 4;  // consecutive conflicts to trigger fast reset
```

#### 2.1b CraftBankState 新增字段（line 97-102）

```cpp
struct CraftBankState {
    int timeout_value = CRAFT_INIT_TIMEOUT;
    int reopen_streak = 0;
    int prev_row = -1;
    bool prev_closed_by_timeout = false;
    int conflict_streak = 0;              // consecutive conflict counter [0-7]
};
```

### 2.2 `dramsim3/src/command_queue.cc` — AddCommand() CRAFT 分支

现有代码（line 463-482）的 conflict 分支修改为：

```cpp
else if(top_row_buf_policy_==RowBufPolicy::CRAFT){
    int index=GetQueueIndex(cmd.Rank(),cmd.Bankgroup(),cmd.Bank());

    if(timeout_ticking[index] && timeout_counter[index] > 0){
        if(cmd.Row() != issued_cmd[index].Row()){
            // Conflict: different row arrived during timeout
            auto& state = craft_state_[index];
            state.conflict_streak = std::min(state.conflict_streak + 1, CRAFT_REOPEN_STREAK_MAX);

            if (state.conflict_streak >= CRAFT_PHASE_THRESHOLD) {
                // Phase change detected: fast reset to initial timeout
                state.timeout_value = CRAFT_INIT_TIMEOUT;
                state.conflict_streak = 0;
                state.reopen_streak = 0;
                simple_stats_.Increment("craft_phase_resets");
            } else {
                // Normal de-escalation with cost-aware fixed step
                state.timeout_value = std::max(state.timeout_value - craft_conflict_step_, CRAFT_T_MIN);
            }
            state.reopen_streak = 0;
            timeout_counter[index] = 0;  // trigger immediate precharge
            simple_stats_.Increment("craft_conflicts");
            simple_stats_.Increment("craft_deescalations");
        }
        else{
            // Row hit during timeout: reset timer, keep row open
            timeout_counter[index] = craft_state_[index].timeout_value;
            timeout_ticking[index] = false;
        }
    }
}
```

**注意**：`reopen_streak = 0` 在两个分支外统一执行（与原逻辑一致，conflict 总是清零 reopen_streak）。fast reset 分支额外清零 `conflict_streak`。

### 2.3 `dramsim3/src/command_queue.cc` — CRAFT_ProcessACT()

在 wrong precharge 和 right precharge 分支中，都需要清零 `conflict_streak`：

```cpp
void CommandQueue::CRAFT_ProcessACT(int queue_idx, int new_row) {
    auto& state = craft_state_[queue_idx];

    if (state.prev_closed_by_timeout) {
        if (new_row == state.prev_row) {
            // Wrong precharge: escalate with exponential backoff
            int shift = std::min(state.reopen_streak, CRAFT_SHIFT_CAP);
            int step = CRAFT_BASE_STEP << shift;
            state.timeout_value = std::min(state.timeout_value + step, CRAFT_T_MAX);
            state.reopen_streak = std::min(state.reopen_streak + 1, CRAFT_REOPEN_STREAK_MAX);
            state.conflict_streak = 0;  // non-conflict event breaks streak
            simple_stats_.Increment("craft_timeout_wrong");
            simple_stats_.Increment("craft_escalations");
        } else {
            // Right precharge: timeout was appropriate, no adjustment
            state.conflict_streak = 0;  // non-conflict event breaks streak
            simple_stats_.Increment("craft_timeout_correct");
        }
        state.prev_closed_by_timeout = false;
    }

    simple_stats_.IncrementVec("craft_reopen_streak_dist", state.reopen_streak);
}
```

### 2.4 `dramsim3/src/simple_stats.cc`

在现有 CRAFT 统计计数器之后添加：

```cpp
InitStat("craft_phase_resets", "counter",
         "CRAFT phase-change fast resets triggered");
```

### 2.5 修改文件总结

| 文件 | 修改量 | 性质 |
|------|--------|------|
| `dramsim3/src/command_queue.h` | +2 行 | 新增 PHASE_THRESHOLD 常量 + conflict_streak 字段 |
| `dramsim3/src/command_queue.cc` | ~10 行 | AddCommand conflict 分支增加 streak 判断；ProcessACT 增加 streak 清零 |
| `dramsim3/src/simple_stats.cc` | +2 行 | 新增 craft_phase_resets 计数器 |

总计约 **15 行** 新增/修改代码。硬件开销增加 3 bits/bank，32 bank = 12 bytes/channel，总开销从 180B 增至 192B。

---

## 3. 实验设计

### 3.1 实验矩阵

| 实验编号 | 配置名 | 说明 | 目的 |
|----------|--------|------|------|
| E0 | `CRAFT_1c` | 已有基础 CRAFT 结果 | Baseline（无需重跑） |
| E1 | `CRAFT_PR4_1c` | PHASE_THRESHOLD = 4 | 主实验 |
| E2 | `CRAFT_PR3_1c` | PHASE_THRESHOLD = 3 | 敏感性分析（更激进） |
| E3 | `CRAFT_PR6_1c` | PHASE_THRESHOLD = 6 | 敏感性分析（更保守） |

**说明**：
- E0 已有完整结果，直接复用
- E1 是主实验，PHASE_THRESHOLD=4 是设计值
- E2、E3 用于验证阈值敏感性（如果 E1 效果显著，再跑 E2/E3；否则先分析原因）

### 3.2 配置文件

每个实验需要：
1. **DRAM 配置 `.ini`**：复制 `DDR5_64GB_4ch_4800_CRAFT.ini`，`row_buf_policy = CRAFT`（不变，Phase Reset 是内嵌逻辑，不需要新的 policy）
2. **代码中的阈值**：通过修改 `CRAFT_PHASE_THRESHOLD` 常量控制

由于阈值是编译时常量，E1/E2/E3 每次需要修改常量后重编译。如果需要频繁实验不同阈值，可以考虑将 PHASE_THRESHOLD 改为运行时可配置参数（从 `.ini` 读取），但这不是必需的。

### 3.3 敏感性分析的阈值选择

如果需要更系统化的阈值扫描，可考虑通过运行时配置支持。在 `dramsim3/src/configuration.cc` 中解析新参数：

```ini
# 在 DDR5_64GB_4ch_4800_CRAFT.ini 中添加
craft_phase_threshold = 4
```

这样每次实验只需修改配置文件，无需重编译。但这是可选优化，初期直接修改常量重编译即可。

### 3.4 Benchmark 套件

使用 `benchmarks_selected.tsv` 中的完整 62 个 benchmark，通过 `scripts/run_selected_slices.sh` 运行。

---

## 4. 评估指标

### 4.1 性能指标

- **IPC**：每个 benchmark 的 IPC 改善（per-benchmark 报告）
- **GEOMEAN speedup**：CRAFT_PR4 vs CRAFT baseline、CRAFT_PR4 vs GS、CRAFT_PR4 vs open_page

### 4.2 行为指标

通过 `ddr.json` 输出的统计计数器分析：

| 指标 | 来源 | 说明 |
|------|------|------|
| `craft_phase_resets` | 新增计数器 | Phase reset 触发总次数 |
| `craft_conflicts` | 已有 | Conflict 总次数 |
| `craft_escalations` | 已有 | Escalation 总次数 |
| `craft_deescalations` | 已有 | De-escalation 总次数 |
| `craft_timeout_value_sum` | 已有 | 平均 timeout 值（通过 sum / precharges 计算） |
| `craft_reopen_streak_dist` | 已有 | Reopen streak 分布 |
| `phase_reset_ratio` | 衍生 | `craft_phase_resets / craft_conflicts`，反映相变频率 |

### 4.3 重点关注的 benchmark

根据 1.2 节分析，以下 benchmark 最有可能从 Phase Reset 中获益：

**第一梯队（预期显著改善）**：
- `crono/PageRank/higgs` — 图迭代，明确的相变（push/pull 切换）
- `crono/PageRank/soc-pokec` — 同上
- `ligra/PageRank/soc-pokec` — 同上
- `hashjoin/hj-2-NPO_st` — build/probe 两阶段

**第二梯队（预期中等改善）**：
- `ligra/CF/soc-pokec` — 散射/聚集模式
- `ligra/Components/soc-pokec` — 图遍历
- `spec06/cactusADM/ref` — 网格计算
- `spec06/zeusmp/ref` — 科学计算

**第三梯队（预期无影响或微小变化）**：
- 流式访问（lbm, bwaves）— timeout 稳定在高值，无相变
- 稳态随机（hpcc/RandAcc）— timeout 稳定在低值，无相变
- 低内存强度（sphinx3, gcc）— conflict 少，不会触发 phase reset

### 4.4 验证 Phase Reset 是否帮助的分析方法

对每个 benchmark，计算：

1. **Phase reset 密度**：`craft_phase_resets / total_instructions`
   - 高密度 → 该 benchmark 有频繁相变
   - 零密度 → 无相变或 conflict 不集中

2. **Performance delta 与 phase reset 密度的相关性**：
   - 正相关 → Phase reset 对相变 benchmark 有效
   - 无相关 → Phase reset 未解决核心问题

3. **Case study**：对 IPC 变化最大的 benchmark 做详细分析
   - 对比 Phase Reset 前后的 `craft_conflicts`、`craft_escalations`、`craft_timeout_value_sum`
   - 如果 phase reset 后 average timeout 更低且 IPC 更高，说明原来 timeout 过高导致了不必要的等待

---

## 5. 构建与运行

### 5.1 实现并构建

```bash
# 1. 修改代码（按 2.1-2.4 节修改三个文件）

# 2. 构建 DRAMSim3
cd /root/data/smartPRE/dramsim3 && mkdir -p build && cd build && cmake .. && make -j8

# 3. 构建 ChampSim-LA
cd /root/data/smartPRE/champsim-la
python3 config.sh champsim_config.json
make -j8

# 4. Smoke test（短 trace 验证不 crash）
export LD_LIBRARY_PATH=/root/data/smartPRE/dramsim3:$LD_LIBRARY_PATH
# 使用一个已知的短 trace 运行 1M 指令
```

### 5.2 运行完整实验

```bash
export LD_LIBRARY_PATH=/root/data/smartPRE/dramsim3:$LD_LIBRARY_PATH

# E1: PHASE_THRESHOLD = 4（默认值）
cd /root/data/smartPRE/champsim-la
TRACE_ROOT=/path/to/traces MANIFEST_SRC=./scripts/selected_slices.tsv scripts/run_selected_slices.sh
# 输入 label: CRAFT_PR4_1c
```

### 5.3 结果分析

```bash
cd /root/data/smartPRE/champsim-la

# 与基础 CRAFT 对比
python3 scripts/compare_ipc.py --a results/CRAFT_1c --b results/CRAFT_PR4_1c \
  --a-label CRAFT --b-label CRAFT_PR4 --out results/compare_CRAFT_vs_CRAFT_PR4_1c.tsv

# 与 GS 对比
python3 scripts/compare_ipc.py --a results/GS_1c --b results/CRAFT_PR4_1c \
  --a-label GS --b-label CRAFT_PR4 --out results/compare_GS_vs_CRAFT_PR4_1c.tsv

# 与 open_page 对比
python3 scripts/compare_ipc.py --a results/open_page_1c --b results/CRAFT_PR4_1c \
  --a-label OPEN_PAGE --b-label CRAFT_PR4 --out results/compare_open_page_vs_CRAFT_PR4_1c.tsv

# 行为分析
python3 scripts/craft_behavior_table.py \
  --craft-dir results/CRAFT_PR4_1c \
  --baseline-dir results/CRAFT_1c \
  --manifest benchmarks_selected.tsv \
  --out results/craft_pr4_behavior.tsv
```

---

## 6. 预期结果与论文故事

### 6.1 预期性能变化

| 对比 | 预期 GEOMEAN | 说明 |
|------|-------------|------|
| CRAFT_PR4 vs CRAFT | +0.1% ~ +0.5% | 仅在相变 benchmark 上有改善 |
| CRAFT_PR4 vs GS | -0.2% ~ +0.2% | 缩小与 GS 的差距（原 -0.28%） |
| CRAFT_PR4 vs open_page | +1.3% ~ +1.7% | 保持对 open_page 的优势 |

**关键预期**：CRAFT_PR4 应在以下 benchmark 上缩小与 GS 的差距：
- crono/PageRank/higgs: 从 -2.07% 改善到 -1.0% 以内
- ligra/PageRank/soc-pokec: 从 -2.10% 改善到 -1.0% 以内
- hashjoin/hj-2-NPO_st: 从 -1.60% 改善到 -0.5% 以内

同时不应在稳态 benchmark 上引入退化（phase reset 不触发则行为不变）。

### 6.2 论文故事

CRAFT 的基础反馈回路针对稳态行为优化，通过非对称步长在连续 timeout 空间中收敛到 cost-optimal 均衡点。但面对突发 phase change 时，线性 de-escalation 的收敛速度 O(T_MAX/CONFLICT_STEP) 成为瓶颈。

Phase-aware fast reset 在反馈回路中引入"跳跃式重置"能力：通过 3-bit 饱和计数器追踪连续 conflict 事件，当检测到持续 conflict 信号（表明访问模式已发生根本变化）时，直接将 timeout 重置到中性初始值，而非逐步线性递减。这将最坏情况收敛时间从 O(T_MAX/CONFLICT_STEP) = 126 步降低到 O(PHASE_THRESHOLD) = 4 步。

硬件代价极小：3 bits/bank，32 bank 仅增加 12 bytes/channel，总开销从 180B 增至 192B，仍远低于 GS (1,042B)、DYMPL (3,390B)、RL_PAGE (4,140B)。

---

## 7. 风险与应对

### 7.1 Phase Reset 误触发

**风险**：在某些 benchmark 中，正常的偶发连续 conflict（非真实相变）可能意外触发 fast reset，导致 timeout 不必要地降低。

**应对**：
- PHASE_THRESHOLD=4 已经提供了足够的过滤：正常的偶发 conflict 之间通常会穿插 row-hit 或 precharge 事件，打断 streak
- 即使误触发，重置到 INIT_TIMEOUT=200（而非 T_MIN=50），影响有限——后续反馈会快速恢复
- 通过 E2 (threshold=3) 和 E3 (threshold=6) 的敏感性分析确认阈值选择的鲁棒性

### 7.2 改善不显著

**风险**：Phase reset 触发次数极少，对整体性能影响可忽略。

**应对**：
- 首先检查 `craft_phase_resets` 计数器，确认 phase reset 是否在目标 benchmark 上触发
- 如果触发次数合理但改善不显著，说明问题不在 timeout 收敛速度而在其他地方
- 可考虑进一步分析：在 phase reset 时同时记录当前 timeout_value，观察 reset 前的 timeout 分布

### 7.3 某些 benchmark 退化

**风险**：Phase reset 在某些稳态 benchmark 上误触发导致性能下降。

**应对**：
- 稳态 benchmark 理论上不会触发（连续 4 次 conflict 不穿插任何其他事件的概率极低）
- 如果出现退化，检查该 benchmark 的 `craft_phase_resets` 值：
  - 如果为 0，退化来源于其他因素（代码 bug）
  - 如果大于 0，说明该 benchmark 实际上有微相变，考虑提高 PHASE_THRESHOLD

---

## 8. 后续实验（可选扩展）

### 8.1 Reset 目标值敏感性

| 实验 | Reset 目标 | 说明 |
|------|-----------|------|
| E4 | T_MIN (50) | 最激进：假设相变后需要最短 timeout |
| E5 | INIT_TIMEOUT (200) | 当前设计：中性起点 |
| E6 | timeout_value / 2 | 渐进式：不完全重置，折半 |

### 8.2 多核场景

Phase change 在多核场景下可能更频繁（不同线程的访问流在 bank 级别交织），Phase Reset 可能有更大收益。

```
配置：champsim_config_4c.json, CRAFT_PR4
Label: CRAFT_PR4_4c
```

### 8.3 与其他优化组合

如果 Phase Reset 效果显著，可进一步探索与其他 CRAFT 增强机制的组合效果。
