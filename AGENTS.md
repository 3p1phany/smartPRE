# AGENTS 协作指南（smartPRE）

## 1. 注意事项（高优先级）
- 在开始任何实现前，先阅读 `docs/` 下相关文档，明确阶段目标与边界。
- 汇报性能时，必须给出每个 benchmark 的提升情况；不能只报告整体 GEOMEAN。
- 若被要求执行 `git commit`，不要在提交信息中添加 `Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>`。
- 性能评估脚本必须基于 `champsim-la/scripts/run_benchmarks.sh`，并使用 `benchmarks_selected.tsv`。
- `champsim-la/scripts/run_selected_slices.sh` 仅用于快速功能冒烟测试，不用于正式性能评估。

## 2. 任务范围
- 实现与调优 SMART 行缓冲策略。
- 维护 `dramsim3` 与 `champsim-la` 的可编译、可运行状态。
- 输出可复现实验结果与必要文档。

## 3. 关键文件
- 策略入口：
  - `dramsim3/src/controller.cc`
  - `dramsim3/src/command_queue.cc`
  - `dramsim3/src/command_queue.h`
  - `dramsim3/src/configuration.cc`
  - `dramsim3/src/common.h`
- 实验与汇总：
  - `champsim-la/scripts/run_benchmarks.sh`
  - `champsim-la/scripts/run_selected_slices.sh`
  - `champsim-la/tools/*.py`
- 设计文档：
  - `docs/smart_strategy_implementation_stages.md`
  - `docs/smart_strategy_experiment_plan.md`
  - `docs/SMART_DEBUG_GUIDE.md`

## 4. 标准工作流
1. 阅读 `docs/` 中对应阶段说明，明确变更边界。
2. 最小化修改代码（只改当前目标相关路径）。
3. 本地构建验证：
   ```bash
   cd /root/data/smartPRE/dramsim3 && make -j
   cd /root/data/smartPRE/champsim-la && python3 config.sh champsim_config.json && make -j
   ```
4. 若有可用 trace，执行至少一个 smoke run 验证功能与计数器。
5. 记录改动影响（策略行为、指标变化、已知风险）。

## 5. 代码与提交要求
- 避免修改第三方目录：`dramsim3/ext/`。
- 不做大规模格式化或无关重命名。
- 新增逻辑需提供最小注释，说明"为什么"。
- 任何计数器/实验流程变化应同步更新文档。

## 6. 项目概览
- `smartPRE` 是 CPU/内存协同仿真项目，由两个子模块组成：
  - `champsim-la`：带 LoongArch 支持的 ChampSim 周期级 CPU 模拟器。
  - `dramsim3`：详细 DRAM 内存系统模拟器。

## 7. 构建与运行
- 构建顺序：先 `dramsim3`，再 `champsim-la`（后者依赖 `libdramsim3.so`）。
- DRAMSim3（CMake）：
  ```bash
  cd dramsim3
  mkdir -p build && cd build && cmake ..
  make -j8
  ```
- DRAMSim3（Makefile 备选）：
  ```bash
  cd /root/data/smartPRE/dramsim3 && make -j
  ```
- ChampSim-LA：
  ```bash
  cd champsim-la
  python3 config.sh champsim_config.json
  make -j8
  ```
- 运行前环境变量（动态链接）：
  ```bash
  export LD_LIBRARY_PATH=/root/data/smartPRE/dramsim3:$LD_LIBRARY_PATH
  ```
- 清理：
  ```bash
  cd champsim-la && make clean
  cd dramsim3 && make clean
  cd dramsim3/build && make clean
  ```

## 8. 技术约束
- ChampSim 使用 C++17；DRAMSim3 使用 C++11。
- ChampSim 的 `Makefile` 由 `config.sh` 生成，不要手改生成后的 `Makefile`。
- 修改 JSON 配置或模块后，需要重新执行 `python3 config.sh ...`。
- DRAM 集成桥接代码位于 `champsim-la/inc/dramsim3_wrapper.hpp`。

## 9. 结果目录约定
- 构建产物：`build/`（已忽略）。
- 实验产物：`results/`（已忽略）。
- 单次运行常见输出：`run.log`、`ddr.txt`、`ddr.json`。
