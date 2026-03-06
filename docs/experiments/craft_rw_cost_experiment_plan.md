# CRAFT Read/Write Cost Differentiation 实验方案

## 1. 动机与背景

当前 CRAFT 对 read 和 write 触发的 wrong precharge / conflict 使用相同的步长调整。但在实际 DRAM 系统中，读写的延迟代价存在本质不对称：

| 事件 | Read 代价 | Write 代价 | 原因 |
|------|----------|-----------|------|
| Wrong precharge | 极高（tRP + tRCD + tCL ~ 120 cycles） | 较低 | Read 在 CPU 关键路径上直接导致流水线 stall；Write 由 write buffer 发起，不在关键路径 |
| Conflict | 中等 | 较低 | Read conflict 导致请求排队等待；Write conflict 可通过 write batching 吸收 |

这一不对称性天然存在于 DRAM timing 参数中（tCL vs tCWL），CRAFT 的反馈步长应当反映此区分。

### 论文故事定位

这是对 CRAFT "Cost-Aware" 核心理念的纵深扩展：不仅编码 hit/conflict 的成本不对称（已有），还编码 read/write 的延迟不对称。使 CRAFT 在读写混合负载中更精确地逼近 cost-optimal timeout。

---

## 2. 机制设计

### 2.1 Escalation 读写区分（Wrong Precharge，ACT 时）

```
CRAFT_ProcessACT(queue_idx, new_row):
  if prev_closed_by_timeout && new_row == prev_row:
    step = BASE_STEP << min(reopen_streak, SHIFT_CAP)
    if triggered_by_write:
      step = step >> 1    // 写触发的 wrong precharge：半步长
    // else: 读触发保持完整步长
    timeout_value = min(timeout_value + step, T_MAX)
    reopen_streak = min(reopen_streak + 1, REOPEN_STREAK_MAX)
```

**原理**：Read wrong precharge 直接 stall CPU，代价高，需要激进拉长 timeout 避免再犯。Write wrong precharge 不阻塞 CPU，温和调整即可。

### 2.2 De-escalation 读写区分（Conflict，AddCommand 时）

```
AddCommand(cmd) when timeout_ticking && cmd.Row() != open_row:
  if cmd.IsRead():
    actual_step = CONFLICT_STEP * 2  // 读 conflict 代价高，激进缩短 timeout
  else:
    actual_step = CONFLICT_STEP      // 写 conflict 温和缩短
  timeout_value = max(timeout_value - actual_step, T_MIN)
  reopen_streak = 0
  timeout_counter = 0
```

**原理**：Read conflict 意味着 CPU 正在等待的 read 被阻塞，需要尽快缩短 timeout 以减少未来冲突。Write conflict 可被 write buffer 吸收，温和调整即可。

### 2.3 硬件代价

**零额外存储**。Read/Write 信息已内含在 command 的 `cmd_type` 字段中（`READ`/`WRITE`）。仅需在步长计算中增加一个条件移位（1 个 MUX + 1 个 shifter），无额外寄存器。

---

## 3. 代码修改

### 3.1 `dramsim3/src/command_queue.h`

无结构体变更。`CraftBankState` 不需要新增字段。

### 3.2 `dramsim3/src/command_queue.cc`

#### 3.2a `CRAFT_ProcessACT()` — 增加读写感知

ACT 命令本身不携带 read/write 信息，但触发 ACT 的 pending CAS 命令在 queue 中可查。修改 `CRAFT_ProcessACT` 签名，增加 `bool triggered_by_read` 参数：

```cpp
void CommandQueue::CRAFT_ProcessACT(int queue_idx, int new_row, bool triggered_by_read) {
    auto& state = craft_state_[queue_idx];
    if (state.prev_closed_by_timeout) {
        if (new_row == state.prev_row) {
            int shift = std::min(state.reopen_streak, CRAFT_SHIFT_CAP);
            int step = CRAFT_BASE_STEP << shift;
            if (!triggered_by_read) {
                step >>= 1;  // write-triggered: half step
            }
            state.timeout_value = std::min(state.timeout_value + step, CRAFT_T_MAX);
            state.reopen_streak = std::min(state.reopen_streak + 1, CRAFT_REOPEN_STREAK_MAX);
            simple_stats_.Increment("craft_timeout_wrong");
            simple_stats_.Increment("craft_escalations");
            if (triggered_by_read) {
                simple_stats_.Increment("craft_wrong_read");
            } else {
                simple_stats_.Increment("craft_wrong_write");
            }
        } else {
            simple_stats_.Increment("craft_timeout_correct");
        }
        state.prev_closed_by_timeout = false;
    }
    simple_stats_.IncrementVec("craft_reopen_streak_dist", state.reopen_streak);
}
```

#### 3.2b `GetFirstReadyInQueue()` — 传递 read/write 信息

在 ACT 处理分支中，从 queue 中查找第一个 ReadWrite 命令判定 read/write：

```cpp
// CRAFT: Process ACT for escalation/right-precharge detection
if (top_row_buf_policy_ == RowBufPolicy::CRAFT) {
    // Determine if triggering request is read or write
    bool triggered_by_read = true;  // default to read (conservative)
    for (const auto& qcmd : queue) {
        if (qcmd.IsReadWrite() && qcmd.Row() == cmd.Row()) {
            triggered_by_read = qcmd.IsRead();
            break;
        }
    }
    CRAFT_ProcessACT(queue_idx_, cmd.Row(), triggered_by_read);
}
```

#### 3.2c `AddCommand()` CRAFT 分支 — 读写感知 de-escalation

```cpp
else if(top_row_buf_policy_==RowBufPolicy::CRAFT){
    int index=GetQueueIndex(cmd.Rank(),cmd.Bankgroup(),cmd.Bank());
    if(timeout_ticking[index] && timeout_counter[index] > 0){
        if(cmd.Row() != issued_cmd[index].Row()){
            auto& state = craft_state_[index];
            int step = craft_conflict_step_;
            if (cmd.IsRead()) {
                step = craft_conflict_step_ * 2;  // read conflict: aggressive
                simple_stats_.Increment("craft_conflict_read");
            } else {
                simple_stats_.Increment("craft_conflict_write");
            }
            state.timeout_value = std::max(state.timeout_value - step, CRAFT_T_MIN);
            state.reopen_streak = 0;
            timeout_counter[index] = 0;
            simple_stats_.Increment("craft_conflicts");
            simple_stats_.Increment("craft_deescalations");
        }
        else{
            timeout_counter[index] = craft_state_[index].timeout_value;
            timeout_ticking[index] = false;
        }
    }
}
```

### 3.3 `dramsim3/src/command_queue.h`

更新 `CRAFT_ProcessACT` 函数声明：

```cpp
void CRAFT_ProcessACT(int queue_idx, int new_row, bool triggered_by_read);
```

### 3.4 `dramsim3/src/simple_stats.cc`

新增 4 个读写区分计数器：

```cpp
InitStat("craft_wrong_read", "counter", "CRAFT wrong precharge triggered by read");
InitStat("craft_wrong_write", "counter", "CRAFT wrong precharge triggered by write");
InitStat("craft_conflict_read", "counter", "CRAFT conflict caused by read");
InitStat("craft_conflict_write", "counter", "CRAFT conflict caused by write");
```

### 3.5 配置文件

复制 `champsim-la/dramsim3_configs/DDR5_64GB_4ch_4800_CRAFT.ini` 为 `DDR5_64GB_4ch_4800_CRAFT_RW.ini`，`row_buf_policy` 保持 `CRAFT`。代码修改直接生效，无需新增配置项（读写区分是 CRAFT 的内在改进，不需要额外开关）。

---

## 4. 修改文件总结

| 文件 | 修改量 | 性质 |
|------|--------|------|
| `dramsim3/src/command_queue.h` | ~1 行 | 更新函数签名 |
| `dramsim3/src/command_queue.cc` | ~25 行 | `CRAFT_ProcessACT` 增加读写判断、`AddCommand` 增加读写区分步长、`GetFirstReadyInQueue` 传递 read/write 信息 |
| `dramsim3/src/simple_stats.cc` | ~4 行 | 注册读写区分计数器 |

总计约 **30 行** 修改的 C++ 代码。

---

## 5. 实验设计

### 5.1 实验配置

| 配置名 | 策略 | 读写区分 | 说明 |
|--------|------|---------|------|
| `CRAFT_1c` | CRAFT | 关闭 | Baseline CRAFT（当前实现） |
| `CRAFT_RW_1c` | CRAFT | 开启 | 本方案：读写代价区分 |

### 5.2 消融实验设计（Ablation Study）

为了量化读写区分各个组件的贡献，设计以下消融变体：

| 变体 | Escalation 读写区分 | De-escalation 读写区分 | 说明 |
|------|---------------------|----------------------|------|
| `CRAFT_1c` (baseline) | 否 | 否 | 基线 |
| `CRAFT_RW_ESC_1c` | **是** | 否 | 仅 escalation 区分（write wrong precharge 半步长） |
| `CRAFT_RW_DEESC_1c` | 否 | **是** | 仅 de-escalation 区分（read conflict 双倍步长） |
| `CRAFT_RW_1c` | **是** | **是** | 完整方案 |

消融实验的实现方式：通过编译时 `#define` 或代码中 `constexpr bool` 控制。例如：

```cpp
// In command_queue.h
static constexpr bool CRAFT_RW_ESCALATION = true;   // write wrong → half step
static constexpr bool CRAFT_RW_DEESCALATION = true;  // read conflict → 2x step
```

### 5.3 步长敏感性分析

对 de-escalation 的读 conflict 乘数进行敏感性测试：

| 参数 | 测试值 | 默认值 | 说明 |
|------|--------|--------|------|
| Read conflict 乘数 | 1.5x, 2x, 3x, 4x | 2x | 读 conflict de-escalation 步长 = CONFLICT_STEP * 乘数 |
| Write wrong 缩减因子 | 1/4, 1/2, 3/4 | 1/2 | 写 wrong precharge escalation 步长 = 原步长 * 因子 |

注：1.5x 和 3/4 可用整数近似实现（`step * 3 >> 1`、`step * 3 >> 2`），避免浮点运算。

### 5.4 Benchmark 分类

根据读写比例和行缓冲局部性，将 benchmark 分为以下类别进行分析：

| 类别 | 特征 | 代表 benchmark | 预期影响 |
|------|------|---------------|----------|
| Read-dominant + 高局部性 | 读多写少，行命中率高 | lbm, fotonik3d | 影响小（few conflicts） |
| Read-dominant + 低局部性 | 读多写少，行冲突多 | mcf, omnetpp | **受益最大**：read conflict 激进降阶减少 stall |
| Write-heavy + 高局部性 | 写多，行命中率高 | bwaves | 影响小 |
| Write-heavy + 低局部性 | 写多，行冲突多 | canneal | 温和调整避免 write 过度影响 timeout |
| 读写混合 | 读写交织频繁 | xalancbmk, gcc | **最有价值的测试点**：区分效果最明显 |

### 5.5 评估指标

#### 主要指标
- **IPC**：每个 benchmark 单独报告 IPC 改善（相对 CRAFT baseline），并按 benchmark 类别分组报告
- **加权 GEOMEAN IPC**：使用 `benchmarks_selected.tsv` 中的 weight 计算

#### 辅助 DRAM 指标
- **Row Buffer Hit Rate**: `(num_read_row_hits + num_write_row_hits) / (num_read_cmds + num_write_cmds)`
- **ACT 次数**: `num_act_cmds`（ACT 越少说明 wrong precharge 越少）
- **平均读延迟**: `average_read_latency`（读延迟对 IPC 影响最直接）
- **平均写延迟**: `average_write_latency`（验证写路径未被损害）

#### CRAFT 内部指标
- **craft_wrong_read / craft_wrong_write**: 读写分别触发的 wrong precharge 次数
- **craft_conflict_read / craft_conflict_write**: 读写分别触发的 conflict 次数
- **craft_timeout_value_sum / craft_timeout_precharges**: 平均 timeout 值
- **craft_reopen_streak_dist**: reopen streak 分布

#### 关键对比分析
- 对比 `CRAFT_1c` vs `CRAFT_RW_1c` 的 `average_read_latency`：读写区分应显著降低读延迟
- 对比 `average_write_latency`：写延迟应不恶化或仅轻微增加（可容忍）
- 对比 `craft_timeout_value_sum`：读写区分后，不同负载的平均 timeout 是否更合理

---

## 6. 构建与运行

```bash
# 1. 构建 DRAMSim3
cd /root/data/smartPRE/dramsim3 && mkdir -p build && cd build && cmake .. && make -j8

# 2. 构建 ChampSim
cd /root/data/smartPRE/champsim-la
python3 config.sh champsim_config.json
make -j8

# 3. 运行 CRAFT baseline（如果尚未跑过）
export LD_LIBRARY_PATH=/root/data/smartPRE/dramsim3:$LD_LIBRARY_PATH
TRACE_ROOT=/root/data/Trace/LA scripts/run_selected_slices.sh
# label: CRAFT_1c

# 4. 运行 CRAFT_RW（代码修改后重新构建）
cd /root/data/smartPRE/dramsim3/build && make -j8
cd /root/data/smartPRE/champsim-la && make -j8
TRACE_ROOT=/root/data/Trace/LA scripts/run_selected_slices.sh
# label: CRAFT_RW_1c

# 5. 对比结果
python3 scripts/compare_ipc.py results/CRAFT_1c results/CRAFT_RW_1c
```

---

## 7. 验证步骤

### 7.1 正确性验证

1. **编译测试**: DRAMSim3 和 ChampSim 编译通过，无 warning
2. **Smoke test**: 运行短 trace（warmup=1M, sim=5M），确认：
   - `craft_wrong_read + craft_wrong_write == craft_timeout_wrong`
   - `craft_conflict_read + craft_conflict_write == craft_conflicts`
   - `craft_wrong_write > 0`（验证写路径被触发）
   - `craft_conflict_read > 0`（验证读路径被触发）
3. **不变量验证**: `timeout_value` 始终在 `[T_MIN, T_MAX] = [50, 3200]` 范围内

### 7.2 行为方向验证

1. **Read-dominant 负载**（如 mcf）：`craft_conflict_read >> craft_conflict_write`，读写区分后 de-escalation 更激进，平均 timeout 应比 baseline 更小
2. **Write-heavy 负载**（如 bwaves）：`craft_wrong_write > 0`，写 wrong precharge 使用半步长，平均 timeout 应比 baseline 略低
3. **混合负载**：不同 bank 根据各自的读写比例独立适配

### 7.3 性能方向预期

- **Read-dominant + 低局部性负载**: IPC 提升（read conflict 激进降阶减少排队延迟）
- **Write-heavy 负载**: IPC 不恶化（写路径温和调整不过度影响 timeout）
- **高局部性负载**: IPC 变化很小（few wrong precharge / conflict 事件）
- **整体**: GEOMEAN IPC 改善或持平

---

## 8. 结果呈现

### 8.1 主结果表

每个 benchmark 报告以下数据：

| Benchmark | CRAFT IPC | CRAFT_RW IPC | IPC 改善(%) | Avg Read Lat | Avg Write Lat | wrong_rd | wrong_wr | conflict_rd | conflict_wr |
|-----------|-----------|-------------|-------------|-------------|-------------|----------|----------|-------------|-------------|

### 8.2 消融结果表

| Benchmark | CRAFT | +ESC only | +DEESC only | +Both (CRAFT_RW) |
|-----------|-------|-----------|-------------|-------------------|
| (IPC 值) |       |           |             |                   |

### 8.3 关键图表

1. **Per-benchmark IPC 对比柱状图**: CRAFT vs CRAFT_RW，按 benchmark 类别分组
2. **读写延迟分布**: 对比两种配置下的读/写延迟 CDF
3. **平均 timeout 对比**: 按 benchmark 展示平均 timeout 值的变化
4. **消融贡献图**: 叠加柱状图展示 ESC 和 DEESC 各自的 IPC 贡献
