# SMART策略调试指南

本文档描述了为SMART策略添加的性能计数器，以及如何使用它们来定位性能问题。

## 简化架构说明（当前版本）

为了专注于timeout预测准确性分析，以下功能已被**禁用**：

| 功能 | 状态 | 说明 |
|------|------|------|
| Stage 5: Early Termination | **禁用** | 队列满/冲突过多/等待过久不再触发提前终止 |
| Stage 6: Hot Row Tracking | **禁用** | 不再追踪热行个体timeout |
| Stage 7: Fallback Mode | **禁用** | 不再回退到OPEN_PAGE/CLOSE_PAGE |
| Lookahead | **禁用** | 事务缓冲区预检测不再触发timeout重置 |

**保留的核心功能**：
- Stage 2-3: 重用距离统计 + Timeout计算
- Stage 4: Timeout计时器管理（启动 → 重置(行命中) → 过期(触发precharge)）

## 新增Timeout预测分析计数器

### Timeout预测准确性

| 计数器名称 | 描述 | 用途 |
|-----------|------|------|
| `smart_timeout_hit_captured` | timeout期间成功捕获的行命中数 | 预测成功率 |
| `smart_timeout_no_hit` | timeout期间无行命中就过期 | 预测失败率 |
| `smart_timeout_waste_cycles` | timeout期间无行命中的等待周期总数 | 浪费的等待时间 |

**关键指标计算**：
- Timeout成功率 = `smart_timeout_hit_captured / smart_timeout_started`
- Timeout浪费率 = `smart_timeout_no_hit / smart_timeout_started`
- 平均浪费周期 = `smart_timeout_waste_cycles / smart_timeout_no_hit`

### Post-Timeout机会损失分析

| 计数器名称 | 描述 | 用途 |
|-----------|------|------|
| `smart_post_timeout_hit_within_50` | timeout过期后50周期内的行命中 | 短预测(timeout太短) |
| `smart_post_timeout_hit_within_100` | timeout过期后100周期内的行命中 | 中预测(timeout太短) |
| `smart_post_timeout_hit_within_200` | timeout过期后200周期内的行命中 | 长预测(timeout太短) |

**关键指标计算**：
- 短预测损失率 = `smart_post_timeout_hit_within_50 / smart_timeout_expired`
- 如果此比例 > 10%，说明timeout设置偏短

### 冲突代价分析

| 计数器名称 | 描述 | 用途 |
|-----------|------|------|
| `smart_conflict_wait_cycles_total` | 所有冲突请求的等待周期总和 | 冲突延迟代价 |
| `smart_conflict_requests_total` | 等待过timeout的冲突请求总数 | 冲突频率 |

**关键指标计算**：
- 平均冲突等待 = `smart_conflict_wait_cycles_total / smart_conflict_requests_total`
- 如果 > 100周期，说明timeout太长导致冲突等待

### Timeout值分布

| 计数器名称 | 描述 |
|-----------|------|
| `smart_actual_timeout_used[X-Y]` | 实际使用的timeout值直方图 |
| `smart_computed_timeout[X-Y]` | 计算的timeout值分布直方图 |

## 调优决策矩阵

| 现象 | 可能原因 | 调优方向 |
|------|---------|---------|
| Timeout成功率 < 30% | timeout太长，浪费等待时间 | 减小重用距离统计窗口或调整边际分析参数 |
| `smart_post_timeout_hit_within_50` / `smart_timeout_expired` > 10% | timeout太短 | 增大边际分析中的C值或调整bin边界 |
| 平均冲突等待 > 100周期 | timeout太长导致冲突等待 | 需要平衡命中率与冲突延迟 |
| 平均浪费周期 > 计算的timeout值 | timeout计算偏保守 | 检查重用距离分布是否准确 |
| Timeout成功率很高但行命中率低 | 其他行缓冲管理问题 | 检查precharge来源和ACT/PRE比例 |

---

## 原有计数器（保留但部分功能禁用）

### Stage 2/3: 重用距离统计和超时计算

| 计数器名称 | 描述 | 预期目标 |
|-----------|------|---------|
| `smart_reuse_samples` | 重用距离样本总数 | 应有足够多的样本（>100）才能准确计算超时 |
| `smart_timeout_computed` | 超时计算次数 | 每10K周期更新一次，应定期更新 |
| `smart_computed_timeout[X-Y]` | 计算超时值分布直方图 | 应分布合理，不应集中在极端值 |

**诊断方法**：
- 如果 `smart_reuse_samples` 太少，说明没有足够的行重用数据，超时值可能不准确
- 如果 `smart_computed_timeout` 分布集中在很大的值（>200），可能导致行保持打开太久

### Stage 4: 超时计时器管理

| 计数器名称 | 描述 | 预期目标 |
|-----------|------|---------|
| `smart_timeout_started` | 超时计时器启动次数 | 应在每个行打开后最后一个命令时启动 |
| `smart_timeout_expired` | 超时过期次数（触发precharge） | 应反映超时策略的积极程度 |
| `smart_timeout_reset_on_hit` | 因行命中重置超时的次数 | 高值说明行命中较多，超时策略有效 |

**诊断方法**：
- 比较 `smart_timeout_started` 和 `smart_timeout_expired`：
  - 如果 expired >> started，说明超时太短
  - 如果 expired << started，说明超时太长或行命中率高

### Stage 5: 提前终止条件 (已禁用)

以下计数器应始终为0（功能已禁用）：

| 计数器名称 | 描述 |
|-----------|------|
| `smart_early_term_queue_full` | 队列满触发的提前终止 |
| `smart_early_term_conflict_count` | 冲突过多触发的提前终止 |
| `smart_early_term_conflict_wait` | 冲突等待太久触发的提前终止 |

### Stage 6: 热行追踪 (已禁用)

以下计数器应始终为0（功能已禁用）：

| 计数器名称 | 描述 |
|-----------|------|
| `smart_hot_row_timeout_used` | 使用热行超时的次数 |

### Stage 7: 回退模式 (已禁用)

以下计数器应始终为0（功能已禁用）：

| 计数器名称 | 描述 |
|-----------|------|
| `smart_fallback_open_page` | 回退到OPEN_PAGE的次数 |
| `smart_fallback_close_page` | 回退到CLOSE_PAGE的次数 |
| `smart_fallback_high_hit_rate` | 高命中率触发回退 |
| `smart_fallback_low_hit_rate` | 低命中率触发回退 |
| `smart_fallback_insufficient_samples` | 样本不足触发回退 |
| `smart_fallback_low_queue_occupancy` | 队列占用低触发回退 |

### Lookahead检测 (已禁用)

以下计数器应始终为0（功能已禁用）：

| 计数器名称 | 描述 |
|-----------|------|
| `smart_lookahead_row_hits` | 事务缓冲区检测到的行命中 |
| `smart_lookahead_row_conflicts` | 事务缓冲区检测到的行冲突 |
| `smart_lookahead_early_terminations` | Lookahead触发的提前终止 |

### Precharge来源追踪

| 计数器名称 | 描述 | 预期目标 |
|-----------|------|---------|
| `smart_precharge_timeout` | 超时触发的precharge数 | 正常超时关闭 |
| `smart_precharge_early_term` | 提前终止触发的precharge数 | **应为0**（已禁用） |

## 诊断信息输出

在最终统计输出（ddr.txt）的末尾，会打印SMART策略诊断信息：

```
###########################################
## SMART Strategy Diagnostics - Channel X
###########################################

## Row Hit Distance Distribution (Reuse Distance)
distance[0-15]: XXXX
distance[16-47]: XXXX
...
total_reuse_samples: XXXX

## Per-Bank Computed Timeout Values
bank_0_timeout: XXX
bank_1_timeout: XXX
...

## Per-Bank Fallback Mode Status
bank_0_mode: SMART (reason: none)   # 禁用后应始终为SMART
bank_1_mode: SMART (reason: none)
...

## Per-Bank Queue Occupancy Statistics
bank_0_avg_queue_occupancy: 1.5 (ratio: 0.1875, threshold: 0.30)
...

## Per-Bank Reuse Samples (for fallback condition 3)
bank_0_reuse_samples: 5000 (threshold: 200)
...

## Per-Bank Hot Row Statistics
bank_0_hot_rows: 0   # 禁用后应始终为0
...

## Per-Bank Row Hit Rate
bank_0_hit_rate: 0.XXXX
...
```

## 验证禁用功能

运行实验后，检查以下计数器确认功能已禁用：

```bash
# 检查回退模式计数器（应为0）
grep "smart_fallback" results/ddr.txt

# 检查热行计数器（smart_hot_row_timeout_used应为0）
grep "smart_hot_row" results/ddr.txt

# 检查提前终止计数器（应为0）
grep "smart_early_term" results/ddr.txt

# 检查lookahead计数器（应为0）
grep "smart_lookahead" results/ddr.txt
```

## 性能问题定位流程（简化版）

### 1. 检查Timeout预测准确性

```
Timeout成功率 = smart_timeout_hit_captured / smart_timeout_started
```
- 如果 < 30%：timeout太长，浪费等待时间
- 如果 > 70%：timeout设置合理

### 2. 检查机会损失

```
短预测损失率 = smart_post_timeout_hit_within_50 / smart_timeout_expired
```
- 如果 > 10%：timeout太短，错过了本可以捕获的行命中

### 3. 检查冲突代价

```
平均冲突等待 = smart_conflict_wait_cycles_total / smart_conflict_requests_total
```
- 如果 > 100周期：timeout太长导致冲突请求等待过久

### 4. 平衡决策

- 如果Timeout成功率低但机会损失也低：timeout太长，可以缩短
- 如果Timeout成功率高但机会损失高：当前设置合理，但可能需要更精细的预测
- 如果冲突等待过长：需要更激进的timeout策略

## 运行新实验

编译完成后，运行以下命令重新进行实验：

```bash
cd /root/data/smartPRE/champsim-la
# 运行单个测试
./bin/champsim --warmup_instructions 10000000 --simulation_instructions 50000000 <trace_file>

# 或使用批量运行脚本
./scripts/run_benchmarks.sh
```

实验完成后，检查结果目录中的ddr.txt文件，分析新增的计数器来定位问题。
