# GS-ML：基于感知机学习的自适应 Timeout 行缓冲管理方案

## 1. 研究动机

### 1.1 GS 的成功与瓶颈

Global Scoreboarding (GS) 是当前实现中性能最优的行缓冲管理策略（vs open_page +1.53%, vs DYMPL +2.15%）。其成功来自三个机制的协同：

1. **Timeout-based Speculative Precharge**：在 row-hit cluster 结束后启动倒计时，超时关闭行
2. **Shadow Simulation**：并行模拟 7 个候选 timeout 值 {50,100,150,200,300,400,800}，每 30000 cycle 仲裁选最优
3. **Row Exclusion (RE)**：保护因 timeout 误关而被重新打开的"长寿行"

但 GS profiling 数据揭示了三个核心瓶颈：

| 瓶颈 | 量化证据 | 受影响 benchmark |
|------|---------|-----------------|
| **800c timeout 天花板** | 15 个 benchmark 的 800c 占比 >20%，7 个 >30% | lbm, sphinx3, bwaves, wrf, leslie3d 等 |
| **Shadow Simulation 反应慢** | 30000 cycle 仲裁周期，无法捕捉 phase 级别的快速变化 | 所有有 phase change 的 workload |
| **RE Store 准确率有限** | 整体 71.3%，hpcc 仅 10.7%；容量 64 项几乎 100% 饱和 | ligra, crono, spec06 等 |

### 1.2 DYMPL 的教训

DYMPL 使用感知机直接预测 open/close，但全面落后于 GS（-2.15%），其失败原因为我们的设计提供了关键约束：

1. **不要替代整个 GS 框架**：纯 ML 替代全部逻辑（DYMPL 的做法）不如 ML 辅助经典机制
2. **必须有时间维度特征**：DYMPL 缺失 inter-access time，而这恰是 GS timeout 的核心信号
3. **训练信号必须对称**：DYMPL 只在 ACT 时训练（正确 open 预测永不训练），导致学习偏差
4. **PRT 容量瓶颈**：512 entries 对大 working set 不够

### 1.3 核心创新点

> **观察**：GS 的 shadow simulation 本质上是对 timeout 值的**暴力搜索**——维护 7 套并行状态，每 30000 cycle 选最优。这既昂贵（per-bank 7 套计数器 + 状态机），又受限于固定候选集（800c 上限）和粗粒度仲裁周期（30000c）。
>
> **方案**：用轻量级感知机**直接预测最优 timeout 等级**，替换 shadow simulation。感知机可以：
> - 输出连续的 timeout 等级（突破 800c 天花板）
> - 逐次预测（突破 30000c 仲裁周期）
> - 使用丰富的上下文特征（超越 hit/conflict 二元计数）

---

## 2. 方案设计

### 2.1 整体架构

```
             GS (原始)                          GS-ML (本方案)
    ┌─────────────────────┐           ┌────────────────────────────┐
    │  Shadow Simulation  │           │  Timeout Perceptron        │
    │  7 套并行状态         │    ──►    │  4 特征 × 权重表            │
    │  30000c 仲裁          │           │  每次 cluster end 预测      │
    │  固定候选集            │           │  连续 timeout 等级          │
    └─────────────────────┘           └────────────────────────────┘
    ┌─────────────────────┐           ┌────────────────────────────┐
    │  Row Exclusion      │           │  ML-guided RE              │
    │  64 项 FIFO          │    ──►    │  基于 reopen 概率的准入       │
    │  静态容量             │           │  confidence-based 淘汰      │
    └─────────────────────┘           └────────────────────────────┘
    ┌─────────────────────┐           ┌────────────────────────────┐
    │  Timeout Mechanism  │           │  Timeout Mechanism         │
    │  (保持不变)          │    ══►    │  (保持不变，使用 ML timeout) │
    └─────────────────────┘           └────────────────────────────┘
```

保持 GS 的 timeout + precharge 执行框架不变，**仅替换 timeout 值的选择机制和 RE 的准入策略**。

### 2.2 模块一：Timeout Perceptron（替换 Shadow Simulation）

#### 2.2.1 预测目标

不再做 open/close 二元预测（DYMPL 的做法），而是预测**最优 timeout 等级**。

定义 8 个 timeout 等级（比 GS 的 7 个多一档，突破 800c 天花板）：

```cpp
static constexpr int GSML_TIMEOUT_COUNT = 8;
static constexpr int GSML_TIMEOUT_VALUES[GSML_TIMEOUT_COUNT] = {
    50, 100, 200, 400, 800, 1600, 3200, 6400
};
```

使用对数间隔而非线性间隔，覆盖更宽的时间范围。800c 以上的档位直接回应了 GS profiling 发现的"800c 天花板"问题。

#### 2.2.2 预测时机

**与 GS 相同**：在 row-hit cluster 结束时（`GetCommandToIssue()` 中 `row_hit_count==1` 且 queue 为空时）。

但关键改变：不是像 GS 那样"启动固定值倒计时"，而是**每次都用感知机现场预测最优 timeout**。

#### 2.2.3 特征设计

4 个特征，针对 DYMPL 的特征工程问题逐一修复：

| 特征 | 位宽 | 范围 | 含义 | 对比 DYMPL |
|------|------|------|------|-----------|
| **Inter-Access Time (IAT)** | 4-bit | [0,15] | 该 bank 上次 CAS 到本次 cluster-end 的 cycle 间隔（对数量化） | DYMPL 完全缺失 |
| **Row Hit Ratio (RHR)** | 4-bit | [0,15] | 该 bank 近期 N 次访问中 row hit 的比例（× 15 取整） | 替代 DYMPL 的 page_hitcnt |
| **Cluster Size (CS)** | 3-bit | [0,7] | 当前 row-hit cluster 中 CAS 命令数（饱和到 7） | 替代 DYMPL 的 page_utilization |
| **Queue Pending (QP)** | 2-bit | [0,3] | 该 bank 的 read/write queue 中 pending 请求数（分档：0/1/2-3/4+） | DYMPL 完全缺失 |

**特征量化函数**：

```cpp
// IAT: log2 量化，覆盖 1 ~ 32768 cycles
// gap = curr_cycle - last_cas_cycle
int QuantizeIAT(uint64_t gap) {
    if (gap == 0) return 0;
    int log_val = 0;
    uint64_t v = gap;
    while (v >>= 1) log_val++;
    return std::min(log_val, 15);
}

// RHR: 最近 32 次 CAS 中 row hit 占比 → [0,15]
// 用 32-bit shift register 实现，popcount × 15 / 32
int QuantizeRHR(uint32_t hit_history) {
    int hits = __builtin_popcount(hit_history);
    return (hits * 15 + 16) / 32;  // 四舍五入
}

// CS: 当前 cluster 中的 CAS 数 → saturate to 7
int QuantizeCS(int cluster_cas_count) {
    return std::min(cluster_cas_count, 7);
}

// QP: pending request 数 → 分 4 档
int QuantizeQP(int pending_count) {
    if (pending_count == 0) return 0;
    if (pending_count == 1) return 1;
    if (pending_count <= 3) return 2;
    return 3;
}
```

#### 2.2.4 预测机制

使用 **multi-output 感知机**：每个 timeout 等级有独立的权重集合，计算 8 个 score，选最高分对应的等级。

```
For each timeout level t ∈ [0, 7]:
    score[t] = wt_iat[t][f_iat] + wt_rhr[t][f_rhr]
             + wt_cs[t][f_cs] + wt_qp[t][f_qp]
             + bias[t]

predicted_level = argmax(score[])
timeout_value = GSML_TIMEOUT_VALUES[predicted_level]
```

**权重表大小**：
- `wt_iat`: 8 levels × 16 entries = 128 × 4-bit = 64 bytes
- `wt_rhr`: 8 levels × 16 entries = 128 × 4-bit = 64 bytes
- `wt_cs`: 8 levels × 8 entries = 64 × 4-bit = 32 bytes
- `wt_qp`: 8 levels × 4 entries = 32 × 4-bit = 16 bytes
- `bias`: 8 × 4-bit = 4 bytes
- **总计 ~180 bytes per channel**（远小于 GS 的 7 套 per-bank shadow state）

#### 2.2.5 训练机制

**训练时机**：每次 ACT 命令到来时（与 DYMPL 相同），但增加了关键的对称训练。

**训练逻辑**：

```
On ACT(bank, new_row):
    prev_pred = saved_prediction[bank]
    if !prev_pred.valid: return

    // 计算理想 timeout
    actual_gap = curr_cycle - prev_pred.last_cas_cycle
    ideal_level = FindClosestLevel(actual_gap)

    // 确定 ground truth
    if new_row == prev_pred.row:
        // 同一行被重新打开 → timeout 太短，ideal_level 应更大
        ideal_level = min(prev_pred.predicted_level + 1, 7)
        // 惩罚过短的预测
    else:
        // 不同行 → timeout 正确或偏长
        // ideal_level = 对应 actual_gap 的等级
        ideal_level = FindClosestLevel(actual_gap)

    // 权重更新（只在预测错误或 confidence 不足时）
    if ideal_level != prev_pred.predicted_level || |margin| < THETA:
        delta = +1 for ideal_level weights, -1 for predicted_level weights
        UpdateWeights(prev_pred.features, prev_pred.predicted_level, -1)
        UpdateWeights(prev_pred.features, ideal_level, +1)
```

**关键改进（vs DYMPL 的训练缺陷）**：

1. **CAS-time 正向强化**：当 row hit 到来且 timeout 未过期时，对当前预测做正向强化
```
On CAS(bank, row) where is_row_hit:
    if saved_prediction[bank].valid && row == saved_prediction[bank].row:
        // 正确保持 open → 强化当前 timeout level 的权重
        if |saved_prediction[bank].margin| < THETA:
            UpdateWeights(features, predicted_level, +1)
```

2. **Timeout 到期训练**：当 timeout 倒计时归零执行 precharge 时：
```
On TimeoutPrecharge(bank):
    // timeout 到期，预计正确（不同行会来） 或 不确定
    // 延迟到下次 ACT 验证，与 GS 的 accuracy tracking 相同
```

#### 2.2.6 硬件开销对比

| 组件 | GS Shadow Simulation | GS-ML Timeout Perceptron |
|------|---------------------|--------------------------|
| Per-bank 状态 | 7 × (hits + conflicts + state) = 56 bytes | 仅 prediction state = 12 bytes |
| Per-channel 全局 | 无 | 权重表 ~180 bytes |
| 仲裁逻辑 | 30000c 周期性遍历所有 bank | 每次 cluster-end 点积运算 |
| Timeout 候选 | 7 个固定值，最大 800c | 8 个对数分布，最大 6400c |

Per-bank 存储从 56 bytes 降至 12 bytes（节省 79%），代价是增加 180 bytes per-channel 共享权重表。对于 128 banks/channel 的 DDR5 配置，总存储从 7168 bytes 降至 1716 bytes。

### 2.3 模块二：ML-guided Row Exclusion（改进 RE）

#### 2.3.1 问题分析

当前 RE 的三个问题：
1. **准入策略过于被动**：只有"timeout 关闭后又被 ACT 打开同一行"才会加入 RE Store，错过了"第一次就应该保护"的场景
2. **淘汰策略过于简单**：FIFO + 冲突优先替换，不考虑条目的有效性历史
3. **容量固定**：64 项对高 RE 活跃度 workload（如 ligra）不够

#### 2.3.2 改进方案

**方案 A（推荐，低风险）：Confidence-based RE 准入**

在 timeout precharge 执行前，使用 Timeout Perceptron 的预测 confidence 决定是否加入 RE：

```
On TimeoutPrecharge(bank, row):
    pred = saved_prediction[bank]

    // 如果预测器对"应该关闭"很不自信（margin 小），加入 RE 保护
    if pred.valid && pred.margin < RE_CONFIDENCE_THRESHOLD:
        RE_AddEntry(rank, bankgroup, bank, row)

    // 正常执行 precharge...
```

这让 RE 从"事后补救"变为"事前预防"——预测器不确定时，先保护再说。

**方案 B（中等风险）：Reopen Counter 淘汰策略**

替换 FIFO 为基于 reopen count 的 LRU：

```cpp
struct RowExclusionEntryML {
    int rank, bankgroup, bank, row;
    int reopen_count;      // 被保护后实际被重新访问的次数
    int timeout_count;     // 在 RE 中经历的 timeout 到期次数
    uint64_t insert_cycle; // 插入时的 cycle
};
```

淘汰优先级：`score = reopen_count - timeout_count`，score 最低的优先淘汰。

#### 2.3.3 实验中先做 A，验证有效后可叠加 B。

### 2.4 Phase Detection 机制（可选增强）

当检测到 workload phase 变化时（如突然大量 ACT），临时重置感知机权重或切换到保守模式。

```
Phase Change Detection:
    每 10000 cycle 检查:
        current_act_rate = num_act_in_window / window_size
        if |current_act_rate - prev_act_rate| > PHASE_THRESHOLD:
            // Phase change detected
            // 选项 1: 将所有权重衰减 50%（保留趋势但减少过时学习）
            // 选项 2: 重置 bias 项为 0
            // 选项 3: 将 THETA 临时提高（增加训练频率以快速适应）
```

**实验中作为可选增强，不作为核心贡献。**

---

## 3. 数据结构设计

### 3.1 新增/修改文件

```
dramsim3/src/
├── gsml_predictor.h      ← 新增：GS-ML 预测器头文件
├── gsml_predictor.cc     ← 新增：GS-ML 预测器实现
├── common.h              ← 修改：添加 GSML 到 RowBufPolicy 枚举
├── command_queue.h       ← 修改：添加 GSML 成员
├── command_queue.cc      ← 修改：集成 GSML 预测和训练
├── controller.cc         ← 修改：添加 GSML 字符串映射 + timeout precharge 中 RE 改进
└── simple_stats.cc       ← 修改：注册 GSML 统计计数器
```

### 3.2 gsml_predictor.h

```cpp
#ifndef __GSML_PREDICTOR_H
#define __GSML_PREDICTOR_H

#include <cstdint>
#include <vector>
#include "simple_stats.h"

namespace dramsim3 {

// Timeout levels: logarithmic spacing, extends beyond GS's 800c ceiling
static constexpr int GSML_TIMEOUT_COUNT = 8;
static constexpr int GSML_TIMEOUT_VALUES[GSML_TIMEOUT_COUNT] = {
    50, 100, 200, 400, 800, 1600, 3200, 6400
};

// Feature sizes
static constexpr int GSML_IAT_SIZE = 16;   // 4-bit inter-access time
static constexpr int GSML_RHR_SIZE = 16;   // 4-bit row hit ratio
static constexpr int GSML_CS_SIZE = 8;     // 3-bit cluster size
static constexpr int GSML_QP_SIZE = 4;     // 2-bit queue pending

// Training threshold (perceptron confidence)
static constexpr int GSML_THETA = 8;

// RE confidence threshold (lower margin → more likely to insert into RE)
static constexpr int GSML_RE_CONF_THRESHOLD = 4;

// Per-bank state for tracking features and deferred training
struct GSMLBankState {
    // Feature tracking
    uint64_t last_cas_cycle = 0;         // for IAT computation
    uint32_t hit_history = 0;            // 32-bit shift register for RHR
    int cluster_cas_count = 0;           // current cluster CAS count

    // Prediction state (deferred training)
    bool pred_valid = false;
    int pred_level = 0;                  // predicted timeout level
    int pred_margin = 0;                 // score[best] - score[second_best]
    int pred_row = -1;                   // row at prediction time
    uint64_t pred_cycle = 0;             // cycle at prediction time
    // Feature snapshot for training
    int f_iat = 0;
    int f_rhr = 0;
    int f_cs = 0;
    int f_qp = 0;
};

class GSMLPredictor {
public:
    GSMLPredictor(int num_banks, SimpleStats& stats);

    // Returns timeout value in cycles
    int PredictTimeout(int bank_id, int row, int pending_count, uint64_t curr_cycle);

    // Track CAS for feature updates; returns true if row hit
    void UpdateOnCAS(int bank_id, int row, bool is_row_hit, uint64_t curr_cycle);

    // Train on ACT (row conflict/reopen signals)
    void TrainOnACT(int bank_id, int new_row, uint64_t curr_cycle);

    // Train positive reinforcement on row hit during open prediction
    void TrainOnRowHit(int bank_id);

    // Check if RE should protect this row (based on prediction confidence)
    bool ShouldProtectRow(int bank_id) const;

    // Reset cluster tracking (called when row changes)
    void ResetCluster(int bank_id);

private:
    int num_banks_;
    SimpleStats& stats_;

    // Per-bank state
    std::vector<GSMLBankState> bank_state_;

    // Weight tables: [timeout_level][feature_index]
    // 4-bit signed weights [-8, +7]
    std::vector<std::vector<int>> wt_iat_;    // [8][16]
    std::vector<std::vector<int>> wt_rhr_;    // [8][16]
    std::vector<std::vector<int>> wt_cs_;     // [8][8]
    std::vector<std::vector<int>> wt_qp_;     // [8][4]
    std::vector<int> bias_;                   // [8]

    // Feature quantization
    static int QuantizeIAT(uint64_t gap);
    static int QuantizeRHR(uint32_t hit_history);
    static int QuantizeCS(int cluster_cas_count);
    static int QuantizeQP(int pending_count);

    // Find closest timeout level for a given cycle gap
    static int FindClosestLevel(uint64_t gap);

    // Compute scores for all timeout levels
    void ComputeScores(int f_iat, int f_rhr, int f_cs, int f_qp,
                       int scores[GSML_TIMEOUT_COUNT]) const;

    // Weight update helpers
    static int ClampWeight(int w);
    void UpdateWeightsForLevel(int level, int f_iat, int f_rhr, int f_cs, int f_qp, int delta);
};

}  // namespace dramsim3
#endif
```

### 3.3 核心流程伪代码

```
=== GetCommandToIssue() ===
// row-hit cluster 结束时（row_hit_count == 1 且 queue 为空）

if (top_row_buf_policy_ == GSML):
    // 计算 pending request 数
    pending = queues_[bank].size() + (is_read ? RQ_pending : WB_pending)

    // 预测 timeout 值
    timeout = gsml_predictor_->PredictTimeout(bank, row, pending, clk_)

    // 启动 timeout 倒计时（与 GS 相同的执行框架）
    timeout_ticking[bank] = true
    timeout_counter[bank] = timeout
    issued_cmd[bank] = cmd


=== GetFirstReadyInQueue() ===
// CAS 命令就绪时

if (cmd.IsReadWrite() && top_row_buf_policy_ == GSML):
    gsml_predictor_->UpdateOnCAS(bank, row, is_row_hit, clk_)
    if (is_row_hit):
        gsml_predictor_->TrainOnRowHit(bank)  // 对称训练

// ACT 命令就绪时
if (cmd.IsACT() && top_row_buf_policy_ == GSML):
    gsml_predictor_->TrainOnACT(bank, new_row, clk_)
    gsml_predictor_->ResetCluster(bank)


=== controller.cc ClockTick() — timeout precharge 路径 ===

if (timeout_ticking && timeout_counter == 0):
    if (top_row_buf_policy_ == GSML):
        // 先检查 RE（与 GS 相同）
        if RE_IsInStore(rank, bg, bank, row):
            // RE hit，延长 timeout
            timeout_counter = gsml_predictor_->PredictTimeout(bank, row, 0, clk_)
            continue

        // ML-guided RE 准入：预测器不自信时，预防性加入 RE
        if gsml_predictor_->ShouldProtectRow(bank):
            RE_AddEntry(rank, bg, bank, row)
            timeout_counter = gsml_predictor_->PredictTimeout(bank, row, 0, clk_)
            continue

        // 正常执行 timeout precharge
        IssueCommand(PRECHARGE)
```

---

## 4. 代码修改清单

### 4.1 `dramsim3/src/common.h` (line 10)

在 `RowBufPolicy` 枚举中 `DYMPL` 之后添加 `GSML`：

```cpp
enum class RowBufPolicy {
    OPEN_PAGE, CLOSE_PAGE, ORACLE, SMART_CLOSE, DPM, GS, GS_NOHOTROW, DYMPL, FAPS, GSML, SIZE
};
```

### 4.2 `dramsim3/src/command_queue.h`

在 `DYMPL Predictor` 成员之后添加：

```cpp
// ===== GS-ML Predictor =====
#include "gsml_predictor.h"
std::unique_ptr<GSMLPredictor> gsml_predictor_;
```

### 4.3 `dramsim3/src/command_queue.cc`

**构造函数** — 初始化：
```cpp
if (top_row_buf_policy_ == RowBufPolicy::GSML) {
    gsml_predictor_ = std::unique_ptr<GSMLPredictor>(
        new GSMLPredictor(num_queues_, simple_stats_));
}
```

**per-bank policy 初始化** — GSML 使用 GS 相同的 OPEN_PAGE 初始状态（因为 timeout 机制会动态管理关闭时机）。

**GetCommandToIssue()** — 在 `DYMPL` 分支之后添加 GSML 的 timeout 启动逻辑。

**GetFirstReadyInQueue()** — 在 CAS/ACT 路径中添加 GSML 的特征更新和训练调用。

**ClockTick()** — 复用 GS 的 timeout arbitration 调用路径（GSML 不需要 ClockTick 内的 shadow simulation 仲裁）。

### 4.4 `dramsim3/src/controller.cc`

**构造函数** — 两处 string→enum 映射中添加 `"GSML" ? RowBufPolicy::GSML`。

**ClockTick() timeout precharge 路径** — 在 GS 的 `if(row_buf_policy_==RowBufPolicy::GS ...)` 之后添加 `|| row_buf_policy_==RowBufPolicy::GSML`，并在 RE 准入判断处加入 ML-guided 逻辑。

### 4.5 `dramsim3/src/simple_stats.cc`

注册 GSML 统计计数器：

```cpp
// GS-ML counters
InitStat("gsml_predictions", "counter", "GSML timeout predictions made");
InitStat("gsml_train_on_act", "counter", "GSML training events on ACT");
InitStat("gsml_train_on_hit", "counter", "GSML positive reinforcement on row hit");
InitStat("gsml_re_preemptive", "counter", "GSML preemptive RE insertions (low confidence)");
InitVecStat("gsml_timeout_dist", "vec_counter", "GSML predicted timeout distribution", "level", 8);
InitStat("gsml_level_up", "counter", "GSML cases where timeout was too short (same row reopen)");
InitStat("gsml_level_correct", "counter", "GSML cases where timeout was correct (different row)");
```

### 4.6 新建配置文件

复制 `champsim-la/dramsim3_configs/DDR5_64GB_4ch_4800.ini` 为 `DDR5_64GB_4ch_4800_GSML.ini`，修改：

```ini
row_buf_policy = GSML
```

复制 `champsim-la/champsim_config.json` 为 `champsim_config_GSML.json`，修改 `dram_io_config` 指向新配置文件。

### 4.7 修改文件总结

| 文件 | 修改量 | 性质 |
|------|--------|------|
| `dramsim3/src/gsml_predictor.h` | ~100 行 | 新建 |
| `dramsim3/src/gsml_predictor.cc` | ~250 行 | 新建 |
| `dramsim3/src/common.h:10` | 1 行 | 添加 GSML 枚举 |
| `dramsim3/src/command_queue.h` | ~5 行 | 添加 GSML 成员 |
| `dramsim3/src/command_queue.cc` | ~40 行 | 构造函数 + CAS/ACT/ClusterEnd 集成 |
| `dramsim3/src/controller.cc` | ~15 行 | 字符串映射 + timeout precharge RE 改进 |
| `dramsim3/src/simple_stats.cc` | ~10 行 | 注册计数器 |
| `dramsim3/dramsim3_configs/DDR5_64GB_4ch_4800_GSML.ini` | 新文件 | DRAM 配置 |
| `champsim-la/champsim_config_GSML.json` | 新文件 | ChampSim 配置 |

总计约 **420 行**新/修改的 C++ 代码。

---

## 5. 实验方案

### 5.1 实验一：GS-ML 基础验证

**目标**：验证 Timeout Perceptron 替换 Shadow Simulation 的可行性。

**配置**：

| 配置名 | row_buf_policy | 说明 |
|--------|---------------|------|
| `GS_1c` | GS | 当前最佳策略（已有结果） |
| `GSML_1c` | GSML | 本方案基础版（Timeout Perceptron + 原始 RE） |
| `oracle_1c` | ORACLE | 理论上限（已有结果） |
| `open_page_1c` | OPEN_PAGE | 基线（已有结果） |

**Benchmark**：`benchmarks_selected.tsv` 中全部 62 个 benchmark。

**评估指标**：

1. **IPC**：每个 benchmark 的 IPC 及改善幅度（per-benchmark 报告）
2. **Timeout 准确率**：`gsml_level_correct / (gsml_level_correct + gsml_level_up)`
3. **Timeout 分布**：`gsml_timeout_dist`，验证是否使用了 >800c 的档位
4. **Row Buffer Hit Rate**：`(num_read_row_hits + num_write_row_hits) / (num_read_cmds + num_write_cmds)`
5. **ACT 次数**：`num_act_cmds`
6. **平均读延迟**：`average_read_latency`

**预期结果**：
- 在 GS 表现好的 benchmark（hpcc, hashjoin）上持平或微弱提升
- 在 GS 受 800c 天花板限制的 benchmark（lbm, sphinx3, bwaves, leslie3d）上显著提升
- GEOMEAN 相比 GS 正向改进

### 5.2 实验二：ML-guided RE 验证

**目标**：验证 confidence-based RE 准入的改进效果。

**配置**：

| 配置名 | 说明 |
|--------|------|
| `GSML_1c` | 基础版（实验一的结果） |
| `GSML_RE_1c` | 基础版 + ML-guided RE 准入 |
| `GS_NOHOTROW_1c` | GS 无 RE（已有结果，用于对比 RE 贡献） |

**额外指标**：
- `gsml_re_preemptive`：预防性 RE 插入次数
- RE 准确率对比（GS 71.3% → GSML_RE 目标 >75%）

### 5.3 实验三：敏感性分析

**目标**：调优超参数，找到最佳配置。

| 参数 | 测试值 | 默认值 |
|------|-------|--------|
| Training threshold (THETA) | 4, 8, 12, 16 | 8 |
| RE confidence threshold | 2, 4, 6, 8 | 4 |
| Timeout 候选集 | {50..800} (7档, 同GS), {50..3200} (8档), {50..6400} (8档) | {50..6400} |
| RHR history length | 16-bit, 32-bit, 64-bit | 32-bit |

每组敏感性实验选取 **20 个有代表性的 benchmark**（从 62 个中选取，覆盖 GS 表现好/差的两类），减少计算开销。

### 5.4 实验四：多核验证（可选）

使用 `champsim_config_4c.json` 验证 GS-ML 在 4 核场景下的表现。多核下 bank-level 竞争加剧，timeout 预测难度更大。

### 5.5 实验五：消融实验

**目标**：量化各组件的独立贡献。

| 配置 | Timeout Perceptron | 扩展候选集 | ML-guided RE | 说明 |
|------|-------------------|-----------|-------------|------|
| GS (baseline) | - | - | - | 原始 GS |
| GSML-base | + | - | - | 仅用感知机替换 shadow sim，保持 7 候选值 |
| GSML-ext | + | + | - | 感知机 + 扩展到 8 档 6400c |
| GSML-full | + | + | + | 完整方案 |

---

## 6. 构建与运行

```bash
# 1. 构建 DRAMSim3
cd /root/data/smartPRE/dramsim3 && mkdir -p build && cd build && cmake .. && make -j8

# 2. 构建 ChampSim (GSML 配置)
cd /root/data/smartPRE/champsim-la
cp champsim_config.json champsim_config_GSML.json
# 修改 champsim_config_GSML.json 中 dram_io_config 指向 GSML DRAM 配置文件
python3 config.sh champsim_config_GSML.json
make -j8

# 3. 运行仿真
export LD_LIBRARY_PATH=/root/data/smartPRE/dramsim3:$LD_LIBRARY_PATH
TRACE_ROOT=/root/data/Trace/LA scripts/run_selected_slices.sh
# 输入 label: GSML_1c

# 4. 对比结果
python3 scripts/compare_ipc.py results/GS_1c results/GSML_1c
```

---

## 7. 验证步骤

1. **编译测试**：DRAMSim3 和 ChampSim 均编译通过，无 warning
2. **功能验证**：运行少量 trace（warmup=1M, sim=5M），确认：
   - `gsml_predictions > 0`（预测器被调用）
   - `gsml_timeout_dist` 中各等级有合理分布
   - `gsml_train_on_act > 0` 和 `gsml_train_on_hit > 0`（双向训练正常）
   - timeout precharge 行为正常（无 assert/crash）
   - 无除零错误
3. **结果对比**：与 GS 在相同 benchmark 上对比 IPC
4. **统计验证**：检查不变量
   - `gsml_level_correct + gsml_level_up <= gsml_predictions`
   - timeout precharge + on-demand precharge = total demand precharge

---

## 8. 论文故事线

### Title
**Learning to Wait: Perceptron-Guided Adaptive Timeout for DRAM Row Buffer Management**

### Abstract 核心论点

1. **问题**：现有自适应行缓冲管理策略（如 Global Scoreboarding）依赖暴力搜索最优 timeout 值——维护多套并行状态、周期性仲裁。这既消耗硬件资源，又受限于固定候选集和粗粒度仲裁周期。
2. **洞察**：最优 timeout 值与 bank 的局部访问模式（访问间隔、命中率、cluster 大小）强相关，可通过轻量级感知机在线学习。
3. **方案**：GS-ML 用 4 特征感知机替换 shadow simulation，直接预测最优 timeout 等级。配合 confidence-based 的行排除准入策略，实现更精准的行缓冲管理。
4. **结果**：相比 GS，GS-ML 在 62 个 benchmark 上实现 X% 的 IPC 提升（待实验填充），同时减少 79% 的 per-bank 存储开销。

### 与 Related Work 的区分

| 工作 | 方法 | 局限 | 本文改进 |
|------|------|------|---------|
| GS (Srikanth 2018) | Shadow simulation + RE | 固定候选集，粗粒度仲裁 | 感知机替换 shadow sim |
| DYMPL (Rafique 2022) | 感知机直接预测 open/close | 替代整个框架，缺乏时间特征 | 保留 GS 框架，仅替换 timeout 选择 |
| FAPS-3D (Rafique 2019) | FSM + per-bank epoch | 启发式阈值，不学习 | 数据驱动的在线学习 |
| DPM | FSM + 全局 epoch | 全局 cycle epoch 不适配 per-bank | Per-bank 感知机 |

### 关键实验图表

1. **Per-benchmark IPC comparison** (GSML vs GS vs DYMPL vs Oracle)
2. **Timeout distribution** (GSML vs GS, 特别关注 >800c 档位的使用)
3. **Timeout accuracy** (GSML vs GS, 特别关注 lbm-like benchmark)
4. **Hardware overhead comparison** (存储和逻辑面积)
5. **Ablation study** (各组件贡献分解)
6. **Sensitivity analysis** (THETA, timeout 候选集)

---

## 9. 风险与缓解

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| 感知机预测精度不够 | 中 | 性能不如 GS | 增加特征（如加入 row address hash）、调大 THETA |
| 过拟合到特定 benchmark | 低 | 泛化性差 | 使用 62 个 diverse benchmark 验证 |
| 训练不稳定（权重震荡） | 中 | 性能抖动 | 限制学习率（ClampWeight ±1）、增大 THETA |
| 扩展 timeout 范围导致行占用过久 | 中 | 增加 row conflict | 通过 RE confidence 机制自动调节 |
| 实现 bug | 高 | 结果不可靠 | 严格的不变量检查 + 与 GS 的 diff 对比 |

---

## 10. 时间规划

| 阶段 | 内容 | 预计工作量 |
|------|------|-----------|
| Phase 1 | 实现 gsml_predictor.h/.cc，集成到 command_queue | 代码实现 |
| Phase 2 | 编译验证 + 功能测试（少量 trace） | 调试 |
| Phase 3 | 实验一：62 benchmark 全量运行 + IPC 对比 | 仿真 + 分析 |
| Phase 4 | 实验二：ML-guided RE | 代码 + 仿真 |
| Phase 5 | 实验三/五：敏感性 + 消融 | 仿真 + 分析 |
| Phase 6 | 论文撰写 | 写作 |
