# AGENTS 协作指南（smartPRE）

## 1. 任务范围
- 实现与调优 SMART 行缓冲策略。
- 维护 `dramsim3` 与 `champsim-la` 的可编译、可运行状态。
- 输出可复现实验结果与必要文档。

## 2. 关键文件
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

## 3. 标准工作流
1. 阅读 `docs/` 中对应阶段说明，明确变更边界。
2. 最小化修改代码（只改当前目标相关路径）。
3. 本地构建验证：
   ```bash
   cd /root/data/smartPRE/dramsim3 && make -j
   cd /root/data/smartPRE/champsim-la && python3 config.sh champsim_config.json && make -j
   ```
4. 若有可用 trace，执行至少一个 smoke run 验证功能与计数器。
5. 记录改动影响（策略行为、指标变化、已知风险）。

## 4. 代码与提交要求
- 避免修改第三方目录：`dramsim3/ext/`。
- 不做大规模格式化或无关重命名。
- 新增逻辑需提供最小注释，说明“为什么”。
- 任何计数器/实验流程变化应同步更新文档。

## 5. 结果目录约定
- 构建产物：`build/`（已忽略）。
- 实验产物：`results/`（已忽略）。
- 单次运行常见输出：`run.log`、`ddr.txt`、`ddr.json`。

