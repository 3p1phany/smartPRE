#!/bin/bash
# Oracle Timeout Sweep Script
# Usage: ./scripts/run_timeout_sweep.sh

set -e

# ===== Configuration =====
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
CHAMPSIM_ROOT="${PROJECT_ROOT}/champsim-la"
DRAMSIM3_ROOT="${PROJECT_ROOT}/dramsim3"
CHAMPSIM_BIN="${CHAMPSIM_ROOT}/bin/champsim"
TRACE_ROOT="${TRACE_ROOT:-/root/data/Trace/LA}"
RESULTS_ROOT="${RESULTS_ROOT:-${PROJECT_ROOT}/results/oracle_sweep}"

WARMUP=${WARMUP:-20000000}      # 20M warmup instructions
SIM=${SIM:-50000000}            # 50M simulation instructions
JOBS=${JOBS:-128}               # 128 parallel jobs

# Timeout values to sweep (interval=20, range 20-400)
TIMEOUT_VALUES=(20 40 60 80 100 120 140 160 180 200 220 240 260 280 300 320 340 360 380 400)

# ===== Environment Setup =====
export LD_LIBRARY_PATH="${DRAMSIM3_ROOT}:${LD_LIBRARY_PATH}"

# ===== Check Prerequisites and Build with EPOCH_STATS =====
if [ ! -f "${CHAMPSIM_BIN}" ] || [ "${REBUILD:-0}" == "1" ]; then
    echo "Building with ENABLE_EPOCH_STATS..."
    "${PROJECT_ROOT}/scripts/build_with_epoch_stats.sh"

    if [ ! -f "${CHAMPSIM_BIN}" ]; then
        echo "ERROR: Build failed!"
        exit 1
    fi
fi

if [ ! -d "${TRACE_ROOT}" ]; then
    echo "ERROR: Trace directory not found at ${TRACE_ROOT}"
    echo "Please set TRACE_ROOT environment variable"
    exit 1
fi

# ===== Create DRAM config files with different timeout values =====
CONFIG_DIR="${RESULTS_ROOT}/configs"
mkdir -p "${CONFIG_DIR}"

BASE_CONFIG="${CHAMPSIM_ROOT}/dramsim3_configs/DDR5_64GB_4ch_4800.ini"
if [ ! -f "${BASE_CONFIG}" ]; then
    BASE_CONFIG="${DRAMSIM3_ROOT}/configs/DDR4_8Gb_x8_2400.ini"
fi

echo "Creating DRAM configs with different timeout values..."
for timeout in "${TIMEOUT_VALUES[@]}"; do
    config_file="${CONFIG_DIR}/DDR4_timeout_${timeout}.ini"

    # Copy base config and modify
    cp "${BASE_CONFIG}" "${config_file}"

    # Set STATIC_TIMEOUT policy and timeout value
    if grep -q "row_buf_policy" "${config_file}"; then
        sed -i "s/row_buf_policy = .*/row_buf_policy = STATIC_TIMEOUT/" "${config_file}"
    else
        echo "row_buf_policy = STATIC_TIMEOUT" >> "${config_file}"
    fi

    if grep -q "static_timeout_cycles" "${config_file}"; then
        sed -i "s/static_timeout_cycles = .*/static_timeout_cycles = ${timeout}/" "${config_file}"
    else
        echo "static_timeout_cycles = ${timeout}" >> "${config_file}"
    fi
done
echo "Created ${#TIMEOUT_VALUES[@]} config files"

# ===== Trace configurations (from trace_phase.tsv) =====
declare -A TRACES
TRACES["graph500_s16-e10"]="${TRACE_ROOT}/graph500/s16-e10/Graph500_s16-e10_0.champsim.trace.xz"
TRACES["ligra_MIS_higgs"]="${TRACE_ROOT}/ligra/MIS/higgs/ligra_MIS_higgs_200000000.champsim.trace.xz"
TRACES["crono_PageRank_soc-pokec"]="${TRACE_ROOT}/crono/PageRank/soc-pokec/crono_PageRank_soc-pokec.champsim.trace.xz"
TRACES["crono_CC_higgs"]="${TRACE_ROOT}/crono/Connected-Components/higgs/crono_Connected-Components_higgs_100000000.champsim.trace.xz"
TRACES["hashjoin_hj-8"]="${TRACE_ROOT}/hashjoin/hj-8-NPO_st/hj-8-NPO_st_9090000000.champsim.trace.xz"
TRACES["hpcc_RandAcc"]="${TRACE_ROOT}/hpcc/RandAcc/hpcc_RandAcc_400000000.champsim.trace.xz"
TRACES["npb_IS"]="${TRACE_ROOT}/npb/IS/npb_IS_B_2590000000.champsim.trace.xz"
TRACES["spmv_mc2depi"]="${TRACE_ROOT}/spmv/mc2depi/spmv_mc2depi_100000000.champsim.trace.xz"

# ===== Verify traces exist =====
echo "Checking trace files..."
MISSING_TRACES=0
for trace_name in "${!TRACES[@]}"; do
    trace_path="${TRACES[$trace_name]}"
    if [ ! -f "${trace_path}" ]; then
        echo "  WARNING: Missing trace: ${trace_path}"
        MISSING_TRACES=$((MISSING_TRACES + 1))
    else
        echo "  OK: ${trace_name}"
    fi
done

if [ "${MISSING_TRACES}" -gt 0 ]; then
    echo ""
    echo "WARNING: ${MISSING_TRACES} traces are missing. Continuing with available traces..."
fi

# ===== Generate Task List =====
mkdir -p "${RESULTS_ROOT}"
TASK_FILE="${RESULTS_ROOT}/tasks.txt"
rm -f "${TASK_FILE}"

for trace_name in "${!TRACES[@]}"; do
    trace_path="${TRACES[$trace_name]}"
    if [ -f "${trace_path}" ]; then
        for timeout in "${TIMEOUT_VALUES[@]}"; do
            echo "${trace_name} ${timeout} ${trace_path}" >> "${TASK_FILE}"
        done
    fi
done

TOTAL_TASKS=$(wc -l < "${TASK_FILE}")
echo ""
echo "Generated ${TOTAL_TASKS} tasks"
echo "Results will be saved to: ${RESULTS_ROOT}"

# ===== Run Function =====
run_single_task() {
    local trace_name=$1
    local timeout=$2
    local trace_path=$3

    local PROJECT_ROOT="$4"
    local RESULTS_ROOT="$5"
    local CHAMPSIM_BIN="$6"
    local WARMUP="$7"
    local SIM="$8"

    local out_dir="${RESULTS_ROOT}/${trace_name}/timeout_${timeout}"
    mkdir -p "${out_dir}"

    local log_file="${out_dir}/run.log"
    local epoch_file="${out_dir}/epoch_stats.csv"
    local config_file="${RESULTS_ROOT}/configs/DDR4_timeout_${timeout}.ini"

    # Skip if already completed
    if [ -f "${out_dir}/.done" ]; then
        echo "SKIP ${trace_name} timeout=${timeout} (already done)"
        return 0
    fi

    echo "START ${trace_name} timeout=${timeout}"

    # Run simulation
    # Note: You may need to modify ChampSim to accept DRAM config path as argument
    # For now, we rely on environment variable or default config
    "${CHAMPSIM_BIN}" \
        --warmup_instructions "${WARMUP}" \
        --simulation_instructions "${SIM}" \
        -loongarch \
        "${trace_path}" \
        > "${log_file}" 2>&1

    local exit_code=$?

    # Extract epoch stats from log
    grep "^\[EPOCH\]" "${log_file}" | cut -d' ' -f2 > "${epoch_file}"

    if [ ${exit_code} -eq 0 ]; then
        touch "${out_dir}/.done"
        echo "DONE ${trace_name} timeout=${timeout}"
    else
        echo "FAIL ${trace_name} timeout=${timeout} (exit code: ${exit_code})"
    fi

    return ${exit_code}
}

export -f run_single_task
export PROJECT_ROOT RESULTS_ROOT CHAMPSIM_BIN WARMUP SIM

# ===== Parallel Execution =====
echo ""
echo "Starting parallel sweep with ${JOBS} jobs..."
echo "Press Ctrl+C to cancel"
echo ""

# Use GNU parallel if available, otherwise fall back to xargs
if command -v parallel &> /dev/null; then
    parallel -j "${JOBS}" --colsep ' ' \
        run_single_task {1} {2} {3} "${PROJECT_ROOT}" "${RESULTS_ROOT}" "${CHAMPSIM_BIN}" "${WARMUP}" "${SIM}" \
        :::: "${TASK_FILE}"
else
    echo "GNU parallel not found, using sequential execution (slower)"
    while IFS=' ' read -r trace_name timeout trace_path; do
        run_single_task "${trace_name}" "${timeout}" "${trace_path}" \
            "${PROJECT_ROOT}" "${RESULTS_ROOT}" "${CHAMPSIM_BIN}" "${WARMUP}" "${SIM}"
    done < "${TASK_FILE}"
fi

echo ""
echo "=========================================="
echo "Sweep complete!"
echo "Results saved to: ${RESULTS_ROOT}"
echo ""
echo "Next step: Run the analysis script:"
echo "  python3 ${PROJECT_ROOT}/scripts/analyze_oracle_sweep.py"
echo "=========================================="
