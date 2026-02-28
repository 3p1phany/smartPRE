# Phase 2 实验方案：Open-page Escalation

**日期:** 2026-02-28
**基于:** `re_driven_timeout_escalation.md` §4.3 Phase 2 定义
**前置:** Phase 1 结果 + `selected_configs_benchmarks.md` 筛选

---

## 1. Phase 1 结果总结

### 1.1 Phase 1 最佳 N/STEP 选择

从 Phase 1 的 8 个配置中，基于 `selected_configs_benchmarks.md` 筛选出 4 个在 11 个 benchmark 上有持续正向改善的配置：

| 配置 | N | STEP | GEOMEAN vs GS | 11-benchmark 表现 |
|------|---|------|--------------|-----------------|
| N12_S2 | 12 | 2 | **-0.10%** | 最稳定，Triangle-Counting +0.83% |
| N8_S2 | 8 | 2 | -0.12% | 均衡，PageRank/roadNet +0.56% |
| N5_S3 | 5 | 3 | -0.14% | 天花板最高，omnetpp +0.92% |
| N3_S2 | 3 | 2 | -0.15% | 激进触发 |

**Phase 2 基准配置选择：**

原方案要求"使用 Phase 1 最佳 N/STEP"。根据以下考量选择 **两个** 基准配置：

- **N12_S2（主配置）**：GEOMEAN regression 最小（-0.10%），escalation 触发最保守，最适合探索 open-page 这一激进提升策略
- **N5_S3（辅配置）**：winner 集天花板最高（omnetpp +0.92%），STEP=3 的激进提升与 open-page 的激进性质匹配

### 1.2 Phase 1 关键发现对 Phase 2 的启示

| 发现 | 对 Phase 2 的意义 |
|------|-----------------|
| 800c+（1600c/3200c）占 Phase 1 escalation 后 57% (lbm) | Open-page 是 escalation 方向的自然极限，值得测试 |
| RE 释放后保护丧失是 regression 主因 | Open-page 模式下 bank 不再做 timeout PRE → RE 不再被查询 → 问题可能加剧或彻底不同 |
| 11 个 benchmark 改善来自 per-bank 靶向 escalation | Open-page escalation 提供更强的 per-bank 靶向保护 |
| De-escalation rate 在 soc-pokec 上仅 48% | Open-page 的 conflict-driven 降级更关键，需监控 |

---

## 2. 实验配置

### 2.1 Baseline（已有，复用）

| 标签 | 说明 | 数据来源 |
|------|------|---------|
| `GS_1c` | GS baseline，7 候选 {50..800} | `results/GS_1c/` |
| `open_page_1c` | 纯 open-page baseline | `results/open_page_1c/` |
| `GS_ext_timeout_only` | 9 候选 {50..3200}，无 escalation | `results/GS_ext_timeout_only/` |
| `GS_esc_N12_S2` | Phase 1 最佳，ALLOW_OPEN=false | `results/GS_esc_N12_S2/` |
| `GS_esc_N5_S3` | Phase 1 辅，ALLOW_OPEN=false | `results/GS_esc_N5_S3/` |

### 2.2 Phase 2 新增配置（4 个）

对应原方案 §4.3 的 3 种策略（best/open/open_ext），在两个基准配置上展开：

| 标签 | 基准 | ALLOW_OPEN | Timeout 候选集 | 说明 |
|------|------|------------|---------------|------|
| `GS_esc_open_N12_S2` | N=12, S=2 | true | 10: {50..3200, **INF**} | 主配置，允许 escalate 到 open-page |
| `GS_esc_open_N5_S3` | N=5, S=3 | true | 10: {50..3200, **INF**} | 辅配置，允许 escalate 到 open-page |
| `GS_esc_open_ext_N12_S2` | N=12, S=2 | true | 10: {50..3200, **INF**} + shadow 可选 INF | 完整扩展：shadow simulation 也可仲裁选择 INF |
| `GS_esc_open_ext_N5_S3` | N=5, S=3 | true | 10: {50..3200, **INF**} + shadow 可选 INF | 完整扩展 |

**三种策略的区别：**

| | `GS_esc_best`（Phase 1） | `GS_esc_open` | `GS_esc_open_ext` |
|---|---|---|---|
| Escalation 上限 | idx 8 (3200c) | **idx 9 (INF/open-page)** | idx 9 (INF/open-page) |
| Shadow simulation 候选 | 9 个 {50..3200} | 9 个 {50..3200} | **10 个 {50..3200, INF}** |
| Open-page 到达路径 | 不可能 | 仅通过 RE-driven escalation | RE-driven escalation **或** shadow 自主仲裁 |

`open` 和 `open_ext` 的核心区别：`open` 中只有 RE-driven escalation 能将 bank 推入 open-page 模式（靶向），而 `open_ext` 中 shadow simulation 也可能通过正常仲裁选择 INF（全局）。这决定了 open-page 行为是 per-bank 精准触发还是 channel-wide 扩散。

### 2.3 其他固定参数

| 参数 | 值 | 说明 |
|------|-----|------|
| DECAY_PERIODS | 5 | Phase 1 固定值，Phase 3 再调 |
| CONFLICT_THR | 3 | Phase 1 固定值，Phase 3 再调 |
| RE Store 大小 | 64 | 原始值 |
| 替换策略 | FIFO + conflict 优先 | 原始值 |

---

## 3. 代码修改

### 3.1 扩展 Timeout 候选集（含 INF）

`command_queue.h`：

```cpp
#ifdef GS_ALLOW_OPEN_PAGE
static constexpr int GS_TIMEOUT_COUNT = 10;
static constexpr int GS_TIMEOUT_VALUES[GS_TIMEOUT_COUNT] =
    {50, 100, 150, 200, 300, 400, 800, 1600, 3200, INT_MAX};
// INT_MAX 代表 open-page 模式：bank 不再执行 timeout precharge
#else
static constexpr int GS_TIMEOUT_COUNT = 9;
static constexpr int GS_TIMEOUT_VALUES[GS_TIMEOUT_COUNT] =
    {50, 100, 150, 200, 300, 400, 800, 1600, 3200};
#endif
```

**影响范围**：`GSShadowState` 的 `hits[]`、`conflicts[]`、`next_cas_state[]` 数组自动扩展到 10。`GS_ArbitrateTimeout` 循环自动适配。`gs_timeout_dist` 向量长度从 9 扩展到 10。

### 3.2 Timeout Precharge 路径处理 INF

`controller.cc` ClockTick 中 timeout 递减逻辑：

当 `GetCurrentTimeout()` 返回 `INT_MAX` 时，timeout_counter 也为 INT_MAX，实质上永远不会归零 → 该 bank 不再触发 timeout precharge → 等效 open-page 模式。

**需确认**：timeout_counter 的递减 `timeout_counter[i]--` 在 INT_MAX 下不溢出。由于 counter 为 `int` 类型，INT_MAX - 1 仍然 > 0，不会在合理仿真周期内归零（数十亿 cycles 仍远小于 INT_MAX ≈ 2.1×10⁹）。但应加一个安全检查：

```cpp
// controller.cc: timeout 递减
if (cmd_queue_.timeout_counter[i] > 0 &&
    cmd_queue_.timeout_counter[i] != INT_MAX) {  // INF不递减
    cmd_queue_.timeout_counter[i]--;
}
```

### 3.3 Shadow Simulation 处理 INF

**`open` 模式（shadow 不可选 INF）**：

`GS_ArbitrateTimeout` 中的仲裁循环上限设为 `GS_TIMEOUT_COUNT - 1`（排除 INF），仅 escalation 可达 idx 9：

```cpp
#if defined(GS_ALLOW_OPEN_PAGE) && !defined(GS_OPEN_EXT)
    // open 模式: shadow 仲裁排除 INF
    int arbitration_count = GS_TIMEOUT_COUNT - 1;
#else
    // open_ext 模式或非 open-page: shadow 仲裁包含全部候选
    int arbitration_count = GS_TIMEOUT_COUNT;
#endif
```

**`open_ext` 模式（shadow 可选 INF）**：

`GS_ProcessACT` 中 INF 的 gap 计算：当 timeout_val = INT_MAX 时，gap < timeout_val 恒成立 → 同行 ACT 恒为 HIT，不同行 ACT 恒为 CONFLICT。这在语义上正确：open-page 模式下，行永不关闭，同行访问必定 hit，不同行访问必定 conflict。

### 3.4 De-escalation 对 Open-page 的处理

当 bank 处于 escalated + open-page (idx=9) 状态时，conflict-driven 降级尤为关键：

```cpp
// GS_ArbitrateTimeout 中的 de-escalation
auto& esc = bank_escalation_state_[q];
if (esc.escalated) {
    int esc_idx = esc.escalated_timeout_idx;
    if (esc_idx == GS_TIMEOUT_COUNT - 1 && GS_TIMEOUT_VALUES[esc_idx] == INT_MAX) {
        // Open-page 状态下：shadow simulation 的 conflict 计数直接可用
        // 因为 INF timeout = 行永不关闭 → 每次不同行 ACT 都是 conflict
        // 此处 conflict 由 shadow simulation 在 ProcessACT 中记录
    }
    // 后续逻辑不变：conflict > THR → 快降级，否则 decay
}
```

关键语义：open-page bank 的 conflict 计数反映了"如果该 bank 一直 open-page，有多少次不同行请求被迫做 on-demand precharge"。如果 conflict 数超过阈值，说明 open-page 不适合该 bank，应降级。

### 3.5 新增性能计数器

```cpp
// 在 simple_stats.cc 中注册
InitStat("gs_open_page_bank_cycles", "counter",
         "Total cycles banks spent in open-page escalation mode");
InitStat("gs_open_page_escalation_count", "counter",
         "Number of times escalation reached open-page (INF)");
InitStat("gs_open_page_demotion_count", "counter",
         "Number of times open-page banks were demoted");

// gs_timeout_dist 和 gs_escalated_timeout_dist 长度改为 10
// idx 9 对应 INF (open-page)
```

### 3.6 构建配置

#### `GS_esc_open` 模式（escalation 可达 INF，shadow 不可选 INF）

```bash
# GS_esc_open_N12_S2
make EXTRA_CXXFLAGS="-DGS_ESC_HIT_THRESHOLD=12 -DGS_ESC_STEP=2 -DGS_ALLOW_OPEN_PAGE" -j8

# GS_esc_open_N5_S3
make EXTRA_CXXFLAGS="-DGS_ESC_HIT_THRESHOLD=5 -DGS_ESC_STEP=3 -DGS_ALLOW_OPEN_PAGE" -j8
```

#### `GS_esc_open_ext` 模式（escalation + shadow 均可达 INF）

```bash
# GS_esc_open_ext_N12_S2
make EXTRA_CXXFLAGS="-DGS_ESC_HIT_THRESHOLD=12 -DGS_ESC_STEP=2 -DGS_ALLOW_OPEN_PAGE -DGS_OPEN_EXT" -j8

# GS_esc_open_ext_N5_S3
make EXTRA_CXXFLAGS="-DGS_ESC_HIT_THRESHOLD=5 -DGS_ESC_STEP=3 -DGS_ALLOW_OPEN_PAGE -DGS_OPEN_EXT" -j8
```

---

## 4. Benchmark 选择

### 4.1 全量评估

62 个 benchmark（`benchmarks_selected.tsv`），与 Phase 1 一致。计算 GEOMEAN 确保不引入全局 regression。

### 4.2 Phase 1 筛选的 11 个 Winner Benchmark（重点分析）

| Benchmark | Phase 1 最佳 (N12_S2) | Phase 1 最佳 (N5_S3) | 预期 open-page 效果 |
|-----------|---------------------|---------------------|-------------------|
| crono/Triangle-Counting/higgs | +0.83% | +0.57% | 图三角计数，部分 bank 高行局部性，open-page 可能进一步提升 |
| spec06/omnetpp/ref | +0.73% | +0.92% | 网络模拟，长事件处理期间行驻留时间长 |
| ligra/Triangle/soc-pokec | +0.57% | +0.81% | 三角计数，类似 Triangle-Counting |
| crono/DFS/higgs | +0.42% | +0.22% | 图搜索，访问模式可预测 |
| crono/Triangle-Counting/roadNet-CA | +0.65% | +0.69% | 路网图，稀疏结构 |
| crono/PageRank/roadNet-CA | +0.52% | +0.53% | 路网 PR，行重访间隔长 |
| spec17/omnetpp/ref | +0.28% | +0.43% | 同 spec06/omnetpp |
| crono/Community/higgs | +0.25% | +0.13% | 社区检测 |
| npb/IS | +0.16% | +0.17% | 整数排序 |
| crono/SSSP/higgs | +0.05% | -0.07% | 最短路径，增益边界 |
| spec17/xz/cld | +0.12% | -0.09% | 压缩，增益不稳定 |

### 4.3 lbm-like 高行局部性集（open-page 重点受益候选）

原方案 §4.3 分析要点第一条："Open-page escalation 是否对 lbm/sphinx3 有额外收益"。

| Benchmark | Phase 1 N12_S2 | 800c占比 (baseline) | TO_Acc (baseline) | 预期 |
|-----------|---------------|--------------------|--------------------|------|
| spec06/lbm/ref | +0.06% | 34.7% | 39.1% | **核心观测对象**：TO_Acc 最低，open-page 可能提供额外收益 |
| spec17/lbm/ref | +0.08% | 28.2% | 43.8% | 同上 |
| spec06/sphinx3/ref | +0.04% | 49.0% | 51.4% | 800c占比最高，最需要更高 timeout |
| spec06/bwaves/ref | -0.33% | 52.0% | 70.2% | ext_timeout_only 中 3200c 占 44.5%，可能自然选择 INF |

### 4.4 Pathological Case 监控集

| Benchmark | 风险 | 监控指标 |
|-----------|------|---------|
| hpcc/RandomAccess | RE_Acc=10.7%，escalation 基于不可靠的 RE 命中 | open_page_escalation_count 应极低 |
| hashjoin/hj-2-NPO_st | Phase 1 regression -0.62%~-1.05% | open-page 是否加剧 regression |
| ligra/PageRank/soc-pokec | Phase 1 regression -1.14%~-1.50% | 降级机制能否将 open-page bank 及时拉回 |

---

## 5. 分析计划

### 5.1 对比矩阵

```bash
# 对比 1: open-page escalation vs Phase 1 best（量化 open-page 增量）
python3 scripts/compare_ipc.py \
    --a results/GS_esc_N12_S2 --b results/GS_esc_open_N12_S2 \
    --a-label N12S2 --b-label open_N12S2

# 对比 2: open-page escalation vs GS baseline（总收益）
python3 scripts/compare_ipc.py \
    --a results/GS_1c --b results/GS_esc_open_N12_S2 \
    --a-label GS --b-label open_N12S2

# 对比 3: open vs open_ext（shadow 可选 INF 的增量）
python3 scripts/compare_ipc.py \
    --a results/GS_esc_open_N12_S2 --b results/GS_esc_open_ext_N12_S2 \
    --a-label open --b-label open_ext

# 对比 4: open_ext vs pure open_page（escalation 靶向 open-page vs 全局 open-page）
python3 scripts/compare_ipc.py \
    --a results/open_page_1c --b results/GS_esc_open_ext_N12_S2 \
    --a-label open_page --b-label open_ext

# 对比 5: N12_S2 vs N5_S3 两个基准在 open 模式下的差异
python3 scripts/compare_ipc.py \
    --a results/GS_esc_open_N12_S2 --b results/GS_esc_open_N5_S3 \
    --a-label open_N12 --b-label open_N5
```

### 5.2 分析维度（对应原方案 §4.3 分析要点）

#### 要点 1: Open-page escalation 是否对 lbm/sphinx3 有额外收益

| 指标 | 来源 | 判断标准 |
|------|------|---------|
| lbm IPC delta (open vs Phase 1) | compare_ipc.py | > +0.1% 为有效收益 |
| sphinx3 IPC delta (open vs Phase 1) | compare_ipc.py | > +0.1% 为有效收益 |
| bwaves IPC delta | compare_ipc.py | bwaves 在 ext_timeout_only 中已大量使用 3200c，INF 是否更好 |
| open-page bank 在 lbm 上的占比 | gs_open_page_bank_cycles / total_cycles | 量化 INF 的实际使用率 |
| lbm 的 TO_Acc 变化 | gs_timeout_correct / total | open-page bank 不触发 timeout → TO_Acc 计算基数变化 |

#### 要点 2: 是否引入 pathological case（某 bank 长期锁在 open-page）

| 指标 | 来源 | 判断标准 |
|------|------|---------|
| gs_open_page_bank_cycles | ddr.json | 单个 bank 占总 cycles > 50% 为 pathological |
| gs_open_page_demotion_count / gs_open_page_escalation_count | ddr.json | ratio < 50% 说明降级不充分 |
| 最长连续 open-page 周期 | 需新计数器或日志 | > 1M cycles 值得关注 |
| soc-pokec、hashjoin 上的 conflict 增加量 | num_on_demand_pres 变化 | on-demand PRE 显著增加说明 open-page 不当 |
| gs_escalated_timeout_dist[9] 占比 | ddr.json | INF 在 escalation 分布中的占比 |

#### 要点 3: 扩展 timeout {1600, 3200} 的选择率

| 指标 | 来源 | 判断标准 |
|------|------|---------|
| gs_timeout_dist[7] (1600c) | ddr.json | open vs Phase 1 对比 |
| gs_timeout_dist[8] (3200c) | ddr.json | open vs Phase 1 对比 |
| gs_timeout_dist[9] (INF) | ddr.json | 仅 open_ext 模式有值 |
| shadow simulation 仲裁中 INF 的 gain 值 | 需日志或新计数器 | INF 在仲裁中的竞争力 |

**关键对比**：Phase 1 中 escalation 的 1600c+3200c 使用率已知（如 lbm 57.4%）。Phase 2 中引入 INF 后，是否分流了 3200c 的份额？还是 INF 吸引了新的 bank？

### 5.3 `open` vs `open_ext` 的差异分析

这是 Phase 2 特有的消融维度：

| 场景 | 结论 | 后续方向 |
|------|------|---------|
| open ≈ open_ext | Shadow simulation 不会自主选择 INF，INF 仅通过 RE escalation 到达 | RE-driven 的靶向性是关键，shadow 全局仲裁不适合极端 timeout |
| open < open_ext | Shadow simulation 选择 INF 有额外价值 | 全局仲裁 + 靶向 escalation 互补 |
| open > open_ext | Shadow simulation 选 INF 有害（类似 ext_timeout_only 的悖论） | 限制 INF 仅通过 escalation 触发 |

### 5.4 11 个 Winner Benchmark 的 Per-Benchmark 深度分析

对每个 winner benchmark 构建完整对比行：

```
               GS     ext_TO  N12S2   N5S3   open_N12  open_N5  open_ext_N12  open_ext_N5
benchmark_i    base   Δ1      Δ2      Δ3     Δ4        Δ5       Δ6            Δ7
```

判断：
- Δ4 > Δ2？→ open-page 在 N12_S2 基础上有增量
- Δ5 > Δ3？→ open-page 在 N5_S3 基础上有增量
- Δ6 vs Δ4？→ shadow 可选 INF 的影响

---

## 6. 预期结果与假设验证

### 6.1 假设

| # | 假设 | 验证方法 |
|---|------|---------|
| H1 | lbm/sphinx3 在 open-page escalation 下 IPC 提升 > Phase 1 | per-benchmark delta |
| H2 | 11 个 winner benchmark 的增益 ≥ Phase 1 同配置 | per-benchmark delta |
| H3 | De-escalation 机制能有效将 open-page bank 降级 | demotion_count / escalation_count > 30% |
| H4 | hpcc/hashjoin 上 open-page escalation 触发极少 | gs_open_page_escalation_count ≈ 0 |
| H5 | open_ext 中 shadow simulation 在高局部性 workload 上自主选择 INF | gs_timeout_dist[9] > 0 |
| H6 | GEOMEAN regression 不超过 Phase 1 同配置 | GEOMEAN 对比 |

### 6.2 风险

| 风险 | 表现 | 缓解 |
|------|------|------|
| Open-page bank 长期无法降级 | 某 bank 锁定 INF 超过 100 万 cycles | CONFLICT_THR=3 应足够敏感，若不够则需 Phase 3 调整 |
| Open-page 加剧 RE 释放的损害 | INF bank 的 RE 条目被释放后完全无保护 | 监控 gs_re_freed_by_escalation 中 idx=9 的占比 |
| Shadow simulation 在 open_ext 中过度选择 INF | 类似 ext_timeout_only 的悖论——INF 无处不在但无性能收益 | 对比 open vs open_ext 可确认 |
| lbm-like workload 的 phase 变化在 open-page 期间未被检测 | 行为从高局部性切换到低局部性时 open-page 制造大量 conflict | Phase 3 的降级策略敏感性实验解决 |

---

## 7. 执行流程

```
Phase 2: Open-page Escalation
  ├─ 代码修改:
  │   ├─ command_queue.h: GS_ALLOW_OPEN_PAGE 宏控制 10 候选集
  │   ├─ controller.cc: timeout_counter INT_MAX 不递减
  │   ├─ command_queue.cc: GS_ArbitrateTimeout 中 open vs open_ext 仲裁范围
  │   ├─ command_queue.cc: GS_ProcessACT 中 INF 的 hit/conflict 语义
  │   └─ simple_stats.cc: 注册 3 个新计数器，gs_timeout_dist 扩展到 10
  │
  ├─ 构建 4 个 libdramsim3.so + champsim:
  │   ├─ GS_esc_open_N12_S2
  │   ├─ GS_esc_open_N5_S3
  │   ├─ GS_esc_open_ext_N12_S2
  │   └─ GS_esc_open_ext_N5_S3
  │
  ├─ 运行 4 配置 × 62 benchmarks
  │
  ├─ 分析:
  │   ├─ Open-page escalation 是否对 lbm/sphinx3 有额外收益 (§5.2 要点1)
  │   ├─ 是否引入 pathological case (§5.2 要点2)
  │   ├─ {1600, 3200, INF} 的选择率 (§5.2 要点3)
  │   ├─ open vs open_ext 消融 (§5.3)
  │   └─ 11 winner benchmark per-benchmark 分析 (§5.4)
  │
  └─ 输出: 是否采用 open-page escalation，以及 open vs open_ext 的选择
```

### 7.1 执行优先级

如果计算资源有限，建议按以下顺序执行：

1. **先跑 `GS_esc_open_N12_S2`**：主配置 + open-page，单个配置即可回答"open-page 是否有额外收益"
2. **再跑 `GS_esc_open_ext_N12_S2`**：同基准 + shadow 可选 INF，回答 open vs open_ext
3. **然后跑 N5_S3 变体**：验证结论在不同 N/STEP 下的一致性
4. **如果资源充足，可先在 11 个 winner benchmark 上快速验证**，确认方向后再扩展到 62 全量

---

## 8. 结果目录结构

```
results/
  # 已有
  open_page_1c/
  GS_1c/
  GS_ext_timeout_only/
  GS_esc_N12_S2/                   # Phase 1 best (复用)
  GS_esc_N5_S3/                    # Phase 1 alt (复用)

  # Phase 2 新增
  GS_esc_open_N12_S2/              # open-page escalation, N=12 S=2
  GS_esc_open_N5_S3/               # open-page escalation, N=5 S=3
  GS_esc_open_ext_N12_S2/          # open-page + shadow INF, N=12 S=2
  GS_esc_open_ext_N5_S3/           # open-page + shadow INF, N=5 S=3
```

---

## 9. Phase 2 输出 → Phase 3 衔接

Phase 2 输出"是否采用 open-page escalation"，供 Phase 3 使用：

| Phase 2 结论 | Phase 3 基准配置 |
|-------------|----------------|
| open > Phase 1 best | Phase 3 用 `GS_esc_open_bestN` 作为基准，扫描 DECAY_PERIODS / CONFLICT_THR |
| open ≈ Phase 1 best | Phase 3 用 `GS_esc_N12_S2`（Phase 1 best），ALLOW_OPEN=false |
| open < Phase 1 best | Phase 3 用 `GS_esc_N12_S2`，放弃 open-page 方向 |
| open_ext 显著优于 open | Phase 3 基准包含 open_ext，shadow 候选扩展为标准配置 |
