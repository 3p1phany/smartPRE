# Oracle TIMEOUT 数据采集实验 - 快速开始指南

## 状态: 代码修改已完成 ✓

所有代码打桩已实现并编译通过。本指南帮助你运行实验。

---

## 编译开关

Epoch 统计功能通过 **编译开关** 控制，正常仿真不会有任何额外开销。

| 编译方式 | 说明 |
|----------|------|
| `make` | **正常模式** - 无 epoch 输出，无性能开销 |
| `./scripts/build_with_epoch_stats.sh` | **实验模式** - 启用 epoch 统计输出 |

> 开关通过 `-DENABLE_EPOCH_STATS` 编译标志控制，相关代码在 `#ifdef ENABLE_EPOCH_STATS` 块内。

---

## 1. 数据采集原理

### 采集的四个关键指标

| 指标 | 含义 | 采集位置 |
|------|------|----------|
| **Epoch ID** | 时间窗口编号 (每 10K 指令 +1) | `main.cc` |
| **PC Hash** | **访存模式特征指纹** (XOR 累积所有到达 DRAM 的访存指令 PC) | `dramsim3_wrapper.hpp` |
| **RBHR** | Row Buffer Hit Rate = row_hits / (hits + misses) | `controller.cc` |
| **IPC** | Instructions Per Cycle = epoch_instrs / epoch_cycles | `main.cc` |

> **PC Hash 采集说明**: PC 从引发 DRAM 访问的 Load/Store 指令一路传递到 DRAM 入口 (`packet->ip`)，
> 只累积实际产生 DRAM 访问的指令 PC，而非所有执行的指令。这使得 PC signature 直接反映"访存模式"。

### 输出格式
```
[EPOCH] epoch_id,pc_hash,rbhr,ipc
[EPOCH] 0,0x120000efc,0.0000,3.171672
[EPOCH] 1,0x3c,0.9905,1.271872
```

---

## 2. 已完成的代码修改

### 文件 1: `champsim-la/src/main.cc`
- 新增 epoch 统计变量和 `print_epoch_stats()` 函数
- 在主循环中添加 epoch 检查逻辑

### 文件 2: `champsim-la/inc/dramsim3_wrapper.hpp`
- 在 `add_rq()` 和 `add_wq()` 中添加 `epoch_pc_hash ^= packet->ip`
- PC hash 采集发生在 DRAM 入口，只记录实际访存指令的 PC
- 新增 `ResetEpochStats()` 函数

### 文件 3: `dramsim3/src/controller.cc`
- 新增全局变量 `dramsim3_epoch_row_hits` 和 `dramsim3_epoch_row_misses`
- 在 `UpdateCommandStats()` 中更新计数器

---

## 3. 快速验证

```bash
cd /root/data/smartPRE/champsim-la

# 设置环境变量
export LD_LIBRARY_PATH=/root/data/smartPRE/dramsim3:$LD_LIBRARY_PATH

# 运行简短测试
./bin/champsim \
    --warmup_instructions 100000 \
    --simulation_instructions 50000 \
    -loongarch \
    /root/data/Trace/LA/graph500/s16-e10/Graph500_s16-e10_0.champsim.trace.xz \
    2>&1 | grep "^\[EPOCH\]"

# 预期输出:
# [EPOCH] 0,0x120000efc,0.0000,3.171672
# [EPOCH] 1,0x3c,0.9905,1.271872
# ...
```

---

## 4. 运行完整扫描

### Step 1: 配置环境
```bash
cd /root/data/smartPRE

# 设置 trace 路径
export TRACE_ROOT=/root/data/Trace/LA

# 设置并行度 (根据你的 CPU 核数)
export JOBS=128

# 设置仿真参数
export WARMUP=20000000    # 20M warmup
export SIM=50000000       # 50M simulation
```

### Step 2: 运行扫描
```bash
./scripts/run_timeout_sweep.sh
```

这将:
- 对 8 个 trace 各运行 16 个不同的 timeout 值 (10, 20, 50, ... 3200)
- 总共 128 个任务，充分利用你的 128 线程
- 结果保存在 `results/oracle_sweep/`

### Step 3: 分析数据
```bash
python3 ./scripts/analyze_oracle_sweep.py
```

这将:
- 合成所有 epoch 数据
- 找出每个 epoch 的最佳 timeout
- 计算 PC Delta 与 timeout 变化的关联性
- 生成可视化图表

---

## 5. Timeout 值集合

实验使用 **20 个 timeout 值**，间隔 20 cycles:
```
20, 40, 60, 80, 100, 120, 140, 160, 180, 200, 220, 240, 260, 280, 300, 320, 340, 360, 380, 400
```

**设计依据**:
- 范围 20-400 cycles，覆盖常用 timeout 区间
- 间隔 20 cycles，提供足够精细的粒度来观察性能变化
- 总共 8 traces × 20 timeouts = 160 任务

---

## 6. 输出文件结构

```
results/oracle_sweep/
├── configs/                        # 自动生成的 DRAM 配置文件
│   ├── DDR4_timeout_10.ini
│   ├── DDR4_timeout_20.ini
│   └── ...
├── graph500_s16-e10/
│   ├── timeout_10/
│   │   ├── run.log                # 完整日志
│   │   ├── epoch_stats.csv        # 提取的 epoch 数据
│   │   └── .done                  # 完成标记
│   ├── timeout_20/
│   └── ...
└── tasks.txt                      # 任务列表

results/analysis/
├── graph500_s16-e10_oracle_analysis.png   # 主要可视化
├── graph500_s16-e10_ipc_comparison.png    # IPC 对比
├── graph500_s16-e10_oracle.csv            # Oracle timeout 序列
└── summary_stats.csv                       # 统计汇总
```

---

## 7. 预期可视化

生成的图表将显示:
- **X 轴**: 指令数 (0 到 50M)
- **Y1 (左, 蓝色)**: PC Signature 变化率 (归一化)
- **Y2 (右, 红色)**: Best Timeout 值 (10 到 3200 cycles)

如果 PC 变化与 timeout 变化存在关联，你会看到:
- PC 变化的波峰/波谷
- Timeout 在相应位置发生跳变

---

## 8. 关键统计指标

分析脚本会输出:
- **Correlation**: PC Delta 与 Timeout 变化的皮尔逊相关系数
- **Timeout Distribution**: 每个 timeout 值被选中的频率
- **Oracle IPC Improvement**: Oracle 比最佳静态 timeout 的性能提升

---

## 9. 故障排除

### 编译问题
```bash
# 重新编译 DRAMSim3
cd /root/data/smartPRE/dramsim3
make clean && make -j4

# 重新编译 ChampSim
cd /root/data/smartPRE/champsim-la
make -j4
```

### 缺少 GNU Parallel
```bash
apt-get install parallel
```

### Trace 文件不存在
检查 `TRACE_ROOT` 环境变量指向正确的目录。

---

## 10. 下一步

完成数据采集后，你将能够:
1. 验证 PC Signature 变化是否可以预测最优 timeout
2. 如果关联性强，设计基于 PC 预测的动态 timeout 策略
3. 如果关联性弱，考虑其他特征 (如 RBHR、Memory Intensity 等)
