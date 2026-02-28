# GS 改进方向候选

**日期：** 2026-02-26
**基于：** `docs/gs_profiling_analysis.md` 性能计数器分析报告

---

## 一、Timeout 机制的根本性改进

### 1. 自适应连续 Timeout 空间（替代离散候选集）

**问题**：当前 `{50, 100, 150, 200, 300, 400, 800}` 是手工选定的离散集合，800c 上限对 13-19 个高行局部性 workload 明显不足（profiling 报告：8 个 benchmark 中 800c 是最高频选择，占比 >30%）。

**方向**：设计一种连续自适应 timeout 机制，用硬件计数器（如 EWMA/指数加权移动平均）追踪每个 bank 的行重访间隔分布，动态计算最优 timeout 值，而非从预定义集合中选择。这本质上将 shadow simulation 从"多臂老虎机在离散动作空间"升级为"连续动作空间的在线学习"。

**学术价值**：消除了对超参数（候选 timeout 集合）的依赖，是对 GS 机制的范式性改进。

### 2. 分层 Shadow Simulation（Per-Bank vs Per-Rank 协同）

**问题**：profiling 报告中 lbm 的 U 型 timeout 分布表明同一 benchmark 内不同 bank 行为差异极大——部分 bank 需要极短 timeout，部分需要远超 800c。

**方向**：引入两层决策架构：
- **Per-bank 层**：快速响应局部行为变化（短时间窗口）
- **Per-rank/channel 层**：捕捉全局访问模式和相变（长时间窗口），在 bank 间共享统计信息

当某个 bank 的局部统计不充分时（如冷启动），可 fallback 到全局策略。这类似于分支预测中的 local/global 两级预测器思想。

**学术价值**：将经典微架构中的分层预测思想引入 DRAM row policy，天然带来方法论层面的类比分析。

---

## 二、RE (Row Exclusion) 机制的深度优化

### 3. RE Store 的智能替换策略

**问题**：RE Store 64 项近 100% 饱和（9.05M 插入 vs 9.00M 驱逐），但 RE 命中中仅 71.27% 是 useful。18x 的命中倍率说明少数"长寿行"贡献了绝大部分命中。

**方向**：设计频率感知的替换策略（类似缓存的 frequency-based replacement），而非简单 FIFO/LRU。可为每个 RE 条目维护一个小的命中计数器，替换时优先驱逐低命中条目。或者引入 RRIP 风格的 re-reference interval prediction，将 RE Store 的替换问题建模为缓存替换问题的变体。

**学术价值**：将缓存替换理论迁移到 DRAM row management 是一个新颖的交叉领域。

### 4. RE 与 Timeout 的闭环协同

**问题**：profiling 报告揭示的规律链——"RBH 高 → 800c 不够 → TO_Acc 低 → RE 拼命补救"——说明 RE 和 timeout 目前是割裂的两套机制。

**方向**：设计 RE-driven timeout escalation：当 RE Store 中某个 bank 的条目连续命中 N 次后，直接将该 bank 的 timeout 提升到更高档位（甚至切换到 open-page 模式），并从 RE Store 中释放该条目。这形成一个负反馈闭环：RE 高命中 → 提升 timeout → 减少错误 precharge → RE 命中减少 → 释放 RE 容量。

**学术价值**：提出了一种统一的行管理理论框架，而非两个独立机制的拼接。

---

## 三、Workload 感知与相变检测

### 5. 在线 Phase Detection 驱动的策略切换

**问题**：lbm 的逐 slice 分析显示明显的阶段性行为——初始阶段 TO_Acc ~94%，稳态阶段骤降到 ~35%。当前 GS 对相变的响应速度取决于 shadow simulation 的收敛速度。

**方向**：集成轻量级 phase detection 硬件（如基于 working set signature 的 phase detector），在检测到相变时：
- 重置 shadow simulation 的统计
- 预加载与该 phase 关联的历史最优 timeout 配置（phase-indexed timeout table）

**学术价值**：将 phase-aware 微架构技术（已有丰富文献）应用于 DRAM row policy 是未被充分探索的领域。

### 6. 基于访问模式分类的策略选择

**问题**：profiling 报告清晰识别了三类子模式（U 型、图遍历、科学计算），但当前 GS 对所有模式使用同一套机制。

**方向**：设计一个轻量级的 pattern classifier（可基于简单特征：row buffer hit rate、inter-arrival time variance、spatial locality metric），在线识别当前 bank 属于哪种访问模式，并选择对应的预设策略族。例如：
- 流式/随机 → 激进短 timeout
- 高局部性科学计算 → 大 timeout + 小 RE
- 图遍历 → 中等 timeout + 大 RE

**学术价值**：从"一种策略适配所有"到"策略空间的在线选择"，是 workload-aware memory management 的前沿方向。

---

## 四、能效与系统级影响

### 7. Timeout Precharge 的能效分析与优化

**问题**：75% 的 precharge 由 timeout 投机驱动，其中 9.48% 是错误的。错误的 timeout precharge 意味着：(1) 浪费了 precharge 能耗；(2) 后续重新 activate 同一行又浪费 activate 能耗。

**方向**：量化 GS 对 DRAM 能效的影响（tRAS energy、precharge energy、activate energy），特别是错误 timeout 造成的 energy overhead。在此基础上设计 energy-aware timeout 策略：在性能和能效之间引入可调节的 trade-off 参数。

**学术价值**：GS 原论文侧重性能，能效维度的分析是自然且重要的延伸，特别适合 HPC/数据中心场景。

### 8. 多核场景下的 GS 扩展性

**问题**：当前分析基于单核（`GS_prof_1c`），但配置文件中已有 4 核和 8 核配置。多核下 bank 竞争加剧，行局部性可能被破坏。

**方向**：研究多核场景下 GS 的行为退化模式：
- 多核共享 bank 时 timeout 准确率的变化
- RE Store 在多线程竞争下的有效性
- 是否需要 per-core 的 shadow simulation

**学术价值**：多核可扩展性是 DRAM 管理策略能否实际部署的关键问题。

---

## 五、优先级排序

| 优先级 | 方向 | 理由 |
|--------|------|------|
| **最高** | #4 RE-Timeout 闭环协同 | 实现简单、数据支撑充分（13 个 benchmark 直接受益）、理论新颖 |
| **高** | #1 连续自适应 Timeout | 解决报告揭示的核心瓶颈（800c 上限），影响面广 |
| **高** | #3 RE 智能替换 | RE Store 饱和是实测问题，改进空间明确 |
| **中** | #5 Phase Detection | 学术价值高但硬件开销需仔细评估 |
| **中** | #8 多核扩展性 | 实验基础已有（4c/8c 配置），具有实际意义 |
| **探索** | #2, #6, #7 | 研究空间大但需要更多前期验证 |

其中 **#4（RE-Timeout 闭环）** 和 **#1（连续 timeout）** 最具"以最小改动获得最大收益"的特点，且 profiling 数据直接支撑了它们的 motivation，适合作为论文的核心贡献点。
