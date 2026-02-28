# GS_ext_timeout_only 消融实验方案

**日期：** 2026-02-27
**目的：** 分离 "扩展 timeout 候选集" 和 "RE-driven escalation" 两个变量的各自贡献
**前置：** Phase 1 实验结果分析（`phase1_results_analysis.md`）

---

## 1. 背景与动机

Phase 1 的 8 个 escalation 配置同时引入了两个变更：

1. **扩展 timeout 候选集**：从 7 个 `{50, 100, 150, 200, 300, 400, 800}` 扩展到 9 个 `{50, 100, 150, 200, 300, 400, 800, 1600, 3200}`
2. **RE-driven Escalation**：连续 RE 命中 N 次后强制提升 timeout 并释放 RE 条目

Phase 1 中 12 个获益 benchmark（如 Triangle-Counting +0.77%、omnetpp +0.64%）的改善可能完全来自 shadow simulation 利用更大的候选集自主选择更优 timeout，而非 escalation 机制。本消融实验通过仅扩展候选集、禁用 escalation，定量回答这一问题。

### 1.1 实验预期

| 场景 | 结论 | 后续方向 |
|------|------|---------|
| `ext_timeout_only` ≈ 最佳 escalation config | Escalation 无增量价值，改善来自扩展候选集 | 放弃 escalation，探索进一步扩展候选集或增大 RE Store |
| `ext_timeout_only` > 最佳 escalation config | Escalation 是 net harmful，扩展候选集独立有益 | 同上，且可直接采用扩展候选集作为改进 |
| `ext_timeout_only` < 最佳 escalation config | Escalation 有真实增量贡献 | 继续 Phase 2 探索 open-page escalation |

---

## 2. 代码修改

### 当前代码状态

- `GS_TIMEOUT_COUNT` 已为 9，候选集已包含 1600c 和 3200c
- Escalation 代码**无预处理器宏保护**，始终编译和执行
- 需新增 `GS_NO_ESCALATION` 宏来条件禁用 escalation 逻辑

### 修改涉及 3 个文件

| 文件 | 改动类型 |
|------|---------|
| `dramsim3/src/controller.cc` | 用 `#ifndef GS_NO_ESCALATION` 包裹 escalation 触发逻辑 |
| `dramsim3/src/command_queue.cc` | 用 `#ifndef GS_NO_ESCALATION` 包裹 de-escalation 逻辑和 `GetCurrentTimeout` 中的 escalation 分支 |
| `dramsim3/Makefile` | 支持 `EXTRA_CXXFLAGS` 传递 |

### 2.1 `controller.cc` — 禁用 escalation 触发

修改位置：ClockTick 中 RE 命中分支（约第 142-183 行）。

用 `#ifndef GS_NO_ESCALATION` 包裹 `consecutive_hits++` 及后续 escalation 触发块：

```cpp
if (auto* re_entry = cmd_queue_.RE_FindEntry(
        cmd.Rank(), cmd.Bankgroup(), cmd.Bank(), cmd.Row())) {
    simple_stats_.Increment("gs_re_hits");
    auto& detect = cmd_queue_.re_detect_state_[i];
    if (!detect.pending_re_hit_check) {
        detect.pending_re_hit_check = true;
        detect.re_hit_row = cmd.Row();
    }

#ifndef GS_NO_ESCALATION
    // ===== Escalation trigger =====
    re_entry->consecutive_hits++;
    if (re_entry->consecutive_hits >= GS_ESC_HIT_THRESHOLD) {
        auto& esc = cmd_queue_.bank_escalation_state_[i];
        auto& shadow = cmd_queue_.gs_shadow_state_[i];
        int current_idx = esc.escalated ? esc.escalated_timeout_idx
                                        : shadow.curr_timeout_idx;
        int target_idx = std::min(current_idx + GS_ESC_STEP,
                                  GS_TIMEOUT_COUNT - 1);
        if (!esc.escalated) {
            esc.original_timeout_idx = shadow.curr_timeout_idx;
        }
        esc.escalated = true;
        esc.escalated_timeout_idx = target_idx;
        esc.escalation_cycle = clk_;
        esc.decay_counter = 0;

        cmd_queue_.RE_RemoveEntry(cmd.Rank(), cmd.Bankgroup(),
                                   cmd.Bank(), cmd.Row());
        simple_stats_.Increment("gs_re_escalation_triggers");
        simple_stats_.Increment("gs_re_freed_by_escalation");
        simple_stats_.IncrementVec("gs_escalated_timeout_dist", target_idx);
    }
    // ===== End escalation trigger =====
#endif

    cmd_queue_.timeout_counter[i] = cmd_queue_.GetCurrentTimeout(i);
    continue;
}
```

**效果：** 定义 `GS_NO_ESCALATION` 后，RE 命中分支行为恢复为原始 GS：命中 → 重置 timeout → 保留 RE 条目。不跟踪 `consecutive_hits`，不触发 escalation，不释放 RE 条目。

### 2.2 `command_queue.cc` — 禁用 de-escalation 和 escalation 状态查询

#### (a) `GetCurrentTimeout()` — 移除 escalation 覆盖

```cpp
int CommandQueue::GetCurrentTimeout(int queue_idx) const {
#ifndef GS_NO_ESCALATION
    if (bank_escalation_state_[queue_idx].escalated) {
        return GS_TIMEOUT_VALUES[bank_escalation_state_[queue_idx].escalated_timeout_idx];
    }
#endif
    return GS_TIMEOUT_VALUES[gs_shadow_state_[queue_idx].curr_timeout_idx];
}
```

#### (b) `GS_ArbitrateTimeout()` — 移除 de-escalation 逻辑

在仲裁循环末尾的 de-escalation 块和 escalated bank 计数块外围包裹：

```cpp
#ifndef GS_NO_ESCALATION
    // ===== De-escalation Logic =====
    auto& esc = bank_escalation_state_[q];
    if (esc.escalated) {
        // ... conflict-driven / decay-driven demotion ...
    }
#endif
```

```cpp
#ifndef GS_NO_ESCALATION
    int escalated_count = 0;
    for (int q = 0; q < num_queues_; q++) {
        if (bank_escalation_state_[q].escalated) escalated_count++;
    }
    simple_stats_.IncrementBy("gs_re_escalation_active_banks", escalated_count);
#endif
```

### 2.3 `dramsim3/Makefile` — 支持额外编译选项

在 `CXXFLAGS` 行末尾追加 `$(EXTRA_CXXFLAGS)`：

```makefile
CXXFLAGS=-Wall -O3 -fPIC -std=c++11 $(INC) -DFMT_HEADER_ONLY=1 -g $(EXTRA_CXXFLAGS)
```

---

## 3. 构建与执行

### 3.1 构建

```bash
# 构建 DRAMSim3（禁用 escalation，保留 9 候选 timeout）
cd /root/data/smartPRE/dramsim3
make clean
make EXTRA_CXXFLAGS="-DGS_NO_ESCALATION" -j8

# 构建 ChampSim（链接新的 libdramsim3.so）
cd /root/data/smartPRE/champsim-la
make clean
python3 config.sh champsim_config.json
make -j8
```

### 3.2 验证构建正确性

编译完成后，在单个 benchmark（如 spec06/lbm/ref slice 0）上快速验证：

```bash
export LD_LIBRARY_PATH=/root/data/smartPRE/dramsim3:$LD_LIBRARY_PATH
# 运行短测试，检查输出中：
# 1. gs_re_escalation_triggers == 0（escalation 未触发）
# 2. gs_re_hits > 0（RE 被动拦截仍在工作）
# 3. gs_timeout_dist 向量长度为 9（候选集已扩展）
```

### 3.3 实验执行

```bash
cd /root/data/smartPRE/champsim-la
BINARY=bin/champsim \
MANIFEST=benchmarks_selected.tsv \
RESULTS_ROOT=results/GS_ext_timeout_only \
bash scripts/run_benchmarks.sh
```

使用与 Phase 1 相同的 benchmarks_selected.tsv（62 benchmarks）、相同的 warmup/simulation 指令数。

---

## 4. 分析计划

### 4.1 生成汇总与对比

```bash
# 汇总 IPC
python3 scripts/summarize_ipc.py --results results/GS_ext_timeout_only

# 对比 1: vs GS baseline（量化扩展候选集的独立贡献）
python3 scripts/compare_ipc.py \
    --a results/GS_1c --b results/GS_ext_timeout_only \
    --a-label GS --b-label ext_only

# 对比 2: vs open_page（量化扩展候选集后 GS 对 open_page 的优势）
python3 scripts/compare_ipc.py \
    --a results/open_page_1c --b results/GS_ext_timeout_only \
    --a-label open --b-label ext_only

# 对比 3: vs 最佳 escalation config（量化 escalation 的增量贡献）
python3 scripts/compare_ipc.py \
    --a results/GS_ext_timeout_only --b results/GS_esc_N12_S2 \
    --a-label ext_only --b-label N12S2
```

### 4.2 关键分析维度

| 维度 | 对比 | 判断标准 |
|------|------|---------|
| 扩展候选集独立贡献 | ext_only vs GS_1c | GEOMEAN 和 per-benchmark delta |
| Escalation 增量贡献 | ext_only vs 最佳 esc | 如果 esc 更好 → escalation 有贡献 |
| 对 open_page 优势变化 | ext_only vs open_page | 是否优于 GS_1c 的 +1.53% |
| Winner 集归因 | ext_only 在 Phase 1 winner 集上的表现 | 如果与 esc 一致 → winner 来自扩展候选集 |
| Loser 集归因 | ext_only 在 Phase 1 loser 集上的表现 | 如果无 regression → loser 来自 escalation |

### 4.3 重点关注 Benchmark

**Phase 1 winner 集**（判断改善是否来自扩展候选集）：
- crono/Triangle-Counting/higgs（esc 最佳 +0.95%）
- spec06/omnetpp/ref（esc 最佳 +0.92%）
- ligra/Triangle/soc-pokec（esc 最佳 +0.81%）
- crono/PageRank/roadNet-CA（esc 最佳 +0.56%）
- crono/DFS/higgs（esc 最佳 +0.62%）

**Phase 1 loser 集**（判断 regression 是否来自 escalation）：
- ligra/PageRank/soc-pokec（esc 最差 -1.50%）
- crono/PageRank/higgs（esc 最差 -1.40%）
- hashjoin/hj-2-NPO_st（esc 最差 -1.05%）

**lbm-like 集**（判断扩展候选集对目标受益者的独立作用）：
- spec06/lbm/ref、spec17/lbm/ref
- spec06/sphinx3/ref

### 4.4 DRAM 统计对比

对代表性 benchmark（lbm、hpcc/RandAcc、PageRank/soc-pokec）提取 ddr.json 中的：
- `gs_timeout_dist`：验证 shadow simulation 是否自主选择了 1600c/3200c
- `gs_re_hits`：验证 RE 被动拦截恢复到 baseline 水平
- `gs_re_escalation_triggers`：确认为 0
- `gs_timeout_correct` / `gs_timeout_wrong`：对比 TO 准确率变化
- `num_on_demand_pres`：对比 on-demand precharge 变化
