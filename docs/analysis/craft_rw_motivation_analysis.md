# CRAFT RW 增强设计动机分析：Read/Write Wrong Precharge 比例

## 概述

本分析从 CRAFT_PRECHARGE (RS+RW+SD) 的仿真结果中提取 wrong precharge 的读/写分布，
用以支撑 RW (Read/Write Cost Differentiation) 增强的设计动机。

数据来源：`champsim-la/results/CRAFT_PRECHARGE_1c/` 中 `ddr.json` 的
`craft_wrong_read`、`craft_wrong_write`、`craft_conflict_read`、`craft_conflict_write` 计数器。

## 全局统计（全部 62 个 benchmark）

| 事件类型 | Read | Write | Read 占比 | Write 占比 | R/W 比值 |
|----------|------|-------|-----------|------------|----------|
| **Wrong Precharge** | 34,197,330 | 8,380,005 | **80.3%** | 19.7% | **4.1x** |
| **Conflict** | 36,862,981 | 21,054,662 | **63.6%** | 36.4% | **1.8x** |

**核心发现**：Wrong precharge 事件中 read 占 80.3%，远高于 write 的 19.7%，
R/W 比值达 4.1 倍。而 conflict 事件中 read 占比为 63.6%，R/W 比值仅 1.8 倍。
这表明 wrong precharge 的读写不对称性显著强于 conflict 事件。

## Top 12 Benchmark 逐项分析

### Wrong Precharge 读写分布

| # | Benchmark | Wrong 总数 | Wrong Read | Wrong Write | Read% | Write% | R/W 比值 |
|---|-----------|-----------|------------|-------------|-------|--------|----------|
| 1 | ligra/CF/roadNet-CA | 110,259 | 39,301 | 70,958 | 35.6% | 64.4% | 0.6x |
| 2 | ligra/CF/higgs | 91,380 | 81,278 | 10,102 | 88.9% | 11.1% | 8.0x |
| 3 | ligra/PageRank/higgs | 612,635 | 611,713 | 922 | 99.8% | 0.2% | 663.5x |
| 4 | ligra/BFSCC/soc-pokec-short | 159,388 | 132,272 | 27,116 | 83.0% | 17.0% | 4.9x |
| 5 | spec06/sphinx3/ref | 236,467 | 194,880 | 41,587 | 82.4% | 17.6% | 4.7x |
| 6 | ligra/CF/soc-pokec | 1,504,169 | 1,402,818 | 101,351 | 93.3% | 6.7% | 13.8x |
| 7 | ligra/Triangle/roadNet-CA | 145,025 | 128,108 | 16,917 | 88.3% | 11.7% | 7.6x |
| 8 | ligra/PageRank/roadNet-CA | 89,205 | 74,996 | 14,209 | 84.1% | 15.9% | 5.3x |
| 9 | crono/Triangle-Counting/roadNet-CA | 62,204 | 42,769 | 19,435 | 68.8% | 31.2% | 2.2x |
| 10 | spec06/wrf/ref | 239,030 | 104,012 | 135,018 | 43.5% | 56.5% | 0.8x |
| 11 | ligra/Components-Shortcut/soc-pokec | 1,846,363 | 1,824,375 | 21,988 | 98.8% | 1.2% | 83.0x |
| 12 | ligra/Radii/higgs | 511,974 | 473,451 | 38,523 | 92.5% | 7.5% | 12.3x |

### Conflict 读写分布

| # | Benchmark | Conflict 总数 | Conflict Read | Conflict Write | Read% | Write% |
|---|-----------|--------------|---------------|----------------|-------|--------|
| 1 | ligra/CF/roadNet-CA | 136,858 | 53,951 | 82,907 | 39.4% | 60.6% |
| 2 | ligra/CF/higgs | 109,067 | 96,572 | 12,495 | 88.5% | 11.5% |
| 3 | ligra/PageRank/higgs | 656,791 | 650,265 | 6,526 | 99.0% | 1.0% |
| 4 | ligra/BFSCC/soc-pokec-short | 115,965 | 85,017 | 30,948 | 73.3% | 26.7% |
| 5 | spec06/sphinx3/ref | 205,686 | 170,783 | 34,903 | 83.0% | 17.0% |
| 6 | ligra/CF/soc-pokec | 2,136,128 | 1,947,637 | 188,491 | 91.2% | 8.8% |
| 7 | ligra/Triangle/roadNet-CA | 88,463 | 63,195 | 25,268 | 71.4% | 28.6% |
| 8 | ligra/PageRank/roadNet-CA | 68,930 | 56,224 | 12,706 | 81.6% | 18.4% |
| 9 | crono/Triangle-Counting/roadNet-CA | 50,995 | 35,152 | 15,843 | 68.9% | 31.1% |
| 10 | spec06/wrf/ref | 369,245 | 164,317 | 204,928 | 44.5% | 55.5% |
| 11 | ligra/Components-Shortcut/soc-pokec | 2,064,459 | 2,021,216 | 43,243 | 97.9% | 2.1% |
| 12 | ligra/Radii/higgs | 352,749 | 299,774 | 52,975 | 85.0% | 15.0% |

## RW 增强设计动机

### 1. 读写分布高度不对称

全局 wrong precharge 中 read 占 **80.3%**，write 仅占 **19.7%**。
在 12 个代表性 benchmark 中，10 个的 read wrong 占比超过 68%，
其中 PageRank/higgs 高达 **99.8%**，Components-Shortcut/soc-pokec 达 **98.8%**。

这意味着统一的 escalation/de-escalation 策略将对本质不同的访问类型做相同处理，
忽视了读写之间固有的代价差异。

### 2. 读写代价本质不同

- **Read wrong precharge**：位于 CPU 关键路径上，处理器必须等待数据返回才能继续执行，
  每次 wrong precharge 的延迟直接转化为 stall cycle。
- **Write wrong precharge**：写操作可被 write buffer 吸收，对延迟不敏感；
  write wrong precharge 的代价远低于 read wrong precharge。

因此，对读写采用相同幅度的 timeout 调整不是最优策略。

### 3. RW 的差异化响应机制

| 事件 | 默认处理 | RW 增强处理 | 设计理由 |
|------|---------|------------|---------|
| Write wrong precharge | 标准 escalation step | **半步 escalation** (step >>= 1) | 写延迟可被 buffer 吸收，无需过度增加 timeout |
| Read conflict | 标准 de-escalation step | **双倍 de-escalation** (step <<= 1) | 读 conflict 代价高，应更积极缩短 timeout 以减少冲突 |

### 4. 两类工作负载模式均受益

**读主导型**（10/12 benchmark，Read% > 68%）：
- 典型代表：PageRank/higgs (R 99.8%)、Components-Shortcut/soc-pokec (R 98.8%)
- 少量 write wrong 采用半步 escalation，防止写操作不当抬高 timeout，
  避免对后续 read 访问造成不必要的行保持

**写主导型**（wrf, CF/roadNet-CA，Write% > 56%）：
- 典型代表：wrf (W 56.5%)、CF/roadNet-CA (W 64.4%)
- 大量 write wrong 的半步 escalation 使 timeout 保持较低水平，
  加速行切换，有利于后续 read 请求更快命中新行

### 5. 性能验证

RW 是所有五个单独增强中**效果最强的**：

| 指标 | CRAFT_BASE | CRAFT_RW | 提升 |
|------|-----------|----------|------|
| 胜出 benchmark 数 (62 中) | 45 | **49** | +4 |
| 胜出 GEOMEAN | +1.188% | +1.172% | -0.016pp |
| 整体 GEOMEAN | +0.653% | **+0.775%** | **+0.122pp** |

RW 在整体 GEOMEAN 上提升 +0.122 个百分点，同时将胜出数从 45 提升到 49，
说明读写差异化处理捕捉到了真实的性能优化机会。

## 结论

Wrong precharge 事件存在显著的读写不对称性（全局 4.1:1），
且读写操作对 precharge 错误的延迟敏感度本质不同。
RW 增强通过差异化的 escalation/de-escalation 步长，
精准匹配了这种不对称性，实现了全部单增强中最高的性能提升。
