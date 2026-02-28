# Global Scoreboarding (GS) 实现讲解

Global Scoreboarding 是一种**自适应行缓冲管理策略**，其核心思想是：通过动态调整 timeout 值来决定何时 speculative precharge（投机性预充电），在 row hit 和 row conflict 之间取得最优平衡。

整个实现由三大机制构成：

---

## 一、基于 Timeout 的 Speculative Precharge

**核心思路**：当一个 bank 的行命中簇（row hit cluster）的最后一个 CAS 发射后，开始倒计时。倒计时归零时，如果没有新请求到来，就投机性地关闭该行。

### 1.1 Timeout 启动

当一个命令被调度且该 bank 队列只剩它一个命令时，启动 timeout 倒计时：

```cpp
// command_queue.cc:146 — GetCommandToIssue()
if(queues_[queue_idx_].size()==1){
    timeout_ticking[queue_idx_] = true;
    timeout_counter[queue_idx_] = GetCurrentTimeout(queue_idx_);  // 动态timeout
    issued_cmd[queue_idx_] = cmd;
}
```

`GetCurrentTimeout()` 返回当前 bank 通过仲裁选出的 timeout 值（而非固定值）。

### 1.2 Timeout 期间的三种情况

在 `AddCommand()` 中处理新请求到达时的行为（`command_queue.cc:334`）：

| 场景 | 行为 | 代码逻辑 |
|------|------|----------|
| **(a) 计时到期，无新请求** | 发送 PRECHARGE 关闭行 | `controller.cc` ClockTick 中处理 |
| **(b) 行命中请求到达** | 重置 timeout 计数器 | `timeout_counter[index] = GetCurrentTimeout(index)` |
| **(c) 行冲突请求到达** | 立即将 counter 归零，加速关闭 | `timeout_counter[index] = 0` |

### 1.3 Timeout Precharge 的执行

在 `controller.cc:128` 的 `ClockTick()` 中，每个周期遍历所有 bank：

```
每个cycle:
  对每个bank:
    if timeout正在计时 且 counter > 0:
        counter--
    if timeout正在计时 且 counter == 0:
        构造 PRECHARGE 命令
        检查 timing 约束 (tRP)
        → 检查 Row Exclusion Store（仅GS策略）
        → 若 RE 命中，延长 timeout，跳过本次 precharge
        → 若 RE 未命中，执行 precharge，记录 detection 状态
```

---

## 二、Shadow Simulation（影子模拟）— 记分板的核心

这是 GS 最精妙的部分。系统在实际运行某个 timeout 的同时，**并行模拟**所有候选 timeout 值下的表现，构建一个"记分板"。

### 2.1 数据结构

```cpp
// command_queue.h:19
struct GSShadowState {
    int curr_timeout_idx = 1;         // 当前选用的timeout索引（默认100 cycles）
    int hits[GS_TIMEOUT_COUNT];       // 每个候选timeout的命中计数
    int conflicts[GS_TIMEOUT_COUNT];  // 每个候选timeout的冲突计数

    enum class NextCASState { NONE, HIT, MISS, CONFLICT };
    NextCASState next_cas_state[GS_TIMEOUT_COUNT];  // ACT阶段设置，CAS阶段消费

    uint64_t last_cas_cycle = 0;      // 上次CAS的时间戳
    int prev_open_row = -1;           // 上一次打开的行号
};
```

7 个候选 timeout 值：`{50, 100, 150, 200, 300, 400, 800}` cycles。

### 2.2 ACT 阶段 — 定性（`GS_ProcessACT`）

当 ACT 命令到来，对**每个**候选 timeout 值进行"如果当时 timeout 是 X，会发生什么"的推演：

**情况 A：访问不同行**（`prev_open_row != new_row`）

```cpp
int64_t gap = curr_cycle - config_.tRP - state.last_cas_cycle;
if (gap < timeout_val) {
    state.next_cas_state[t] = CONFLICT;   // timeout太长，行还开着 → 冲突
} else {
    state.next_cas_state[t] = MISS;       // timeout内行已关闭 → 正常miss
}
```

含义：如果从上次 CAS 到现在（减去 tRP）的时间 < 假设的 timeout，说明在那个 timeout 下行还没关，必须强制关闭 → 冲突。

**情况 B：访问同一行**（`prev_open_row == new_row`）

```cpp
int64_t gap = curr_cycle - state.last_cas_cycle;
if (gap < timeout_val) {
    state.next_cas_state[t] = HIT;    // timeout足够长，行还开着 → 命中
} else {
    state.next_cas_state[t] = MISS;   // timeout太短，行已关闭 → miss
}
```

含义：既然真实系统发了 ACT（说明行已关），但如果 timeout 足够长，行可能还开着，那就是一个 miss→hit 的转换。

### 2.3 CAS 阶段 — 记分（`GS_ProcessCAS`）

根据 ACT 阶段设定的 `next_cas_state` 更新计数器：

```cpp
for (int t = 0; t < GS_TIMEOUT_COUNT; t++) {
    if (state.next_cas_state[t] == CONFLICT) {
        state.conflicts[t]++;                 // 冲突 +1
    }
    else if (state.next_cas_state[t] == HIT) {
        if (t >= curr_timeout_idx) {
            state.hits[t]++;                  // 更大的timeout → 直接记hit
        } else {
            // 更小的timeout → 需要再次验证间隔
            int64_t gap = curr_cycle - state.last_cas_cycle;
            if (gap < timeout_val) {
                state.hits[t]++;
            }
        }
    }
    state.next_cas_state[t] = NONE;  // 重置
}
state.last_cas_cycle = curr_cycle;   // 更新时间戳
```

### 2.4 Timeout 仲裁（`GS_ArbitrateTimeout`）

每 **30000 个周期**（`GS_ARBITRATION_PERIOD`），对每个 bank 独立做一次仲裁：

```
对每个bank:
    T = 当前timeout
    对每个候选timeout t:
        hitsIncr[t]      = hits[t] - hits[T]
        conflictsIncr[t]  = conflicts[t] - conflicts[T]
        gain[t]           = hitsIncr[t] - conflictsIncr[t]

    best = argmax(gain[])

    if 波动足够大（max_gain vs min_gain 超过 5% 阈值）:
        切换到 best timeout
    else:
        保持不变

    清零所有 hits[] 和 conflicts[]
```

关键：**只有波动足够显著时才切换**，避免在收益差异不大的候选值之间频繁抖动。

---

## 三、Row Exclusion（行排除机制）

Row Exclusion 保护"长寿行"——那些被 timeout 关闭后又立即被重新打开的行。这主要在**请求非常稀疏**的场景下起作用。

### 3.1 Detection — 检测长寿行

在 `GS_ProcessACT()` 中：

```cpp
auto& detect = re_detect_state_[queue_idx];
if (detect.prev_closed_by_timeout && detect.prev_row == new_row) {
    // 上一行因timeout关闭，现在又要打开同一行 → 长寿行
    RE_AddEntry({rank, bankgroup, bank, new_row, false});
}
detect.prev_closed_by_timeout = false;
```

`prev_closed_by_timeout` 在 `controller.cc` 中执行 timeout precharge 时被设置为 `true`。

逻辑：如果一个行因 timeout 被关闭，紧接着下一个 ACT 又打开**同一行**，说明这个行是"长寿行"，timeout 关早了。

### 3.2 Execution — 保护长寿行

在 `controller.cc` 的 timeout precharge 路径中：

```cpp
if (cmd_queue_.RE_IsInStore(cmd.Rank(), cmd.Bankgroup(), cmd.Bank(), cmd.Row())) {
    // 行在 RE Store 中 → 延长timeout，跳过本次precharge
    cmd_queue_.timeout_counter[i] = cmd_queue_.GetCurrentTimeout(i);
    continue;
}
```

被保护的行不会因 timeout 而关闭，直到有一个**冲突请求**（访问不同行）到来时才关闭。

### 3.3 Row Exclusion Store 的组织

```
结构：std::deque<RowExclusionEntry>，64 项，per-channel 共享
替换策略：类 FIFO + 冲突优先替换
```

- **正常插入**：新条目 `push_back`（FIFO 尾部）
- **容量满时**：`pop_front`（弹出队头）
- **冲突标记**（`RE_MarkConflict`）：如果某个被 RE 保护的行引发了行冲突（本来该 timeout 关闭的行因 RE 保护而存活，结果导致了冲突请求），该条目被移到**队头**，下次满时优先被替换

这个设计很巧妙——如果 RE 的保护反而造成了冲突，说明保护决策是错误的，应该优先淘汰。

### 3.4 GS vs GS_NOHOTROW

`GS_NOHOTROW` 是消融实验变体，禁用了 Row Exclusion，其余逻辑（timeout + shadow simulation + arbitration）完全相同。通过对比两种策略的性能可以量化 Row Exclusion 的贡献。

---

## 四、整体数据流总结

```
                    ┌─────────────────────────────────────┐
                    │         Shadow Simulation            │
                    │  对7个候选timeout并行推演hits/conflicts │
  ACT到来 ─────────►│  GS_ProcessACT() → 定性              │
  CAS到来 ─────────►│  GS_ProcessCAS() → 记分              │
                    └────────────┬────────────────────────┘
                                 │ 每30000周期
                                 ▼
                    ┌────────────────────────────┐
                    │   GS_ArbitrateTimeout()    │
                    │   选出最优timeout值          │
                    └────────────┬───────────────┘
                                 │ 更新 curr_timeout_idx
                                 ▼
  ┌──────────────────────────────────────────────────┐
  │              Timeout Precharge 执行               │
  │  controller.cc: ClockTick() 每周期递减counter      │
  │  counter归零 → 检查RE Store → 执行/延迟 precharge  │
  └───────────────────────┬──────────────────────────┘
                          │
            ┌─────────────┴─────────────┐
            ▼                           ▼
   RE Store 命中:               RE Store 未命中:
   延长timeout                  执行precharge
   行继续存活                    记录detection状态
                                        │
                                        ▼
                               下次ACT到来时
                            GS_ProcessACT()检查
                            是否重新打开同一行
                                → 插入RE Store
```

整个机制形成了一个**自适应闭环**：shadow simulation 观测 → 仲裁调整 timeout → timeout 影响 precharge 决策 → 新的访问模式被 shadow simulation 继续观测。Row Exclusion 则作为一个**补丁机制**，专门处理 timeout 对稀疏长寿行的误判。

---

## 五、关键代码文件索引

| 文件 | 内容 |
|------|------|
| `dramsim3/src/common.h:10` | `RowBufPolicy` 枚举定义（含 `GS`, `GS_NOHOTROW`） |
| `dramsim3/src/command_queue.h:13-51` | GS 常量、`GSShadowState`、`RowExclusionEntry`、`RowExclusionDetectState` |
| `dramsim3/src/command_queue.cc:86-92` | GS 状态初始化 |
| `dramsim3/src/command_queue.cc:146-154` | Timeout 启动（`GetCommandToIssue`） |
| `dramsim3/src/command_queue.cc:334-357` | 新请求到达时的 timeout 处理（`AddCommand`） |
| `dramsim3/src/command_queue.cc:433-443` | CAS/ACT 命令的 shadow simulation 触发 |
| `dramsim3/src/command_queue.cc:530-545` | `GetBankFromIndex`、`GetCurrentTimeout` 辅助函数 |
| `dramsim3/src/command_queue.cc:547-605` | `GS_ProcessACT()` — ACT 阶段影子模拟 + RE 检测 |
| `dramsim3/src/command_queue.cc:607-639` | `GS_ProcessCAS()` — CAS 阶段记分 |
| `dramsim3/src/command_queue.cc:641-699` | `GS_ArbitrateTimeout()` — 动态 timeout 仲裁 |
| `dramsim3/src/command_queue.cc:703-758` | RE Store 操作（`RE_AddEntry`、`RE_IsInStore`、`RE_MarkConflict`、`RE_RemoveEntry`） |
| `dramsim3/src/controller.cc:128-163` | Timeout precharge 执行 + RE 保护逻辑 |
