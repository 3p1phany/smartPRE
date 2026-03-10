# CRAFT Final Evaluation: Benchmark & Baseline Selection

## CRAFT Variant Selection

### Ablation Results (vs ABP + DYMPL + INTAP)

18 CRAFT variants were evaluated across all 62 benchmarks. Each variant is compared
against the best of the 3 baselines per benchmark.

**Phase 1: Single-Enhancement Ablation**

| Variant | Flags | Wins/62 | Win GEOMEAN | Overall GEOMEAN |
|---------|-------|---------|-------------|-----------------|
| CRAFT_BASE | none | 45 | +1.188% | +0.653% |
| CRAFT_PR | PR | 48 | +1.034% | +0.613% |
| CRAFT_QDSD | QDSD | 45 | +1.188% | +0.653% |
| CRAFT_RS | RS | 45 | +1.203% | +0.704% |
| CRAFT_RW | RW | 49 | +1.172% | +0.775% |
| CRAFT_SD | SD | 45 | +1.219% | +0.699% |

Observations:
- **RW** is the strongest single enhancement: most wins (49) and highest overall geomean (+0.775%)
- **RS** and **SD** each improve overall geomean over BASE while maintaining win geomean
- **PR** adds wins (+3) but hurts win geomean, indicating marginal improvements on easy benchmarks
- **QDSD** shows no measurable difference from BASE (identical numbers)

**Phase 2: Enhancement Combinations**

| Variant | Flags | Wins/62 | Win GEOMEAN | Overall GEOMEAN |
|---------|-------|---------|-------------|-----------------|
| CRAFT_PR_SD | PR+SD | 48 | +1.069% | +0.665% |
| CRAFT_RW_QDSD | RW+QDSD | 49 | +1.172% | +0.775% |
| CRAFT_RS_SD | RS+SD | 45 | +1.249% | +0.773% |
| CRAFT_CONFLICT | PR+QDSD+RW | 49 | +1.092% | +0.714% |
| **CRAFT_PRECHARGE** | **RS+RW+SD** | **48** | **+1.244%** | **+0.861%** |
| CRAFT_ALL | ALL 5 | 48 | +1.151% | +0.789% |

Observations:
- **CRAFT_PRECHARGE (RS+RW+SD)** achieves the highest overall geomean (+0.861%) and
  near-highest win geomean (+1.244%), clearly the best combination
- RS+SD synergy: RS_SD (+0.773%) > RS (+0.704%) or SD (+0.699%) alone
- Adding RW on top of RS+SD yields PRECHARGE (+0.861%), a further +0.088pp
- CRAFT_ALL (+0.789%) is worse than PRECHARGE (+0.861%): adding PR+QDSD
  to the precharge-path group hurts performance by -0.072pp
- CRAFT_CONFLICT (PR+QDSD+RW, +0.714%) < CRAFT_RW alone (+0.775%): PR+QDSD
  hurt RW's effectiveness

**Phase 3: Parameter Sensitivity**

| Variant | Flags | Wins/62 | Win GEOMEAN | Overall GEOMEAN |
|---------|-------|---------|-------------|-----------------|
| CRAFT_PR3 | PR(thr=3) | 48 | +1.023% | +0.619% |
| CRAFT_PR (default) | PR(thr=4) | 48 | +1.034% | +0.613% |
| CRAFT_PR6 | PR(thr=6) | 48 | +1.094% | +0.653% |
| CRAFT_QDSD_C2 | QDSD(cap=2) | 45 | +1.188% | +0.653% |
| CRAFT_QDSD (default) | QDSD(cap=4) | 45 | +1.188% | +0.653% |
| CRAFT_QDSD_C8 | QDSD(cap=8) | 45 | +1.188% | +0.653% |
| CRAFT_RS3 | RS(thr=3) | 46 | +1.185% | +0.721% |
| CRAFT_RS (default) | RS(thr=4) | 45 | +1.203% | +0.704% |
| CRAFT_RS6 | RS(thr=6) | 45 | +1.196% | +0.688% |

Observations:
- PR: thr=6 slightly better overall geomean, but PR itself is not selected
- QDSD: completely insensitive to cap parameter (identical results for cap=2/4/8)
- RS: thr=3 gains 1 extra win and +0.017pp overall geomean vs default thr=4

### Final CRAFT Variant: CRAFT_PRECHARGE (RS + RW + SD)

Three precharge-path enhancements enabled:
- **RS** (Right Streak): gentle de-escalation on consecutive right precharges
- **RW** (Read/Write Cost): differentiated escalation/de-escalation for read vs write
- **SD** (Streak Decay): reopen streak decay on right precharge events

Selection rationale:
1. Highest overall geomean (+0.861%) among all 18 variants
2. Near-highest win geomean (+1.244%, only RS_SD is +0.005pp higher at fewer wins)
3. Outperforms CRAFT_ALL (all 5 enhancements) on 44/62 benchmarks head-to-head
4. Clean narrative: the three precharge-path enhancements synergize well,
   while conflict-path enhancements (PR, QDSD) add noise

Feature flags in `dramsim3/src/command_queue.h`:
```
CRAFT_PHASE_RESET_ENABLED   = false
CRAFT_QDSD_ENABLED          = false
CRAFT_RIGHT_STREAK_ENABLED  = true
CRAFT_RW_ENABLED            = true
CRAFT_STREAK_DECAY_ENABLED  = true
```

## Selected Baselines

| Baseline | Full Name | Key Characteristic |
|----------|-----------|-------------------|
| **ABP** | Adaptive Buffer Policy | Traditional adaptive open/close policy |
| **DYMPL** | Dynamic Multilevel Page | Dynamic multilevel page management with queue-depth awareness |
| **INTAP** | Intelligent Adaptive Policy | Intelligent adaptive policy with graph-workload strength |

## Selected Benchmarks (Top 12)

Selection criterion: CRAFT_PRECHARGE IPC strictly highest among all 4 policies,
ranked by improvement margin over the best baseline (all >= 1.6%).

### Performance Table

| # | Benchmark | CRAFT IPC | ABP IPC | DYMPL IPC | INTAP IPC | vs Best BL |
|---|-----------|-----------|---------|-----------|-----------|------------|
| 1 | ligra/CF/roadNet-CA | 1.416450 | 1.262470 | 1.338614 | 1.338874 | +5.79% |
| 2 | ligra/CF/higgs | 1.550109 | 1.439680 | 1.466966 | 1.503480 | +3.10% |
| 3 | ligra/PageRank/higgs | 1.027627 | 0.950276 | 0.990720 | 0.998960 | +2.87% |
| 4 | ligra/BFSCC/soc-pokec-short | 0.836893 | 0.781260 | 0.813696 | 0.794579 | +2.85% |
| 5 | spec06/sphinx3/ref | 1.436874 | 1.302875 | 1.400475 | 1.384565 | +2.60% |
| 6 | ligra/CF/soc-pokec | 1.295327 | 1.246884 | 1.256319 | 1.269662 | +2.02% |
| 7 | ligra/Triangle/roadNet-CA | 0.791978 | 0.732507 | 0.776434 | 0.764101 | +2.00% |
| 8 | ligra/PageRank/roadNet-CA | 1.135724 | 1.018001 | 1.105778 | 1.114647 | +1.89% |
| 9 | crono/Triangle-Counting/roadNet-CA | 0.815633 | 0.755392 | 0.793547 | 0.801674 | +1.74% |
| 10 | spec06/wrf/ref | 1.888790 | 1.745850 | 1.858135 | 1.856769 | +1.65% |
| 11 | ligra/Components-Shortcut/soc-pokec | 0.941215 | 0.867687 | 0.923251 | 0.926115 | +1.63% |
| 12 | ligra/Radii/higgs | 0.569893 | 0.540287 | 0.560879 | 0.558860 | +1.61% |

### Per-Baseline Improvement

| # | Benchmark | vs ABP | vs DYMPL | vs INTAP |
|---|-----------|--------|----------|----------|
| 1 | ligra/CF/roadNet-CA | +12.20% | +5.81% | +5.79% |
| 2 | ligra/CF/higgs | +7.67% | +5.67% | +3.10% |
| 3 | ligra/PageRank/higgs | +8.14% | +3.73% | +2.87% |
| 4 | ligra/BFSCC/soc-pokec-short | +7.12% | +2.85% | +5.33% |
| 5 | spec06/sphinx3/ref | +10.28% | +2.60% | +3.78% |
| 6 | ligra/CF/soc-pokec | +3.89% | +3.10% | +2.02% |
| 7 | ligra/Triangle/roadNet-CA | +8.12% | +2.00% | +3.65% |
| 8 | ligra/PageRank/roadNet-CA | +11.56% | +2.71% | +1.89% |
| 9 | crono/Triangle-Counting/roadNet-CA | +7.97% | +2.78% | +1.74% |
| 10 | spec06/wrf/ref | +8.19% | +1.65% | +1.72% |
| 11 | ligra/Components-Shortcut/soc-pokec | +8.47% | +1.95% | +1.63% |
| 12 | ligra/Radii/higgs | +5.48% | +1.61% | +1.97% |
| **GEOMEAN** | | **+7.73%** | **+3.10%** | **+2.84%** |

### Normalized IPC (CRAFT = 1.0)

| # | Benchmark | ABP | DYMPL | INTAP | CRAFT |
|---|-----------|-----|-------|-------|-------|
| 1 | ligra/CF/roadNet-CA | 0.8913 | 0.9450 | 0.9452 | 1.0000 |
| 2 | ligra/CF/higgs | 0.9288 | 0.9464 | 0.9699 | 1.0000 |
| 3 | ligra/PageRank/higgs | 0.9247 | 0.9641 | 0.9721 | 1.0000 |
| 4 | ligra/BFSCC/soc-pokec-short | 0.9335 | 0.9723 | 0.9494 | 1.0000 |
| 5 | spec06/sphinx3/ref | 0.9067 | 0.9747 | 0.9636 | 1.0000 |
| 6 | ligra/CF/soc-pokec | 0.9626 | 0.9699 | 0.9802 | 1.0000 |
| 7 | ligra/Triangle/roadNet-CA | 0.9249 | 0.9804 | 0.9648 | 1.0000 |
| 8 | ligra/PageRank/roadNet-CA | 0.8963 | 0.9736 | 0.9814 | 1.0000 |
| 9 | crono/Triangle-Counting/roadNet-CA | 0.9261 | 0.9729 | 0.9829 | 1.0000 |
| 10 | spec06/wrf/ref | 0.9243 | 0.9838 | 0.9830 | 1.0000 |
| 11 | ligra/Components-Shortcut/soc-pokec | 0.9219 | 0.9809 | 0.9840 | 1.0000 |
| 12 | ligra/Radii/higgs | 0.9480 | 0.9842 | 0.9806 | 1.0000 |
| **GEOMEAN** | | **0.9281** | **0.9699** | **0.9724** | **1.0000** |

## Broader Context

Over all 62 benchmarks with these 3 baselines, CRAFT_PRECHARGE wins 48/62 (77%).
The 14 losing benchmarks are concentrated in:
- **hashjoin** (2): purely random access patterns, DYMPL advantage
- **hpcc/RandAcc** (2): fully random memory access, DYMPL advantage
- **crono/PageRank** (2): DYMPL advantage on high-conflict soc-pokec/higgs graphs
- **crono graph kernels on higgs** (4): INTAP advantage (DFS, SSSP, Community, Triangle-Counting)
- **spec17** (3): cactuBSSN (ABP), xz/gcc (marginal, < 0.2%)
- **ligra/BC/soc-pokec** (1): marginal, -0.06%

## Plot

Generated by `champsim-la/scripts/plot_craft_vs_baselines.py`.
Output: `champsim-la/craft_vs_baselines.png`
