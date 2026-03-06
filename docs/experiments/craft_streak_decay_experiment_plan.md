# CRAFT Reopen Streak Decay on Right Precharge 实验方案

## 1. 问题分析

### 1.1 现有缺陷

`reopen_streak` 是 CRAFT 的指数回退控制器，决定 wrong precharge 时的升阶幅度（`BASE_STEP << min(reopen_streak, SHIFT_CAP)`）。当前逻辑：

| 事件 | reopen_streak 操作 |
|------|-------------------|
| Wrong precharge（同行重开） | `min(reopen_streak + 1, 7)` — 递增 |
| Conflict（异行到达） | `0` — 清零 |
| Right precharge（异行重开） | **不变** — 冻结 |

问题在于 **right precharge 不修改 streak**。考虑以下场景：

```
Phase 1: 热行阶段（bank 连续访问同一行）
  wrong precharge x5 → reopen_streak 升到 5
  timeout_value 被推到很高（接近 T_MAX）

Phase 2: 热行结束，进入正常访问阶段
  right precharge x20 → reopen_streak 仍然冻结在 5
  （没有 conflict 发生，因为 timeout 已经很高，
    大多数情况下行会在 timeout 到期前被其他行替换，触发 right precharge）

Phase 3: 偶发的热行短暂回归
  1 次 wrong precharge → 升阶步长 = 50 << 5 = 1600 cycles！
  timeout_value 一步跳升 1600，严重过度保护
```

核心问题：streak 值反映的是**历史峰值**而非**近期热度**。在热行阶段结束后，streak 冻结在高位不衰减，导致后续偶发的 wrong precharge 获得不必要的大幅升阶。

### 1.2 现有数据中的线索

从 CRAFT vs GS 对比数据中，CRAFT 落后较多的 benchmark 按成因可分为两类：

**A. 可能受 streak 冻结影响的 benchmark**（行局部性有波动但非完全相变）：

| Benchmark | CRAFT vs GS | 特征分析 |
|-----------|-------------|---------|
| spec06/cactusADM/ref | -1.98% | 科学计算，网格遍历有局部热行但频繁切换网格区域 |
| spec06/zeusmp/ref | -1.66% | 天体物理，类似的热行间歇模式 |
| spec17/cactuBSSN/ref | -0.93% | 同类科学计算 |
| spec06/GemsFDTD/ref | -0.80% | FDTD 电磁模拟，结构化网格访问 |
| spec17/bwaves/bw1 | -0.67% | 流体力学，网格扫描有方向切换 |
| ligra/CF/higgs | -0.66% | 协同过滤，散射/聚集模式交替 |

这些 benchmark 的共同特征是：**行局部性在中高水平波动**，但不会出现剧烈的相变（连续 conflict）。在热行阶段 streak 被推高后，进入一段 right precharge 为主的正常阶段，streak 冻结；之后偶发的 wrong precharge 获得过大的升阶步长，导致 timeout 被不必要地推高。

**B. 相变类 benchmark**（这些更适合 Phase Reset 机制处理）：

| Benchmark | CRAFT vs GS | 特征 |
|-----------|-------------|------|
| crono/PageRank/higgs | -2.07% | 明确的 push/pull 相变 |
| crono/PageRank/soc-pokec | -1.87% | 同上 |
| ligra/PageRank/soc-pokec | -2.10% | 同上 |
| hashjoin/hj-2-NPO_st | -1.60% | build/probe 两阶段 |

Streak Decay 和 Phase Reset 解决不同的问题：Phase Reset 解决剧烈相变时的收敛速度问题，Streak Decay 解决 streak 冻结导致的升阶过度问题。两者互补。

### 1.3 Streak Decay 机制

```
On right precharge:
  reopen_streak = max(reopen_streak - 1, 0)  // 温和衰减
```

每次 right precharge 把 streak 减 1，让 streak 反映**近期**的热行强度而非历史峰值。

**完整修改后的 streak 行为**：

| 事件 | reopen_streak 操作 | 说明 |
|------|-------------------|------|
| Wrong precharge | `min(reopen_streak + 1, 7)` | 不变：递增保护热行 |
| Conflict | `0` | 不变：清零去保护 |
| Right precharge | **`max(reopen_streak - 1, 0)`** | **新增**：温和衰减 |

**设计要点**：

1. **衰减速率为 1/次**：这是最温和的衰减。从 streak=5 衰减到 0 需要 5 次 right precharge，给热行充分的"尾部保护"。如果热行在衰减过程中回归（又一次 wrong precharge），streak 会立即回升。

2. **Right precharge 是天然的"负反馈信号"**：right precharge 意味着 timeout 预关闭后开了不同的行，说明旧行确实不再被需要。多次 right precharge 意味着热行阶段确实结束了。

3. **不改变 timeout_value**：Streak Decay 只影响 streak 本身，不直接调整 timeout。timeout 的调整仍然完全由 wrong precharge（升阶）和 conflict（降阶）驱动。Streak Decay 的效果是**间接的**——当未来发生 wrong precharge 时，衰减后的较低 streak 值产生较小的升阶步长。

**示例对比**：

```
场景：热行阶段结束后经历 5 次 right precharge，然后 1 次 wrong precharge

改进前：
  streak 冻结在 5 → wrong precharge 步长 = 50 << 5 = 1600

改进后：
  streak: 5 → 4 → 3 → 2 → 1 → 0 → wrong precharge 步长 = 50 << 0 = 50

差异：升阶步长从 1600 降至 50，减少 32 倍
```

### 1.4 硬件代价

**零额外存储**。仅在 right precharge 分支增加一个减 1 操作和 max(x, 0) 饱和逻辑。硬件实现为 1 个 3-bit 减法器 + 1 个下界饱和比较器，面积可忽略。

---

## 2. 代码修改

### 2.1 `dramsim3/src/command_queue.h`

**无修改**。不需要新增常量或字段，`CraftBankState` 结构体保持不变。

### 2.2 `dramsim3/src/command_queue.cc` — CRAFT_ProcessACT()

仅修改 right precharge 分支（line 1038-1040），增加 streak 衰减：

```cpp
void CommandQueue::CRAFT_ProcessACT(int queue_idx, int new_row) {
    auto& state = craft_state_[queue_idx];

    if (state.prev_closed_by_timeout) {
        if (new_row == state.prev_row) {
            // Wrong precharge: escalate with exponential backoff (unchanged)
            int shift = std::min(state.reopen_streak, CRAFT_SHIFT_CAP);
            int step = CRAFT_BASE_STEP << shift;
            state.timeout_value = std::min(state.timeout_value + step, CRAFT_T_MAX);
            state.reopen_streak = std::min(state.reopen_streak + 1, CRAFT_REOPEN_STREAK_MAX);
            simple_stats_.Increment("craft_timeout_wrong");
            simple_stats_.Increment("craft_escalations");
        } else {
            // Right precharge: timeout was appropriate
            // NEW: decay streak to reflect fading hot-row intensity
            state.reopen_streak = std::max(state.reopen_streak - 1, 0);
            simple_stats_.Increment("craft_timeout_correct");
        }
        state.prev_closed_by_timeout = false;
    }

    // Record reopen streak distribution
    simple_stats_.IncrementVec("craft_reopen_streak_dist", state.reopen_streak);
}
```

### 2.3 修改总结

| 文件 | 修改量 | 性质 |
|------|--------|------|
| `dramsim3/src/command_queue.cc` | +1 行 | right precharge 分支增加 `state.reopen_streak = std::max(state.reopen_streak - 1, 0);` |

总计 **1 行** 新增代码。零额外存储，零新增统计计数器（已有的 `craft_reopen_streak_dist` 足以观察衰减效果）。

---

## 3. 实验设计

### 3.1 实验矩阵

| 实验编号 | 配置名 | 说明 | 目的 |
|----------|--------|------|------|
| E0 | `CRAFT_1c` | 基础 CRAFT（已有结果） | Baseline（无需重跑） |
| E1 | `CRAFT_SD_1c` | Streak Decay（衰减 -1） | 主实验 |

**说明**：
- E0 已有完整结果，直接复用
- E1 是主实验，衰减步长为 -1（最温和的线性衰减）
- 由于机制极其简单（仅 1 行代码），不设计衰减步长敏感性实验。-1 是最自然的选择：更大的衰减步长（如 -2）会过快削弱保护，失去指数回退的意义

### 3.2 配置文件

无需新建配置文件。Streak Decay 是对 CRAFT 内部逻辑的修改，`row_buf_policy = CRAFT` 不变。代码修改后重编译即可。

### 3.3 Benchmark 套件

使用 `benchmarks_selected.tsv` 中的完整 62 个 benchmark，通过 `scripts/run_selected_slices.sh` 运行。

---

## 4. 评估指标

### 4.1 性能指标

- **IPC**：每个 benchmark 的 IPC 改善（per-benchmark 报告，相对 CRAFT baseline）
- **GEOMEAN speedup**：CRAFT_SD vs CRAFT baseline、CRAFT_SD vs GS、CRAFT_SD vs open_page

### 4.2 行为指标

通过 `ddr.json` 输出的已有统计计数器分析：

| 指标 | 来源 | 说明 |
|------|------|------|
| `craft_reopen_streak_dist` | 已有 | **核心观察指标**：衰减后 streak 分布应向低值偏移 |
| `craft_escalations` | 已有 | 升阶总次数（应大致不变或略减） |
| `craft_timeout_wrong` | 已有 | Wrong precharge 总次数（应不变，衰减不影响 wrong precharge 的发生） |
| `craft_timeout_correct` | 已有 | Right precharge 总次数（应不变） |
| `craft_timeout_value_sum` | 已有 | 平均 timeout 值（通过 sum / precharges 计算，预期下降） |
| `num_act_cmds` | 已有 | ACT 总次数（wrong precharge 减少间接导致 ACT 减少） |

### 4.3 重点关注的 benchmark

**第一梯队（预期显著改善）**：行局部性波动型，streak 容易冻结在高位

| Benchmark | CRAFT vs GS | 预期改善原因 |
|-----------|-------------|------------|
| spec06/cactusADM/ref | -1.98% | 网格遍历产生间歇热行，right precharge 频繁但 streak 冻结 |
| spec06/zeusmp/ref | -1.66% | 同类科学计算 |
| spec17/cactuBSSN/ref | -0.93% | 同类 |
| spec06/GemsFDTD/ref | -0.80% | 结构化网格，局部性有规律波动 |
| ligra/CF/soc-pokec | -1.28% | 散射/聚集模式交替 |
| ligra/CF/higgs | -0.66% | 同上 |

**第二梯队（预期中等改善）**：

| Benchmark | CRAFT vs GS | 说明 |
|-----------|-------------|------|
| ligra/Components/soc-pokec | -0.91% | 图遍历有局部热行切换 |
| ligra/Radii/soc-pokec | -0.91% | 同类 |
| spec17/fotonik3d/ref | -0.56% | 光子模拟，结构化网格 |
| spec17/bwaves/bw1 | -0.67% | 流体力学，网格方向切换 |

**第三梯队（预期无影响或微小变化）**：

- **相变类 benchmark**（PageRank, hashjoin）：streak 在 conflict 时被清零（不走 right precharge 路径），Streak Decay 不影响
- **稳态高局部性**（lbm, milc）：streak 持续被 wrong precharge 推高，right precharge 很少，衰减机会少
- **稳态低局部性**（hpcc/RandAcc）：streak 持续被 conflict 清零，本身就是 0，无衰减空间
- **低内存强度**（sphinx3, gcc, wrf）：事件稀少，streak 多为 0

### 4.4 验证 Streak Decay 效果的分析方法

1. **Streak 分布偏移**：对比 CRAFT_SD vs CRAFT 的 `craft_reopen_streak_dist`
   - 预期：streak 高值（5, 6, 7）的占比下降，低值（0, 1, 2）的占比上升
   - 如果分布几乎不变 → Streak Decay 的衰减机会少（right precharge 事件少），机制触发不足

2. **平均 timeout 变化**：对比 `craft_timeout_value_sum / craft_timeout_precharges`
   - 预期：平均 timeout 小幅下降（升阶步长减小的间接效果）
   - 如果平均 timeout 大幅下降 → 可能衰减过快，热行保护不足（检查 wrong precharge 是否增多）

3. **Wrong precharge 变化**：对比 `craft_timeout_wrong`
   - 预期：不变或略微增加（衰减使 timeout 整体偏低，可能导致少量额外的 wrong precharge）
   - 如果大幅增加 → 衰减过度削弱了热行保护，需要重新评估

4. **IPC 与 streak 偏移的相关性**：
   - 正相关（streak 向低值偏移越多，IPC 提升越大）→ Streak 冻结确实是性能瓶颈
   - 无相关 → 问题出在其他方面

---

## 5. 构建与运行

### 5.1 实现并构建

```bash
# 1. 修改代码（仅 command_queue.cc 的 CRAFT_ProcessACT 函数，right precharge 分支增加 1 行）

# 2. 构建 DRAMSim3
cd /root/data/smartPRE/dramsim3 && mkdir -p build && cd build && cmake .. && make -j8

# 3. 构建 ChampSim-LA
cd /root/data/smartPRE/champsim-la
python3 config.sh champsim_config.json
make -j8

# 4. Smoke test
export LD_LIBRARY_PATH=/root/data/smartPRE/dramsim3:$LD_LIBRARY_PATH
# 使用一个已知的短 trace 运行 1M 指令，确认不 crash
# 检查 ddr.json 中 craft_reopen_streak_dist 分布是否合理（应有 0 值出现）
```

### 5.2 运行完整实验

```bash
export LD_LIBRARY_PATH=/root/data/smartPRE/dramsim3:$LD_LIBRARY_PATH

# E1: Streak Decay
cd /root/data/smartPRE/champsim-la
TRACE_ROOT=/root/data/Trace/LA MANIFEST_SRC=./scripts/selected_slices.tsv scripts/run_selected_slices.sh
# 输入 label: CRAFT_SD_1c
```

### 5.3 结果分析

```bash
cd /root/data/smartPRE/champsim-la

# 与基础 CRAFT 对比
python3 scripts/compare_ipc.py --a results/CRAFT_1c --b results/CRAFT_SD_1c \
  --a-label CRAFT --b-label CRAFT_SD --out results/compare_CRAFT_vs_CRAFT_SD_1c.tsv

# 与 GS 对比
python3 scripts/compare_ipc.py --a results/GS_1c --b results/CRAFT_SD_1c \
  --a-label GS --b-label CRAFT_SD --out results/compare_GS_vs_CRAFT_SD_1c.tsv

# 与 open_page 对比
python3 scripts/compare_ipc.py --a results/open_page_1c --b results/CRAFT_SD_1c \
  --a-label OPEN_PAGE --b-label CRAFT_SD --out results/compare_open_page_vs_CRAFT_SD_1c.tsv

# 行为分析（streak 分布对比）
python3 scripts/craft_behavior_table.py \
  --craft-dir results/CRAFT_SD_1c \
  --baseline-dir results/CRAFT_1c \
  --manifest benchmarks_selected.tsv \
  --out results/craft_sd_behavior.tsv
```

---

## 6. 预期结果与论文故事

### 6.1 预期性能变化

| 对比 | 预期 GEOMEAN | 说明 |
|------|-------------|------|
| CRAFT_SD vs CRAFT | +0.05% ~ +0.3% | 仅在局部性波动型 benchmark 上改善 |
| CRAFT_SD vs GS | -0.1% ~ +0.1% | 缩小与 GS 的差距（原 -0.28%） |
| CRAFT_SD vs open_page | +1.2% ~ +1.6% | 保持对 open_page 的优势 |

**关键预期**（per-benchmark）：

| Benchmark | 原 CRAFT vs GS | 预期改善 | 改善后 vs GS |
|-----------|---------------|---------|-------------|
| spec06/cactusADM/ref | -1.98% | +0.3% ~ +0.8% | -1.7% ~ -1.2% |
| spec06/zeusmp/ref | -1.66% | +0.2% ~ +0.6% | -1.5% ~ -1.0% |
| spec17/cactuBSSN/ref | -0.93% | +0.1% ~ +0.4% | -0.8% ~ -0.5% |
| spec06/GemsFDTD/ref | -0.80% | +0.1% ~ +0.3% | -0.7% ~ -0.5% |
| ligra/CF/soc-pokec | -1.28% | +0.1% ~ +0.4% | -1.2% ~ -0.9% |

稳态 benchmark 不应退化：当 streak 已为 0 时衰减无效（max(0-1, 0) = 0），当 streak 持续被 wrong precharge 推高时衰减被抵消。

### 6.2 论文故事

CRAFT 的指数回退机制通过 `reopen_streak` 控制升阶幅度，对连续的 wrong precharge 提供 O(2^n) 级别的快速保护。然而原设计中 streak 仅在 conflict 时清零，在 right precharge（正确预关闭）时不做任何调整。这导致在热行阶段结束后，streak 值冻结在历史高位，使后续偶发的 wrong precharge 获得 32 倍（streak=5 vs streak=0）的过度升阶。

Streak Decay 通过在 right precharge 时将 streak 减 1 来解决此问题。Right precharge 是天然的"热行退出"信号——每次正确预关闭都表明被关闭的行确实不再被需要。连续的 right precharge 逐步将 streak 从历史峰值衰减到 0，使 streak 真正反映近期热行强度。

这一改进仅需 1 行代码，零额外存储（仅修改已有 3-bit 计数器的更新逻辑），但修正了指数回退机制的一个结构性偏差：将 streak 从"历史峰值记录器"转变为"近期热度指示器"。

---

## 7. 风险与应对

### 7.1 热行保护不足

**风险**：衰减过快导致热行保护减弱。如果一个 bank 在热行阶段中偶尔穿插 right precharge（例如热行 → 一次冷行访问 → 热行回归），streak 被减 1 后升阶步长降低。

**应对**：
- 衰减步长仅为 -1，非常温和。穿插 1 次 right precharge 仅将 streak 从 5 降到 4，升阶步长从 1600 降到 800，仍然提供强保护
- wrong precharge 的 +1 递增与 right precharge 的 -1 衰减形成平衡：如果热行仍然活跃（wrong precharge 多于 right precharge），streak 净增长
- 通过对比 `craft_timeout_wrong` 计数器验证：如果大幅增加（>5%），说明保护不足

### 7.2 改善不显著

**风险**：目标 benchmark 中 right precharge 在 streak 高位时很少发生，衰减机会少。

**应对**：
- 检查 `craft_reopen_streak_dist` 分布：如果改进前后几乎无变化，确认衰减未触发
- 如果衰减确实不触发，说明问题不在 streak 冻结，而在其他方面（如 Phase Reset 需求）
- 此时仍可保留 Streak Decay 机制——零成本且逻辑正确，不会造成退化

### 7.3 与 Phase Reset 的交互

**风险**：如果同时启用 Streak Decay 和 Phase Reset，两者对 streak 的操作可能冲突。

**应对**：
- 实际上两者完全互补：Phase Reset 在连续 conflict 时清零 streak，Streak Decay 在连续 right precharge 时衰减 streak。两个路径不会同时触发（conflict 和 right precharge 是互斥事件）
- 组合实验（CRAFT + Phase Reset + Streak Decay）可作为后续实验

---

## 8. 后续实验（可选扩展）

### 8.1 与 Phase Reset 组合

如果 Streak Decay 和 Phase Reset 单独都有效果，测试两者组合：

```
配置：CRAFT_SD_PR4_1c
Label: CRAFT + Streak Decay + Phase Reset (threshold=4)
```

预期：两者解决不同问题（streak 冻结 vs 相变收敛），组合效果应接近两者独立改善之和。

### 8.2 与 RW Cost 区分组合

三机制全部开启的完整 CRAFT 增强版本：

```
配置：CRAFT_FULL_1c
Label: CRAFT + Streak Decay + Phase Reset + RW Cost
```

### 8.3 多核场景

多核场景下 bank 级访问模式更复杂，热行阶段可能更频繁地被其他核的请求打断，产生更多的 right precharge。Streak Decay 在多核下可能有更大收益。

```
配置：champsim_config_4c.json, CRAFT_SD
Label: CRAFT_SD_4c
```
