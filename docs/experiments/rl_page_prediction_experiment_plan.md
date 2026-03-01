# RL-PAGE：基于强化学习的关页预测策略

> 来源：Ipek et al., "Self-Optimizing Memory Controllers: A Reinforcement Learning Approach", ISCA 2008

---

## 1. 论文创新点与关页预测的关联

### 1.1 论文方法概述

Ipek et al. 将 DRAM 命令调度建模为强化学习问题。每个 DRAM cycle，RL agent 从所有合法 DRAM 命令（Precharge、Activate、Read、Write、NOP）中选择一条发射。Agent 通过 SARSA 在线学习 + CMAC 函数近似来估计每个 state-action pair 的长期价值 Q(s,a)，并以数据总线利用率作为奖励信号。

### 1.2 关页预测部分的创新提取

论文中与关页预测直接相关的创新点：

1. **用 Q-value 衡量"现在关页"的长期价值**：传统方案（open/close/timeout）用启发式规则决定何时关页；论文的 RL agent 通过累积折扣奖励学习到：在某些状态下发 Precharge（即时 reward=0）能为后续访问腾出更优路径，获得更高的长期收益。

2. **CMAC 函数近似解决状态空间爆炸**：关页决策所需的状态组合（queue depth、row hit 情况、pending 请求分布等）数量巨大，直接建表不可行。CMAC 用多张偏移的粗粒度表近似 Q(s,a)，使相近状态共享学习经验。

3. **SARSA 在线学习自适应 workload**：不像固定策略（OPEN_PAGE/CLOSE_PAGE）或慢仲裁策略（GS 30000c 周期），RL 每次做关页决策后都能获得反馈并更新策略，天然适应 phase 变化。

4. **ε-greedy 持续探索**：即使策略已收敛，5% 的随机探索保证策略在 workload 变化时不会陷入过时的局部最优。

### 1.3 本方案的定位

将论文的 RL 方法（SARSA + CMAC + ε-greedy）**专门应用于关页决策**，作为一种新的 `row_buf_policy`，与 GS、FAPS、DYMPL 等同级别对比。

- **命令调度（FR-FCFS）保持不变**：Read/Write/Activate 的选择和排序沿用现有逻辑
- **RL 只回答一个问题**：当 row-hit cluster 结束时（`row_hit_count==1`），是否关页？
- **决策点与 DYMPL 完全一致**：在 `GetCommandToIssue()` 的 `row_hit_count==1` 分支中做判断

---

## 2. 方案设计

### 2.1 决策时机

与 DYMPL、SMART_CLOSE 相同：在 `command_queue.cc: GetCommandToIssue()` 中，当检测到 `row_hit_count==1`（当前命令是该 row 上最后一条 pending 的 CAS）时触发关页决策。

```
if (row_hit_count == 1):
    if SMART_CLOSE: → 直接 auto-precharge（总是关页）
    if GS:          → 启动 timeout 倒计时（延迟关页）
    if DYMPL:       → 感知机预测 open/close
    if RL_PAGE:     → RL agent 根据 Q-value 决定 open/close    ← 本方案
```

### 2.2 MDP 建模

#### 2.2.1 状态 (State)

每次触发关页决策时，从当前 bank 和 queue 中提取状态属性：

| # | 属性 | 位宽 | 含义 | 来源 |
|---|------|------|------|------|
| S1 | Read queue depth | 4-bit [0,15] | 全局 read 压力 | `pending_rd_q_.size() >> 2` |
| S2 | Write queue depth | 4-bit [0,15] | 全局 write 压力 | `pending_wr_q_.size() >> 2` |
| S3 | Bank queue depth | 3-bit [0,7] | 当前 bank 的 pending CAS 数 | `queues_[bank].size()` |
| S4 | Row hit cluster size | 3-bit [0,7] | 当前 row 上连续 hit 了多少次 | `channel_state_.RowHitCount(...)` |
| S5 | Same-row pending | 3-bit [0,7] | transaction queue 中还有多少请求目标是同一 row | 遍历 read/write queue 统计 |

状态空间大小：16 × 16 × 8 × 8 × 8 = 131,072

#### 2.2.2 动作 (Action)

**二元决策**：

| 动作 | 含义 | 对应操作 |
|------|------|---------|
| CLOSE (0) | 关页 | 将 READ → READ_PRECHARGE 或 WRITE → WRITE_PRECHARGE |
| KEEP_OPEN (1) | 保持打开 | 不修改命令，行保持 OPEN 等待后续访问 |

#### 2.2.3 奖励 (Reward)

奖励在**下一次**同一 bank 触发关页决策时回溯给出：

```
上次决策为 KEEP_OPEN:
    如果本次是 row hit（同一行再次被访问）:
        reward = +1     // 保持打开是正确的——省了一次 PRE+ACT
    如果本次是 row conflict（不同行到来，触发了 on-demand precharge）:
        reward = -1     // 保持打开是错误的——行白占着 bank

上次决策为 CLOSE:
    如果下次访问该 bank 时是同一行:
        reward = -1     // 关早了——白白多了一次 PRE+ACT
    如果下次访问该 bank 时是不同行:
        reward = +1     // 关对了——提前释放了 bank
```

这个奖励直接反映了关页决策的正确性。与 Ipek 论文的总线利用率奖励（+1 for Read/Write, 0 otherwise）精神一致——正确的关页决策最终提升总线利用率——但更直接地针对关页问题给出信号。

#### 2.2.4 SARSA 更新

每次同一 bank 触发关页决策时，用上次该 bank 的 (s, a, r) 更新：

```
Q(s_prev, a_prev) ← (1-α) Q(s_prev, a_prev) + α [r + γ Q(s_curr, a_curr)]

α = 0.1, γ = 0.95（论文推荐值）
```

注意：这是 **per-bank** 的 SARSA 更新链。每个 bank 独立维护自己的 prev_state/prev_action/reward 状态，但**共享同一组 CMAC 权重表**——通过不同 bank 的不同访问模式共同训练。

#### 2.2.5 ε-greedy 探索

```
以概率 ε=0.05 随机选 CLOSE 或 KEEP_OPEN
以概率 0.95 选 Q-value 更大的动作
```

### 2.3 CMAC 函数近似

#### 2.3.1 结构

```cpp
static constexpr int NUM_TILINGS = 8;       // 8 张偏移粗粒度表
static constexpr int TABLE_SIZE = 256;      // 每张表 256 entries
static constexpr int NUM_ACTIONS = 2;       // CLOSE / KEEP_OPEN

// Q(s, a) = Σ cmac_[tiling][hash(s, a, tiling)]  for tiling in [0, NUM_TILINGS)
int16_t cmac_[NUM_TILINGS][TABLE_SIZE];     // 16-bit 定点数

// 总存储 = 8 × 256 × 2B = 4 KB per channel
// 远小于论文原始设计（32 KB），因为动作空间从 6 缩小到 2
```

#### 2.3.2 索引生成

每张 tiling 以不同的固定偏移量错位划分状态空间（论文 Figure 5(c)）：

```cpp
static constexpr int OFFSETS[NUM_TILINGS][5] = {
    {0,  0,  0, 0, 0},
    {3,  7,  2, 5, 1},
    {11, 3,  5, 1, 2},
    {7,  13, 1, 3, 3},
    {5,  9,  4, 6, 2},
    {2,  11, 6, 2, 1},
    {13, 5,  3, 7, 3},
    {9,  1,  7, 4, 2},
};

int CMACIndex(int tiling, RLState s, int action) {
    uint32_t raw = (((s.s1 + OFFSETS[tiling][0]) & 0xF) << 13)
                 | (((s.s2 + OFFSETS[tiling][1]) & 0xF) << 9)
                 | (((s.s3 + OFFSETS[tiling][2]) & 0x7) << 6)
                 | (((s.s4 + OFFSETS[tiling][3]) & 0x7) << 3)
                 | (((s.s5 + OFFSETS[tiling][4]) & 0x7));
    raw ^= (action ? 0xA5A5 : 0x5A5A);   // 区分两个动作
    return ((raw ^ (raw >> 8)) & 0xFF);   // 哈希折叠到 256
}
```

#### 2.3.3 Q-value 计算与更新

```cpp
int32_t GetQ(RLState s, int action) {
    int32_t sum = 0;
    for (int t = 0; t < NUM_TILINGS; t++)
        sum += cmac_[t][CMACIndex(t, s, action)];
    return sum;
}

void UpdateQ(RLState s, int action, int32_t td_error) {
    int16_t delta = (int16_t)(RL_ALPHA * td_error / NUM_TILINGS);
    for (int t = 0; t < NUM_TILINGS; t++) {
        int idx = CMACIndex(t, s, action);
        cmac_[t][idx] = Clamp16(cmac_[t][idx] + delta);
    }
}
```

### 2.4 整体流程

```
=== command_queue.cc: GetCommandToIssue() ===

cmd = GetFirstReadyInQueue(queue);      // FR-FCFS 选出命令（不变）
if (cmd.IsReadWrite()):
    row_hit_count = ...;                // 统计 pending same-row CAS（不变）

    if (row_hit_count == 1):            // cluster 结束，触发关页决策
        if (top_row_buf_policy_ == RowBufPolicy::RL_PAGE):
            // 1. 提取状态
            state = ExtractState(queue_idx_, cmd, pending_rd_q_, pending_wr_q_);

            // 2. ε-greedy 选择动作
            action = rl_page_agent_->SelectAction(state);

            // 3. 计算上次决策的 reward 并做 SARSA 更新
            rl_page_agent_->UpdateOnDecision(queue_idx_, state, action);

            // 4. 执行动作
            if (action == CLOSE):
                cmd.cmd_type = READ → READ_PRECHARGE / WRITE → WRITE_PRECHARGE;
                autoPRE_added = true;
            // else: KEEP_OPEN, 不修改 cmd
```

---

## 3. 代码修改清单

### 3.1 新增/修改文件

```
dramsim3/src/
├── rl_page_agent.h      ← 新增：RL 关页 agent（SARSA + CMAC）
├── rl_page_agent.cc     ← 新增：实现
├── common.h             ← 修改：枚举添加 RL_PAGE
├── command_queue.h      ← 修改：添加 rl_page_agent_ 成员
├── command_queue.cc     ← 修改：GetCommandToIssue() 中添加 RL_PAGE 分支
│                                 + ACT 时回溯 reward
├── controller.cc        ← 修改：两处 string→enum 映射
└── simple_stats.cc      ← 修改：注册统计计数器
```

### 3.2 rl_page_agent.h

```cpp
#ifndef __RL_PAGE_AGENT_H
#define __RL_PAGE_AGENT_H

#include <cstdint>
#include <vector>
#include <random>
#include "simple_stats.h"

namespace dramsim3 {

static constexpr int RLPAGE_NUM_TILINGS = 8;
static constexpr int RLPAGE_TABLE_SIZE = 256;
static constexpr double RLPAGE_ALPHA = 0.1;
static constexpr double RLPAGE_GAMMA = 0.95;
static constexpr double RLPAGE_EPSILON = 0.05;

struct RLPageState {
    int s1, s2, s3, s4, s5;
};

// Per-bank 上下文（跟踪上次关页决策以便回溯 reward）
struct RLPageBankCtx {
    bool valid = false;       // 上次决策是否存在
    RLPageState state;        // 上次决策时的状态
    int action;               // 0=CLOSE, 1=KEEP_OPEN
    int row;                  // 上次决策时的 open row
};

class RLPageAgent {
public:
    RLPageAgent(int num_banks, SimpleStats& stats);

    // 在 cluster-end 时调用：选动作 + 回溯更新上次决策
    // 返回 0=CLOSE, 1=KEEP_OPEN
    int Decide(int bank_id, int row,
               int rd_q_depth, int wr_q_depth,
               int bank_q_depth, int row_hit_count, int same_row_pending);

    // 在 ACT 时调用：若上次是 KEEP_OPEN，这里回溯 reward
    void OnActivate(int bank_id, int new_row);

private:
    int num_banks_;
    SimpleStats& stats_;
    std::mt19937 rng_;

    // CMAC 表（per channel 共享）
    int16_t cmac_[RLPAGE_NUM_TILINGS][RLPAGE_TABLE_SIZE];

    // Per-bank 上下文
    std::vector<RLPageBankCtx> bank_ctx_;

    int CMACIndex(int tiling, RLPageState s, int action) const;
    int32_t GetQ(RLPageState s, int action) const;
    void UpdateQ(RLPageState s, int action, int32_t td_error);
    RLPageState MakeState(int rd_q, int wr_q, int bk_q, int rh, int sr) const;
};

}  // namespace dramsim3
#endif
```

### 3.3 command_queue.cc 中的集成

**在 `row_hit_count==1` 的分支中添加 RL_PAGE**：

```cpp
else if (top_row_buf_policy_ == RowBufPolicy::RL_PAGE) {
    int rd_q = controller_->read_queue().size();
    int wr_q = controller_->write_buffer().size();
    int bk_q = queues_[queue_idx_].size();
    int rh = channel_state_.RowHitCount(cmd.Rank(), cmd.Bankgroup(), cmd.Bank());
    int sr = row_hit_count;  // same-row pending（已在上方计算）

    int action = rl_page_agent_->Decide(
        queue_idx_, cmd.Row(), rd_q, wr_q, bk_q, rh, sr);

    if (action == 0) {  // CLOSE
        cmd.cmd_type = cmd.IsRead() ? CommandType::READ_PRECHARGE
                                    : CommandType::WRITE_PRECHARGE;
        autoPRE_added = true;
    }
    // action == 1: KEEP_OPEN, 不修改
}
```

**在 ACT 命令发射处（`GetFirstReadyInQueue` 中检测到 ACT）回调**：

```cpp
if (cmd.IsActivate() && top_row_buf_policy_ == RowBufPolicy::RL_PAGE) {
    rl_page_agent_->OnActivate(queue_idx_, cmd.Row());
}
```

### 3.4 修改文件总结

| 文件 | 修改量 | 性质 |
|------|--------|------|
| `dramsim3/src/rl_page_agent.h` | ~70 行 | 新建 |
| `dramsim3/src/rl_page_agent.cc` | ~200 行 | 新建 |
| `dramsim3/src/common.h` | 1 行 | 枚举添加 `RL_PAGE` |
| `dramsim3/src/command_queue.h` | ~5 行 | 添加 rl_page_agent_ 成员 |
| `dramsim3/src/command_queue.cc` | ~30 行 | cluster-end RL 分支 + ACT 回调 |
| `dramsim3/src/controller.cc` | 2 行 | 字符串映射 |
| `dramsim3/src/simple_stats.cc` | ~8 行 | 注册计数器 |

总计约 **320 行**新/修改代码。

---

## 4. 实验方案

### 4.1 实验一：RL_PAGE 与现有关页策略对比

**目标**：将 RL 关页策略与所有现有关页策略直接对比。

**配置**：

| 配置名 | row_buf_policy | 说明 |
|--------|---------------|------|
| `OPEN_1c` | OPEN_PAGE | 从不主动关页（最极端的 keep-open） |
| `CLOSE_1c` | CLOSE_PAGE | 每次 CAS 后立即关页 |
| `GS_1c` | GS | Timeout + Shadow Simulation + RE（已有结果） |
| `DYMPL_1c` | DYMPL | 感知机预测 open/close |
| `FAPS_1c` | FAPS | FSM 切换 open/close |
| `DPM_1c` | DPM | FSM + 全局 epoch |
| `RLPAGE_1c` | RL_PAGE | **本方案：SARSA + CMAC** |
| `oracle_1c` | ORACLE | 理论上限（已有结果） |

**Benchmark**：`benchmarks_selected.tsv` 中全部 62 个 benchmark。

**评估指标**（每个 benchmark 单独报告）：

1. **IPC** 及相对 OPEN_PAGE 的改善百分比
2. **Row Buffer Hit Rate**：`(num_read_row_hits + num_write_row_hits) / (num_read_cmds + num_write_cmds)`
3. **ACT 次数**：`num_act_cmds`
4. **平均读延迟**：`average_read_latency`
5. **Precharge 分布**：auto-precharge（RL 主动关页） vs on-demand precharge（row conflict 被迫关页）的比例
6. **RL 特有指标**：
   - 决策数：`rlpage_decisions`
   - 关页率：`rlpage_close_count / rlpage_decisions`
   - 探索率：`rlpage_explorations / rlpage_decisions`（应 ≈ 5%）
   - 正奖励占比：`rlpage_positive_rewards / rlpage_rewards`（关页决策的准确率）

### 4.2 实验二：RL 学习过程分析

**目标**：验证 RL 确实在学习，并展示收敛过程。

**方法**：选取 5 个有代表性的 benchmark，运行长仿真（sim=200M），每 10M instructions 输出一次快照。

| Benchmark | 选取原因 |
|-----------|---------|
| 高 locality（如 stream） | RL 应学到 KEEP_OPEN 为主 |
| 低 locality（如 mcf） | RL 应学到 CLOSE 为主 |
| Phase 变化（如 omnetpp） | RL 应动态切换策略 |
| GS 受限（如 lbm, 800c 天花板） | RL 无 timeout 天花板限制 |
| 均衡型 | RL 应学到中间策略 |

**分析图表**：
- 关页率（close_count/decisions）随仿真时间的变化曲线
- Row buffer hit rate 随时间的变化曲线
- 每 10M 窗口内 positive_reward 比例的变化

### 4.3 实验三：RL 超参数敏感性

**目标**：找到最优超参数（对应论文 Section 5.1.4 Figure 12）。

| 参数 | 测试值 | 默认值 | 论文结论 |
|------|-------|--------|---------|
| α (学习率) | 0.01, 0.05, **0.1**, 0.2, 0.5 | 0.1 | 0.1 最优 |
| γ (折扣因子) | 0, 0.5, 0.9, **0.95**, 0.99 | 0.95 | 0.95 最优；γ=0 严重退化 |
| ε (探索率) | **0**, 0.01, **0.05**, 0.1, 0.2 | 0.05 | 0~0.1 均可，>0.2 退化 |
| NUM_TILINGS | 2, 4, **8**, 16 | 8 | 更多更好但边际递减 |
| TABLE_SIZE | 64, 128, **256**, 512 | 256 | 256 足够 |

选 20 个代表性 benchmark 运行。

### 4.4 实验四：状态属性消融

**目标**：量化每个属性的贡献。

| 配置 | 属性 | 状态空间 |
|------|------|---------|
| RL-1attr | S4 (row hit count) | 8 |
| RL-2attr | S4 + S5 (row hit + same-row pending) | 64 |
| RL-3attr | S4 + S5 + S3 (+ bank queue depth) | 512 |
| RL-4attr | S4 + S5 + S3 + S1 (+ read queue) | 8,192 |
| RL-full | S1 + S2 + S3 + S4 + S5 | 131,072 |

**预期**：S4 (row hit count) 和 S5 (same-row pending) 贡献最大——它们直接反映了"保持打开是否有后续 hit"。

### 4.5 实验五：RL vs DYMPL 详细对比

**目标**：RL（SARSA+CMAC）与 DYMPL（感知机）的学习机制对比。两者做的是同一件事（cluster-end 时预测 open/close），但学习方法不同。

| 维度 | DYMPL（感知机） | RL_PAGE（SARSA+CMAC） |
|------|--------------|---------------------|
| 学习目标 | 即时 correct/incorrect | 折扣累积奖励 |
| 函数近似 | 线性加权 | CMAC 多表（非线性） |
| 训练时机 | 仅 ACT 时（不对称） | ACT 和 cluster-end 时（对称） |
| 探索 | 无 | ε-greedy |
| 状态特征 | page_hitcnt, page_utilization | 5 个属性 |

**关键问题**：
- RL 的折扣累积奖励在关页场景中是否真的优于即时 correct/incorrect 反馈？
- CMAC 的非线性泛化是否比感知机线性分割更好？
- ε-greedy 探索在 phase 变化 benchmark 上是否带来可测量的优势？

### 4.6 实验六：多核验证（可选）

使用 4 核配置 `champsim_config_4c.json`。多核下 bank 竞争加剧，RL 能否学到更好的关页时机。

论文 Section 5.2 报告多通道下独立 agent 可自行收敛。

---

## 5. 构建与运行

```bash
# 1. 修改 CMakeLists.txt 添加 rl_page_agent.cc
cd /root/data/smartPRE/dramsim3
# 在 CMakeLists.txt 的 src 列表中添加 src/rl_page_agent.cc
mkdir -p build && cd build && cmake .. && make -j8

# 2. 准备配置
cp /root/data/smartPRE/champsim-la/dramsim3_configs/DDR5_64GB_4ch_4800.ini \
   /root/data/smartPRE/champsim-la/dramsim3_configs/DDR5_64GB_4ch_4800_RLPAGE.ini
# 修改: row_buf_policy = RL_PAGE

cd /root/data/smartPRE/champsim-la
cp champsim_config.json champsim_config_RLPAGE.json
# 修改 dram_io_config 指向 RLPAGE 配置

# 3. 构建 ChampSim
python3 config.sh champsim_config_RLPAGE.json
make -j8

# 4. 运行仿真
export LD_LIBRARY_PATH=/root/data/smartPRE/dramsim3:$LD_LIBRARY_PATH
TRACE_ROOT=/root/data/Trace/LA scripts/run_selected_slices.sh
# 输入 label: RLPAGE_1c

# 5. 对比结果
python3 scripts/compare_ipc.py results/GS_1c results/RLPAGE_1c
python3 scripts/compare_ipc.py results/DYMPL_1c results/RLPAGE_1c
```

---

## 6. 验证步骤

1. **编译**：DRAMSim3 + ChampSim 编译通过
2. **功能验证**（warmup=1M, sim=5M）：
   - `rlpage_decisions > 0`
   - `rlpage_close_count` 和 `rlpage_keepopen_count` 均非零
   - 探索率 ≈ 5%
   - 无 crash / assert / deadlock
3. **学习验证**：
   - 在同一 benchmark 上连续两次运行，第二次的前期性能应更好（如果做了 warmup 阶段的 weight 保存；否则每次从零开始学）
4. **正确性不变量**：
   - `rlpage_close_count + rlpage_keepopen_count == rlpage_decisions`
   - auto-precharge 次数 == `rlpage_close_count`

---

## 7. 硬件开销

| 组件 | 大小 |
|------|------|
| CMAC 表 (8 tiling × 256 entry × 16-bit) | 4 KB |
| Per-bank 上下文 (RLPageBankCtx × 128 banks) | ~2 KB |
| 哈希逻辑 + 比较器 | 组合电路 |
| **总计** | **~6 KB / channel** |

对比：
- GS Shadow Simulation：128 banks × 7 × 8B ≈ 7 KB
- DYMPL 感知机：权重表 + PRT ≈ 数百 bytes
- Ipek 论文原始设计：32 KB

---

## 8. 风险与缓解

| 风险 | 缓解 |
|------|------|
| Warmup 阶段 RL 未收敛，性能差 | 乐观初始化 Q=1/(1-γ)（论文做法）；仿真 warmup 足够长 |
| 二元决策（open/close）信息量不如 GS 的连续 timeout | RL_PAGE 学到的是"是否关"，不控制"何时关"；可扩展为 K 档延迟 |
| KEEP_OPEN 后缺乏主动关页机制 | 需要配合 `ArbitratePrecharge()` 的 on-demand precharge 兜底 |
| 与 GS 差距不明显 | 分析 per-benchmark 结果，关注 GS 弱势的 benchmark |
| CMAC 冲突影响学习 | 增大 TABLE_SIZE 或 NUM_TILINGS |
