# CRAFT Timeout 自适应分布分析

**日期**: 2026-03-10
**数据来源**: `champsim-la/results/CRAFT_PRECHARGE_1c/` (ddr.json)
**CRAFT 配置**: CRAFT_PRECHARGE (RS + RW + SD)
**Benchmark 数量**: 12（craft_final_evaluation.md 中的 Top-12 winning benchmarks）
**脚本**: `champsim-la/scripts/craft_timeout_distribution.py`

## 概述

CRAFT 的核心机制是 per-bank adaptive timeout：每个 bank 独立维护一个 timeout 值（范围 [50, 3200] cycles，初始值 200），通过反馈环路（wrong precharge → 升高，conflict → 降低）自适应调整。不同 workload 的行级访问模式差异显著，CRAFT 应当自适应到不同的 timeout 区间——本分析验证了这一点。

**Timeout 区间定义**：
- **Low [0, 800)**：激进关闭策略，适合行复用率低的随机/流式访问
- **Mid [800, 2000)**：均衡自适应策略
- **High [2000, 3200]**：保守保持策略，适合行复用率高的局部性访问

## 分布总表

| # | Benchmark | Low% | Mid% | High% | 自适应模式 | vs Best BL |
|---|-----------|------|------|-------|------------|------------|
| 1 | ligra/CF/roadNet-CA | 3.9 | 10.8 | 85.2 | 高度保守 | +5.79% |
| 2 | ligra/CF/higgs | 35.7 | 38.0 | 26.3 | 均衡分散 | +3.10% |
| 3 | ligra/PageRank/higgs | 96.9 | 2.9 | 0.2 | 极度激进 | +2.87% |
| 4 | ligra/BFSCC/soc-pokec-short | 7.2 | 18.2 | 74.7 | 保守 | +2.85% |
| 5 | spec06/sphinx3/ref | 9.2 | 15.8 | 75.0 | 保守 | +2.60% |
| 6 | ligra/CF/soc-pokec | 36.9 | 46.7 | 16.4 | 均衡偏低 | +2.02% |
| 7 | ligra/Triangle/roadNet-CA | 6.7 | 8.3 | 85.0 | 高度保守 | +2.00% |
| 8 | ligra/PageRank/roadNet-CA | 3.6 | 5.4 | 90.9 | 极度保守 | +1.89% |
| 9 | crono/Triangle-Counting/roadNet-CA | 2.8 | 5.2 | 92.0 | 极度保守 | +1.74% |
| 10 | spec06/wrf/ref | 25.7 | 17.1 | 57.3 | 偏保守 | +1.65% |
| 11 | ligra/Components-Shortcut/soc-pokec | 73.5 | 19.9 | 6.6 | 偏激进 | +1.63% |
| 12 | ligra/Radii/higgs | 14.8 | 24.3 | 60.9 | 偏保守 | +1.61% |

## 三种典型自适应模式

### 模式一：Aggressive Close（激进关闭）

**代表 benchmark**：PageRank/higgs (Low=96.9%)、Components-Shortcut/pokec (Low=73.5%)

PageRank/higgs 的 timeout 分布几乎完全集中在低区间，其中 [0, 100) 占 33.5%，[100, 200) 占 29.7%——这意味着绝大多数 bank 的 timeout 被快速压低到初始值 200 以下。

```
PageRank/higgs timeout 分布 (top bins):
  [  0- 99]  33.5%  █████████████████
  [100-199]  29.7%  ███████████████
  [200-299]  15.3%  ████████
  [300-399]   8.1%  ████
  [400-499]   4.7%  ██
  [500-599]   2.7%  █
  [600-699]   1.7%  █
```

**解读**：PageRank 在 higgs 图上的访问模式接近随机——顶点的邻接表分布不规则，行级局部性极差。CRAFT 通过频繁的 conflict 事件快速降低 timeout，实际效果等价于 close-page 策略。对比固定策略的 baseline，CRAFT 的优势在于不需要全局预设，而是 per-bank 自然收敛。

Components-Shortcut/pokec 类似但稍温和：Low 占 73.5%，分布从 [100, 200) 的 14.6% 逐渐递减。这反映了连通分量算法中大量短期探索式访问。

### 模式二：Balanced（均衡分散）

**代表 benchmark**：CF/higgs (Low=35.7%, Mid=38.0%, High=26.3%)、CF/pokec (Low=36.9%, Mid=46.7%, High=16.4%)

```
CF/higgs timeout 分布:
  [  0- 99]   4.2%  ██
  [100-199]   7.5%  ████
  [200-299]   5.2%  ███
  [300-399]   3.8%  ██
  ...（平缓递减）...
  [800-899]   4.6%  ██
  [900-999]   4.5%  ██
  [1000-1099]  4.1%  ██
  ...（继续平缓递减）...
  [3200-  +]  1.1%  █

CF/pokec timeout 分布:
  [  0- 99]   5.0%  ███
  [100-199]   5.8%  ███
  ...
  [800-899]   5.0%  ███
  [900-999]   5.3%  ███
  [1000-1099]  5.4%  ███
  ...（中部最密集）...
  [3100-3199]  0.7%
```

**解读**：Collaborative Filtering (CF) 的访问模式内部多样——不同 bank 面临不同的行复用率。部分 bank 上的用户-物品矩阵行被频繁重复访问（timeout 收敛到高值），另一部分 bank 的稀疏向量操作导致行冲突频繁（timeout 被压低）。CRAFT 的 per-bank 自适应恰好捕捉到了这种 workload 内部的异质性，这是全局固定策略无法做到的。

CF/pokec 的分布更集中于 Mid 区间（46.7%），峰值在 [1000, 1100) 附近，说明 pokec 图的社交网络结构提供了比 higgs 更强的中等局部性。

### 模式三：Keep Open（保守保持）

**代表 benchmark**：TriCnt/roadNet (High=92.0%)、PR/roadNet (High=90.9%)、Tri/roadNet (High=85.0%)、CF/roadNet (High=85.2%)

```
TriCnt/roadNet timeout 分布 (top bins):
  [3200-  +]  34.2%  █████████████████
  [3100-3199] 22.9%  ███████████
  [3000-3099] 10.2%  █████
  [2900-2999]  6.2%  ███
  [2800-2899]  4.4%  ██
  [2700-2799]  3.4%  ██
  ...（逐级递减至低区间）...

PR/roadNet timeout 分布 (top bins):
  [3200-  +]  51.6%  ██████████████████████████
  [3100-3199] 13.9%  ███████
  [3000-3099]  6.7%  ███
```

**解读**：roadNet-CA 是道路网络图，具有强局部性特征——相邻节点的编号通常接近，同一 DRAM 行内的数据被反复访问。CRAFT 通过反复观测到 "right precharge"（正确预关闭）来持续升高 timeout，最终大量 bank 收敛到上限 3200。

值得注意的是，同一算法在不同图上的分布差异巨大：
- PageRank/roadNet → High 90.9%（保守）
- PageRank/higgs → Low 96.9%（激进）

这说明 timeout 自适应不是由算法决定的，而是由**算法×数据集**的组合决定的——CRAFT 捕捉的是运行时实际的行级访问模式。

## 按图数据集分组对比

| 图数据集 | 特征 | 典型 timeout 倾向 |
|----------|------|-------------------|
| **roadNet-CA** | 道路网络，强空间局部性，节点编号有序 | High 85-92%，极度保守 |
| **higgs** | 社交传播网络，弱局部性，幂律度分布 | 取决于算法：PR→极度激进，CF→均衡，Radii→偏保守 |
| **soc-pokec** | 社交网络，中等局部性 | CF→均衡偏低，BFSCC→保守，Comp→偏激进 |

## 与 IPC 提升的关联

CRAFT 在所有三种自适应模式下均实现了正向 IPC 提升（+1.61% ~ +5.79% vs best baseline），说明：

1. **激进关闭模式**（如 PR/higgs +2.87%）：baseline 的 open-page/adaptive 策略过于保守，CRAFT 通过快速降低 timeout 减少了不必要的 row buffer conflict 延迟
2. **均衡模式**（如 CF/higgs +3.10%）：baseline 的全局策略无法适应 workload 内部的 bank 间异质性，CRAFT 的 per-bank 自适应弥补了这一差距
3. **保守保持模式**（如 CF/roadNet +5.79%）：baseline 的 close-page/短 timeout 策略过于激进，CRAFT 通过升高 timeout 充分利用了行级时间局部性

这三种模式分别对应了 CRAFT 相对于 "过保守 baseline"、"固定策略 baseline"、"过激进 baseline" 的三种胜出路径。

## 数据文件

| 文件 | 说明 |
|------|------|
| `champsim-la/results/craft_timeout_distribution.tsv` | 12 benchmark 的 Low/Mid/High 百分比汇总 |
| `champsim-la/results/craft_timeout_histogram.tsv` | 100-cycle 粒度完整直方图（可用于画图） |
| `champsim-la/scripts/craft_timeout_distribution.py` | 提取脚本 |
