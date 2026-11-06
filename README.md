# Multicore Project

This project connects **gem5, McPAT and HotSpot** to evaluate the performance, power and temperature of multicore out-of-order CPUs. It studies how adding cores and widening pipelines affect execution time, chip power, energy and modeled steady-state temperature.

The workloads are PARSEC 3.0 `simsmall`: **blackscholes, canneal, freqmine and streamcluster**, measured over their regions of interest (ROIs). All **20 non-warm-up cases** have completed the flow. Read the [full analysis](reports/non_warm_up/non_warm_up_AI_analysis.md) for the findings and limitations, or browse the [reports guide](reports/README.md) for data and plots. Reading the published report requires no simulator build.

## Experiment

| Sweep | Core counts | Pipeline widths | Speedup baseline |
| --- | --- | --- | --- |
| Thread-level parallelism | 1, 4, 8 | Fixed at 4 | 1 core / width 4 |
| Instruction-level parallelism | Fixed at 4 | 2, 4, 8 | 4 cores / width 2 |

The common 4-core / width-4 point is measured once per workload: **five configurations × four workloads = 20 cases**, with one measured run per case. Width changes fetch, decode, rename, dispatch, issue, writeback and commit together. Worker count follows core count; there is no SMT. Hardware and measurement settings are in [configs/experiment-startup.json](configs/experiment-startup.json).

The current method fast-forwards to `WORKBEGIN` on Atomic cores and saves a checkpoint. Restoring switches to O3 and measures the complete ROI through `WORKEND`. No detailed warm-up prefix is excluded; startup effects are included.

## Evaluation flow

```text
PARSEC workload + experiment policy
                 │
         gem5 prepare: Atomic boot/setup → checkpoint at WORKBEGIN
                 │
         gem5 restore: O3 execution → WORKEND
                 │
         ROI time + hardware configuration + activity counters
                 │  scripts/gem5_to_mcpat.py
                 ▼
         McPAT XML → McPAT → power and area
                 │  scripts/mcpat_to_hotspot.py
                 ▼
         block power + floorplan + cooling settings → HotSpot
                 │
         steady-state silicon temperatures
                 │  scripts/analyze_non_warm_up.py
                 ▼
         normalized metrics + CSVs + plots + analysis report
```

Average chip power is **Runtime Dynamic + Subthreshold Leakage + Gate Leakage**, including modeled cores, caches, interconnect and memory controllers. External DRAM and board power are excluded. HotSpot estimates equilibrium under sustained ROI-average power, not the temperature reached during the short ROI. Power/floorplan models are approximate, leakage is fixed at 350 K, and all completed McPAT cases report L2 timing-constraint warnings. See the [analysis](reports/non_warm_up/non_warm_up_AI_analysis.md) before interpreting absolute physical estimates.

## Directory structure

```text
Multicore-project/
├── README.md
├── requirements-analysis.txt       # Python analysis/plotting dependencies
├── .gitignore
├── .gitmodules                     # Pinned upstream submodule locations
├── configs/
│   ├── README.md                   # Settings, consumers and ROI behavior
│   ├── experiment-startup.json     # Experiment matrix and common policy
│   ├── gem5/run_parsec.py           # Board, CPUs, guest workload and execution
│   ├── mcpat/model-v1.json         # Power-model assumptions
│   └── hotspot/model-v1.json       # Thermal-model assumptions
├── external/                       # Git submodules; build each tool locally
│   ├── gem5/
│   ├── mcpat/
│   └── hotspot/
├── scripts/
│   ├── README.md                   # Script relationships, inputs and outputs
│   ├── batch_{prepare,restore,mcpat,hotspot}.py
│   ├── batch_common.py             # Shared scheduling and validation
│   ├── batch_status.py             # Per-case progress viewer
│   ├── roi_control.py              # ROI events and checkpoint compatibility
│   ├── gem5_to_mcpat.py
│   ├── mcpat_to_hotspot.py
│   └── analyze_non_warm_up.py
└── reports/
    ├── README.md                   # Data sources, extraction and figure guide
    └── non_warm_up/
        ├── non_warm_up_AI_analysis.md # Full interpretation of the study
        ├── data/                   # Extracted metrics and provenance
        ├── figures/                # 23 figures, each in PNG/SVG/PDF
        └── interactive_tradeoffs.html
```

Runtime files are created locally and excluded from Git:

```text
resources/                                      # Downloaded kernel and PARSEC disk
checkpoints/v1/<benchmark>-c<cores>/              # 12 reusable ROI checkpoints
results/v1/<benchmark>-c<cores>-prepare/          # Preparation outputs
results/v1/<benchmark>-c<cores>-w<width>/          # 20 measured gem5 cases
    mcpat/                                      # Power inputs and results
    hotspot/                                    # Thermal inputs and results
results/v1/batch_logs/<stage>/<batch-id>/         # Job logs and status.json
```

The repository includes the source and published analysis, not raw simulation outputs, downloaded images or checkpoints. A clone is enough to read the report; executing the flow requires local builds and resources.

## Getting started

Run commands from the project root. The reference environment is Ubuntu 22.04, including Ubuntu under WSL2. Batch scripts use Linux process/locking facilities.

### 1. Clone the project and tools

```bash
git clone --recurse-submodules https://github.com/carjh0621/Multicore-project.git
cd Multicore-project
```

For an existing clone without initialized tools:

```bash
git submodule update --init --recursive
```

Use the pinned submodule commits. Updating upstream revisions can change behavior, output formats and checkpoint compatibility.

### 2. Build the tools

An Ubuntu dependency set based on the pinned gem5 source's [Ubuntu 22.04 recipe](https://github.com/gem5/gem5/blob/e97d8fbfbe4ff549b2c90183956f719ada856756/util/dockerfiles/ubuntu-22.04_all-dependencies/Dockerfile):

```bash
sudo apt update
sudo apt install build-essential git m4 scons zlib1g-dev \
  libprotobuf-dev protobuf-compiler libprotoc-dev libgoogle-perftools-dev \
  python3-dev python3-venv python3-pip libboost-all-dev libhdf5-dev \
  libpng-dev libelf-dev pkg-config

scons -C external/gem5 build/X86_MESI_Two_Level/gem5.opt -j2
make -C external/mcpat -j2 CXX=g++ CC=gcc NTHREADS=1
make -C external/hotspot -j2 hotspot
```

The outputs are `external/gem5/build/X86_MESI_Two_Level/gem5.opt`, `external/mcpat/mcpat` and `external/hotspot/hotspot`. The gem5 target includes X86 and Ruby `MESI_Two_Level`. McPAT's compiler overrides avoid its default 32-bit build requirement; `NTHREADS=1` controls host-side analytical search threads, not simulated cores. Start with modest build concurrency because gem5 compilation can consume substantial memory.

### 3. Inspect settings and prepare the first checkpoint

Print the default settings without launching gem5:

```bash
python3 configs/gem5/run_parsec.py --describe
```

On a fresh clone, prepare blackscholes at four cores **without `--offline`**, allowing gem5 to download the pinned kernel and PARSEC image:

```bash
external/gem5/build/X86_MESI_Two_Level/gem5.opt \
  -d results/v1/blackscholes-c4-prepare \
  configs/gem5/run_parsec.py \
  --mode prepare --benchmark blackscholes --cores 4 --width 4 \
  --checkpoint-dir checkpoints/v1/blackscholes-c4 \
  --boot-cpu atomic
```

Resources are downloaded into `resources/`; allow time, network access and sufficient disk space for the large disk image. Preparation finishes at `WORKBEGIN`, after saving a checkpoint. It does not measure the detailed ROI. This command requires fresh output/checkpoint destinations; use the batch workflow to inspect or reuse existing work.

### 4. Run the four stages

Batch jobs use cached resources in offline mode. After the first preparation succeeds:

```bash
python3 scripts/batch_prepare.py --dry-run

python3 scripts/batch_prepare.py --jobs 2 && \
python3 scripts/batch_restore.py --jobs 2 && \
python3 scripts/batch_mcpat.py --jobs 2 && \
python3 scripts/batch_hotspot.py --jobs 2
```

The stages cover **12 checkpoint preparations**, **20 detailed ROI runs**, **20 McPAT runs** and **20 HotSpot runs**. Compatible completed work is skipped. Each stage finishes before the next begins; `--jobs 2` runs independent cases concurrently inside a stage. Use `--jobs 1` for serial execution or limited host memory. This does not parallelize one gem5 simulation.

Monitor from another terminal:

```bash
python3 scripts/batch_status.py --details
watch -n 10 -t python3 scripts/batch_status.py
```

See the [scripts guide](scripts/README.md) for single-case commands, batch options, retries, logs and tmux usage.

### 5. Generate the analysis

After all 20 cases complete:

```bash
python3 -m venv .venv-analysis
.venv-analysis/bin/python -m pip install -r requirements-analysis.txt
.venv-analysis/bin/python scripts/analyze_non_warm_up.py
```

This validates saved artifacts and regenerates the analysis report, CSVs, 23 static figure sets and offline interactive chart. It does not run simulators. Full provenance-checked regeneration requires raw results and McPAT/HotSpot executables matching the recorded binary hashes; published CSVs alone are insufficient. See [reports/README.md](reports/README.md) for extraction and sharing details.

## Third-party software and licenses

[gem5](https://github.com/gem5/gem5), [McPAT](https://github.com/HewlettPackard/mcpat) and [HotSpot](https://github.com/uvahotspot/HotSpot) remain Git submodules under their upstream licenses. The parent repository records their commits rather than copying their source into project-owned directories. Original license files, copyright notices and source headers are preserved; individual files and bundled dependencies may have separate licenses.
