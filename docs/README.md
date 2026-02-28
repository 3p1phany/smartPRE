# Global Scoreboarding (GS) 研究文档

本目录包含 GS DRAM 行缓冲管理机制的设计、实验与分析文档。

## 目录结构

```
docs/
├── design/        设计文档（机制原理、计数器定义、改进方向）
├── experiments/   实验方案（实验设计、实现计划、配置筛选）
├── analysis/      分析结果（消融实验、profiling 数据、结果评估）
└── references/    参考文献（PDF）
```

## 推荐阅读顺序

### 1. 基础设计

| 文件 | 内容 |
|------|------|
| [design/gs_implementation.md](design/gs_implementation.md) | GS 机制全解：timeout 预充电、shadow simulation 多候选仲裁、RE Store 热行保护 |
| [design/gs_accuracy_counters.md](design/gs_accuracy_counters.md) | 性能计数器的实现规格，用于衡量 timeout 决策准确率和 RE Store 有效性 |

### 2. 基线分析

| 文件 | 内容 |
|------|------|
| [analysis/GS_NOHOTROW_analysis.md](analysis/GS_NOHOTROW_analysis.md) | RE Store 消融实验：去除热行保护导致 3.1% 性能回归，证明 RE Store 不可或缺 |
| [analysis/gs_profiling_analysis.md](analysis/gs_profiling_analysis.md) | 62 benchmarks 计数器数据分析：90.5% timeout 准确率、71.3% RE 准确率、18x RE hit 乘数，识别出 13 个受 800c timeout 上限瓶颈的 benchmark |

### 3. 改进规划

| 文件 | 内容 |
|------|------|
| [design/gs_improvement_directions.md](design/gs_improvement_directions.md) | 8 个改进方向及优先级排序，基于 profiling 数据的发现 |
| [experiments/re_driven_timeout_escalation.md](experiments/re_driven_timeout_escalation.md) | RE 驱动超时升级实验的完整 4 阶段设计：参数空间、21 组配置、6 个可验证假设 |

### 4. Phase 1 实现与评估

| 文件 | 内容 |
|------|------|
| [experiments/phase1_implementation_plan.md](experiments/phase1_implementation_plan.md) | Phase 1 代码级实现方案：涉及 4 个源文件的修改细节和 8 组参数配置 |
| [analysis/phase1_results_analysis.md](analysis/phase1_results_analysis.md) | Phase 1 实验结果：所有配置均出现回归（GEOMEAN -0.10% ~ -0.23%），升级机制净有害 |

### 5. 消融验证

| 文件 | 内容 |
|------|------|
| [experiments/gs_ext_timeout_only_plan.md](experiments/gs_ext_timeout_only_plan.md) | 消融实验设计：通过 `GS_NO_ESCALATION` 宏隔离扩展候选集 vs 升级机制的影响 |
| [analysis/gs_ext_timeout_only_analysis.md](analysis/gs_ext_timeout_only_analysis.md) | 消融结果：仅扩展候选集对性能几乎无影响（+0.01%），回归来自升级机制本身 |

### 6. 配置筛选

| 文件 | 内容 |
|------|------|
| [experiments/selected_configs_benchmarks.md](experiments/selected_configs_benchmarks.md) | 筛选出 4 组有效配置和 11 个升级确实有益的 benchmark |

## 参考文献

| 文件 | 内容 |
|------|------|
| [references/Global scoreboarding实现方案.pdf](references/Global%20scoreboarding实现方案.pdf) | GS 实现方案参考 |
| [references/Srikanth 等 - 2018 - Tackling memory access latency...pdf](references/Srikanth%20等%20-%202018%20-%20Tackling%20memory%20access%20latency%20through%20DRAM%20row%20management.pdf) | 论文：通过 DRAM 行管理解决内存访问延迟 |

## 核心结论

RE Store 的被动拦截机制（~18x hit 乘数，等效 ~14,400 周期的行保持时间）对热行保护已近最优，显式 timeout 升级在整体 benchmark 层面是冗余甚至有害的。
