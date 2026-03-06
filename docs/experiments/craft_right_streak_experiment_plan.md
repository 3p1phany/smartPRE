# CRAFT Right Precharge Streak Gentle De-escalation 实验方案

## 1. 问题分析

### 1.1 现有缺陷

CRAFT 的三种反馈信号中，right precharge 是唯一不触发 timeout 调整的信号。设计理由是 "timeout 正确不意味着 timeout 过大"。这在概率上是对的，但存在信息浪费：

```
三种反馈信号的当前行为：
  Wrong precharge  → 升阶（指数退避）  ↑ timeout
  Conflict         → 降阶（固定步长）  ↓ timeout
  Right precharge  → 无操作            — timeout（信息浪费）
```

当某个 bank 连续观察到 N 次 right precharge 而零次 wrong precharge 时，说明 timeout 大概率偏保守——关了行之后总是打开不同行，几乎不需要这么长的保护窗口。当前设计下这些 bank 的 timeout 会 "粘滞" 在高值，无法自然回落。

**粘滞效应的量化**：从现有 `CRAFT_1c` 数据中，以下 benchmark 的 `timeout_high_pct`（timeout 处于 3200 附近的占比）显著偏高：

| Benchmark | timeout_high_pct | CRAFT vs GS | 特征 |
|-----------|-----------------|-------------|------|
| ligra/BFS-Bitvector/soc-pokec | 72.8% | +0.17% | 局部性极强阶段推高 timeout 后无回落通道 |
| ligra/Triangle/roadNet-CA | 63.1% | -0.16% | 同上 |
| npb/CG | 62.7% | -0.11% | 科学计算，稳态高局部性 |
| ligra/PageRank/roadNet-CA | 62.0% | -0.11% | 阶段性高局部性 |
| crono/Triangle-Counting/roadNet-CA | 53.2% | +0.84% | 高局部性但 timeout 偏高 |
| spec06/sphinx3/ref | 51.1% | +0.30% | 稳态高局部性 |
| ligra/PageRankDelta/roadNet-CA | 49.4% | +0.11% | 中等局部性 |
| ligra/BFSCC/soc-pokec-short | 46.2% | +0.19% | 阶段性局部性 |
| ligra/BC/Amazon0312 | 45.8% | +0.16% | 高局部性 |

这些 benchmark 中 timeout 长期处于高值区间，意味着 DRAM bank 在 timeout 倒计时期间空闲等待，期间可能错过为其他请求服务的机会。右预关闭正确 streak 机制可提供一条温和的回落通道。

### 1.2 Right Precharge Streak 机制

添加 per-bank `right_streak` 计数器（3-bit 饱和计数器），追踪连续 right precharge 次数：

```
right_streak: 3-bit saturating counter per bank [0-7]
RIGHT_THRESHOLD = 4  (连续 4 次 right precharge 触发一次 gentle de-escalation)
GENTLE_STEP = CONFLICT_STEP / 2  (比 conflict 更温和的降阶步长)

On right precharge:
  right_streak++
  if right_streak >= RIGHT_THRESHOLD:
    timeout_value = max(timeout_value - GENTLE_STEP, T_MIN)
    right_streak = 0

On wrong precharge:
  right_streak = 0  // wrong precharge 说明 timeout 不够大，停止降压

On conflict:
  right_streak = 0  // conflict 已有自己的降阶逻辑
```

**设计要点**：
- **GENTLE_STEP = CONFLICT_STEP / 2**：比 conflict de-escalation 更温和。conflict 是明确的"timeout 太长"信号（有请求被阻塞），而 right precharge streak 仅是"timeout 可能偏保守"的弱信号，步长应更小
- **RIGHT_THRESHOLD = 4**：要求连续 4 次 right precharge 才触发，避免偶发正确预关闭导致 timeout 不必要下降
- 任何 wrong precharge 或 conflict 都清零 streak：这两个信号都表明 timeout 不应继续下降

**最坏情况降阶速率对比**：

| 降阶通道 | 步长 | 触发间隔 | 等效速率 |
|----------|------|---------|---------|
| Conflict de-escalation | CONFLICT_STEP (~25 cycles) | 每次 conflict | 25 cycles/event |
| Right streak de-escalation | GENTLE_STEP (~12 cycles) | 每 4 次 right precharge | ~3 cycles/event |

Right streak 的等效降阶速率远低于 conflict，确保不会过度激进地拉低 timeout。

---

## 2. 代码修改

### 2.1 `dramsim3/src/command_queue.h`

#### 2.1a 新增常量（line 95 之后，CRAFT_SHIFT_CAP 之后）

```cpp
static constexpr int CRAFT_RIGHT_THRESHOLD = 4;  // consecutive right precharges to trigger gentle de-escalation
```

#### 2.1b CraftBankState 新增字段（line 97-102）

```cpp
struct CraftBankState {
    int timeout_value = CRAFT_INIT_TIMEOUT;
    int reopen_streak = 0;
    int prev_row = -1;
    bool prev_closed_by_timeout = false;
    int right_streak = 0;              // consecutive right-precharge counter [0-7]
};
```

### 2.2 `dramsim3/src/command_queue.cc` — 初始化

在 `CommandQueue` 构造函数中（line 128 附近），计算 gentle step：

```cpp
craft_conflict_step_ = CRAFT_BASE_STEP * config_.tRP / (config_.tRP + config_.tRCD);
craft_gentle_step_ = craft_conflict_step_ / 2;  // half of conflict step
```

相应在 `command_queue.h` 的 `CommandQueue` 类中声明成员变量：

```cpp
int craft_gentle_step_ = 0;
```

### 2.3 `dramsim3/src/command_queue.cc` — CRAFT_ProcessACT()

修改 right precharge 分支以实现 streak 追踪和 gentle de-escalation；在 wrong precharge 分支中清零 right_streak：

```cpp
void CommandQueue::CRAFT_ProcessACT(int queue_idx, int new_row) {
    auto& state = craft_state_[queue_idx];

    if (state.prev_closed_by_timeout) {
        if (new_row == state.prev_row) {
            // Wrong precharge: timeout was too short, escalate with exponential backoff
            int shift = std::min(state.reopen_streak, CRAFT_SHIFT_CAP);
            int step = CRAFT_BASE_STEP << shift;
            state.timeout_value = std::min(state.timeout_value + step, CRAFT_T_MAX);
            state.reopen_streak = std::min(state.reopen_streak + 1, CRAFT_REOPEN_STREAK_MAX);
            state.right_streak = 0;  // wrong precharge: stop gentle de-escalation
            simple_stats_.Increment("craft_timeout_wrong");
            simple_stats_.Increment("craft_escalations");
        } else {
            // Right precharge: timeout was appropriate
            state.right_streak = std::min(state.right_streak + 1, CRAFT_REOPEN_STREAK_MAX);
            if (state.right_streak >= CRAFT_RIGHT_THRESHOLD) {
                // Gentle de-escalation: timeout is likely too conservative
                state.timeout_value = std::max(state.timeout_value - craft_gentle_step_, CRAFT_T_MIN);
                state.right_streak = 0;
                simple_stats_.Increment("craft_gentle_deescalations");
            }
            simple_stats_.Increment("craft_timeout_correct");
        }
        state.prev_closed_by_timeout = false;
    }

    // Record reopen streak distribution
    simple_stats_.IncrementVec("craft_reopen_streak_dist", state.reopen_streak);
}
```

### 2.4 `dramsim3/src/command_queue.cc` — AddCommand() CRAFT 分支

在 conflict 处理中清零 right_streak（line 469-474 附近）：

```cpp
if(cmd.Row() != issued_cmd[index].Row()){
    // Conflict: timeout too long, de-escalate with cost-aware fixed step
    auto& state = craft_state_[index];
    state.timeout_value = std::max(state.timeout_value - craft_conflict_step_, CRAFT_T_MIN);
    state.reopen_streak = 0;
    state.right_streak = 0;  // conflict: stop gentle de-escalation
    timeout_counter[index] = 0;  // trigger immediate precharge
    simple_stats_.Increment("craft_conflicts");
    simple_stats_.Increment("craft_deescalations");
}
```

### 2.5 `dramsim3/src/simple_stats.cc`

在现有 CRAFT 统计计数器之后添加：

```cpp
InitStat("craft_gentle_deescalations", "counter",
         "CRAFT gentle de-escalations triggered by right-precharge streak");
```

### 2.6 修改文件总结

| 文件 | 修改量 | 性质 |
|------|--------|------|
| `dramsim3/src/command_queue.h` | +3 行 | 新增 RIGHT_THRESHOLD 常量 + right_streak 字段 + gentle_step 成员 |
| `dramsim3/src/command_queue.cc` | ~12 行 | ProcessACT right 分支增加 streak 判断；AddCommand conflict 分支增加 streak 清零；构造函数计算 gentle_step |
| `dramsim3/src/simple_stats.cc` | +2 行 | 注册 craft_gentle_deescalations 计数器 |

总计约 **17 行** 新增/修改代码。硬件开销增加 3 bits/bank，32 bank = 12 bytes/channel，总开销从 180B 增至 192B。

---

## 3. 实验设计

### 3.1 实验矩阵

| 实验编号 | 配置名 | GENTLE_STEP | RIGHT_THRESHOLD | 说明 |
|----------|--------|-------------|-----------------|------|
| E0 | `CRAFT_1c` | — | — | 已有基础 CRAFT 结果（Baseline，无需重跑） |
| E1 | `CRAFT_RS4_1c` | CONFLICT_STEP/2 | 4 | 主实验 |
| E2 | `CRAFT_RS3_1c` | CONFLICT_STEP/2 | 3 | 敏感性：更激进的阈值 |
| E3 | `CRAFT_RS6_1c` | CONFLICT_STEP/2 | 6 | 敏感性：更保守的阈值 |
| E4 | `CRAFT_RS4F_1c` | CONFLICT_STEP | 4 | 敏感性：更大的 gentle step（= conflict step） |

**说明**：
- E0 已有完整结果，直接复用
- E1 是主实验，RIGHT_THRESHOLD=4、GENTLE_STEP=CONFLICT_STEP/2 是设计值
- E2/E3 验证阈值敏感性（如果 E1 效果显著，再跑 E2/E3）
- E4 验证步长敏感性：GENTLE_STEP = CONFLICT_STEP 是上界，观察是否过于激进

### 3.2 配置文件

无需新增 DRAM `.ini` 配置。Right Precharge Streak 是 CRAFT 的内嵌逻辑改进，使用相同的 `DDR5_64GB_4ch_4800_CRAFT.ini`。

由于 RIGHT_THRESHOLD 和 GENTLE_STEP 是编译时常量，E1-E4 每次需修改常量后重编译。如需频繁实验不同参数组合，可将参数改为运行时可配置（从 `.ini` 读取），但初期直接修改常量重编译即可。

### 3.3 Benchmark 套件

使用 `benchmarks_selected.tsv` 中的完整 benchmark 集合，通过 `scripts/run_selected_slices.sh` 运行。

---

## 4. 评估指标

### 4.1 性能指标

- **IPC**：每个 benchmark 的 IPC 改善（per-benchmark 报告）
- **GEOMEAN speedup**：CRAFT_RS4 vs CRAFT baseline、CRAFT_RS4 vs GS、CRAFT_RS4 vs open_page

### 4.2 行为指标

通过 `ddr.json` 输出的统计计数器分析：

| 指标 | 来源 | 说明 |
|------|------|------|
| `craft_gentle_deescalations` | 新增计数器 | Gentle de-escalation 触发总次数 |
| `craft_timeout_correct` | 已有 | Right precharge 总次数 |
| `craft_conflicts` | 已有 | Conflict 总次数 |
| `craft_escalations` | 已有 | Escalation 总次数 |
| `craft_deescalations` | 已有 | Conflict de-escalation 总次数 |
| `craft_timeout_value_sum` | 已有 | 平均 timeout 值（通过 sum / precharges 计算） |
| `gentle_ratio` | 衍生 | `craft_gentle_deescalations / craft_timeout_correct`，反映 right streak 触发率 |

### 4.3 重点关注的 benchmark

根据 1.1 节分析，按 timeout 粘滞程度分组：

**第一梯队（timeout_high_pct > 50%，最可能获益）**：

| Benchmark | timeout_high_pct | 预期 |
|-----------|-----------------|------|
| ligra/BFS-Bitvector/soc-pokec | 72.8% | timeout 高值区大量 right precharge，gentle de-escalation 持续施压回落 |
| ligra/Triangle/roadNet-CA | 63.1% | 同上 |
| npb/CG | 62.7% | 同上 |
| ligra/PageRank/roadNet-CA | 62.0% | 同上 |
| crono/Triangle-Counting/roadNet-CA | 53.2% | 同上 |
| spec06/sphinx3/ref | 51.1% | 同上 |

**第二梯队（timeout_high_pct 30%-50%，中等获益可能）**：
- ligra/PageRankDelta/roadNet-CA (49.4%)
- ligra/BFSCC/soc-pokec-short (46.2%)
- ligra/BC/Amazon0312 (45.8%)
- ligra/Radii/higgs (41.4%)
- crono/PageRank/roadNet-CA (41.1%)
- ligra/CF/roadNet-CA (41.5%)
- spec17/gcc/ref32-O5 (41.2%)
- spec06/leslie3d/ref (38.4%)
- spec06/wrf/ref (36.1%)

**第三梯队（timeout_high_pct < 10%，预期无影响）**：
- crono/PageRank/higgs (0.0%)
- crono/PageRank/soc-pokec (0.0%)
- ligra/PageRank/soc-pokec (0.0%)
- hashjoin/hj-2-NPO_st (0.0%)
- hpcc/RandAcc (0.0%)

这些 benchmark 的 timeout 已处于低值区间，right precharge streak 几乎不会触发（streak 被 wrong precharge 频繁打断），因此 gentle de-escalation 不会生效，行为与 baseline 一致。

### 4.4 验证 Right Streak 是否帮助的分析方法

对每个 benchmark，计算：

1. **Gentle de-escalation 密度**：`craft_gentle_deescalations / craft_timeout_correct`
   - 高密度 → 该 benchmark 有持续的 right precharge streak，timeout 正在被温和拉低
   - 零密度 → 无连续 right precharge，gentle de-escalation 未触发

2. **timeout 分布迁移**：对比 E0 和 E1 的 `craft_timeout_value_sum` 直方图
   - 如果 E1 的高值 bin 占比下降、中值 bin 占比上升，说明 gentle de-escalation 成功将 timeout 从保守区间拉回
   - 关注 `timeout_high_pct` 的变化量

3. **Performance delta 与 timeout_high_pct 的相关性**：
   - 正相关 → Right streak 对高粘滞 benchmark 有效
   - 无相关 → 可能 timeout 高值对这些 benchmark 本身就是合理的

4. **Case study**：对 IPC 变化最大的 benchmark 做详细分析
   - 如果 `craft_gentle_deescalations > 0` 且 average timeout 下降且 IPC 上升，说明 timeout 确实过保守
   - 如果 `craft_gentle_deescalations > 0` 但 IPC 下降，说明 timeout 高值其实是合理的（row 确实需要保持打开），需要调整 threshold 或 step

---

## 5. 构建与运行

### 5.1 实现并构建

```bash
# 1. 修改代码（按 2.1-2.5 节修改三个文件）

# 2. 构建 DRAMSim3
cd /root/data/smartPRE/dramsim3 && mkdir -p build && cd build && cmake .. && make -j8

# 3. 构建 ChampSim-LA
cd /root/data/smartPRE/champsim-la
python3 config.sh champsim_config.json
make -j8

# 4. Smoke test（短 trace 验证不 crash）
export LD_LIBRARY_PATH=/root/data/smartPRE/dramsim3:$LD_LIBRARY_PATH
# 使用一个已知的短 trace 运行 1M 指令
```

### 5.2 运行完整实验

```bash
export LD_LIBRARY_PATH=/root/data/smartPRE/dramsim3:$LD_LIBRARY_PATH

# E1: RIGHT_THRESHOLD=4, GENTLE_STEP=CONFLICT_STEP/2（默认值）
cd /root/data/smartPRE/champsim-la
TRACE_ROOT=/root/data/Trace/LA scripts/run_selected_slices.sh
# 输入 label: CRAFT_RS4_1c
```

### 5.3 结果分析

```bash
cd /root/data/smartPRE/champsim-la

# 与基础 CRAFT 对比
python3 scripts/compare_ipc.py --a results/CRAFT_1c --b results/CRAFT_RS4_1c \
  --a-label CRAFT --b-label CRAFT_RS4 --out results/compare_CRAFT_vs_CRAFT_RS4_1c.tsv

# 与 GS 对比
python3 scripts/compare_ipc.py --a results/GS_1c --b results/CRAFT_RS4_1c \
  --a-label GS --b-label CRAFT_RS4 --out results/compare_GS_vs_CRAFT_RS4_1c.tsv

# 行为分析
python3 scripts/craft_behavior_table.py \
  --craft-dir results/CRAFT_RS4_1c \
  --baseline-dir results/CRAFT_1c \
  --manifest benchmarks_selected.tsv \
  --out results/craft_rs4_behavior.tsv
```

---

## 6. 预期结果与论文故事

### 6.1 预期性能变化

| 对比 | 预期 GEOMEAN | 说明 |
|------|-------------|------|
| CRAFT_RS4 vs CRAFT | +0.0% ~ +0.3% | 仅在 timeout 粘滞 benchmark 上有改善，其余不变 |
| CRAFT_RS4 vs GS | 缩小差距 0.05-0.15% | 减少高 timeout_high_pct benchmark 的无效等待 |

**关键预期**：
- 第一梯队 benchmark（timeout_high_pct > 50%）的 timeout_high_pct 下降 5-15 个百分点
- 第三梯队 benchmark 行为完全不变（right_streak 被 wrong precharge 频繁打断，gentle de-escalation 不触发）
- 无 benchmark 出现显著退化（gentle step 非常小，即使误触发影响也有限）

### 6.2 论文故事

CRAFT 的基础反馈回路通过 wrong precharge（升阶）和 conflict（降阶）两条通道在连续 timeout 空间中收敛。然而 right precharge——三种反馈信号中出现频率最高的信号——是"沉默反馈"：它确认 timeout 预关闭是正确的（关闭后打开了不同行），但不提供 timeout 是否过大的方向性信号。

这种信息不对称导致 **timeout 粘滞效应**：一旦 timeout 被 wrong precharge 推高，在没有 conflict 的情况下（高局部性 workload 中很常见），timeout 无法自然回落。尽管 bank 持续在 timeout 窗口内空闲等待，且每次等待结束后都正确关闭了行（right precharge），这种"一切正常"的反馈反而阻止了系统优化。

Right precharge streak 机制将这个沉默信号转化为一种弱但持续的降阶压力。3-bit 饱和计数器追踪连续 right precharge 事件：当连续 4 次 right precharge 都未被 wrong precharge 或 conflict 打断时，以 GENTLE_STEP（= CONFLICT_STEP/2，约 12 cycles）温和地降低 timeout。这在 wrong precharge（升阶）和 conflict（降阶）之间建立了第三条渐进的 timeout 回归通道，消除了 timeout 在无负面反馈时的粘滞效应。

**三信号完整反馈框架**：

```
Wrong precharge  → 强升阶（指数退避）     "timeout 太短，立刻大幅拉长"
Conflict         → 中等降阶（固定步长）   "timeout 太长，适度缩短"
Right streak     → 弱降阶（半步长 + 连续条件） "timeout 可能偏保守，缓慢探索更短值"
```

三条通道形成一个完整的双向反馈环：强升阶快速保护热行，中等降阶快速响应冲突，弱降阶缓慢消除保守偏差。最终 timeout 收敛到成本最优均衡点附近的更紧凑区间。

硬件代价极小：3 bits/bank，32 bank 仅增加 12 bytes/channel，总开销从 180B 增至 192B。

---

## 7. 风险与应对

### 7.1 Gentle De-escalation 过度降低 timeout

**风险**：在确实需要高 timeout 的稳态高局部性 workload（如流式访问）中，right precharge streak 持续触发，将 timeout 不断拉低直到频繁触发 wrong precharge。

**应对**：
- GENTLE_STEP 非常小（~12 cycles），从 T_MAX=3200 降到 T_MIN=50 需要 `(3200-50)/12 ≈ 263` 次触发，即 `263 * 4 = 1052` 次连续 right precharge。在此过程中只要出现一次 wrong precharge（escalation step 最小 50 cycles），就会大幅抵消多次 gentle de-escalation 的效果
- 升阶步长（最小 50 cycles）远大于 gentle step（~12 cycles），升降比约 4:1，确保稳态时 timeout 不会被拉低到触发 wrong precharge 的水平
- 通过 E2 (threshold=3) 验证更激进阈值是否导致退化，如果退化则说明 threshold=4 的设计是合理的

### 7.2 改善不显著

**风险**：timeout_high_pct 高的 benchmark 中，高 timeout 实际上是最优值（row 确实有长期局部性，需要保持打开），gentle de-escalation 只是浪费降阶然后被 wrong precharge 拉回。

**应对**：
- 首先检查 `craft_gentle_deescalations` 和 `craft_timeout_wrong` 的比例：
  - 如果 gentle de-escalation 触发后立即跟随 wrong precharge escalation → 说明 timeout 高值是合理的，gentle step 被立即抵消
  - 如果 gentle de-escalation 触发后 right precharge streak 继续 → 说明 timeout 确实偏保守
- 对比 E1 和 E0 的 `craft_timeout_value_sum` 直方图，观察 timeout 分布是否真正迁移

### 7.3 某些 benchmark 退化

**风险**：某些 benchmark 的 IPC 下降。

**应对**：
- 检查退化 benchmark 的 `craft_gentle_deescalations` 值：
  - 如果为 0，退化不来源于此机制（代码 bug 或其他问题）
  - 如果大于 0，对比 `craft_timeout_wrong` 是否相应增加——如果 wrong precharge 增加，说明 gentle de-escalation 将 timeout 拉低到了不合适的水平
- 如果退化显著，考虑：
  - 增加 RIGHT_THRESHOLD（E3, threshold=6）
  - 减小 GENTLE_STEP（改为 CONFLICT_STEP/4）
  - 或引入 wrong precharge 后的 "冷却期"：在 wrong precharge 后的 K 次 right precharge 内不计入 streak

---

## 8. 后续实验（可选扩展）

### 8.1 与 Phase Reset 组合

Right streak gentle de-escalation 与 Phase Reset 是正交的优化：
- Phase Reset 加速从高 timeout 到中性值的跳跃式重置（应对相变）
- Right streak 提供从高 timeout 的渐进回落（应对稳态粘滞）

两者可同时启用：

```
配置名: CRAFT_RS4_PR4_1c
参数: RIGHT_THRESHOLD=4, PHASE_THRESHOLD=4, GENTLE_STEP=CONFLICT_STEP/2
```

### 8.2 与 Read/Write Cost 区分组合

如果 RW cost 区分已实现，可进一步在 gentle de-escalation 中区分 read/write 触发的 right precharge。例如 write-triggered right precharge 的 streak 权重较低（写 wrong precharge 代价本来就小，timeout 保守一点也没关系）。

### 8.3 多核场景

在多核场景下，来自不同核心的请求交织可能使 right precharge streak 更难持续（被其他核心的 conflict 打断），但 timeout 粘滞效应可能更严重（某些 bank 长期被单个核心的局部性支配后失去降阶通道）。

```
配置：champsim_config_4c.json, CRAFT_RS4
Label: CRAFT_RS4_4c
```
