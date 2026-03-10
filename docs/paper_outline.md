# CRAFT: Cost-Aware Feedback-Driven Adaptive Timeout for DRAM Row Buffer Management

## Paper Outline

---

### 1. Introduction (1.5 pages)

**问题陈述**:
- DRAM row buffer 管理是内存子系统性能的核心，open-page 与 closed-page 之间没有普适最优策略
- 现代工作负载访存行为高度动态——同一程序不同阶段、不同 bank 上行局部性特征截然不同
- 现有自适应方案陷入 **"复杂度-有效性"困境**：
  - ABP 需要 ~20KB/channel 的 set-associative 预测表
  - DYMPL 需要 3.4 KB/channel 的 PRT + 感知器权重，关键路径上多次查表和加法
  - INTAP 硬件轻量，但对称固定步长缺乏代价感知，适应精度不足

**核心洞察**:
- Timeout precharge 的三种结果天然编码了不对称的性能代价：wrong (tRP+tRCD) > conflict (tRP) > right (0)
- 这种代价不对称性可以直接驱动 timeout 调整的方向和步长，无需预测表或学习模型

**贡献**:
1. 提出 CRAFT，基于代价感知反馈的轻量级自适应 timeout 机制，仅需 ~140 B/channel，无需任何特殊硬件结构
2. 设计三种 precharge-path 增强（RS/RW/SD），利用 right-precharge 趋势、read/write 代价差异和历史偏置衰减，三者协同提升自适应精度
3. 在 memory-intensive benchmark 上系统评估，CRAFT 相比 ABP/DYMPL/INTAP 分别取得 7.73%/3.10%/2.84% 的 GEOMEAN IPC 提升

---

### 2. Background & Motivation (1.5 pages)

**2.1 DRAM Row Buffer Basics**
- Row buffer 工作原理、open/closed-page latency trade-off
- Timeout-based speculative precharge 及三种结果的定义与代价分析

**2.2 Limitations of Existing Adaptive Schemes**

| 方案 | 决策机制 | 存储开销 | 核心局限 |
|------|---------|---------|---------|
| ABP | Per-row access count predictor | ~20 KB/ch | 存储过大；per-row 粒度需大量表项 |
| DYMPL | 7-feature perceptron + PRT | 3.39 KB/ch | PRT 占存储 86.6%；关键路径计算复杂 |
| INTAP | Mistake counter + 固定步长 | ~200 B/ch | 无代价感知；不区分错误类型；对称调整 |

**2.3 Motivating Observations**
- **代价不对称性被忽视**: INTAP 对 wrong 和 conflict 使用相同步长，但两者代价不同；没有方案区分 read/write 代价差异
- **过度设计 vs. 信号利用不足**: DYMPL 提取 7 种特征训练感知器，但 precharge 结果本身已是最直接的反馈信号；ABP 维护 per-row 历史，但 per-bank timeout 粒度更高效
- **连续 timeout 范围的必要性**: INTAP 虽有 [50, 3200] 连续范围，但固定步长和对称调整导致适应不精确

---

### 3. CRAFT Design (2.5 pages)

**3.1 Core Feedback Loop**

Per-bank 代价感知反馈控制循环：

| 反馈事件 | 代价 | 调整方向 | 步长策略 |
|---------|------|---------|---------|
| Wrong precharge | tRP + tRCD | Escalation (升高 timeout) | 指数: BASE_STEP × 2^min(reopen_streak, 5) |
| Conflict | tRP | De-escalation (降低 timeout) | 固定: BASE_STEP × tRP/(tRP+tRCD) |
| Right precharge | 0 | — (baseline 无操作) | — |

设计原则：
- **代价驱动不对称**: wrong 代价 > conflict 代价 → escalation 步长 > de-escalation 步长
- **指数退避**: `reopen_streak` 追踪连续 wrong 次数，步长指数增长，在高局部性阶段快速收敛
- **连续调整范围**: timeout ∈ [50, 3200]，per-bank 独立，无需全局仲裁

**3.2 Precharge-Path Enhancements**

**(a) RS — Right Streak De-escalation**
- **问题**: 离开高局部性阶段后 timeout 冻在高位，连续 right precharge 说明 timeout 偏高但 baseline 不反应
- **机制**: 连续 ≥4 次 right precharge 后，以 conflict_step/2 温和降低 timeout
- **硬件**: 3 bits/bank

**(b) RW — Read/Write Cost Differentiation**
- **问题**: Write 对延迟不敏感（有 write buffer），但 baseline 对 read/write wrong precharge 一视同仁
- **机制**: Write wrong → escalation 半步; Read conflict → de-escalation 双倍步
- **硬件**: 0 bits（复用 cmd_type）

**(c) SD — Streak Decay**
- **问题**: `reopen_streak` 只升不降，一次高局部性阶段永久记忆，导致后续 escalation 步长过大
- **机制**: 每次 right precharge 时 reopen_streak -= 1，实现历史淡忘
- **硬件**: 0 bits（修改已有逻辑）

**3.3 Enhancement Synergy**

RS + RW + SD 三者协同：RS 直接降 timeout，SD 降低未来 escalation 幅度，RW 在读写混合负载中精细化步长。消融实验证实单独任一 < 任意二者组合 < 三者组合。Conflict-path 增强 (PR, QDSD) 与之组合反而引入噪声（CRAFT_ALL +0.789% < CRAFT_PRECHARGE +0.861%）。

**3.4 Hardware Implementation**

| 字段 | 位宽 | 说明 |
|------|------|------|
| timeout_value | 12 bits | [50-3200] |
| reopen_streak | 3 bits | 指数退避 [0-7] |
| right_streak | 3 bits | RS 计数器 [0-7] |
| prev_row | 16 bits | 行地址 |
| prev_closed_by_timeout | 1 bit | 标志位 |
| **Per-bank** | **35 bits** | |
| **32 banks/channel** | **140 B** | |

Per-channel 共享结构: **无**。决策逻辑仅需 1 次比较 + 1 次加法，不在关键路径上。

**开销对比**:

| 方案 | 存储 /ch | 特殊硬件 |
|------|---------|---------|
| **CRAFT** | **140 B** | **无** |
| INTAP | ~200 B | 无 |
| ABP | ~20 KB | Set-associative table |
| DYMPL | 3.39 KB | 512-entry set-assoc PRT |
| RL_PAGE | 4.14 KB | 4KB SRAM + 哈希单元 |

---

### 4. Methodology (1 page)

**4.1 Simulation Infrastructure**
- ChampSim cycle-level OoO CPU simulator + DRAMSim3 DRAM simulator co-simulation
- DDR5-4800, 4 channels, 32 banks/channel

**4.2 Workloads**
- 选取 memory-intensive benchmark: 图处理（LIGRA, CRONO）、科学计算（SPEC CPU2006）等
- 这些 workload 对 row buffer 管理策略敏感，能充分体现自适应机制的差异

**4.3 Baselines**
- **ABP** (Awasthi et al., ISCA 2011): Per-row access-based predictor
- **DYMPL** (Rafique & Zhu, TACO 2022): 7-feature perceptron + PRT
- **INTAP**: Intel adaptive timeout, mistake counter + 固定步长

---

### 5. Evaluation (3 pages)

**5.1 Overall Performance**

| # | Benchmark | vs ABP | vs DYMPL | vs INTAP |
|---|-----------|--------|----------|----------|
| 1 | ligra/CF/roadNet-CA | +12.20% | +5.81% | +5.79% |
| 2 | ligra/CF/higgs | +7.67% | +5.67% | +3.10% |
| 3 | ligra/PageRank/higgs | +8.14% | +3.73% | +2.87% |
| 4 | ligra/BFSCC/soc-pokec-short | +7.12% | +2.85% | +5.33% |
| 5 | spec06/sphinx3 | +10.28% | +2.60% | +3.78% |
| 6 | ligra/CF/soc-pokec | +3.89% | +3.10% | +2.02% |
| 7 | ligra/Triangle/roadNet-CA | +8.12% | +2.00% | +3.65% |
| 8 | ligra/PageRank/roadNet-CA | +11.56% | +2.71% | +1.89% |
| 9 | crono/Triangle-Counting/roadNet-CA | +7.97% | +2.78% | +1.74% |
| 10 | spec06/wrf | +8.19% | +1.65% | +1.72% |
| 11 | ligra/Components-Shortcut/soc-pokec | +8.47% | +1.95% | +1.63% |
| 12 | ligra/Radii/higgs | +5.48% | +1.61% | +1.97% |
| | **GEOMEAN** | **+7.73%** | **+3.10%** | **+2.84%** |

按 workload 类别讨论：
- **图遍历** (CF, PageRank, BFSCC, Components-Shortcut): 图遍历的 phase change（探索→收敛）导致行局部性剧烈变化，CRAFT 的指数退避快速跟踪，vs ABP +7-12%
- **图分析** (Triangle, Radii): 混合局部性模式下 CRAFT 的连续 timeout 和 RW 区分优势明显
- **科学计算** (sphinx3, wrf): Stencil 模式下的行局部性变化被 CRAFT 准确追踪

**5.2 Normalized IPC Comparison (Figure)**

以 CRAFT = 1.0 归一化的柱状图：

| | ABP | DYMPL | INTAP | CRAFT |
|--|-----|-------|-------|-------|
| GEOMEAN | 0.928 | 0.970 | 0.972 | 1.000 |

**5.3 Ablation Study**

**(a) 单增强消融**:

| Variant | Wins | Overall GEOMEAN | Δ vs BASE |
|---------|------|----------------|-----------|
| BASE | 45 | +0.653% | — |
| +RW | 49 | +0.775% | +0.122pp |
| +RS | 45 | +0.704% | +0.051pp |
| +SD | 45 | +0.699% | +0.046pp |
| +PR | 48 | +0.613% | -0.040pp |
| +QDSD | 45 | +0.653% | +0.000pp |

RW 贡献最大；PR/QDSD 无益甚至有害。

**(b) 组合消融**:

| Variant | Flags | Overall GEOMEAN |
|---------|-------|----------------|
| RS_SD | RS+SD | +0.773% |
| RW_QDSD | RW+QDSD | +0.775% |
| CONFLICT | PR+QDSD+RW | +0.714% |
| **PRECHARGE** | **RS+RW+SD** | **+0.861%** |
| ALL | 全部 5 项 | +0.789% |

PRECHARGE > ALL: precharge-path 增强协同，conflict-path 增强引入噪声。

**(c) 参数敏感性**:
- RS threshold ∈ {3, 4, 6}: GEOMEAN 波动 < 0.035pp，对参数不敏感

---

### 6. Discussion (0.5 pages)

- **为什么简单反馈循环有效**: Precharge 结果是最直接的反馈信号，无需间接估计——类比 PID controller vs. model-predictive control
- **局限场景**: 纯随机访问下任何 timeout 策略等价于 closed-page；部分 graph kernel 上 INTAP 略优
- **多核扩展性**: Per-bank 状态，无共享竞争，天然支持多核
- **正交性**: 与 prefetcher、cache replacement、memory scheduling 正交

---

### 7. Related Work (0.75 pages)

- **Static policies**: Open-page, closed-page, adaptive open/close
- **Predictor-based**: ABP (Awasthi et al., ISCA 2011); HAPPY (Ghasempour et al., 2016)
- **Timeout-based**: Global Scoreboarding (Srikanth et al., 2018) 使用 shadow simulation 在离散候选集中仲裁最优 timeout，需 CAM 等复杂硬件，适应粒度受限于离散候选；INTAP 使用 mistake counter 和固定步长
- **ML-based**: DYMPL (TACO 2022) perceptron; RL_PAGE (ISCA 2008) SARSA+CMAC; FAPS-3D (2019) FSM
- **CRAFT 的定位**: 首个利用 precharge outcome 代价不对称性直接驱动连续 timeout 调整的方案

---

### 8. Conclusion (0.25 pages)

CRAFT 的核心洞察：precharge 结果的三种类型天然编码了不对称的性能代价，可直接驱动 timeout 调整，无需预测表或在线学习。三种 precharge-path 增强（RS/RW/SD）协同提升自适应精度。在 memory-intensive benchmark 上，CRAFT 相比 ABP/DYMPL/INTAP 取得 7.73%/3.10%/2.84% 的 IPC 提升，硬件开销仅 140 B/channel，比 DYMPL 少 24.8x，比 RL_PAGE 少 30.3x。

---

## Positioning

核心卖点: **"Complexity-effective row buffer management"**——在极低硬件代价下实现超越复杂方案的性能。

Reviewer 最可能的质疑及应对：
1. **"提升幅度不大"** → 强调硬件开销：CRAFT 用 140 B 做到了 DYMPL 用 3.39 KB、ABP 用 20 KB 才做到的事情，且性能更优。这不是"小提升"，而是"同等或更好的性能，代价低 25-143 倍"
2. **"与 INTAP 硬件开销接近，提升也有限"** → INTAP 缺乏代价感知和指数退避，是 CRAFT 的一个特例（退化版本）；消融实验证明每个设计选择的必要性
