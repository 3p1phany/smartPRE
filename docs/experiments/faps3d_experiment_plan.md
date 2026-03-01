# FAPS-3D 实现方案

## Context

FAPS-3D (Feedback-directed Adaptive Page management Scheme for 3D-stacked DRAM) 是 Rafique & Zhu 在 MEMSYS 2019 发表的动态页管理方案。该方案通过 per-bank 的 2-bit 饱和计数器 FSM，根据 row-buffer hit-rate 动态切换 open-page / close-page 模式。其核心创新点在于：

1. **非对称算法**：对当前 open-page 的 bank 使用 Algorithm I（基于实际 hit-rate），对当前 close-page 的 bank 使用 Algorithm II（基于"potential hit-rate"，即假设页面保持打开时本可获得的 hit）
2. **Per-bank epoch**：以每个 bank 的访问计数（1000 次访问）为 epoch 单位，而非全局时钟周期
3. **Hit Register**：为 close-page bank 维护一个 last_accessed_row 寄存器，追踪连续相同 row 的访问比例

本项目已有相似的 DPM 策略实现，FAPS-3D 可以在其基础上高效添加。

## 与现有 DPM 的关系

FAPS-3D 和 DPM 共享完全相同的 FSM 状态转移逻辑和阈值 (25%, 50%, 75%)，差异仅在两点：

1. **Epoch 触发方式**: DPM 全局每 1000 cycle 评估所有 bank；FAPS 按 per-bank 每 1000 次访问独立触发
2. **Close-page bank 的 hit 指标**: DPM 使用 `true_row_hit_count_`（SMART_CLOSE 模式下 cluster 内的实际 hit）；FAPS 使用 hit register 追踪的 `potential_hit_count`（连续相同 row 访问的比例，能捕获 cluster 结束后 precharge 掉的 row 的后续访问）

---

## 1. 代码修改

### 1.1 `dramsim3/src/common.h` (line 10)

在 `RowBufPolicy` 枚举中添加 `FAPS`：

```cpp
enum class RowBufPolicy { OPEN_PAGE, CLOSE_PAGE, ORACLE, SMART_CLOSE, DPM, GS, GS_NOHOTROW, FAPS, SIZE };
```

### 1.2 `dramsim3/src/command_queue.h`

**添加 FAPS 常量**（在 line 17 GS_VARIATION_THRESHOLD 之后）：

```cpp
// ===== FAPS-3D Constants =====
static constexpr int FAPS_EPOCH_ACCESSES = 1000;
```

**添加 FAPS per-bank 状态结构**（在 RowExclusionDetectState 之后，line 56 之后）：

```cpp
struct FAPSBankState {
    int last_accessed_row = -1;     // Hit register: 上一次访问的 row
    int potential_hit_count = 0;    // Close-page bank 的 potential hit 计数
};
```

**在 CommandQueue 类中添加成员变量和函数声明**（在 line 137 RE_RemoveEntry 之后）：

```cpp
// ===== FAPS-3D Members =====
std::vector<FAPSBankState> faps_bank_state_;  // per bank
void FAPS_ArbitratePagePolicy();
void FAPS_TrackAccess(int queue_idx, int row);
```

### 1.3 `dramsim3/src/command_queue.cc`

#### 1.3a 构造函数初始化（line 92 之后，在 `re_detect_state_` 初始化之后）

```cpp
// ===== FAPS-3D State Init =====
faps_bank_state_.resize(num_queues_);
```

#### 1.3b per-bank 初始策略（line 48-61 的 policy init 循环中添加）

在 `else if(this->top_row_buf_policy_==RowBufPolicy::DPM)` 之后添加：

```cpp
else if(this->top_row_buf_policy_==RowBufPolicy::FAPS){
    pp=RowBufPolicy::OPEN_PAGE;  // FAPS 所有 bank 初始为 open-page (state=3)
}
```

#### 1.3c GetCommandToIssue() — close-page bank 行为（line 140-145）

FAPS 切换到 close-page 时，内部使用 `SMART_CLOSE` 作为 per-bank policy（与 DPM 相同），因此 **无需额外修改**——现有 line 141 的 `row_buf_policy_[queue_idx_] == RowBufPolicy::SMART_CLOSE` 检查已经覆盖了 FAPS 的 close-page bank。

#### 1.3d GetCommandToIssue() — FAPS 访问追踪（line 159 `total_command_count_` 之后）

在 `total_command_count_[queue_idx_]++;` (line 159) 之后添加：

```cpp
// FAPS: Track access for potential hit counting
if (top_row_buf_policy_ == RowBufPolicy::FAPS) {
    FAPS_TrackAccess(queue_idx_, cmd.Row());
}
```

#### 1.3e 实现 FAPS_TrackAccess()

```cpp
void CommandQueue::FAPS_TrackAccess(int queue_idx, int row) {
    auto& fstate = faps_bank_state_[queue_idx];
    // Hit register: 仅对 close-page bank 追踪 potential hit
    if (row_buf_policy_[queue_idx] == RowBufPolicy::SMART_CLOSE) {
        if (fstate.last_accessed_row == row && fstate.last_accessed_row != -1) {
            fstate.potential_hit_count++;
        }
    }
    // 始终更新 last_accessed_row
    fstate.last_accessed_row = row;
}
```

#### 1.3f 实现 FAPS_ArbitratePagePolicy()

这是核心仲裁逻辑，实现论文中的 Algorithm I 和 Algorithm II：

```cpp
void CommandQueue::FAPS_ArbitratePagePolicy() {
    for (int i = 0; i < num_queues_; i++) {
        // Per-bank epoch：仅在访问次数达到阈值时触发
        if (total_command_count_[i] < FAPS_EPOCH_ACCESSES) {
            continue;
        }

        auto& fstate = faps_bank_state_[i];
        int total = total_command_count_[i];

        if (row_buf_policy_[i] == RowBufPolicy::OPEN_PAGE) {
            // ====== Algorithm I: 当前 open-page 模式 ======
            // 使用实际 row-buffer hit-rate
            // hit_rate < 0.25
            if (true_row_hit_count_[i] < (total >> 2)) {
                bank_sm[i] = 0;
            }
            // hit_rate < 0.5
            else if (true_row_hit_count_[i] < (total >> 1)) {
                bank_sm[i] = bank_sm[i] > 0 ? bank_sm[i] - 1 : 0;
            }
            // hit_rate >= 0.5
            else {
                bank_sm[i] = bank_sm[i] < 3 ? bank_sm[i] + 1 : 3;
            }
            // 基于 FSM 状态更新策略
            if (bank_sm[i] <= 1) {
                row_buf_policy_[i] = RowBufPolicy::SMART_CLOSE;
                simple_stats_.Increment("faps_switch_to_close");
            } else {
                row_buf_policy_[i] = RowBufPolicy::OPEN_PAGE;
            }

        } else if (row_buf_policy_[i] == RowBufPolicy::SMART_CLOSE) {
            // ====== Algorithm II: 当前 close-page 模式 ======
            // 使用 potential hit-rate (PBHR)
            int potential_hits = fstate.potential_hit_count;
            // pbhr >= 0.75
            if (potential_hits * 4 >= total * 3) {
                bank_sm[i] = 3;
            }
            // pbhr >= 0.5
            else if (potential_hits * 2 >= total) {
                bank_sm[i] = bank_sm[i] < 3 ? bank_sm[i] + 1 : 3;
            }
            // pbhr < 0.5
            else {
                bank_sm[i] = bank_sm[i] > 0 ? bank_sm[i] - 1 : 0;
            }
            // 基于 FSM 状态更新策略
            if (bank_sm[i] >= 2) {
                row_buf_policy_[i] = RowBufPolicy::OPEN_PAGE;
                simple_stats_.Increment("faps_switch_to_open");
            } else {
                row_buf_policy_[i] = RowBufPolicy::SMART_CLOSE;
            }
        }

        simple_stats_.Increment("faps_epoch_count");

        // Per-bank 重置计数器
        total_command_count_[i] = 0;
        true_row_hit_count_[i] = 0;
        demand_row_hit_count_[i] = 0;
        fstate.potential_hit_count = 0;
        // 注意：last_accessed_row 不重置，跨 epoch 保持
    }
}
```

#### 1.3g ClockTick() 集成（line 508-526）

在 line 525 GS_ArbitrateTimeout 调用之后添加：

```cpp
// FAPS arbitration
if(top_row_buf_policy_==RowBufPolicy::FAPS){
    FAPS_ArbitratePagePolicy();
}
```

#### 1.3h FinishRefresh() 中重置 FAPS 状态（line 266-271 的 for 循环中）

在 `demand_row_hit_count_[i]=0;` (line 270) 之后添加：

```cpp
if (top_row_buf_policy_ == RowBufPolicy::FAPS) {
    faps_bank_state_[i].potential_hit_count = 0;
}
```

### 1.4 `dramsim3/src/controller.cc` (line 19-25, 31-36)

在两处 string→enum ternary chain 中添加 FAPS 映射。

**Line 24**（第一处，cmd_queue_ 初始化），在 `GS_NOHOTROW` 之后添加：

```cpp
config.row_buf_policy == "FAPS"         ? RowBufPolicy::FAPS:
```

**Line 35**（第二处，row_buf_policy_ 初始化），同样添加。

### 1.5 `dramsim3/src/simple_stats.cc` (line 107 之后)

添加 FAPS 统计计数器：

```cpp
// FAPS-3D counters
InitStat("faps_epoch_count", "counter",
         "FAPS epoch evaluations performed (per-bank)");
InitStat("faps_switch_to_close", "counter",
         "FAPS switches from open-page to close-page");
InitStat("faps_switch_to_open", "counter",
         "FAPS switches from close-page to open-page");
```

---

## 2. 配置文件

### 2.1 新建 DRAM 配置文件

复制 `champsim-la/dramsim3_configs/DDR5_64GB_4ch_4800.ini` 为 `DDR5_64GB_4ch_4800_FAPS.ini`，仅修改：

```ini
row_buf_policy = FAPS
```

### 2.2 ChampSim 配置

复制 `champsim-la/champsim_config.json` 为 `champsim_config_FAPS.json`，修改 `dram_io_config` 指向新 DRAM 配置文件。

---

## 3. 构建与运行

```bash
# 1. 构建 DRAMSim3
cd /root/data/smartPRE/dramsim3 && mkdir -p build && cd build && cmake .. && make -j8

# 2. 构建 ChampSim (FAPS配置)
cd /root/data/smartPRE/champsim-la
cp champsim_config.json champsim_config_FAPS.json
# 修改 champsim_config_FAPS.json 指向 FAPS DRAM 配置
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

## 4. 实验方案

### 4.1 基线对比

| 配置名 | row_buf_policy | 说明 |
|--------|---------------|------|
| `GS_1c` | GS | 当前最佳策略（Global Scoreboarding + RE） |
| `DPM_1c` | DPM | 现有动态页管理（cycle-based epoch，无 potential hit） |
| `OPEN_PAGE_1c` | OPEN_PAGE | 静态 open-page |
| `CLOSE_PAGE_1c` | CLOSE_PAGE | 静态 close-page |
| `FAPS_1c` | FAPS | 本方案 |

### 4.2 评估指标

- **IPC**: 每个 benchmark 的 IPC 改善（per-benchmark 报告，不仅报告 GEOMEAN）
- **Row Buffer Hit Rate**: `num_read_row_hits + num_write_row_hits` / `num_read_cmds + num_write_cmds`
- **ACT 次数**: `num_act_cmds`（越少说明 row buffer 管理越好）
- **平均读延迟**: `average_read_latency`
- **FAPS 切换统计**: `faps_epoch_count`, `faps_switch_to_open`, `faps_switch_to_close`

### 4.3 Benchmark 套件

使用 `benchmarks_selected.tsv` 中的完整 benchmark 集合（62 个 benchmark），通过 `scripts/run_selected_slices.sh` 运行。

### 4.4 敏感性分析（可选后续实验）

| 参数 | 测试值 | 默认值 |
|------|-------|--------|
| Epoch 长度 | 500, 1000, 2000 | 1000 访问 |
| thl (低阈值) | 0.15, 0.25, 0.35 | 0.25 |
| thh (高阈值) | 0.65, 0.75, 0.85 | 0.75 |

---

## 5. 验证步骤

1. **编译测试**: DRAMSim3 和 ChampSim 均编译通过
2. **功能验证**: 运行少量 trace（warmup=1M, sim=5M），确认：
   - `faps_epoch_count > 0`（epoch 触发）
   - `faps_switch_to_close` 和 `faps_switch_to_open` 有合理数值
   - 无除零错误（`total_command_count_[i]` 始终 >= `FAPS_EPOCH_ACCESSES` 时才触发）
3. **结果对比**: 与 DPM、GS 在相同 benchmark 上对比 IPC

---

## 6. 修改文件总结

| 文件 | 修改量 | 性质 |
|------|--------|------|
| `dramsim3/src/common.h:10` | 1 行 | 添加 FAPS 到枚举 |
| `dramsim3/src/command_queue.h` | ~15 行 | 添加常量、结构体、成员声明 |
| `dramsim3/src/command_queue.cc` | ~70 行 | 构造函数初始化、FAPS_TrackAccess、FAPS_ArbitratePagePolicy、ClockTick 集成 |
| `dramsim3/src/controller.cc` | 2 行 | 添加 "FAPS" string 映射 |
| `dramsim3/src/simple_stats.cc` | ~6 行 | 注册 FAPS 统计计数器 |
| `champsim-la/dramsim3_configs/DDR5_64GB_4ch_4800_FAPS.ini` | 新文件 | DRAM 配置 |

总计约 **100 行** 新/修改的 C++ 代码。

## 7. 关键设计决策说明

1. **close-page 内部使用 SMART_CLOSE**：与 DPM 一致。真正的 CLOSE_PAGE 需要在 `TransToCommand()` 中做 per-bank 判断（该函数无 bank 信息），改动较大且不必要。SMART_CLOSE 在 row-hit cluster 末尾自动 precharge，语义上等价于论文描述的 close-page 行为。

2. **Per-bank epoch 而非全局 cycle epoch**：FAPS 的核心创新。通过在 `FAPS_ArbitratePagePolicy()` 中检查 `total_command_count_[i] >= FAPS_EPOCH_ACCESSES` 实现。高访问率 bank 更频繁地评估策略，低访问率 bank 评估频率低。

3. **Hit rate 计算使用整数比较**：避免浮点除法（论文提到除法开销大）。`hit < total/4` 等价于 `hit_rate < 0.25`，`potential * 4 >= total * 3` 等价于 `pbhr >= 0.75`。
