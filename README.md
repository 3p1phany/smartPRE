# smartPRE

CPU/Memory 协同仿真研究平台，用于研究 DRAM 行缓冲管理（Row Buffer Management）策略。基于 ChampSim 周期精确 CPU 模拟器和 DRAMSim3 详细 DRAM 模拟器构建。

## 项目结构

```
smartPRE/
├── champsim-la/          ChampSim CPU 模拟器（LoongArch ISA）
│   ├── src/              核心仿真代码（流水线、缓存、内存控制器）
│   ├── loongarch/        LoongArch 指令解码器
│   ├── prefetcher/       预取器模块（12 种：stride, berti, spp_dev, bop 等）
│   ├── branch/           分支预测器（5 种：tage-sc-l, perceptron 等）
│   ├── btb/              分支目标缓冲（basic_btb）
│   ├── replacement/      缓存替换策略（4 种：lru, drrip, ship, srrip）
│   ├── dramsim3_configs/ 实验用 DRAM 时序配置
│   ├── scripts/          构建、运行、分析脚本
│   ├── batch_run/        批量并行任务管理
│   └── results/          仿真结果
├── dramsim3/             DRAMSim3 内存系统模拟器
│   ├── src/              模拟器核心
│   └── configs/          90+ DRAM 时序配置（DDR3/4/5, GDDR, HBM, LPDDR）
└── docs/                 研究文档
    ├── design/           机制设计文档
    ├── experiments/      实验方案（GS, FAPS-3D, GS-ML, RL-PAGE）
    ├── analysis/         分析结果
    └── references/       参考文献
```

## 研究方向

本项目聚焦 **DRAM 行缓冲管理策略**的优化——即在 row-hit cluster 结束后，何时关闭行缓冲（precharge）的决策问题。当前实现和对比的策略包括：

| 策略 | 方法 | 核心思路 |
|------|------|----------|
| **Global Scoreboarding (GS)** | Shadow simulation | 并行模拟 7 个候选 timeout 值，每 30K 周期选最优；RE Store 保护热行 |
| **FAPS-3D** | 2-bit 饱和计数器 FSM | 根据 row-buffer hit rate 在 open-page/close-page 间动态切换 |
| **DYMPL (GS-ML)** | 感知机预测 | 用轻量感知机替代 shadow simulation，消除 timeout 上限和仲裁延迟 |
| **RL-PAGE** | SARSA + CMAC | 在线强化学习，从 data bus utilization 长期奖励信号中学习预充电策略 |

详细文档见 [docs/README.md](docs/README.md)。

## 构建

### 前置条件

- GCC（支持 C++17）
- CMake 3.0+
- Python 3

### 1. 构建 DRAMSim3

```bash
cd dramsim3
mkdir -p build && cd build && cmake ..
make -j8
```

### 2. 构建 ChampSim-LA

```bash
cd champsim-la
python3 config.sh champsim_config.json
make -j8
```

### 一键构建

```bash
cd champsim-la
DRAMSIM3_ROOT=/root/data/smartPRE/dramsim3 python3 scripts/build_with_dramsim3.sh
```

## 运行仿真

运行前需设置动态链接库路径：

```bash
export LD_LIBRARY_PATH=/root/data/smartPRE/dramsim3:$LD_LIBRARY_PATH
```

### 快速测试（选定 slice）

```bash
cd champsim-la
bash scripts/run_selected_slices.sh
```

### 完整 benchmark 套件

```bash
cd champsim-la
bash scripts/run_benchmarks.sh
```

### 批量并行运行

```bash
cd champsim-la/batch_run
python3 run.py
```

## 结果分析

```bash
# 汇总 IPC 结果
python3 scripts/summarize_ipc.py results/<config_name>/

# 对比两组配置的 IPC
python3 scripts/compare_ipc.py results/<baseline>/ results/<experiment>/
```

## 仿真配置

仿真参数通过 JSON 配置文件定义：

| 配置文件 | 用途 |
|----------|------|
| `champsim_config.json` | 单核基线（4GHz, ROB 350, DDR5-4800） |
| `champsim_config_4c.json` | 4 核配置 |
| `champsim_config_8C.json` | 8 核配置 |
| `champsim_config_FAPS.json` | FAPS-3D 实验 |
| `champsim_config_RLPAGE.json` | RL-PAGE 实验 |

修改配置后需重新运行 `python3 config.sh <config>.json` 生成 Makefile 和头文件。

## Benchmark 工作负载

Trace 驱动仿真，支持以下 benchmark 套件：

- SPEC CPU2006 / CPU2017
- CRONO 图算法
- LIGRA 图处理框架
- PARSEC 并行计算
- CloudSuite 云服务

Benchmark 元数据定义在 `champsim-la/benchmarks.tsv`（全集，1733 条）和 `benchmarks_selected.tsv`（精选子集，799 条）中。

## 技术要点

- ChampSim 使用 C++17，DRAMSim3 使用 C++11
- ChampSim 的 Makefile 由 `config.sh` 自动生成，不要手动编辑
- DRAMSim3 通过 `inc/dramsim3_wrapper.hpp` 与 ChampSim 集成，实现周期精确的协同仿真
- 模块系统通过 `-D` 编译宏重命名符号，允许多个同类模块（预取器、替换策略等）共存
