# 行缓冲管理策略存储开销与硬件可行性对比

本文档对比 GS、FAPS、DYMPL、RL_PAGE 四种行缓冲管理策略的 per-channel 存储开销和硬件可行性。

## DRAM 配置基准

基于 `DDR5_64GB_4ch_4800.ini` 配置：

| 参数 | 值 |
|------|-----|
| Protocol | DDR5-4800 |
| Ranks per Channel | 1 |
| Bank Groups | 8 |
| Banks per Group | 4 |
| **Banks per Channel** | **32** |
| Rows per Bank | 65,536 (16-bit row address) |
| Columns per Bank | 2,048 |
| Bus Width | 32 bits |
| tRP / tRCD | 40 cycles |

---

## 1. GS (Global Scoreboarding)

GS 由三部分组成：Timeout Speculative Precharge、Shadow Simulation、Row Exclusion。

### 1.1 Per-bank 状态 (×32 banks)

| 组件 | 位宽 | 说明 |
|------|------|------|
| `curr_timeout_idx` | 3 bits | 7 个候选 timeout 的索引 (50/100/150/200/300/400/800) |
| `hits[7]` | 7×10 = 70 bits | 每个候选 timeout 的命中计数 (30000 周期内 ≤1024) |
| `conflicts[7]` | 7×10 = 70 bits | 每个候选 timeout 的冲突计数 |
| `next_cas_state[7]` | 7×2 = 14 bits | ACT 阶段定性结果 (NONE/HIT/MISS/CONFLICT) |
| `last_cas_cycle` | 15 bits | 时间戳，用于 gap 计算 (需覆盖 30000 周期) |
| `prev_open_row` | 16 bits | 上一次打开的行号 |
| `timeout_counter` | 10 bits | 当前倒计时 (max 800) |
| `timeout_ticking` | 1 bit | 计时中标志 |
| RE detect: `prev_row` | 16 bits | 上次被 timeout 关闭的行 |
| RE detect: `prev_closed_by_timeout` | 1 bit | timeout 关闭标志 |
| **Per-bank 小计** | **216 bits (27 B)** | |
| **32 banks 合计** | **6,912 bits (864 B)** | |

> Shadow Simulation 的 7 组 hits/conflicts 计数器占 per-bank 存储的 64.8%。

### 1.2 Per-channel 共享结构

| 组件 | 位宽 | 说明 |
|------|------|------|
| RE Store: 64 entries × (5b bank_id + 16b row + 1b conflict) | 64×22 = 1,408 bits (176 B) | CAM 结构，per-channel 所有 bank 共享 |
| Arbitration period counter | 15 bits | 计数到 30000 |
| **Shared 小计** | **1,423 bits (178 B)** | |

### 1.3 GS 总计

**Per Channel: 8,335 bits = 1,042 bytes ≈ 1.02 KB**

---

## 2. FAPS (Feedback-directed Adaptive Page Scheme)

FAPS 使用 per-bank 2-bit 饱和计数器 FSM，基于 per-bank access-count epoch (1000 次访问) 动态切换 open/close-page 模式。

### 2.1 Per-bank 状态 (×32 banks)

| 组件 | 位宽 | 说明 |
|------|------|------|
| FSM state | 2 bits | 4 状态饱和计数器 (0,1=CLOSE; 2,3=OPEN) |
| Epoch access counter | 10 bits | 计数到 1000 后触发 epoch 评估 |
| Hit counter | 10 bits | open-page 实际命中 / close-page 潜在命中 (复用) |
| Hit Register: `last_accessed_row` | 16 bits | close-page bank 检测潜在命中 |
| **Per-bank 小计** | **38 bits (4.75 B)** | |
| **32 banks 合计** | **1,216 bits (152 B)** | |

### 2.2 Per-channel 共享结构

无。FAPS 的所有状态均为 per-bank，无需 channel 级共享结构。

### 2.3 FAPS 总计

**Per Channel: 1,216 bits = 152 bytes**

---

## 3. DYMPL (Dynamic ML Predictor)

DYMPL 使用 7 特征感知器预测 open/close 决策，配合 PRT (Page Row Table) 和 BRT (Bank Row Table) 提取特征。

### 3.1 Per-channel 共享 — Weight Tables

| Weight Table | 条目数 | 位宽/条目 | 总 bits | 索引特征 |
|--------------|--------|-----------|---------|----------|
| `wt_page_util` | 16 | 4 | 64 | 页面空间局部性 [0,15] |
| `wt_page_hot` | 32 | 4 | 128 | 页面访问频率 [0,31] |
| `wt_page_rec` | 16 | 4 | 64 | 页面时间局部性 [0,15] |
| `wt_col_stride` | 16 | 4 | 64 | 列步长 [0,15] |
| `wt_page_hitcnt` | 16 | 4 | 64 | 页面命中/miss 趋势 [-8,+7] |
| `wt_bank_rec` | 16 | 4 | 64 | Bank 时间局部性 [0,15] |
| `wt_bank_hitcnt` | 256 | 4 | 1,024 | Bank 命中趋势 [0,255] |
| **Weight 小计** | **368** | | **1,472 bits (184 B)** | |

### 3.2 Per-channel 共享 — PRT (Page Row Table)

16 sets × 32 ways = 512 entries，set-associative 结构。

| PRT Entry 字段 | 位宽 | 说明 |
|----------------|------|------|
| `row_id` (tag) | 16 bits | 行地址标签 |
| `last_col_id` | 4 bits | 上次访问的列 |
| `utilization` | 4 bits | 空间局部性 [0,15] |
| `hotness` | 5 bits | 生命周期访问频率 [0,31] |
| `recency` | 4 bits | 时间局部性排序 [0,15] |
| `stride` | 4 bits | 列步长 [0,15] |
| `hit_count` | 4 bits | 命中/miss 趋势 (signed [-8,+7]) |
| `valid` | 1 bit | 有效位 |
| LRU counter | 5 bits | 32-way LRU 排序位 |
| **Per-entry 小计** | **47 bits** | |
| **512 entries 合计** | **24,064 bits (3,008 B)** | |

### 3.3 Per-bank 状态 (×32 banks)

| 组件 | 位宽 | 说明 |
|------|------|------|
| BRT: `recency` | 4 bits | Bank-level 时间局部性 |
| BRT: `hit_count` | 8 bits | Bank-level 命中趋势 |
| PredictionState: `valid` + `predicted_open` | 2 bits | 延迟训练用 |
| PredictionState: `sum` | 7 bits | 感知器求和 [-56, +49] |
| PredictionState: 7 feature indices | 33 bits | 4+5+4+4+4+4+8 |
| `predicted_row` | 16 bits | 上次预测时的行号 |
| **Per-bank 小计** | **70 bits (8.75 B)** | |
| **32 banks 合计** | **2,240 bits (280 B)** | |

### 3.4 DYMPL 总计

| 组件 | Bits | Bytes |
|------|------|-------|
| Weight Tables | 1,472 | 184 |
| PRT (512 entries) | 24,064 | 3,008 |
| Per-bank state (32 banks) | 2,240 | 280 |
| **Total** | **27,776** | **3,472 ≈ 3.39 KB** |

> PRT 占总存储的 **86.6%**，是 DYMPL 的存储瓶颈。

---

## 4. RL_PAGE (Reinforcement Learning — SARSA + CMAC)

RL_PAGE 使用 SARSA 在线学习 + CMAC 函数逼近，在 cluster-end 做 binary 决策 (CLOSE/KEEP_OPEN)。

### 4.1 Per-channel 共享 — CMAC Tables

| 组件 | 位宽 | 说明 |
|------|------|------|
| CMAC tables: 8 tilings × 256 entries × 16-bit | 32,768 bits (4,096 B) | Q8.8 定点数权重，所有 bank 共享 |
| PRNG (LFSR) | 32 bits | ε-greedy exploration 用 |
| **Shared 小计** | **32,800 bits (4,100 B)** | |

CMAC tiling offsets (8×5×4 bits = 160 bits) 为常量，可硬连线实现，不计入存储。

### 4.2 Per-bank 状态 (×32 banks)

| 组件 | 位宽 | 说明 |
|------|------|------|
| `valid` | 1 bit | 前一决策是否存在 |
| State: s1(4)+s2(4)+s3(3)+s4(3)+s5(3) | 17 bits | 5 维状态向量 |
| `action` | 1 bit | 上次动作 (CLOSE/KEEP_OPEN) |
| `row` | 16 bits | 上次决策时的行号 |
| **Per-bank 小计** | **35 bits (4.4 B)** | |
| **32 banks 合计** | **1,120 bits (140 B)** | |

### 4.3 RL_PAGE 总计

| 组件 | Bits | Bytes |
|------|------|-------|
| CMAC tables (8×256×16b) | 32,768 | 4,096 |
| PRNG | 32 | 4 |
| Per-bank state (32 banks) | 1,120 | 140 |
| **Total** | **33,920** | **4,240 ≈ 4.14 KB** |

> CMAC tables 占总存储的 **96.6%**，是 RL_PAGE 的存储瓶颈。

---

## 5. 总览对比

### 5.1 存储开销

| | **GS** | **FAPS** | **DYMPL** | **RL_PAGE** |
|--|--------|----------|-----------|-------------|
| Per-bank 状态 | 216 bits (27 B) | 38 bits (4.75 B) | 70 bits (8.75 B) | 35 bits (4.4 B) |
| Per-channel 共享 | 1,423 bits (178 B) | 0 | 25,536 bits (3,192 B) | 32,800 bits (4,100 B) |
| **Channel 总计** | **8,335 bits (1.02 KB)** | **1,216 bits (152 B)** | **27,776 bits (3.39 KB)** | **33,920 bits (4.14 KB)** |
| 存储瓶颈 | Shadow 计数器 (82.9%) | 无 (均匀分布) | PRT (86.6%) | CMAC tables (96.6%) |
| 相对 GS | 1× (基线) | **0.15×** | **3.3×** | **4.1×** |

### 5.2 计算复杂度

| | **GS** | **FAPS** | **DYMPL** | **RL_PAGE** |
|--|--------|----------|-----------|-------------|
| 决策时计算 | 1 次比较 (timeout 到期) | 无额外计算 (FSM 直接决定) | 7 次查表 + 6 次加法 | 16 次哈希 + 16 次查表 + 8 次加法 + 1 次比较 |
| 更新/训练计算 | 7 次并行比较 (shadow sim) | 1 次加法 + 1 次比较 (epoch 结束) | 7 次查表 + 7 次 ±1 权重更新 | 8 次查表 + 8 次乘加 (SARSA TD update) |
| 触发频率 | Shadow: 每次 ACT/CAS; 仲裁: 每 30000 周期 | Epoch 评估: 每 1000 次 bank 访问 | Predict: cluster-end; Train: 每次 ACT | Decide: cluster-end; Update: 每次 ACT |

### 5.3 关键路径影响

| 方案 | 影响 | 分析 |
|------|------|------|
| **GS** | 低 | Timeout 递减是后台操作；Shadow simulation 与实际调度解耦；仲裁每 30000 周期一次，均摊开销极小 |
| **FAPS** | 极低 | FSM 状态直接决定策略，无需在线计算；Epoch 评估在第 1000 次访问时触发，均摊开销极小 |
| **DYMPL** | 中 | Predict 在 cluster-end 触发，需要 PRT tag 查找 (16-set × 32-way 组相联) + 7 次加法；可在 timeout 窗口内流水线化 |
| **RL_PAGE** | 中偏高 | Decide 在 cluster-end 触发，需要对 2 个 action 各做 8 次哈希+查表+求和再比较；SARSA 更新可延迟到下次 ACT 时完成 |

### 5.4 硬件结构需求

| 方案 | 需要的特殊硬件 | 复杂度评级 |
|------|---------------|-----------|
| **GS** | 64-entry CAM (RE Store), 7 组并行比较器, 计数器阵列 | 中 |
| **FAPS** | 仅计数器和比较器，标准 CMOS 逻辑 | **极低** |
| **DYMPL** | 512-entry set-associative table (PRT), 7-input 加法器, LRU 逻辑 | **高** |
| **RL_PAGE** | 4 KB SRAM (CMAC), 哈希单元 ×8, 定点乘法器, LFSR | **高** |

### 5.5 综合评估

| 维度 | **GS** | **FAPS** | **DYMPL** | **RL_PAGE** |
|------|--------|----------|-----------|-------------|
| 存储开销 | 中 (1.02 KB) | **极小 (152 B)** | 大 (3.39 KB) | 最大 (4.14 KB) |
| 计算复杂度 | 低 | **极低** | 中 | 中高 |
| 关键路径风险 | 低 | **极低** | 中 | 中高 |
| 在线学习复杂度 | 无 (静态仲裁) | 无 (阈值 FSM) | 感知器训练 (简单) | SARSA+CMAC (较复杂) |
| 适应速度 | 慢 (30K 周期) | 中 (1K 访问) | 快 (每次 ACT 训练) | 中 (需 exploration) |
| 硬件实现难度 | ★★☆☆☆ | ★☆☆☆☆ | ★★★★☆ | ★★★★☆ |

---

## 6. 关键结论

1. **FAPS 是硬件最友好的方案** — 存储仅 152 B/channel，是 GS 的 1/7、DYMPL 的 1/23、RL_PAGE 的 1/28。全部使用计数器+比较器，无需特殊存储结构 (CAM, SRAM, set-associative table)。

2. **GS 的存储开销适中但设计复杂** — 1 KB/channel 可以接受，但 RE Store 的 64-entry CAM 和 7 路 shadow simulation 的并行比较器增加了设计复杂度。Shadow simulation 占 per-bank 存储的 82.9%，如果减少候选 timeout 数量 (如从 7 降到 4)，存储可显著降低。

3. **DYMPL 的瓶颈在 PRT** — 512-entry set-associative table 占总存储 86.6%。若缩小 PRT 规模 (如 8 sets × 16 ways = 128 entries)，存储可降至约 1.1 KB/channel，但会降低行特征覆盖率，影响预测准确率。Weight tables 仅 184 B，感知器本身很轻量。

4. **RL_PAGE 存储最大但结构最规整** — 4 KB CMAC 是纯 SRAM 阵列，工艺上最友好（规整、可用编译器生成）；但 16 次哈希计算的延迟和定点 SARSA 更新所需的乘法器是实际部署的主要障碍。Per-bank 状态反而是四种方案中最小的 (35 bits)。

5. **存储与性能的 trade-off** — FAPS 存储极小但决策粒度粗 (只有 open/close 两档)；GS 和 DYMPL/RL_PAGE 在存储换取决策精度之间提供了不同的 trade-off 点。最终选择取决于性能提升是否能 justify 额外的硬件开销。
