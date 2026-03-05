# ABP (Access Based Predictor) 实验方案总结

> 来源: Awasthi et al., "Prediction Based DRAM Row-Buffer Management in the Many-Core Era", PACT 2011

## 1. 研究背景与动机

在多核时代，来自不同线程/核心的内存访问流在内存控制器处被交织，使得 DRAM 层面的空间局部性大幅降低。传统的 open-page 和 timer-based 策略在核心数增加时效果下降。作者提出：**基于访问次数的预测**比基于时间的策略更能有效指示 DRAM 局部性。

## 2. 对比策略

论文评估了以下四种 row-buffer 管理策略：

| 策略 | 描述 |
|------|------|
| **Open Page** | 始终保持 row-buffer 打开，直到发生 page-conflict |
| **Closed Page** | 每次访问后立即关闭 row-buffer（precharge） |
| **Xu09** | 两级 access-based 预测器（Xu et al., SAMOS 2009），使用 n-bit 移位寄存器记录 row-hit/row-miss 历史，索引到 history table 的饱和计数器；采用 per-bank HR + global HT 组织 |
| **ABP（本文提出）** | 一级 access-based 预测器，在 per-page 粒度追踪访问历史，以更低的存储开销实现与 Xu09 相当的性能 |

## 3. ABP 预测器设计

### 3.1 核心机制

ABP 在 DRAM-page 粒度维护一个历史表，记录每个 page 的预测访问次数：

1. **首次访问某 DRAM-page**：查找历史表
   - **未命中**：保持 row-buffer 打开，直到 page-conflict 发生；关闭时记录该 page 的实际访问次数
   - **命中**：在达到预测的访问次数后关闭 row-buffer，或在 page-conflict 时关闭
2. **预测更新**：
   - 若发生 page-conflict（预测过高）：历史表中该 page 的访问计数减 1
   - 若预测精确关闭后下一次访问是不同 page：预测完美，无需更新
   - 若预测关闭后又重新打开同一 page：说明过早关闭，保持打开直到 conflict，然后更新为累计访问次数

### 3.2 硬件组织

- **结构**：组织为 cache 形式，总计 2048 set / 4-way
- **Per-bank 分配**：32 个 DRAM bank，每个 bank 有 64 set × 4 way 的预测器
- **存储开销**：约 **20 KB**
- **关键路径**：预测器查找**不在关键路径上**——预测仅在 DRAM-page 被打开（RAS）和读取（CAS）之后才需要
- **命中率**：平均 **92.6%**

## 4. 实验配置

### 4.1 系统配置

- **场景**：多核（Many-Core）设定
- **DRAM 配置**：32 个 bank

> 注：该论文为 2 页短文（PACT 2011 Workshop），未详细列出完整的系统配置参数（如核心数、cache 层次、频率等）。

### 4.2 Benchmark

从 Figure 1 中可提取使用的 benchmark 集合（主要为 PARSEC 套件 + 其他）：

| Benchmark | 来源 |
|-----------|------|
| Canneal | PARSEC |
| Vips | PARSEC |
| Ferret | PARSEC |
| Streamcluster | PARSEC |
| Freqmine | PARSEC |
| Facesim | PARSEC |
| Bodytrack | PARSEC |
| Blackscholes | PARSEC |
| SPECjbb2005 | SPEC |
| MIX1 | 混合负载 |

### 4.3 评估指标

- **Normalized Throughput**（归一化吞吐量）：以某一基准策略归一化后的系统吞吐量

## 5. 实验结果

### 5.1 整体性能

- ABP 相对于 **Open Page** 策略：系统吞吐量提升 **12.3%**
- ABP 相对于 **Closed Page** 策略：系统吞吐量提升 **21.6%**
- ABP 与 Xu09 性能相当，但存储开销显著更小

### 5.2 各 Benchmark 表现（从 Figure 1 估读）

| Benchmark | Open Page | ABP | Xu09 | Closed Page | 备注 |
|-----------|-----------|-----|------|-------------|------|
| Canneal | ~0.65 | ~1.0 | ~1.0 | ~0.95 | ABP 大幅优于 Open Page |
| Vips | ~1.0 | ~1.05 | ~1.05 | ~1.0 | 差异较小 |
| Ferret | ~1.0 | ~1.05 | ~1.05 | ~0.95 | ABP 略优 |
| Streamcluster | ~0.85 | ~1.0 | ~1.0 | ~0.95 | ABP 明显优于 Open Page |
| Freqmine | ~1.0 | ~1.0 | ~1.0 | ~0.85 | Closed Page 明显较差 |
| Facesim | ~1.0 | ~1.05 | ~1.05 | ~0.95 | 差异较小 |
| Bodytrack | ~1.0 | ~1.0 | ~1.0 | ~0.95 | 差异较小 |
| Blackscholes | ~1.55 | ~1.55 | ~1.55 | ~1.0 | Open Page 和 ABP 均大幅优于 Closed Page |
| SPECjbb2005 | ~0.6 | ~1.1 | ~1.1 | ~1.0 | ABP 大幅优于 Open Page |
| MIX1 | ~0.85 | ~1.0 | ~1.0 | ~0.95 | ABP 优于 Open Page |
| **Average** | ~0.85 | ~1.05 | ~1.05 | ~0.95 | ABP 整体最优 |

> 注：以上数值为从论文 Figure 1 柱状图估读的近似值，以 ABP 为归一化基准（≈1.0）。

### 5.3 关键观察

1. **Canneal 和 SPECjbb2005**：Open Page 策略表现极差（吞吐量仅为 ABP 的 60-65%），说明这些负载在多核场景下 row-buffer conflict 严重
2. **Blackscholes**：Closed Page 策略表现极差（仅为 ABP/Open Page 的 ~65%），说明该负载有很强的 row-buffer 局部性，不应关闭页面
3. **ABP 和 Xu09 几乎一致**：在所有 benchmark 上两者性能几乎相同，验证了 ABP 用更低存储开销达到同等性能的主张
4. **跨负载稳定性好**：ABP 在所有 benchmark 上均接近或达到最优，没有某个负载上出现明显退化

## 6. 对本项目的启示

- ABP 的核心思想（基于访问次数预测而非时间）可作为 smartPRE 中 row-buffer 管理策略的参考
- Per-page 粒度的预测器以 cache 形式组织（64 set × 4 way per bank），20KB 的存储开销在现代系统中非常合理
- 预测器不在关键路径上，适合硬件实现
- 在多核场景下，access-based 方法明显优于传统 open/closed-page 策略
