#!/usr/bin/env python3
"""
Oracle Timeout Data Synthesis and Analysis
File: /root/data/smartPRE/scripts/analyze_oracle_sweep.py

This script processes the output from the timeout sweep experiment to:
1. Align epoch data across all timeout values
2. Find the best timeout for each epoch (oracle)
3. Compute PC signature change correlation
4. Generate visualizations
"""

import os
import sys
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from pathlib import Path
from collections import defaultdict
import argparse

# ===== Configuration =====
DEFAULT_RESULTS_ROOT = Path("/root/data/smartPRE/results/oracle_sweep")
DEFAULT_OUTPUT_DIR = Path("/root/data/smartPRE/results/analysis")
# Timeout values: interval=20, range 20-400
TIMEOUT_VALUES = list(range(20, 420, 20))  # [20, 40, 60, ..., 400]
EPOCH_INSTRUCTIONS = 10000  # Instructions per epoch


def load_epoch_data(trace_name: str, results_root: Path) -> dict:
    """Load epoch data for all timeout values of a given trace"""
    data = {}
    trace_dir = results_root / trace_name

    for timeout in TIMEOUT_VALUES:
        epoch_file = trace_dir / f"timeout_{timeout}" / "epoch_stats.csv"
        if epoch_file.exists():
            try:
                # Format: epoch_id,pc_hash,rbhr,ipc
                df = pd.read_csv(epoch_file, header=None,
                               names=['epoch_id', 'pc_hash', 'rbhr', 'ipc'],
                               dtype={'epoch_id': int, 'pc_hash': str, 'rbhr': float, 'ipc': float})
                data[timeout] = df
                print(f"  Loaded {trace_name}/timeout_{timeout}: {len(df)} epochs")
            except Exception as e:
                print(f"  ERROR loading {epoch_file}: {e}")
        else:
            print(f"  WARNING: Missing {epoch_file}")

    return data


def align_epochs(data: dict) -> pd.DataFrame:
    """
    Align epoch data across all timeout values.
    Ensures all files have the same epoch count.
    """
    if not data:
        return pd.DataFrame()

    # Find minimum epoch count across all timeouts
    min_epochs = min(len(df) for df in data.values())
    print(f"  Aligning to {min_epochs} epochs")

    # Create aligned dataframe
    aligned = pd.DataFrame({'epoch_id': range(min_epochs)})

    for timeout, df in data.items():
        df_aligned = df.head(min_epochs).reset_index(drop=True)
        aligned[f'ipc_{timeout}'] = df_aligned['ipc']
        aligned[f'rbhr_{timeout}'] = df_aligned['rbhr']

        # PC hash only needed once (same trace = same PC sequence)
        if 'pc_hash' not in aligned.columns:
            aligned['pc_hash'] = df_aligned['pc_hash']

    return aligned


def find_best_timeout(aligned: pd.DataFrame, timeout_values: list) -> pd.DataFrame:
    """
    For each epoch, find the timeout value that gives the best IPC.
    """
    if aligned.empty:
        return aligned

    # Find best timeout for each epoch
    best_timeout = []
    best_ipc = []

    for idx, row in aligned.iterrows():
        ipcs = {}
        for t in timeout_values:
            col = f'ipc_{t}'
            if col in row and pd.notna(row[col]):
                ipcs[t] = row[col]

        if ipcs:
            best_t = max(ipcs, key=ipcs.get)
            best_timeout.append(best_t)
            best_ipc.append(ipcs[best_t])
        else:
            best_timeout.append(timeout_values[0])
            best_ipc.append(0.0)

    aligned['best_timeout'] = best_timeout
    aligned['best_ipc'] = best_ipc

    return aligned


def compute_pc_delta(aligned: pd.DataFrame) -> pd.DataFrame:
    """
    Compute PC signature change rate between consecutive epochs.
    Uses Hamming distance approximation via XOR and popcount.
    """
    if aligned.empty or 'pc_hash' not in aligned.columns:
        return aligned

    def parse_hash(x):
        if isinstance(x, str):
            try:
                return int(x, 16)
            except ValueError:
                return 0
        return int(x) if pd.notna(x) else 0

    pc_hashes = aligned['pc_hash'].apply(parse_hash)

    # Compute delta (XOR then count bits)
    pc_delta = []
    for i in range(len(pc_hashes)):
        if i == 0:
            pc_delta.append(0)
        else:
            xor_result = pc_hashes.iloc[i] ^ pc_hashes.iloc[i-1]
            # Count number of different bits (Hamming distance approximation)
            delta = bin(xor_result).count('1')
            pc_delta.append(delta)

    aligned['pc_delta'] = pc_delta

    # Normalize to 0-1 range for visualization
    max_delta = max(pc_delta) if max(pc_delta) > 0 else 1
    aligned['pc_delta_norm'] = aligned['pc_delta'] / max_delta

    return aligned


def plot_oracle_analysis(trace_name: str, aligned: pd.DataFrame, output_dir: Path, timeout_values: list):
    """
    Generate the target visualization:
    - X-axis: Instruction count (0 to 50M)
    - Y1 (left): PC Signature change rate
    - Y2 (right): Best Timeout value
    """
    if aligned.empty:
        print(f"  Skipping plot for {trace_name}: no data")
        return

    output_dir.mkdir(parents=True, exist_ok=True)

    # Convert epoch to instruction count (each epoch = 10K instructions)
    instructions = aligned['epoch_id'] * EPOCH_INSTRUCTIONS  # In actual instructions
    instructions_M = instructions / 1e6  # In millions

    fig, ax1 = plt.subplots(figsize=(14, 6))

    # Plot PC Delta (left Y-axis)
    color1 = 'tab:blue'
    ax1.set_xlabel('Instructions (Millions)', fontsize=12)
    ax1.set_ylabel('PC Signature Change Rate', color=color1, fontsize=12)
    ax1.plot(instructions_M, aligned['pc_delta_norm'], color=color1,
             linewidth=0.8, alpha=0.8, label='PC Delta')
    ax1.tick_params(axis='y', labelcolor=color1)
    ax1.set_ylim(0, 1.1)

    # Plot Best Timeout (right Y-axis)
    ax2 = ax1.twinx()
    color2 = 'tab:red'
    ax2.set_ylabel('Best Timeout (cycles)', color=color2, fontsize=12)
    ax2.step(instructions_M, aligned['best_timeout'], color=color2,
             linewidth=1.2, where='post', label='Best Timeout')
    ax2.tick_params(axis='y', labelcolor=color2)
    ax2.set_ylim(0, max(timeout_values) * 1.1)

    # Title and grid
    plt.title(f'Oracle Timeout Analysis: {trace_name}', fontsize=14)
    ax1.grid(True, alpha=0.3)

    # Legend
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc='upper right')

    plt.tight_layout()
    plt.savefig(output_dir / f'{trace_name}_oracle_analysis.png', dpi=150)
    plt.savefig(output_dir / f'{trace_name}_oracle_analysis.pdf')
    plt.close()

    print(f"  Saved plot: {output_dir / f'{trace_name}_oracle_analysis.png'}")


def plot_ipc_comparison(trace_name: str, aligned: pd.DataFrame, output_dir: Path, timeout_values: list):
    """
    Plot IPC comparison across different timeout values.
    """
    if aligned.empty:
        return

    output_dir.mkdir(parents=True, exist_ok=True)

    instructions_M = (aligned['epoch_id'] * EPOCH_INSTRUCTIONS) / 1e6

    fig, ax = plt.subplots(figsize=(14, 6))

    # Plot IPC for a few key timeout values
    colors = plt.cm.viridis(np.linspace(0, 1, len(timeout_values)))
    for i, t in enumerate([20, 100, 200, 300, 400]):
        if t in timeout_values:
            col = f'ipc_{t}'
            if col in aligned.columns:
                ax.plot(instructions_M, aligned[col], color=colors[timeout_values.index(t)],
                       linewidth=0.8, alpha=0.7, label=f't={t}')

    # Plot best IPC
    ax.plot(instructions_M, aligned['best_ipc'], 'k-', linewidth=1.5,
            alpha=0.9, label='Oracle Best')

    ax.set_xlabel('Instructions (Millions)', fontsize=12)
    ax.set_ylabel('IPC', fontsize=12)
    ax.set_title(f'IPC Comparison: {trace_name}', fontsize=14)
    ax.legend(loc='upper right')
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(output_dir / f'{trace_name}_ipc_comparison.png', dpi=150)
    plt.close()

    print(f"  Saved IPC comparison: {output_dir / f'{trace_name}_ipc_comparison.png'}")


def compute_correlation(aligned: pd.DataFrame, trace_name: str, timeout_values: list) -> dict:
    """
    Compute correlation between PC delta and timeout changes.
    """
    if aligned.empty:
        return {'trace': trace_name, 'correlation': np.nan}

    # Compute timeout change
    timeout_change = aligned['best_timeout'].diff().abs().fillna(0)

    # Pearson correlation
    correlation = aligned['pc_delta'].corr(timeout_change)

    # Timeout distribution
    timeout_dist = aligned['best_timeout'].value_counts().sort_index()

    # IPC improvement over worst static timeout
    static_ipcs = []
    for t in timeout_values:
        col = f'ipc_{t}'
        if col in aligned.columns:
            static_ipcs.append(aligned[col].mean())

    worst_static = min(static_ipcs) if static_ipcs else 0
    best_static = max(static_ipcs) if static_ipcs else 0
    oracle_mean = aligned['best_ipc'].mean()

    improvement_over_worst = ((oracle_mean - worst_static) / worst_static * 100) if worst_static > 0 else 0
    improvement_over_best = ((oracle_mean - best_static) / best_static * 100) if best_static > 0 else 0

    stats = {
        'trace': trace_name,
        'correlation': correlation,
        'timeout_distribution': timeout_dist.to_dict(),
        'mean_oracle_ipc': oracle_mean,
        'best_static_ipc': best_static,
        'worst_static_ipc': worst_static,
        'improvement_over_worst_%': improvement_over_worst,
        'improvement_over_best_%': improvement_over_best,
        'ipc_variance': aligned[[f'ipc_{t}' for t in timeout_values if f'ipc_{t}' in aligned.columns]].var(axis=1).mean()
    }

    return stats


def save_oracle_data(trace_name: str, aligned: pd.DataFrame, output_dir: Path):
    """Save the oracle timeout sequence for future use."""
    if aligned.empty:
        return

    output_dir.mkdir(parents=True, exist_ok=True)

    cols_to_save = ['epoch_id', 'pc_hash', 'best_timeout', 'best_ipc']
    if 'pc_delta' in aligned.columns:
        cols_to_save.append('pc_delta')

    oracle_df = aligned[cols_to_save]
    oracle_df.to_csv(output_dir / f'{trace_name}_oracle.csv', index=False)
    print(f"  Saved oracle data: {output_dir / f'{trace_name}_oracle.csv'}")


def main():
    parser = argparse.ArgumentParser(description='Analyze Oracle Timeout Sweep Results')
    parser.add_argument('--results', type=str, default=str(DEFAULT_RESULTS_ROOT),
                       help='Results directory from sweep')
    parser.add_argument('--output', type=str, default=str(DEFAULT_OUTPUT_DIR),
                       help='Output directory for analysis')
    args = parser.parse_args()

    RESULTS_ROOT = Path(args.results)
    OUTPUT_DIR = Path(args.output)

    print("=" * 60)
    print("Oracle Timeout Data Synthesis and Analysis")
    print("=" * 60)
    print(f"Results directory: {RESULTS_ROOT}")
    print(f"Output directory: {OUTPUT_DIR}")

    if not RESULTS_ROOT.exists():
        print(f"\nERROR: Results directory not found: {RESULTS_ROOT}")
        print("Please run the sweep script first:")
        print("  ./scripts/run_timeout_sweep.sh")
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Get list of traces
    traces = [d.name for d in RESULTS_ROOT.iterdir()
              if d.is_dir() and not d.name.startswith('.') and d.name != 'configs']
    print(f"\nFound {len(traces)} traces: {traces}\n")

    if not traces:
        print("No trace results found. Check that the sweep completed successfully.")
        sys.exit(1)

    all_stats = []

    for trace_name in sorted(traces):
        print(f"\nProcessing {trace_name}...")

        # Step 1: Load data
        data = load_epoch_data(trace_name, RESULTS_ROOT)
        if len(data) < 2:
            print(f"  Skipping {trace_name}: insufficient timeout data")
            continue

        # Step 2: Align epochs
        aligned = align_epochs(data)
        if aligned.empty:
            print(f"  Skipping {trace_name}: no aligned data")
            continue

        # Step 3: Find best timeout per epoch
        aligned = find_best_timeout(aligned, TIMEOUT_VALUES)

        # Step 4: Compute PC delta
        aligned = compute_pc_delta(aligned)

        # Step 5: Generate visualizations
        plot_oracle_analysis(trace_name, aligned, OUTPUT_DIR, TIMEOUT_VALUES)
        plot_ipc_comparison(trace_name, aligned, OUTPUT_DIR, TIMEOUT_VALUES)

        # Step 6: Compute statistics
        stats = compute_correlation(aligned, trace_name, TIMEOUT_VALUES)
        all_stats.append(stats)
        print(f"  Correlation(PC_delta, Timeout_change): {stats['correlation']:.4f}")
        print(f"  Oracle IPC improvement over best static: {stats['improvement_over_best_%']:.2f}%")

        # Step 7: Save oracle data
        save_oracle_data(trace_name, aligned, OUTPUT_DIR)

    # Summary report
    print("\n" + "=" * 60)
    print("Summary Statistics")
    print("=" * 60)

    if all_stats:
        summary_df = pd.DataFrame(all_stats)
        print(summary_df[['trace', 'correlation', 'mean_oracle_ipc',
                         'best_static_ipc', 'improvement_over_best_%']].to_string())
        summary_df.to_csv(OUTPUT_DIR / 'summary_stats.csv', index=False)
        print(f"\nSummary saved to: {OUTPUT_DIR / 'summary_stats.csv'}")
    else:
        print("No statistics computed.")

    print(f"\nAll results saved to: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
