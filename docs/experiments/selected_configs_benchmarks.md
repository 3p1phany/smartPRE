# Phase 1 筛选配置与 Benchmark 性能变动

**日期**: 2026-02-28
**Baseline**: GS_1c（原始 GS 实现）
**对照**: GS_ext_timeout_only_1c（仅扩展候选集 {50..3200}，无 escalation）

## 筛选标准

从 Phase 1 的 8 种 escalation 配置 × 62 benchmarks 中，选出在 ext_timeout_only 为 baseline 时持续正向（平均提升 > +0.1%）的配置与 benchmark 组合。

### 选出的配置（4个）

| 配置 | N | STEP | GEOMEAN vs ext_TO | GEOMEAN vs GS |
|------|---|------|-------------------|---------------|
| N3_S2 | 3 | 2 | -0.16% | -0.15% |
| N5_S3 | 5 | 3 | -0.15% | -0.14% |
| N8_S2 | 8 | 2 | -0.13% | -0.12% |
| N12_S2 | 12 | 2 | -0.12% | -0.10% |

### 选出的 Benchmark（11个）

图算法（Triangle-Counting、DFS、Community、SSSP、PageRank/roadNet）和网络模拟（omnetpp）为主。

---

## Per-Benchmark IPC 变化（% vs GS baseline）

| Benchmark | ext_TO | N3S2 | N5S3 | N8S2 | N12S2 |
|-----------|--------|------|------|------|-------|
| crono/Triangle-Counting/higgs | -0.18% | +0.65% | +0.57% | +0.74% | +0.83% |
| spec06/omnetpp/ref | +0.08% | +0.81% | +0.92% | +0.76% | +0.73% |
| ligra/Triangle/soc-pokec | -0.01% | +0.73% | +0.81% | +0.66% | +0.57% |
| crono/DFS/higgs | -0.21% | +0.30% | +0.22% | +0.41% | +0.42% |
| crono/Triangle-Counting/roadNet-CA | +0.09% | +0.64% | +0.69% | +0.67% | +0.65% |
| crono/Community/higgs | -0.17% | +0.16% | +0.13% | +0.23% | +0.25% |
| crono/SSSP/higgs | -0.34% | -0.04% | -0.07% | +0.00% | +0.05% |
| crono/PageRank/roadNet-CA | +0.20% | +0.43% | +0.53% | +0.56% | +0.52% |
| spec17/omnetpp/ref | +0.04% | +0.29% | +0.43% | +0.28% | +0.28% |
| npb/IS | +0.00% | +0.17% | +0.17% | +0.15% | +0.16% |
| spec17/xz/cld | -0.03% | +0.11% | -0.09% | +0.12% | +0.12% |

## 观察

1. **Escalation 在这 11 个 benchmark 上的改善是真实的**：四个配置几乎全部优于 GS baseline（仅 crono/SSSP/higgs 和 spec17/xz/cld 的 N5S3 例外），而非仅仅弥补 ext_timeout_only 的退化。

2. **ext_timeout_only 在部分 benchmark 上劣于 GS baseline**：crono/SSSP/higgs (-0.34%)、crono/DFS/higgs (-0.21%)、crono/Triangle-Counting/higgs (-0.18%)、crono/Community/higgs (-0.17%)。对于这些 benchmark，扩展候选集本身有害，escalation 不仅抵消了退化还带来了额外增益。

3. **crono/SSSP/higgs 需要谨慎**：vs ext_timeout_only 显示 +0.27%~0.39%，但 vs GS baseline 仅 -0.07%~+0.05%，"改善"主要来自弥补 ext_timeout_only 自身退化，真实增量很小。
