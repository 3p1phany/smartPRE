# RE-driven Timeout Escalation 实验方案

**日期：** 2026-02-26
**基于：** GS_prof_1c profiling 分析结果

---

## 1. 背景与动机

### 1.1 当前问题

GS 的 RE Store 对热行的保护是**被动拦截**——每次 timeout 到期都需要查询 RE Store 来阻止 precharge。对于真正的长寿行（如 lbm 中 800c 仍不够的 bank），RE 条目会被反复命中数十次（profiling 显示平均 18x），造成：

- **无谓的 timeout-reset 循环**：timeout 到期 → RE 拦截 → 重置 timeout → 再到期 → 再拦截...
- **RE Store 容量浪费**：长期驻留的条目占据有限的 64 项容量，挤占其他 bank 的保护机会
- **800c 上限不足**：13 个 lbm-like benchmark 中，shadow simulation 已将大量 bank 推到 800c 上限但仍频繁触发错误 precharge（TO_Acc 39-65%）

### 1.2 核心思想

将被动拦截转为**主动调整**：RE 条目连续命中 N 次后，直接提升该 bank 的 timeout 到更高档位（甚至切换到 open-page 模式），并释放 RE 条目。形成负反馈闭环：

```
RE 高命中 → 提升 timeout → 减少错误 precharge → RE 命中减少 → 释放 RE 容量
```

---

## 2. 机制设计

### 2.1 数据结构修改

#### 扩展 timeout 候选集

配合 escalation 增加更高档位：

```cpp
// 原始: {50, 100, 150, 200, 300, 400, 800}
// 扩展: 增加 1600, 3200, INF(open-page mode)
static constexpr int GS_TIMEOUT_COUNT_EXT = 10;
static constexpr int GS_TIMEOUT_VALUES_EXT[GS_TIMEOUT_COUNT_EXT] =
    {50, 100, 150, 200, 300, 400, 800, 1600, 3200, INT_MAX};
// INT_MAX 代表 open-page 模式：该 bank 不再执行 timeout precharge
```

#### RE 条目增加命中计数器

```cpp
struct RowExclusionEntry {
    int rank, bankgroup, bank, row;
    bool caused_conflict = false;
    int consecutive_hits = 0;  // 新增：连续命中计数
};
```

#### Per-bank escalation 状态

```cpp
struct BankEscalationState {
    bool escalated = false;          // 是否被 RE 提升
    int escalated_timeout_idx = -1;  // 提升后的 timeout index
    int original_timeout_idx = -1;   // 提升前 shadow simulation 的选择
    uint64_t escalation_cycle = 0;   // 提升发生的时刻
    int decay_counter = 0;           // 衰减计数器（用于降级）
};
std::vector<BankEscalationState> bank_escalation_state_;  // per bank
```

### 2.2 触发路径（Escalation）

在 `controller.cc` ClockTick 的 RE 命中分支中：

```
timeout 到期 → RE_IsInStore() 命中 → entry.consecutive_hits++
  → if consecutive_hits >= N:
      1. 计算 escalation target:
         target_idx = min(current_timeout_idx + STEP, MAX_IDX)
      2. 设置 bank escalation state:
         bank_escalation[queue_idx].escalated = true
         bank_escalation[queue_idx].escalated_timeout_idx = target_idx
         bank_escalation[queue_idx].original_timeout_idx = shadow 当前选择
      3. 释放 RE 条目: RE_RemoveEntry(...)
      4. 更新实际 timeout: timeout_counter = TIMEOUT_VALUES[target_idx]
      5. 计数器: gs_re_escalation_triggers++
  → else:
      (现有逻辑: 重置 timeout_counter, 保留 RE 条目)
```

### 2.3 降级路径（De-escalation）

降级解决三个问题：

1. **Workload 相变**：lbm 不同阶段行为差异极大（初始阶段 TO_Acc ~94% vs 稳态 ~35%），escalation 后的高 timeout 在行为变化时会制造 conflict
2. **错误 escalation 累积损害**：RE 准确率仅 71.27%，连续 N 次命中不保证每次都 useful，误 escalation 需要纠正机制
3. **恢复 shadow simulation 自适应能力**：escalation 覆盖了 shadow simulation 的仲裁结果，降级将决策权归还给 shadow simulation

在 `GS_ArbitrateTimeout()` 中增加降级逻辑：

```
每个 arbitration period (30k cycles):
  → if bank_escalation[q].escalated:
      检查该 bank 在本 period 内的 conflict 数
      → if conflicts[escalated_idx] > CONFLICT_THR:
          // 行为已变，escalation 不再合适，立即降级
          bank_escalation[q].escalated = false
          恢复 shadow simulation 正常仲裁
          计数器: gs_re_escalation_demotions++
      → else:
          decay_counter++
          if decay_counter >= DECAY_PERIODS:
              // 长时间无 conflict 但也无新证据，逐步降级
              escalated_timeout_idx = max(escalated_idx - 1, original_idx)
              if escalated_idx == original_idx:
                  escalated = false  // 完全恢复
```

两条降级路径互补：

| 触发 | 条件 | 语义 |
|------|------|------|
| **Conflict 驱动**（快降级） | 单 period 内 conflict > CONFLICT_THR | 行为已变，高 timeout 在制造 conflict，立即回退 |
| **Decay 驱动**（慢降级） | 连续 DECAY_PERIODS 个 period 无新证据 | 行为趋于平稳，逐步归还 shadow simulation 控制权 |

### 2.4 与 Shadow Simulation 的交互

- Escalation 状态下，`GetCurrentTimeout()` 返回 `escalated_timeout_idx` 对应的值，**覆盖** shadow simulation 的选择
- Shadow simulation 持续运行（不停止），保持对真实访问模式的跟踪
- 降级后，shadow simulation 的最新仲裁结果立即生效，无缝恢复

---

## 3. 参数空间

| 参数 | 符号 | 候选值 | 说明 |
|------|------|--------|------|
| 连续命中阈值 | N | **3, 5, 8, 12** | RE 条目连续命中多少次触发 escalation |
| 提升步长 | STEP | **1, 2, 3** | 每次提升跳过几个 timeout 档位 |
| 是否允许 open-page | ALLOW_OPEN | **true, false** | 是否允许 escalation 到 INT_MAX（等效 open-page） |
| 衰减周期数 | DECAY_PERIODS | **3, 5, 10** | 多少个 arbitration period 无新证据后尝试降级 |
| Conflict 降级阈值 | CONFLICT_THR | **1, 3, 5** | 单 period 内多少次 conflict 触发立即降级 |

---

## 4. 实验配置

### 4.1 Baseline（3 个）

| 标签 | 策略 | 说明 |
|------|------|------|
| `open_page` | OPEN_PAGE | 纯 open-page baseline |
| `GS_baseline` | GS（原始） | 当前 GS 实现，timeout 候选 {50..800}，RE=64 |
| `GS_NOHOTROW` | GS_NOHOTROW | 消融：无 RE Store |

### 4.2 Phase 1: N / STEP 参数扫描（8 个）

固定 ALLOW_OPEN=false, DECAY_PERIODS=5, CONFLICT_THR=3：

| 标签 | N | STEP | 说明 |
|------|---|------|------|
| `GS_esc_N3_S1` | 3 | 1 | 激进触发，保守提升 |
| `GS_esc_N3_S2` | 3 | 2 | 激进触发，中等提升 |
| `GS_esc_N5_S1` | 5 | 1 | 中等触发，保守提升 |
| `GS_esc_N5_S2` | 5 | 2 | 中等触发，中等提升 |
| `GS_esc_N5_S3` | 5 | 3 | 中等触发，激进提升 |
| `GS_esc_N8_S1` | 8 | 1 | 保守触发，保守提升 |
| `GS_esc_N8_S2` | 8 | 2 | 保守触发，中等提升 |
| `GS_esc_N12_S2` | 12 | 2 | 极保守触发 |

**分析要点：**
- Geomean IPC vs GS_baseline（挑选 top-3 配置）
- lbm-like 集的 per-benchmark speedup
- hpcc/hashjoin 是否 regress

**输出：** 最佳 (N, STEP) 组合

### 4.3 Phase 2: Open-page Escalation（3 个）

使用 Phase 1 最佳 N/STEP：

| 标签 | ALLOW_OPEN | 说明 |
|------|------------|------|
| `GS_esc_best` | false | Phase 1 最佳配置（复用） |
| `GS_esc_open` | true | 允许 escalate 到 open-page |
| `GS_esc_open_ext` | true + 扩展 timeout {50..3200,INF} | 完整扩展候选集 |

**分析要点：**
- Open-page escalation 是否对 lbm/sphinx3 有额外收益
- 是否引入 pathological case（某 bank 长期锁在 open-page）
- 扩展 timeout {1600, 3200} 的选择率

**输出：** 是否采用 open-page escalation

### 4.4 Phase 3: 降级策略敏感性（4 个）

使用 Phase 2 最佳配置：

| 标签 | DECAY_PERIODS | CONFLICT_THR | 说明 |
|------|---------------|--------------|------|
| `GS_esc_D3_C1` | 3 | 1 | 快降级，敏感 conflict |
| `GS_esc_D5_C3` | 5 | 3 | 中等（默认） |
| `GS_esc_D10_C5` | 10 | 5 | 慢降级，容忍 conflict |
| `GS_esc_nodegrade` | INF | INF | 消融：永不降级 |

**分析要点：**
- 降级频率 vs 性能
- nodegrade 消融：量化降级路径的实际价值
- 相变 workload（不同 phase 行为差异大）的适应性

**输出：** 最佳降级参数

### 4.5 Phase 4: 组合消融 & 最终评估（3 个）

| 标签 | 说明 |
|------|------|
| `GS_esc_final` | 全部最优参数组合 |
| `GS_ext_timeout_only` | 仅扩展 timeout 候选集 {50..3200}，不做 RE-driven escalation |
| `GS_RE128` | 仅增大 RE Store 到 128 项，不做 escalation |

**分析要点：**
- Escalation 的增量贡献（与单纯扩展 timeout / 增大 RE 比较）
- 三种改进手段的正交性
- 全量 geomean 和 per-suite 分析

**输出：** 最终推荐配置

---

## 5. Benchmark 选择

### 5.1 全量评估

所有 62 个 benchmark（`benchmarks_selected.tsv`），用于计算 geomean speedup，确保不 regress。

### 5.2 重点关注集（13 个 lbm-like benchmark）

RE-driven escalation 预期收益最大的 benchmark：

| Benchmark | TO_Acc | 800c占比 | RE_Acc | 预期收益 |
|-----------|--------|---------|--------|---------|
| spec06/lbm/ref | 39.1% | 34.7% | 92.9% | 极高 |
| crono/PageRank/roadNet-CA | 42.9% | 18.6% | 79.7% | 高 |
| spec17/lbm/ref | 43.8% | 28.2% | 93.0% | 极高 |
| ligra/PageRank/roadNet-CA | 47.7% | 18.8% | 91.6% | 高 |
| spec06/sphinx3/ref | 51.4% | 49.0% | 86.8% | 极高 |
| ligra/PageRankDelta/roadNet-CA | 51.9% | 31.7% | 93.9% | 高 |
| ligra/Triangle/roadNet-CA | 55.9% | 32.1% | 89.7% | 高 |
| ligra/BFS-Bitvector/soc-pokec | 57.5% | 25.5% | 87.8% | 高 |
| spec06/wrf/ref | 61.6% | 22.7% | 92.1% | 中 |
| spec17/gcc/ref32-O5 | 62.9% | 24.5% | 84.9% | 中 |
| spec06/soplex/pds | 63.8% | 15.3% | 74.9% | 中 |
| spec06/leslie3d/ref | 64.4% | 21.7% | 85.4% | 中 |
| ligra/BC/Amazon0312 | 65.1% | 16.9% | 83.8% | 中 |

### 5.3 负面敏感集

RE 准确率低的 benchmark，escalation 可能放大错误：

| Benchmark | RE_Acc | 风险 |
|-----------|--------|------|
| hpcc/RandomAccess | 10.7% | 高——RE 命中多数无效，escalation 会放大错误 |
| hashjoin | 79.8% | 中 |
| crono（suite 均值） | 60.0% | 中 |

---

## 6. 评估指标

### 6.1 性能指标

| 指标 | 来源 | 说明 |
|------|------|------|
| IPC | ChampSim 输出 | 主要性能指标 |
| Geomean Speedup vs open_page | compare_ipc.py | 与 GS_baseline 的 +1.53% 对比 |
| Geomean Speedup vs GS_baseline | compare_ipc.py | 增量收益 |
| Per-benchmark IPC delta | compare_ipc.py | 识别 winner/loser |

### 6.2 新增性能计数器

```
gs_re_escalation_triggers      — RE 触发的 timeout 提升次数
gs_re_escalation_demotions     — timeout 降级次数（conflict 驱动 + decay 驱动）
gs_re_escalation_active_banks  — 仲裁时处于 escalated 状态的 bank 数
gs_escalated_timeout_dist.0-9  — escalation 后的 timeout 分布
gs_re_freed_by_escalation      — 因 escalation 释放的 RE 条目数
```

### 6.3 诊断指标（从已有计数器推导）

| 指标 | 公式 | 预期变化 |
|------|------|---------|
| Timeout 准确率 | correct / (correct+wrong) | 上升（减少对热行的错误 precharge） |
| RE 命中次数 | gs_re_hits | 下降（热行被 escalation 处理，不再需要 RE 拦截） |
| RE 命中倍率 | re_hits / re_insertions | 下降（条目更快释放） |
| RE Store 饱和度 | re_evictions / re_insertions | 下降（容量压力缓解） |
| 平均实际 timeout | weighted avg of timeout_dist | 上升（更多 bank 使用高 timeout） |
| 800c+ 占比 | sum(dist[6..9]) / sum(dist) | 上升 |
| On-demand PRE 比例 | ondemand / (ondemand+timeout) | 可能微降 |

---

## 7. 实验执行流程

```
Phase 1: N / STEP 参数扫描
  ├─ 8 配置 × 62 benchmarks × ~13 slices/benchmark
  ├─ 分析:
  │   ├─ Geomean IPC vs GS_baseline（挑选 top-3）
  │   ├─ lbm-like 集的 per-benchmark speedup
  │   └─ hpcc/hashjoin 是否 regress
  └─ 输出: 最佳 (N, STEP) 组合

Phase 2: Open-page Escalation
  ├─ 3 配置 × 62 benchmarks
  ├─ 分析:
  │   ├─ open-page escalation 是否对 lbm/sphinx3 有额外收益
  │   ├─ 是否引入 pathological case（某 bank 长期锁在 open-page）
  │   └─ 扩展 timeout {1600, 3200} 的选择率
  └─ 输出: 是否采用 open-page escalation

Phase 3: 降级策略敏感性
  ├─ 4 配置 × 62 benchmarks
  ├─ 分析:
  │   ├─ 降级频率 vs 性能
  │   ├─ nodegrade 消融：量化降级路径的实际价值
  │   └─ 相变 workload 的适应性
  └─ 输出: 最佳降级参数

Phase 4: 组合消融 & 最终评估
  ├─ 最终配置 vs GS_baseline vs GS_ext_timeout_only vs GS_RE128
  ├─ 分析:
  │   ├─ Escalation 的增量贡献（与单纯扩展 timeout/增大 RE 比较）
  │   ├─ 三种改进手段的正交性
  │   └─ 全量 geomean 和 per-suite 分析
  └─ 输出: 最终推荐配置
```

---

## 8. 预期结果与验证假设

### 8.1 核心假设

| # | 假设 | 验证方法 |
|---|------|---------|
| H1 | lbm-like benchmark 的 IPC 显著提升（>2%） | per-benchmark IPC delta |
| H2 | RE Store 命中数大幅下降（>50%） | gs_re_hits counter |
| H3 | Timeout 准确率提升至 >93% | gs_timeout_correct / total |
| H4 | hpcc/hashjoin 等随机访问不 regress（<0.5%） | per-benchmark IPC delta |
| H5 | 负反馈闭环成立：escalation 后 RE 插入率下降 | gs_re_insertions counter |
| H6 | 全量 geomean speedup vs open_page > +1.53%（优于 GS_baseline） | compare_ipc.py geomean |

### 8.2 风险与缓解

| 风险 | 表现 | 缓解措施 |
|------|------|---------|
| 过度 escalation | 某些 bank timeout 飙升，增加 conflict | 降级机制 + CONFLICT_THR |
| hpcc regression | RE 准确率仅 10.7%，错误 escalation | escalation 前检查 RE 条目 usefulness |
| Phase 变化不适应 | workload 从高局部性切换到低局部性 | DECAY_PERIODS 确保及时降级 |
| Shadow simulation 干扰 | escalation 覆盖仲裁结果 | shadow simulation 持续运行，降级后无缝恢复 |

---

## 9. 结果目录结构

```
results/
  open_page/                    # Baseline 1
  GS_baseline/                  # Baseline 2 (现有 GS)
  GS_NOHOTROW/                  # Baseline 3
  GS_esc_N3_S1/                 # Phase 1
  GS_esc_N3_S2/
  GS_esc_N5_S1/
  GS_esc_N5_S2/
  GS_esc_N5_S3/
  GS_esc_N8_S1/
  GS_esc_N8_S2/
  GS_esc_N12_S2/
  GS_esc_open/                  # Phase 2
  GS_esc_open_ext/
  GS_esc_D3_C1/                 # Phase 3
  GS_esc_D5_C3/
  GS_esc_D10_C5/
  GS_esc_nodegrade/
  GS_esc_final/                 # Phase 4
  GS_ext_timeout_only/
  GS_RE128/
```

---

## 10. 设计要点总结

本方案的核心优势是**分阶段递进**：先扫描最关键的 N/STEP 参数，再逐步探索 open-page escalation 和降级策略，最后与其他改进手段做正交对比。每个 phase 的决策输入下一个 phase，避免全组合爆炸（总计 ~21 个实验配置，而非 4 × 3 × 2 × 3 × 3 = 216 的全笛卡尔积）。
