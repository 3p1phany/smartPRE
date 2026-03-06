# CRAFT Queue-Depth Scaled De-escalation (QDSD) 实验方案

## 1. 动机与背景

当前 CRAFT 在 conflict 事件触发 de-escalation 时，使用固定步长 `CONFLICT_STEP`（DDR5-4800 下为 25 cycles），不考虑当前 bank 的拥塞程度。这在以下场景中存在问题：

| 场景 | bank queue depth | 固定 CONFLICT_STEP 的问题 |
|------|-----------------|--------------------------|
| 低拥塞（1-2 pending） | 1-2 | 单个冲突请求，保持行打开的机会成本低。固定 25 cycles 降阶合理 |
| 中等拥塞（3-4 pending） | 3-4 | 多个不同行请求排队等待，行打开过久阻塞多个请求。25 cycles 降阶太慢 |
| 高拥塞（>4 pending） | >4 | 大量请求积压，bank 资源被锁定。timeout 应快速收敛到最小值 |

**核心洞察**：在高 bank contention 下，过长的 timeout 不仅浪费当前行的等待时间，还阻塞了多个排队请求。CRAFT 的反馈回路应当感知这种系统级的 opportunity cost，而非仅仅感知单次 conflict 事件的代价。

### 论文故事定位

这是对 CRAFT "Cost-Aware" 核心理念的又一个纵深扩展：
- **已有**：hit/conflict 的成本不对称（escalation 指数退避 vs de-escalation 固定步长）
- **CRAFT_RW**：read/write 的延迟不对称
- **本方案（QDSD）**：bank 级拥塞信息引入反馈回路，编码 opportunity cost

三者可以正交组合，形成 CRAFT 的完整 "cost-aware" 层次结构。

---

## 2. 机制设计

### 2.1 Queue-Depth Scaled De-escalation

```
On conflict (AddCommand: timeout_ticking && cmd.Row() != open_row):
  pending_count = queue_size_for_this_bank   // queues_[index].size()
  scale = min(pending_count, QDSD_SCALE_CAP) // cap at 4, prevent over-aggression
  actual_step = CONFLICT_STEP * scale
  timeout_value = max(timeout_value - actual_step, T_MIN)
  reopen_streak = 0
  timeout_counter = 0  // immediate precharge
```

**行为分析**：

| pending_count | scale | actual_step (DDR5-4800) | 效果 |
|---------------|-------|------------------------|------|
| 1 | 1 | 25 cycles | 与当前行为一致 |
| 2 | 2 | 50 cycles | 两倍降阶速率 |
| 3 | 3 | 75 cycles | 三倍降阶速率 |
| >=4 | 4 (capped) | 100 cycles | 四倍降阶速率，快速释放 bank |

当 queue 中积压 4 个不同行请求时，单次 conflict 降阶 100 cycles 而非 25 cycles，能在 2 次 conflict 事件后就将 timeout 从 INIT(200) 降至 T_MIN 附近。

### 2.2 pending_count 的语义

`pending_count` 直接取 `queues_[index].size()`，即当前 bank 的 command queue 中的**总命令数**（包括 row hit 和 row conflict 命令）。这比过滤"不同行命令数"更简单，且在语义上也合理：

- queue 深度高 = bank 忙碌 = 应更快释放当前行
- 即使 queue 中有同行命令，高 queue depth 本身就说明内存压力大，此时偏向更短的 timeout 有助于减少排队延迟

### 2.3 QDSD_SCALE_CAP 的选择

Cap 在 4 的理由：
1. **防止过激**：command queue 典型大小为 16-32 entries。若不封顶，极端情况下单次 conflict 可将 timeout 降 400+ cycles，导致 timeout 立即跳到 T_MIN，丧失自适应性
2. **硬件实现简洁**：`×1/2/3/4` 可用移位加法实现（`×2 = <<1`，`×3 = <<1 + ×1`，`×4 = <<2`），无需真正的乘法器
3. **与 CONFLICT_STEP=25 配合**：最大单次降阶 100 cycles，对应 timeout 从 200 降到 100，仍在合理范围内

### 2.4 硬件代价

**零额外存储**：queue depth 已是 command queue 的固有信息（硬件中就是 queue 的 occupancy counter）。仅增加一个小乘法器（实际可用 2-bit 移位 + 加法实现），无额外寄存器。

---

## 3. 代码修改

### 3.1 `dramsim3/src/command_queue.h`

新增常量：

```cpp
// ===== CRAFT QDSD (Queue-Depth Scaled De-escalation) =====
static constexpr int CRAFT_QDSD_SCALE_CAP = 4;
static constexpr bool CRAFT_QDSD_ENABLED = true;  // compile-time toggle for ablation
```

无结构体变更。`CraftBankState` 不需要新增字段。

### 3.2 `dramsim3/src/command_queue.cc` — AddCommand() CRAFT 分支

当前代码（`command_queue.cc:463-481`）：

```cpp
else if(top_row_buf_policy_==RowBufPolicy::CRAFT){
    int index=GetQueueIndex(cmd.Rank(),cmd.Bankgroup(),cmd.Bank());

    if(timeout_ticking[index] && timeout_counter[index] > 0){
        if(cmd.Row() != issued_cmd[index].Row()){
            // Conflict: timeout too long, de-escalate with cost-aware fixed step
            auto& state = craft_state_[index];
            state.timeout_value = std::max(state.timeout_value - craft_conflict_step_, CRAFT_T_MIN);
            state.reopen_streak = 0;
            timeout_counter[index] = 0;  // trigger immediate precharge
            simple_stats_.Increment("craft_conflicts");
            simple_stats_.Increment("craft_deescalations");
        }
        else{
            // Row hit during timeout: reset timer, keep row open
            timeout_counter[index] = craft_state_[index].timeout_value;
            timeout_ticking[index] = false;
        }
    }
}
```

修改为：

```cpp
else if(top_row_buf_policy_==RowBufPolicy::CRAFT){
    int index=GetQueueIndex(cmd.Rank(),cmd.Bankgroup(),cmd.Bank());

    if(timeout_ticking[index] && timeout_counter[index] > 0){
        if(cmd.Row() != issued_cmd[index].Row()){
            // Conflict: de-escalate with queue-depth scaled step
            auto& state = craft_state_[index];
            int step = craft_conflict_step_;
            if (CRAFT_QDSD_ENABLED) {
                int pending = static_cast<int>(queues_[index].size());
                int scale = std::min(pending, CRAFT_QDSD_SCALE_CAP);
                step = craft_conflict_step_ * scale;
                simple_stats_.AddValue("craft_qdsd_scale", scale);
            }
            state.timeout_value = std::max(state.timeout_value - step, CRAFT_T_MIN);
            state.reopen_streak = 0;
            timeout_counter[index] = 0;  // trigger immediate precharge
            simple_stats_.Increment("craft_conflicts");
            simple_stats_.Increment("craft_deescalations");
        }
        else{
            // Row hit during timeout: reset timer, keep row open
            timeout_counter[index] = craft_state_[index].timeout_value;
            timeout_ticking[index] = false;
        }
    }
}
```

**注意**：`queues_[index].size()` 此时已包含刚 push_back 的 `cmd`（`AddCommand` 在 push_back 之后才进入此分支），因此 `pending >= 1` 始终成立，`scale >= 1`。当只有这一个 conflict 命令时 `pending=1`，`scale=1`，行为退化为原始 CRAFT。

### 3.3 `dramsim3/src/simple_stats.cc`

新增统计计数器：

```cpp
InitStat("craft_qdsd_scale", "histogram", "CRAFT QDSD scale factor distribution");
```

如果 `AddValue` 对 histogram 类型不适用，可改用：

```cpp
InitVecStat("craft_qdsd_scale_dist", 5, "CRAFT QDSD scale factor distribution (0-4)");
// 在 AddCommand 中: simple_stats_.IncrementVec("craft_qdsd_scale_dist", scale);
```

---

## 4. 修改文件总结

| 文件 | 修改量 | 性质 |
|------|--------|------|
| `dramsim3/src/command_queue.h` | ~3 行 | 新增 QDSD 常量 |
| `dramsim3/src/command_queue.cc` | ~8 行 | AddCommand() CRAFT 分支增加 queue depth 缩放逻辑 |
| `dramsim3/src/simple_stats.cc` | ~2 行 | 注册 QDSD 统计计数器 |

总计约 **13 行** 修改的 C++ 代码。

---

## 5. 实验设计

### 5.1 实验配置

| 配置名 | 策略 | QDSD | 说明 |
|--------|------|------|------|
| `CRAFT_1c` | CRAFT | 关闭 | Baseline CRAFT（当前实现） |
| `CRAFT_QDSD_1c` | CRAFT | 开启（cap=4） | 本方案：Queue-Depth Scaled De-escalation |

### 5.2 消融与敏感性实验

#### 5.2a QDSD_SCALE_CAP 敏感性

| 变体 | QDSD_SCALE_CAP | 说明 |
|------|---------------|------|
| `CRAFT_QDSD_C2_1c` | 2 | 保守：最多 2 倍降阶 |
| `CRAFT_QDSD_C4_1c` | 4 | 默认方案 |
| `CRAFT_QDSD_C8_1c` | 8 | 激进：最多 8 倍降阶 |
| `CRAFT_QDSD_INF_1c` | 无上限 (=queue_size) | 极端：完全无封顶 |

通过编译时修改 `CRAFT_QDSD_SCALE_CAP` 或运行时 constexpr 控制。

#### 5.2b 与 CRAFT_RW 的正交组合

| 变体 | RW 区分 | QDSD | 说明 |
|------|---------|------|------|
| `CRAFT_1c` | 否 | 否 | 纯 baseline |
| `CRAFT_RW_1c` | 是 | 否 | 仅 RW 区分 |
| `CRAFT_QDSD_1c` | 否 | 是 | 仅 QDSD |
| `CRAFT_RW_QDSD_1c` | 是 | 是 | 完整组合 |

这组实验验证 RW 和 QDSD 是否正交叠加受益。

### 5.3 Benchmark 分类与预期

根据 bank contention 特征分类：

| 类别 | 特征 | 代表 benchmark | 预期影响 |
|------|------|---------------|----------|
| 高 bank contention + 低局部性 | 多行交替访问，queue 经常有多个不同行请求 | mcf, omnetpp | **受益最大**：高 queue depth 触发大步降阶，快速释放 bank |
| 高 bank contention + 高局部性 | queue 深但同行请求多 | lbm | 影响小：同行请求重置 timer 而非触发 conflict |
| 低 bank contention | queue 通常只有 1-2 个请求 | fotonik3d | **影响极小**：scale=1-2，行为接近原始 CRAFT |
| 图算法 (irregular) | 大量随机访问，bank queue 爆满 | PageRank, hashjoin | **潜在受益**：极端 contention 场景下快速降阶减少排队延迟 |

### 5.4 评估指标

#### 主要指标
- **IPC**：每个 benchmark 单独报告 IPC 改善（相对 CRAFT baseline）
- **加权 GEOMEAN IPC**：使用 `benchmarks_selected.tsv` 中的 weight 计算

#### 辅助 DRAM 指标
- **Row Buffer Hit Rate**: `(num_read_row_hits + num_write_row_hits) / (num_read_cmds + num_write_cmds)`
- **ACT 次数**: `num_act_cmds`（频繁降阶 → 更多 precharge → 更多 ACT）
- **平均读延迟**: `average_read_latency`（减少排队等待 → 读延迟降低）
- **Bank 利用率**: 通过 `num_act_cmds` / `num_pre_cmds` 间接衡量 bank 周转率

#### CRAFT 内部指标
- **craft_qdsd_scale_dist**: scale factor 分布 — 直观展示各 benchmark 的 bank contention 程度
- **craft_conflicts**: conflict 总次数（QDSD 不改变 conflict 次数，只改变每次降阶幅度）
- **craft_timeout_value_sum / craft_timeout_precharges**: 平均 timeout 值（QDSD 应使高 contention 负载的平均 timeout 更低）
- **craft_deescalations**: 与 craft_conflicts 相同（验证一致性）

#### 关键对比分析
- 对比 `CRAFT_1c` vs `CRAFT_QDSD_1c` 的 `craft_qdsd_scale_dist`：高 contention 负载应有更多 scale=3-4 事件
- 对比 `average_read_latency`：QDSD 应在高 contention 负载上降低读延迟
- 对比 `craft_timeout_value_sum`：QDSD 后高 contention 负载的平均 timeout 应更低
- 对比 `num_act_cmds`：QDSD 可能略增 ACT（更快关行），但读延迟改善应超过 ACT 开销

---

## 6. 构建与运行

```bash
# 1. 修改代码（按 Section 3 的描述修改 3 个文件）

# 2. 构建 DRAMSim3
cd /root/data/smartPRE/dramsim3 && mkdir -p build && cd build && cmake .. && make -j8

# 3. 构建 ChampSim
cd /root/data/smartPRE/champsim-la
python3 config.sh champsim_config.json
make -j8

# 4. 运行 CRAFT baseline（如果尚未跑过）
export LD_LIBRARY_PATH=/root/data/smartPRE/dramsim3:$LD_LIBRARY_PATH
TRACE_ROOT=/root/data/Trace/LA scripts/run_selected_slices.sh
# label: CRAFT_1c

# 5. 运行 CRAFT_QDSD（代码修改后重新构建）
cd /root/data/smartPRE/dramsim3/build && make -j8
cd /root/data/smartPRE/champsim-la && make -j8
TRACE_ROOT=/root/data/Trace/LA scripts/run_selected_slices.sh
# label: CRAFT_QDSD_1c

# 6. 对比结果
python3 scripts/compare_ipc.py results/CRAFT_1c results/CRAFT_QDSD_1c
```

---

## 7. 验证步骤

### 7.1 正确性验证

1. **编译测试**: DRAMSim3 和 ChampSim 编译通过，无 warning
2. **Smoke test**: 运行短 trace（warmup=1M, sim=5M），确认：
   - `craft_qdsd_scale_dist[1]` > 0（至少有 scale=1 事件，即低 contention conflict）
   - `craft_conflicts` > 0（QDSD 不影响 conflict 检测本身）
   - `sum(craft_qdsd_scale_dist) == craft_conflicts`（每次 conflict 都产生一个 scale 记录）
3. **不变量验证**: `timeout_value` 始终在 `[T_MIN, T_MAX] = [50, 3200]` 范围内
4. **退化验证**: 将 `CRAFT_QDSD_ENABLED` 设为 `false`，验证行为与原始 CRAFT 完全一致

### 7.2 行为方向验证

1. **高 contention 负载**（mcf, omnetpp）：`craft_qdsd_scale_dist` 中 scale=3-4 占比显著（>20%），平均 timeout 低于 CRAFT baseline
2. **低 contention 负载**（fotonik3d）：`craft_qdsd_scale_dist` 集中在 scale=1-2，平均 timeout 与 CRAFT baseline 接近
3. **图算法**（PageRank）：`craft_qdsd_scale_dist` 中 scale=4 (capped) 占比最高，timeout 快速收敛到 T_MIN 附近

### 7.3 性能方向预期

- **高 bank contention + 低局部性**（mcf, omnetpp）: IPC 提升（快速降阶减少多请求排队延迟）
- **低 bank contention**（fotonik3d）: IPC 变化极小（scale 大多为 1，退化为原始行为）
- **高局部性负载**（lbm）: IPC 变化小（冲突少，QDSD 少有触发机会）
- **图算法/hashjoin**: IPC 可能有显著提升（极端 contention 场景最受益）
- **整体**: GEOMEAN IPC 改善或持平。QDSD 是 "不会更差" 的安全优化——低 contention 时退化为 baseline

### 7.4 风险分析

| 风险 | 描述 | 缓解措施 |
|------|------|----------|
| 过度降阶 | 高 queue depth 时步长过大，timeout 频繁跌到 T_MIN，增加 wrong precharge | SCALE_CAP=4 封顶；escalation 有指数退避恢复 |
| 同行命令膨胀 pending_count | queue 中多数是同行 hit 命令，但 pending_count 仍高，导致不必要的大步降阶 | conflict 本身只在 diff_row 时触发，同行命令走 row-hit 路径不受影响 |
| 与 RW 区分交互 | QDSD × RW 组合可能导致 read conflict 在高 contention 下步长过大 | 正交组合实验验证；必要时对组合步长加额外 cap |

---

## 8. 结果呈现

### 8.1 主结果表

每个 benchmark 报告以下数据：

| Benchmark | CRAFT IPC | CRAFT_QDSD IPC | IPC 改善(%) | Avg Read Lat | Avg Timeout | scale=1(%) | scale=2(%) | scale=3(%) | scale=4(%) |
|-----------|-----------|----------------|-------------|-------------|------------|-----------|-----------|-----------|-----------|

### 8.2 SCALE_CAP 敏感性表

| Benchmark | cap=2 IPC | cap=4 IPC | cap=8 IPC | uncapped IPC |
|-----------|-----------|-----------|-----------|-------------|

### 8.3 正交组合表

| Benchmark | CRAFT | +RW | +QDSD | +RW+QDSD |
|-----------|-------|-----|-------|----------|
| (IPC 值)  |       |     |       |          |

### 8.4 关键图表

1. **Per-benchmark IPC 对比柱状图**: CRAFT vs CRAFT_QDSD，按 bank contention 程度分组
2. **QDSD scale 分布堆叠图**: 每个 benchmark 的 scale factor 分布（展示 contention 特征差异）
3. **平均 timeout 对比**: 按 benchmark 展示 CRAFT vs CRAFT_QDSD 的平均 timeout 值
4. **读延迟改善散点图**: x 轴 = 平均 queue depth（可从 scale 分布推算），y 轴 = 读延迟改善百分比。预期呈正相关
5. **正交组合贡献图**: 2×2 组合（RW × QDSD）的 GEOMEAN IPC，展示各优化的独立和叠加贡献
