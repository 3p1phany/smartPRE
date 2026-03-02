# DRAM 行缓冲管理研究文档

本目录包含 DRAM 行缓冲管理机制（Row Buffer Management）的设计、实验与分析文档，涵盖 Global Scoreboarding (GS)、FAPS-3D、GS-ML 混合、RL-PAGE 等多种策略。

## 目录结构

```
docs/
├── design/        设计文档（机制原理、计数器定义）
├── experiments/   实验方案（GS baseline、FAPS-3D、GS-ML、RL-PAGE）
├── analysis/      分析结果（消融实验、profiling 数据、存储开销对比）
└── references/    参考文献（PDF）
```

## 推荐阅读顺序

### 1. GS 基础设计

| 文件 | 内容 |
|------|------|
| [design/gs_implementation.md](design/gs_implementation.md) | GS 机制全解：timeout 预充电、shadow simulation 多候选仲裁、RE Store 热行保护 |
| [design/gs_accuracy_counters.md](design/gs_accuracy_counters.md) | 性能计数器的实现规格，用于衡量 timeout 决策准确率和 RE Store 有效性 |

### 2. GS 基线分析

| 文件 | 内容 |
|------|------|
| [analysis/GS_NOHOTROW_analysis.md](analysis/GS_NOHOTROW_analysis.md) | RE Store 消融实验：去除热行保护导致 3.1% 性能回归，证明 RE Store 不可或缺 |
| [analysis/gs_profiling_analysis.md](analysis/gs_profiling_analysis.md) | 62 benchmarks 计数器数据分析：90.5% timeout 准确率、71.3% RE 准确率、18x RE hit 乘数 |

### 3. 对比策略实验方案

| 文件 | 内容 |
|------|------|
| [experiments/faps3d_experiment_plan.md](experiments/faps3d_experiment_plan.md) | FAPS-3D：基于 2-bit 饱和计数器 FSM 的反馈式自适应页管理方案 |
| [experiments/gs_ml_hybrid_experiment_plan.md](experiments/gs_ml_hybrid_experiment_plan.md) | GS-ML：用轻量感知机替代 shadow simulation，消除 800c timeout 上限和 30K 周期仲裁延迟 |
| [experiments/rl_page_prediction_experiment_plan.md](experiments/rl_page_prediction_experiment_plan.md) | RL-PAGE：SARSA + CMAC 函数逼近的在线强化学习页策略预测 |

### 4. 存储开销对比

| 文件 | 内容 |
|------|------|
| [analysis/storage_overhead_comparison.md](analysis/storage_overhead_comparison.md) | GS / FAPS-3D / DYMPL / RL-PAGE 四种策略的硬件存储开销量化对比 |

## 参考文献

| 文件 | 内容 |
|------|------|
| [references/Global scoreboarding实现方案.pdf](references/Global%20scoreboarding实现方案.pdf) | GS 实现方案参考 |
| [references/Srikanth 等 - 2018 - Tackling memory access latency...pdf](references/Srikanth%20等%20-%202018%20-%20Tackling%20memory%20access%20latency%20through%20DRAM%20row%20management.pdf) | 通过 DRAM 行管理解决内存访问延迟 |
| [references/Rafique和Zhu - 2019 - FAPS-3D...pdf](references/Rafique和Zhu%20-%202019%20-%20FAPS-3D%20feedback-directed%20adaptive%20page%20management%20scheme%20for%203D-stacked%20DRAM.pdf) | FAPS-3D：面向 3D 堆叠 DRAM 的反馈式自适应页管理 |
| [references/Rafique和Zhu - 2022 - Dynamic Page Policy...pdf](references/Rafique和Zhu%20-%202022%20-%20Dynamic%20Page%20Policy%20Using%20Perceptron%20Learning.pdf) | DYMPL：基于感知机学习的动态页策略 |
| [references/Ipek et al. - 2008 - Self-Optimizing Memory Controllers...pdf](references/Ipek%20et%20al.%20-%202008%20-%20Self-Optimizing%20Memory%20Controllers%20A%20Reinforcement%20Learning%20Approach.pdf) | 自优化内存控制器：强化学习方法 |

## 核心结论

GS 的 RE Store 被动拦截机制（~18x hit 乘数，等效 ~14,400 周期的行保持时间）对热行保护已近最优。当前研究方向是通过 ML/RL 方法（DYMPL、RL-PAGE）进一步提升 timeout 选择的精度和响应速度。
