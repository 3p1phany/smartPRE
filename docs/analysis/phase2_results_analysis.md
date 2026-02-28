# Phase 2 实验结果分析：Open-page Escalation

**日期：** 2026-02-28
**实验方案：** `docs/experiments/phase2_experiment_plan.md`
**数据来源：** `champsim-la/results/GS_esc_open_*/`、`champsim-la/results/GS_esc_open_ext_*/`（4 种配置 × 62 benchmarks）
**Baseline：** `champsim-la/results/GS_1c/`（GS 原始实现）、`champsim-la/results/open_page_1c/`（纯 open-page）、`champsim-la/results/GS_ext_timeout_only_1c/`（扩展候选集无 escalation）
**Phase 1 参照：** `champsim-la/results/GS_esc_N12_S2/`、`champsim-la/results/GS_esc_N5_S3/`

---

## 1. 实验回顾

### 1.1 Phase 2 目标

Phase 2 探索将 RE-driven Timeout Escalation 的上限从 3200c 扩展到 INF（open-page 模式）：当某 bank 被 escalation 推到 idx=9（timeout=INT_MAX）时，该 bank 实质上进入 open-page 模式——不再执行 timeout precharge。

核心问题：
1. Open-page escalation 是否对 lbm/sphinx3 等高行局部性 workload 有额外收益？
2. 是否引入 pathological case（某 bank 长期锁定 open-page）？
3. `open` vs `open_ext`（shadow simulation 是否可自主选择 INF）的差异？

### 1.2 配置矩阵

| 标签 | N | STEP | ALLOW_OPEN | GS_OPEN_EXT | 说明 |
|------|---|------|------------|-------------|------|
| GS_esc_open_N12_S2 | 12 | 2 | true | false | **主配置**：escalation 可达 INF，shadow 不可选 INF |
| GS_esc_open_N5_S3 | 5 | 3 | true | false | 辅配置 |
| GS_esc_open_ext_N12_S2 | 12 | 2 | true | true | 完整扩展：shadow 也可仲裁选择 INF |
| GS_esc_open_ext_N5_S3 | 5 | 3 | true | true | 完整扩展 |

三种策略的区别：

| | Phase 1 (`GS_esc_best`) | `open` | `open_ext` |
|---|---|---|---|
| Escalation 上限 | idx 8 (3200c) | **idx 9 (INF)** | idx 9 (INF) |
| Shadow 候选集 | 9 个 {50..3200} | 9 个 {50..3200} | **10 个 {50..3200, INF}** |
| Open-page 到达路径 | 不可能 | 仅 RE-driven escalation | RE escalation **或** shadow 仲裁 |

---

## 2. IPC 总览

### 2.1 GEOMEAN Speedup（全 62 benchmarks，% vs GS baseline）

| 配置 | GEOMEAN | 变化率 |
|------|---------|--------|
| GS_1c (baseline) | — | 0.00% |
| GS_ext_timeout_only | 1.000136 | **+0.014%** |
| open_page_1c | 0.984901 | **-1.510%** |
| GS_esc_N12_S2 (Phase 1) | 0.998987 | -0.10% |
| GS_esc_N5_S3 (Phase 1) | 0.998592 | -0.14% |
| **GS_esc_open_N12_S2** | 0.999030 | **-0.097%** |
| **GS_esc_open_N5_S3** | 0.998633 | **-0.137%** |
| **GS_esc_open_ext_N12_S2** | 0.998838 | **-0.116%** |
| **GS_esc_open_ext_N5_S3** | 0.998586 | **-0.141%** |

Phase 2 所有配置的 GEOMEAN 均为负值（-0.097% ~ -0.141%），与 Phase 1 同基准配置几乎持平。纯 open-page 策略 GEOMEAN 为 -1.51%（灾难性），Phase 2 的 escalation 机制成功将 regression 控制在了 ~0.1%，但相对 Phase 1 无增量改善。

### 2.2 全量 Per-Benchmark IPC 变化（% vs GS baseline，按 open_N12_S2 排序）

```
benchmark                                       GS_IPC   ext_TO  open_pg  op_N12   op_N5  ox_N12   ox_N5
---------------------------------------------------------------------------------------------------------
crono/Triangle-Counting/higgs                   0.8933   -0.18%   -0.73%  +0.79%  +0.52%  +0.83%  +0.52%
spec06/omnetpp/ref                              0.5879   +0.08%   +1.16%  +0.73%  +0.79%  +0.71%  +0.83%
crono/Triangle-Counting/roadNet-CA              0.8085   +0.09%   +0.98%  +0.62%  +0.67%  +0.57%  +0.64%
ligra/Triangle/soc-pokec                        0.5881   -0.01%   -0.03%  +0.55%  +0.70%  +0.54%  +0.70%
crono/DFS/higgs                                 0.7051   -0.21%   -1.09%  +0.49%  +0.20%  +0.44%  +0.23%
crono/PageRank/roadNet-CA                       0.8877   +0.20%   +1.10%  +0.49%  +0.52%  +0.44%  +0.40%
spec17/omnetpp/ref                              0.7323   +0.04%   +0.45%  +0.30%  +0.34%  +0.27%  +0.32%
crono/Community/higgs                           0.9480   -0.17%   -0.98%  +0.26%  +0.15%  +0.24%  +0.12%
spec17/roms/ref                                 2.0439   +0.10%   +0.46%  +0.18%  +0.21%  +0.12%  +0.20%
npb/IS                                          1.5159   +0.00%   +0.14%  +0.16%  +0.12%  +0.15%  +0.15%
ligra/BFS-Bitvector/soc-pokec                   1.4667   +0.12%   +0.17%  +0.13%  +0.15%  +0.08%  +0.12%
spec17/xz/cld                                   0.8553   -0.03%   -2.07%  +0.13%  -0.05%  +0.12%  -0.07%
graph500/s16-e10                                0.8278   +0.09%   +0.09%  +0.12%  +0.15%  +0.09%  +0.14%
ligra/BC/soc-pokec                              0.5389   +0.14%   +0.21%  +0.12%  +0.11%  +0.14%  +0.12%
npb/CG                                         1.5508   +0.09%   +0.16%  +0.12%  +0.13%  +0.08%  +0.10%
spec17/lbm/ref                                  1.1211   +0.14%   +0.32%  +0.08%  +0.07%  +0.08%  +0.08%
ligra/BC/Amazon0312                             0.6476   +0.12%   +0.36%  +0.07%  +0.13%  +0.04%  +0.11%
spec06/lbm/ref                                  0.7631   +0.11%   +0.30%  +0.06%  +0.05%  +0.05%  +0.04%
spec06/soplex/pds                               0.5537   +0.07%   +0.26%  +0.06%  +0.04%  +0.05%  +0.02%
ligra/BellmanFord/Amazon0312                    0.4757   +0.06%   +0.20%  +0.05%  +0.04%  +0.07%  +0.03%
spec06/sphinx3/ref                              1.4335   +0.30%   +0.52%  +0.05%  +0.17%  -0.03%  +0.09%
crono/SSSP/higgs                                0.7643   -0.34%   -1.26%  +0.03%  -0.13%  +0.03%  -0.12%
hpcc/RandAcc                                    0.3078   -0.03%  -10.30%  -0.01%  -0.03%  +0.03%  -0.04%
spec06/astar/lakes                              0.5936   -0.02%   -0.13%  -0.01%  -0.01%  +0.02%  -0.04%
ligra/PageRankDelta/roadNet-CA                  0.6765   +0.10%   +0.30%  -0.01%  +0.06%  -0.01%  +0.03%
hpcc/RandAcc_LCG                                0.5116   +0.02%   -6.86%  -0.02%  -0.07%  -0.01%  -0.06%
ligra/Triangle/higgs                            0.7842   +0.14%   +0.64%  -0.02%  +0.25%  -0.07%  +0.27%
ligra/Radii/higgs                               0.5696   +0.04%   -0.07%  -0.04%  -0.00%  -0.08%  -0.03%
spec06/leslie3d/ref                             1.5141   -0.00%   -0.10%  -0.06%  -0.08%  -0.09%  -0.10%
spec17/cactuBSSN/ref                            2.1646   -0.02%   -1.64%  -0.06%  -0.12%  -0.07%  -0.12%
spec17/wrf/ref                                  2.1965   +0.05%   +0.04%  -0.07%  -0.06%  -0.10%  -0.07%
spec06/wrf/ref                                  1.8890   +0.06%   +0.05%  -0.07%  -0.03%  -0.10%  -0.08%
ligra/PageRankDelta/Amazon0312                  0.8309   +0.02%   -0.15%  -0.07%  -0.11%  -0.11%  -0.09%
ligra/PageRankDelta/higgs                       1.0207   -0.00%   -0.33%  -0.08%  -0.07%  -0.00%  -0.06%
spec06/milc/ref                                 0.9795   -0.05%   -0.02%  -0.08%  +0.06%  -0.12%  +0.04%
spec17/gcc/ref32-O5                             0.9894   +0.06%   +0.17%  -0.10%  -0.07%  -0.14%  -0.11%
ligra/BFSCC/soc-pokec-short                     0.8356   +0.12%   +0.18%  -0.10%  -0.00%  -0.17%  -0.02%
spec17/mcf/ref                                  0.5484   +0.01%   +0.10%  -0.12%  -0.12%  -0.19%  -0.15%
ligra/PageRank/roadNet-CA                       1.1369   +0.02%   -0.02%  -0.13%  -0.14%  -0.17%  -0.15%
spec06/cactusADM/ref                            2.3549   -0.15%   -2.76%  -0.15%  -0.26%  -0.16%  -0.22%
crono/Connected-Components/higgs                0.7172   -0.25%   -1.88%  -0.16%  -0.24%  -0.21%  -0.26%
ligra/Triangle/roadNet-CA                       0.7933   +0.03%   +0.04%  -0.16%  -0.12%  -0.20%  -0.14%
spec17/bwaves/bw1                               1.4374   -0.03%   -0.80%  -0.18%  -0.18%  -0.17%  -0.19%
ligra/MIS/soc-pokec                             0.6088   +0.02%   -0.55%  -0.20%  -0.18%  -0.20%  -0.19%
spec06/zeusmp/ref                               2.0499   -0.03%   -2.90%  -0.24%  -0.24%  -0.23%  -0.28%
ligra/CF/higgs                                  1.5572   -0.20%   -1.63%  -0.27%  -0.44%  -0.27%  -0.43%
ligra/PageRankDelta/soc-pokec                   0.5244   -0.03%   -2.11%  -0.28%  -0.47%  -0.28%  -0.47%
spec06/bwaves/ref                               1.1668   +0.30%   -0.30%  -0.31%  -0.24%  -0.35%  -0.18%
ligra/PageRank/higgs                            1.0262   -0.03%   -5.40%  -0.35%  -0.70%  -0.36%  -0.70%
hashjoin/hj-8-NPO_st                            0.3352   -0.08%   -6.39%  -0.36%  -0.59%  -0.34%  -0.55%
ligra/CF/roadNet-CA                             1.4201   +0.07%   +0.11%  -0.41%  -0.39%  -0.42%  -0.36%
spec06/mcf/ref                                  0.1998   -0.00%   -2.80%  -0.42%  -0.56%  -0.43%  -0.56%
ligra/Components-Shortcut/soc-pokec             0.9437   +0.06%   -1.86%  -0.45%  -0.49%  -0.47%  -0.48%
spec17/fotonik3d/ref                            1.0824   +0.08%   +0.11%  -0.46%  -0.42%  -0.43%  -0.39%
spec06/GemsFDTD/ref                             1.1713   +0.04%   -1.11%  -0.47%  -0.48%  -0.51%  -0.47%
ligra/CF/soc-pokec                              1.3057   -0.17%   -2.77%  -0.56%  -0.76%  -0.57%  -0.76%
hashjoin/hj-2-NPO_st                            0.4719   -0.05%   -9.59%  -0.62%  -0.67%  -0.61%  -0.67%
ligra/Radii/soc-pokec                           0.4501   -0.06%   -2.68%  -0.63%  -0.69%  -0.64%  -0.70%
ligra/Components/soc-pokec                      0.7509   -0.01%   -2.95%  -0.70%  -0.78%  -0.71%  -0.78%
crono/PageRank/soc-pokec                        0.4612   +0.13%   -8.18%  -0.88%  -1.27%  -1.05%  -1.11%
ligra/PageRank/soc-pokec                        0.7806   +0.00%   -7.97%  -1.13%  -1.49%  -1.14%  -1.50%
crono/PageRank/higgs                            0.5050   -0.06%   -9.36%  -1.15%  -1.31%  -1.13%  -1.34%
---------------------------------------------------------------------------------------------------------
GEOMEAN                                                  +0.01%   -1.51%  -0.10%  -0.14%  -0.12%  -0.14%
```

Winner/Loser 统计（阈值 ±0.1%，open_N12_S2）：Winners 15, Losers 27, Neutral 20, Total 62。

---

## 3. 分组分析

### 3.1 11 Winner Benchmarks（Phase 1 筛选集）

完整进展对比（% vs GS baseline）：

```
benchmark                               ext_TO  open_pg  P1_N12   P1_N5  op_N12   op_N5  ox_N12   ox_N5
--------------------------------------------------------------------------------------------------------------
crono/Triangle-Counting/higgs           -0.18%   -0.73%  +0.83%  +0.57%  +0.79%  +0.52%  +0.83%  +0.52%
spec06/omnetpp/ref                      +0.08%   +1.16%  +0.73%  +0.92%  +0.73%  +0.79%  +0.71%  +0.83%
ligra/Triangle/soc-pokec                -0.01%   -0.03%  +0.57%  +0.81%  +0.55%  +0.70%  +0.54%  +0.70%
crono/DFS/higgs                         -0.21%   -1.09%  +0.42%  +0.22%  +0.49%  +0.20%  +0.44%  +0.23%
crono/Triangle-Counting/roadNet-CA      +0.09%   +0.98%  +0.65%  +0.69%  +0.62%  +0.67%  +0.57%  +0.64%
crono/PageRank/roadNet-CA               +0.20%   +1.10%  +0.52%  +0.53%  +0.49%  +0.52%  +0.44%  +0.40%
spec17/omnetpp/ref                      +0.04%   +0.45%  +0.28%  +0.43%  +0.30%  +0.34%  +0.27%  +0.32%
crono/Community/higgs                   -0.17%   -0.98%  +0.25%  +0.13%  +0.26%  +0.15%  +0.24%  +0.12%
npb/IS                                  +0.00%   +0.14%  +0.16%  +0.17%  +0.16%  +0.12%  +0.15%  +0.15%
crono/SSSP/higgs                        -0.34%   -1.26%  +0.05%  -0.07%  +0.03%  -0.13%  +0.03%  -0.12%
spec17/xz/cld                           -0.03%   -2.07%  +0.12%  -0.09%  +0.13%  -0.05%  +0.12%  -0.07%
--------------------------------------------------------------------------------------------------------------
GEOMEAN                                 -0.05%   -0.22%  +0.42%  +0.39%  +0.41%  +0.35%  +0.39%  +0.34%
```

**关键发现：**

1. **Phase 2 在 11 Winner 上无增量改善**。GEOMEAN 从 Phase 1 的 +0.42%（N12_S2）微降至 +0.41%（open_N12_S2），差异在噪声范围内。
2. 逐 benchmark 比较，绝大多数 benchmark 表现持平：
   - 微弱改善（< +0.1%）：DFS/higgs（+0.42% → +0.49%）、Community/higgs（+0.25% → +0.26%）
   - 持平：omnetpp/06（+0.73% → +0.73%）、IS（+0.16% → +0.16%）
   - 微弱退步：Triangle-Counting/higgs（+0.83% → +0.79%）、PageRank/roadNet（+0.52% → +0.49%）
3. open-page escalation 的增量效果在 winner 集上几乎为零——INF timeout 未能为这些已从 escalation 获益的 benchmark 提供额外保护。

### 3.2 lbm-like 高行局部性 Benchmarks

```
benchmark                               ext_TO  open_pg  P1_N12   P1_N5  op_N12   op_N5  ox_N12   ox_N5
--------------------------------------------------------------------------------------------------------------
spec06/lbm/ref                          +0.11%   +0.30%  +0.06%  +0.06%  +0.06%  +0.05%  +0.05%  +0.04%
spec17/lbm/ref                          +0.14%   +0.32%  +0.08%  +0.08%  +0.08%  +0.07%  +0.08%  +0.08%
spec06/sphinx3/ref                      +0.30%   +0.52%  +0.04%  +0.14%  +0.05%  +0.17%  -0.03%  +0.09%
spec06/bwaves/ref                       +0.30%   -0.30%  -0.33%  -0.16%  -0.31%  -0.24%  -0.35%  -0.18%
--------------------------------------------------------------------------------------------------------------
GEOMEAN                                 +0.21%   +0.21%  -0.04%  +0.03%  -0.03%  +0.01%  -0.06%  +0.01%
```

**假设 H1 验证结果：不成立。**

- **lbm（06/17）**：Phase 2 与 Phase 1 完全一致（+0.06~0.08%），INF escalation 未提供额外收益。值得注意的是，纯 ext_timeout_only（+0.11~0.14%）和纯 open_page（+0.30~0.32%）在 lbm 上反而更好——说明 lbm 本身适合更大的 timeout，但 escalation 机制的 overhead 抵消了增益。
- **sphinx3**：Phase 2 与 Phase 1 接近，无显著差异。ext_timeout_only（+0.30%）和 open_page（+0.52%）仍优于所有 escalation 配置。
- **bwaves**：持续 regression（-0.31%），Phase 2 无缓解。bwaves 在 ext_timeout_only 中表现最好（+0.30%），说明其需要的是更大的 timeout 候选集，而非 escalation 驱动的动态调整。

### 3.3 Pathological / 风险 Benchmarks

```
benchmark                               ext_TO  open_pg  op_N12   op_N5  ox_N12   ox_N5
------------------------------------------------------------------------------------------
hpcc/RandAcc                            -0.03%  -10.30%  -0.01%  -0.03%  +0.03%  -0.04%
hashjoin/hj-2-NPO_st                    -0.05%   -9.59%  -0.62%  -0.67%  -0.61%  -0.67%
ligra/PageRank/soc-pokec                +0.00%   -7.97%  -1.13%  -1.49%  -1.14%  -1.50%
crono/PageRank/higgs                    -0.06%   -9.36%  -1.15%  -1.31%  -1.13%  -1.34%
------------------------------------------------------------------------------------------
GEOMEAN                                 -0.03%   -9.31%  -0.73%  -0.88%  -0.71%  -0.89%
```

**假设 H4 验证结果：部分成立。**

1. **RandAcc（RE_Acc=10.7%）控制良好**：regression < 0.05%，escalation 机制正确避免在随机访问 workload 上触发 open-page。纯 open-page 在 RandAcc 上 regression -10.30%，而 Phase 2 仅 -0.01%，说明 RE-driven 的靶向性是有效的。
2. **hashjoin regression 约 -0.6%**：比纯 open-page 的 -9.6% 好得多，但仍然显著。Phase 1（N12_S2 为 -0.62%）和 Phase 2 几乎一致，说明 INF 扩展未加剧问题。
3. **PageRank/soc-pokec 和 PageRank/higgs 是最大风险点**：regression -1.1% ~ -1.5%，与 Phase 1 水平一致。De-escalation 机制未能将这些 bank 及时拉回，但 INF 扩展也未使情况恶化。

### 3.4 显著 Regression Benchmarks

以下 benchmark 在所有 Phase 2 配置中 regression 超过 -0.5%：

| Benchmark | op_N12 | op_N5 | 特征 | 分析 |
|-----------|--------|-------|------|------|
| crono/PageRank/higgs | -1.15% | -1.31% | 社交网络 PageRank | RE 释放后保护丧失 |
| ligra/PageRank/soc-pokec | -1.13% | -1.49% | 社交网络 PageRank | 同上 |
| crono/PageRank/soc-pokec | -0.88% | -1.27% | 社交网络 PageRank | 同上 |
| ligra/Components/soc-pokec | -0.70% | -0.78% | 连通分量 | soc-pokec 图结构不适合 escalation |
| ligra/Radii/soc-pokec | -0.63% | -0.69% | 图半径 | 同上 |
| hashjoin/hj-2-NPO_st | -0.62% | -0.67% | Hash join | 随机访问，escalation 不当 |
| ligra/CF/soc-pokec | -0.56% | -0.76% | 协同过滤 | soc-pokec 图结构 |

Regression 模式与 Phase 1 完全一致：集中在 soc-pokec 图算法和高 conflict workload 上。INF 扩展未加剧也未缓解这些 regression。

---

## 4. 消融分析

### 4.1 open vs open_ext

| 对比 | GEOMEAN speedup | 变化率 |
|------|----------------|--------|
| open_ext_N12 / open_N12 | 0.999808 | **-0.019%** |
| open_ext_N5 / open_N5 | 0.999953 | **-0.005%** |

Per-benchmark 差异全部在 ±0.05% 以内，无任何 benchmark 出现显著变化。

**结论：open ≈ open_ext。** 对应实验方案 §5.3 的第一种场景——Shadow simulation 不会自主选择 INF（或选择了但效果为零/微负）。这意味着：
- INF 在 shadow simulation 的仲裁竞争中不具竞争力——在绝大多数 bank 上，有限 timeout 的 hit/conflict 权衡优于 INF
- RE-driven 的靶向 escalation 是 INF 唯一合理的到达路径
- **open_ext 没有价值，应放弃**

### 4.2 Phase 2 vs Phase 1（open-page 增量）

| 对比 | GEOMEAN speedup | 变化率 |
|------|----------------|--------|
| open_N12 / Phase1_N12 | 1.000043 | **+0.004%** |
| open_N5 / Phase1_N5 | 1.000041 | **+0.004%** |

open-page escalation 相对 Phase 1 的增量几乎恰好为零——INF 作为 escalation 的额外档位，既未帮助也未损害。

### 4.3 N12_S2 vs N5_S3 一致性

在所有 Phase 2 变体中，N12_S2 一致优于 N5_S3：
- 全局 GEOMEAN：-0.097%（N12）vs -0.137%（N5）
- 11 Winner GEOMEAN：+0.41%（N12）vs +0.35%（N5）
- Pathological GEOMEAN：-0.73%（N12）vs -0.88%（N5）

更保守的 N=12 在引入 open-page 后仍是更优选择，与 Phase 1 结论一致。

---

## 5. 假设验证总结

| 假设 | 结论 | 证据 |
|------|------|------|
| H1: lbm/sphinx3 在 open-page escalation 下 IPC 提升 > Phase 1 | **不成立** | lbm: +0.06% → +0.06%；sphinx3: +0.04% → +0.05%；增量为零 |
| H2: 11 winner benchmark 增益 ≥ Phase 1 同配置 | **勉强成立** | GEOMEAN +0.42% → +0.41%，持平但无增量 |
| H3: De-escalation 能有效将 open-page bank 降级 | **部分成立** | 大部分 benchmark 控制良好，但 PageRank/higgs,soc-pokec 仍有 -1.1%~-1.5% regression |
| H4: RandAcc/hashjoin 上 open-page escalation 触发极少 | **RandAcc 成立**（<0.05%）；hashjoin 部分成立（-0.6%） | RandAcc regression 从纯 open-page -10.3% 降至 -0.01% |
| H5: open_ext 中 shadow 自主选择 INF | **不成立** | open ≈ open_ext（差异 <0.02%） |
| H6: GEOMEAN regression 不超过 Phase 1 同配置 | **成立** | -0.097% vs Phase 1 -0.10%，几乎相同 |

---

## 6. 关键发现

### 6.1 INF Timeout 为何无增量

Phase 2 的核心假设是：将 escalation 上限从 3200c 扩展到 INF（open-page）可以为高行局部性 workload 提供额外保护。实验证明这一假设不成立。原因分析：

1. **Phase 1 中 RE 被动拦截已提供等效保护**：Phase 1 分析表明 RE 条目在 store 中的驻留时间等效于 14400c+（18x × 800c），远超 3200c。将 escalation 推到 INF 看似更激进，但 Phase 1 的 3200c escalation + RE 驻留已经覆盖了这些 workload 的保护需求。

2. **Escalation 到 INF 的 bank 数量极少**：只有连续命中 N 次且当前 timeout 已达 3200c 的 bank 才会被推到 INF。大部分 bank 在 1600c 或 3200c 已稳定（Phase 1 中 1600c+3200c 占 57%），很少进一步触发。

3. **INF 的 de-escalation 不确定性**：进入 INF 的 bank 依赖 conflict-driven 降级机制退出。如果该 bank 在 INF 期间恰好行局部性好，降级不触发，退出后 RE 条目已被释放——等效保护反而降低。

### 6.2 ext_timeout_only 的参照价值

纯扩展候选集（ext_timeout_only）在 lbm-like 集上始终表现最好（+0.21% GEOMEAN），优于所有 escalation 配置。这进一步确认了 Phase 1 的发现：**escalation 机制本身是 net harmful 的，获益完全来自扩展候选集使 shadow simulation 有更多选择。**

### 6.3 Regression 模式不变

Phase 2 的 regression 模式与 Phase 1 完全一致：
- soc-pokec 图算法（PageRank、Components、CF、Radii）：RE 释放后保护丧失
- 科学计算（fotonik3d、GemsFDTD、bwaves）：escalation 打破 shadow simulation 稳态
- 高 conflict workload（hashjoin）：escalation 在不应触发时触发

INF 扩展既未加剧也未缓解这些 regression——问题根源在 escalation 机制本身，不在 timeout 上限。

---

## 7. Phase 2 结论与 Phase 3 衔接

### 7.1 Phase 2 结论

**Open-page escalation 没有提供有价值的增量。** Phase 2 的 4 个配置（open/open_ext × N12/N5）与 Phase 1 对应配置几乎完全一致。INF 作为 escalation 的额外档位对性能无影响。

| Phase 2 结论 | 判定 |
|-------------|------|
| open > Phase 1 best | 不成立 |
| **open ≈ Phase 1 best** | **成立** |
| open < Phase 1 best | 不成立 |
| open_ext 显著优于 open | 不成立 |

### 7.2 Phase 3 基准配置建议

根据实验方案 §9 的衔接规则，Phase 2 结论为 "open ≈ Phase 1 best"：

> Phase 3 用 `GS_esc_N12_S2`（Phase 1 best），ALLOW_OPEN=false

**Phase 3 应使用 `GS_esc_N12_S2` 作为基准**，放弃 open-page 方向。Phase 3 的重点应转向：
1. **DECAY_PERIODS 调优**：当前固定为 5，调整降级速度可缓解 soc-pokec 类 workload 的 regression
2. **CONFLICT_THR 调优**：当前固定为 3，降低阈值可加速错误 escalation 的降级
3. **RE Store 容量扩展**（128/256）：解决 Phase 1 发现的高 N 下 RE 容量压力问题
