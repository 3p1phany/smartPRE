# FAPS-3D 严格复现方案

## 1. 论文核心算法精确描述

参考论文: Rafique & Zhu, "FAPS-3D: Feedback-directed Adaptive Page Management Scheme for 3D-Stacked DRAM", MEMSYS 2019.

### 1.1 Close-page 语义（论文原文）

论文 Section 3 (p.4):
> "The close-page mode, on the other hand, precharges the row after the access, **provided currently no other requests go to the same row**."

论文 Section 4.3 CLOSE-P 定义:
> "CLOSE-P (S): the baseline close-page policy, where row-buffer is precharged **if no pending request in the queue goes to this row**, making room for next row to be activated upon request."

**关键结论**: 论文的 close-page **不是** "每次访问后都 precharge"，而是 "当队列中没有更多同 row 请求时才 precharge"。这与代码中 SMART_CLOSE 的行为 (`row_hit_count==1` 时添加 auto-precharge) 语义一致。

### 1.2 `row_hit_count==1` 语义澄清

代码 `command_queue.cc:155-183` 中 `row_hit_count` 的含义:
- 统计 per-bank command queue 中与当前 cmd 同 row 的命令数（**包含当前命令自身**）
- 额外扫描 transaction 级 read_queue / write_buffer 中尚可调度（`queue.size() < queue_size_`）的同 bank 同 row 事务

`row_hit_count==1` 表示: **当前命令是队列中唯一一个目标为该 row 的请求**，即 "no other pending request goes to this row"。这 **不是** "队列中只有一个请求"——队列中可以有任意多个请求，只要它们目标是不同的 row。

### 1.3 Algorithm I — Open-page bank 评估（论文 p.5）

每个 epoch（每 bank 1000 次访问）对当前 open-page 的 bank 评估实际 row-buffer hit-rate:

```
bankHitAvg = bank_hits / total_accesses

if bankHitAvg < thl (25%):
    bank_ns ← (00)₂    → closePage (直接跳转，不受当前状态影响)
else if thl ≤ bankHitAvg < th (50%):
    bank_ns ← bank_cs - 1  (饱和递减)
else (bankHitAvg ≥ th):
    bank_ns ← bank_cs + 1  (饱和递增)

if bank_ns ≤ (01)₂:  closePage
else:                 openPage
```

### 1.4 Algorithm II — Close-page bank 评估（论文 p.5-6）

使用 hit register 追踪的 "potential bank hit-rate" (PBHR):

```
PBHR = potential_hits / total_accesses

if PBHR ≥ thh (75%):
    bank_ns ← (11)₂    → openPage (直接跳转)
else if th (50%) ≤ PBHR < thh:
    bank_ns ← bank_cs + 1  (饱和递增)
else (PBHR < th):
    bank_ns ← bank_cs - 1  (饱和递减)

if bank_ns ≥ (10)₂:  openPage
else:                 closePage
```

### 1.5 Hit Register（论文 Section 3.1.4, 3.2）

论文 Section 3.1.4:
> "an SRAM-based 'hit-register' is used to keep track of the number of 'potential hits' if the **next access goes to the same page as the previous one**."

论文 Section 3.2:
> "the hit register holds 16 entries (one per bank), with each entry being 52 bits to keep **bank ID, total bank access count, last accessed page id, currently accessed page id and total bank hit count**."

Hit register 仅对 close-page bank 追踪：若本次访问的 page 与上一次相同，则 potential_hit_count++。

### 1.6 初始状态与调度策略

论文 Section 3:
> "Initially, the scheme applies **open-page mode** and scheduling policy that gives preference to row-hits over all other accesses [5]."

- 所有 bank 初始为 open-page，FSM state = (11)₂ = 3
- 调度策略: FR-FCFS（优先 row hit）

---

## 2. 当前实现与论文的对照分析

### 2.1 已确认一致的部分

| 论文描述 | 代码实现 | 状态 |
|---------|---------|------|
| Close-page = precharge when no pending same-row requests | SMART_CLOSE + `row_hit_count==1` check (line 186-190) | ✓ 一致 |
| Algorithm I: open-page bank 使用实际 hit-rate | `true_row_hit_count_[i]` / `total_command_count_[i]` (line 1018) | ✓ 一致 |
| Algorithm II: close-page bank 使用 PBHR | `potential_hit_count` / `total` (line 1040-1042) | ✓ 一致 |
| FSM 阈值: thl=25%, th=50%, thh=75% | 整数比较: `total>>2`, `total>>1`, `*4 >= *3` | ✓ 一致 |
| 2-bit 饱和计数器 FSM 转移 | `bank_sm[i]` 0-3 范围，饱和递增/递减 | ✓ 一致 |
| Per-bank epoch = 1000 accesses | `FAPS_EPOCH_ACCESSES = 1000`，per-bank 独立触发 | ✓ 一致 |
| 所有 bank 初始 open-page (state=3) | `pp=RowBufPolicy::OPEN_PAGE` (line 65) | ✓ 一致 |
| Hit register 跨 epoch 保持 last_accessed_row | `last_accessed_row` 不在 epoch 重置时清零 (line 1069) | ✓ 一致 |
| Algorithm I 低 hit-rate 直接跳转 closePage | `bank_sm[i] = 0`，然后 `0 ≤ 1` → SMART_CLOSE | ✓ 等效 |
| Algorithm II 高 PBHR 直接跳转 openPage | `bank_sm[i] = 3`，然后 `3 ≥ 2` → OPEN_PAGE | ✓ 等效 |
| Refresh 不重置计数器 | `FinishRefresh()` 跳过 FAPS 计数器清零 (line 368) | ✓ 一致 |

### 2.2 需要修正的差异

#### 差异 1: `last_accessed_row` 更新范围

**论文**: Hit register 仅用于 close-page bank 追踪 potential hit。

**当前代码** (`command_queue.cc:1000-1001`):
```cpp
// Always update last_accessed_row
fstate.last_accessed_row = row;
```

`last_accessed_row` 对所有 bank（包括 open-page bank）都更新。这导致当 bank 从 open-page 切换到 close-page 时，`last_accessed_row` 保留了 open-page 时期的值。如果 close-page 的第一次访问碰巧命中同一 row，会被错误计为 potential hit。

**修正**: `last_accessed_row` 只在 close-page bank 发出命令时更新；或者在 open→close 模式切换时重置。

#### 差异 2: `row_hit_count` 扫描范围过广

**论文 (Section 4.3, CLOSE-P 定义)**: "precharged if no pending request **in the queue** goes to this row"

**当前代码** (`command_queue.cc:158-183`): `row_hit_count` 不仅统计 per-bank command queue 中的同 row 命令，还扫描 transaction 级的 `read_queue()` / `write_buffer()` 中尚可调度的同 bank 同 row 事务。

这使得 close-page bank 的 precharge 决策比论文描述更保守（更倾向于保持页面打开），因为它能"看到"尚未进入 command queue 的 pending 事务。

**影响分析**: 论文 Figure 4 中 policy engine 确实可以访问 Read Buffer 和 Write Buffer，因此从架构上讲，扫描 transaction 级缓冲区可能是合理的。但为严格复现论文中 CLOSE-P 的定义（"pending request in the queue"），应仅基于 command queue 做决策。

**修正方案**: 对 FAPS close-page bank，`row_hit_count` 仅统计 per-bank command queue 内的同 row 命令，不扫描 transaction 级缓冲区。为避免改动共享逻辑影响其他策略，可在 FAPS SMART_CLOSE 分支内单独计算 command-queue-only 的 row_hit_count。

#### 差异 3: potential hit 计数包含 queued row hit

**论文**: potential hit 追踪 "if the **next** access goes to the same page as the **previous** one"——描述的是逐次访问序列的连续性。

**当前代码**: `FAPS_TrackAccess()` 在每个 RW 命令发出时调用，检查与上一次发出命令的 row 是否相同。当多个同 row 请求同时在 command queue 中排队时（SMART_CLOSE 会将它们作为 row hit 连续服务），这些请求全部被计为 potential hit，即使它们在 close-page 模式下也是 actual hit（因为它们已经在队列中排好了，无论 open/close 都会被连续服务）。

**影响分析**: 这会轻微抬高 PBHR。但由于论文的 close-page 也有相同的 row-hit clustering 行为（"precharge when no pending same-row requests"），在 close-page 下这些 queued row hit 同样会被连续服务。因此这些 consecutive same-row accesses 被计为 potential hit 是合理的——它们确实反映了 access pattern 中的局部性。此项与论文行为**基本一致**，属于灰色区域，暂不修改。

---

## 3. 代码修改方案

### 3.1 修正差异 1: `last_accessed_row` 更新范围

**文件**: `dramsim3/src/command_queue.cc`, `FAPS_TrackAccess()` 函数

**当前代码**:
```cpp
void CommandQueue::FAPS_TrackAccess(int queue_idx, int row) {
    auto& fstate = faps_bank_state_[queue_idx];
    // Hit register: track potential hits only for close-page banks
    if (row_buf_policy_[queue_idx] == RowBufPolicy::SMART_CLOSE) {
        if (fstate.last_accessed_row == row && fstate.last_accessed_row != -1) {
            fstate.potential_hit_count++;
        }
    }
    // Always update last_accessed_row
    fstate.last_accessed_row = row;
}
```

**修改为**:
```cpp
void CommandQueue::FAPS_TrackAccess(int queue_idx, int row) {
    auto& fstate = faps_bank_state_[queue_idx];
    if (row_buf_policy_[queue_idx] == RowBufPolicy::SMART_CLOSE) {
        // Hit register: only active during close-page mode
        if (fstate.last_accessed_row == row && fstate.last_accessed_row != -1) {
            fstate.potential_hit_count++;
        }
        // Only update last_accessed_row during close-page mode (paper Section 3.1.4)
        fstate.last_accessed_row = row;
    }
}
```

### 3.2 修正差异 2: FAPS close-page 仅基于 command queue 做 precharge 决策

**文件**: `dramsim3/src/command_queue.cc`, `GetCommandToIssue()` 函数

**方案**: 在 `row_hit_count==1` 的外层，对 FAPS SMART_CLOSE bank 增加独立判断路径。FAPS close-page bank 使用 command-queue-only 的 `row_hit_count`（即仅 line 156 的统计，不含 line 158-183 的 transaction buffer 扫描）。

**当前逻辑** (line 155-191):
```cpp
int row_hit_count=0;
row_hit_count += std::count_if(queue.begin(),queue.end(),
    [&cmd](Command x){return x.Row() == cmd.Row() ;});

// ... 扫描 write_buffer 和 read_queue (line 158-183) ...

if(row_hit_count==1){
    if(row_buf_policy_[queue_idx_] == RowBufPolicy::SMART_CLOSE){
        // auto-precharge
    }
}
```

**修改方案**: 将 command-queue-only 的计数在扫描 transaction buffer 之前保存，供 FAPS 使用:
```cpp
int row_hit_count=0;
row_hit_count += std::count_if(queue.begin(),queue.end(),
    [&cmd](Command x){return x.Row() == cmd.Row() ;});

// FAPS: use command-queue-only count for close-page precharge decision
int row_hit_count_cmdq = row_hit_count;

// ... existing write_buffer / read_queue scan (line 158-183) ...

// FAPS close-page: decide based on command queue only (paper Section 4.3)
if(top_row_buf_policy_==RowBufPolicy::FAPS
   && row_buf_policy_[queue_idx_] == RowBufPolicy::SMART_CLOSE
   && row_hit_count_cmdq==1){
    cmd.cmd_type = cmd.cmd_type==CommandType::READ ? CommandType::READ_PRECHARGE:
                   cmd.cmd_type==CommandType::WRITE? CommandType::WRITE_PRECHARGE:cmd.cmd_type;
    autoPRE_added=true;
}
// Other policies: use full row_hit_count (including transaction buffers)
else if(row_hit_count==1){
    // ... existing GS / CRAFT / ABP / DYMPL / RL_PAGE logic (unchanged) ...
    // Note: SMART_CLOSE check here should exclude FAPS to avoid double handling
    if(row_buf_policy_[queue_idx_] == RowBufPolicy::SMART_CLOSE
       && top_row_buf_policy_ != RowBufPolicy::FAPS){
        cmd.cmd_type = cmd.cmd_type==CommandType::READ ? CommandType::READ_PRECHARGE:
                       cmd.cmd_type==CommandType::WRITE? CommandType::WRITE_PRECHARGE:cmd.cmd_type;
        autoPRE_added=true;
    }
    // ... rest unchanged ...
}
```

### 3.3 其他文件（无需修改）

以下文件无需修改:
- `dramsim3/src/command_queue.h` — 常量、结构体、声明均正确
- `dramsim3/src/common.h` — FAPS 枚举已存在
- `dramsim3/src/controller.cc` — 字符串映射已存在
- `dramsim3/src/simple_stats.cc` — FAPS 统计计数器已注册
- `FAPS_ArbitratePagePolicy()` — Algorithm I/II 实现已正确

---

## 4. 配置文件

已有配置文件无需修改:
- `champsim-la/dramsim3_configs/DDR5_64GB_4ch_4800_FAPS.ini` — `row_buf_policy = FAPS`
- `champsim-la/champsim_config_FAPS.json` — 指向 FAPS DRAM 配置

---

## 5. 构建与运行

```bash
# 1. 构建 DRAMSim3
cd /root/data/smartPRE/dramsim3/build && cmake .. && make -j8

# 2. 构建 ChampSim (FAPS配置)
cd /root/data/smartPRE/champsim-la
python3 config.sh champsim_config_FAPS.json
make -j8

# 3. 运行仿真
export LD_LIBRARY_PATH=/root/data/smartPRE/dramsim3:$LD_LIBRARY_PATH
TRACE_ROOT=/path/to/traces scripts/run_selected_slices.sh
# 输入 label: FAPS_1c

# 4. 对比结果
python3 scripts/compare_ipc.py results/GS_1c results/FAPS_1c
```

---

## 6. 实验方案

### 6.1 基线对比

| 配置名 | row_buf_policy | 说明 |
|--------|---------------|------|
| `GS_1c` | GS | 当前最佳策略（Global Scoreboarding + RE） |
| `DPM_1c` | DPM | 现有动态页管理（cycle-based epoch，无 potential hit） |
| `OPEN_PAGE_1c` | OPEN_PAGE | 静态 open-page |
| `CLOSE_PAGE_1c` | CLOSE_PAGE | 静态 close-page |
| `FAPS_1c` | FAPS | 本方案 |

### 6.2 评估指标

- **IPC**: 每个 benchmark 的 IPC 改善（per-benchmark 报告，不仅报告 GEOMEAN）
- **Row Buffer Hit Rate**: `num_read_row_hits + num_write_row_hits` / `num_read_cmds + num_write_cmds`
- **ACT 次数**: `num_act_cmds`（越少说明 row buffer 管理越好）
- **平均读延迟**: `average_read_latency`
- **FAPS 切换统计**: `faps_epoch_count`, `faps_switch_to_open`, `faps_switch_to_close`

### 6.3 Benchmark 套件

使用 `benchmarks_selected.tsv` 中的完整 benchmark 集合，通过 `scripts/run_selected_slices.sh` 运行。

### 6.4 敏感性分析（可选后续实验）

| 参数 | 测试值 | 默认值 |
|------|-------|--------|
| Epoch 长度 | 500, 1000, 2000 | 1000 访问 |
| thl (低阈值) | 0.15, 0.25, 0.35 | 0.25 |
| thh (高阈值) | 0.65, 0.75, 0.85 | 0.75 |

---

## 7. 验证步骤

1. **编译测试**: DRAMSim3 和 ChampSim 均编译通过
2. **功能验证**: 运行少量 trace（warmup=1M, sim=5M），确认：
   - `faps_epoch_count > 0`（epoch 触发）
   - `faps_switch_to_close` 和 `faps_switch_to_open` 有合理数值
   - 无除零错误
3. **差异 1 验证**: 在 open→close 切换后，检查 `potential_hit_count` 是否不再包含 open-page 时期残留的 false positive
4. **差异 2 验证**: 对比修正前后的 auto-precharge 次数——修正后 FAPS close-page bank 应更积极地 precharge（因为不再因 transaction buffer 中的同 row 事务而延迟关闭）
5. **结果对比**: 与 DPM、GS 在相同 benchmark 上对比 IPC

---

## 8. 修改文件总结

| 文件 | 修改量 | 性质 |
|------|--------|------|
| `dramsim3/src/command_queue.cc` — `FAPS_TrackAccess()` | ~3 行 | 修正差异1: `last_accessed_row` 仅在 close-page 更新 |
| `dramsim3/src/command_queue.cc` — `GetCommandToIssue()` | ~15 行 | 修正差异2: FAPS close-page 仅基于 command queue 决策 |

总计约 **18 行** 修改的 C++ 代码。

---

## 9. 关键设计决策说明

1. **Close-page 使用 SMART_CLOSE**: 论文原文明确描述 close-page 为 "precharge when no pending same-row requests"，这与 SMART_CLOSE 的 `row_hit_count==1` 检查语义一致。SMART_CLOSE **不是** 对论文的近似，而是**精确实现**。

2. **`row_hit_count==1` 不等于 "队列只有一个请求"**: `row_hit_count` 统计的是与当前 cmd **同 row** 的请求数（包含 cmd 自身）。`row_hit_count==1` 表示 "当前命令是队列中唯一目标为该 row 的请求"。队列中可以有任意数量的请求，只要它们目标是其他 row。

3. **Per-bank epoch**: FAPS 的核心创新。通过 `total_command_count_[i] >= FAPS_EPOCH_ACCESSES` 判断。高访问率 bank 更频繁评估策略。

4. **FinishRefresh() 不清零 FAPS 计数器**: DDR5 tREFI=9360 cycles 下每个 bank 在两次 refresh 间最多约 36 次访问，远不及 1000 阈值。论文也未描述 refresh 时重置。

5. **差异 2 的权衡**: 修正后 FAPS close-page bank 仅看 command queue，可能导致更频繁的 "precharge 后马上又 activate 同一 row"（因为 transaction buffer 中的同 row 事务还没调度进来）。但这更忠实于论文描述。如果性能显著下降，可回退此修改。
