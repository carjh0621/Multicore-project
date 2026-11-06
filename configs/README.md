# Configurations

This directory defines **which experiments run**, **how gem5 executes them**, and **which physical assumptions the converters use**. Generated XML, floorplans and simulator results belong under local `results/`, not here.

## Contents and consumers

```text
configs/
├── README.md
├── experiment-startup.json
├── gem5/run_parsec.py
├── mcpat/model-v1.json
└── hotspot/model-v1.json
```

| File | Defines | Read by |
| --- | --- | --- |
| [experiment-startup.json](experiment-startup.json) | Workloads, sweeps, clock/voltage, CPU/cache/memory settings, kernel/disk versions, ROI policy and common power/thermal settings | gem5 runner, batch orchestration, converters and analysis |
| [gem5/run_parsec.py](gem5/run_parsec.py) | Actual board/CPU objects, guest launch command, resources, checkpoint execution and ROI output | gem5 executable; host Python supports `--describe` for inspection |
| [mcpat/model-v1.json](mcpat/model-v1.json) | Physical hardware/activity mappings not directly recoverable from gem5 | `scripts/gem5_to_mcpat.py` and analysis |
| [hotspot/model-v1.json](hotspot/model-v1.json) | Area-derived placement, package/material/cooling assumptions and conservation tolerance | `scripts/mcpat_to_hotspot.py`; identity checked by shared validators |

## Experiment policy

The current revision is `v1`: PARSEC 3.0 `simsmall`, using blackscholes, canneal, freqmine and streamcluster. The core sweep is C1/C4/C8 at width 4; the width sweep is W2/W4/W8 at four cores. The common C4/W4 point appears once per workload, giving 20 cases.

The requested clock is 3 GHz at 1 V. ROB capacity is 192, shared IQ capacity 64, integer/FP physical registers 256 each, and load/store queues 32 each. Width changes all seven listed pipeline widths together, while functional-unit counts remain fixed. Each core has 32 KiB L1I and L1D caches; two shared L2 banks total 2 MiB. Memory is 3 GiB, dual-channel DDR4-2400. Unspecified structures retain the pinned gem5 revision's defaults; each run's generated `config.json` records resolved hardware.

Arguments such as `--cores 8 --width 4` select **one case within the policy**; they do not replace the JSON's shared settings. The runner rejects cases outside the two sweeps. Worker count follows core count, with one hardware thread per core.

Inspect the default case without starting simulation:

```bash
python3 configs/gem5/run_parsec.py --describe
```

## Execution and ROI behavior

The runner creates an X86 board with a switchable Atomic/O3 processor, Ruby `MESI_Two_Level` caches and DDR4 memory. It loads the guest kernel/disk and provides a shell command that initializes and launches PARSEC. Guest `m5` operations cause host simulator events; [scripts/roi_control.py](../scripts/roi_control.py) controls the response.

| Mode | Behavior | Output |
| --- | --- | --- |
| `direct` | Atomic boot/setup → `WORKBEGIN` → O3 → `WORKEND` | Measured ROI without a reusable checkpoint |
| `prepare` | Atomic boot/setup → `WORKBEGIN` → save and stop | Checkpoint and compatibility metadata |
| `restore` | Load boundary → switch to O3 → reset stats → `WORKEND` | Measured ROI, statistics snapshot and tick bounds |

The guest's early `m5 exit` is a launch marker, not the CPU-switch point. Atomic with Ruby bypasses caches. Detailed execution begins at the ROI, so startup effects are included and no warm-up prefix is excluded. On restore, `WORKBEGIN` has already occurred; the host begins measurement after switching instead of waiting for a second marker.

Checkpoints are specific to workload, core/thread count, policy and gem5 revision. The four-core checkpoint can serve widths 2/4/8 because detailed O3 cores were inactive when saved. It cannot serve another core count. Four workloads × three core counts give 12 checkpoints for the 20 cases. At-ROI checkpoints cannot recover earlier detailed warm-up execution.

## McPAT mapping

The converter reads `roi.json`, actual hardware in `config.json`, named activity from `stats.txt`, and the `roi_stats.json` snapshot. It selects the text dump matching both the ROI end tick and elapsed ticks; intermediate progress dumps are ignored. The experiment policy supplies technology, voltage and leakage temperature, while [mcpat/model-v1.json](mcpat/model-v1.json) supplies additional physical assumptions.

| Output under a case's `mcpat/` | Contents |
| --- | --- |
| `input.xml` | Hardware parameters and separate ROI activity per core/L2 bank |
| `mapping.json` | Parameter/counter sources, derivations, hashes, timing and limitations |
| `mcpat.txt`, `mcpat.stderr.txt` | Detailed upstream power/area report and diagnostics |
| `power.json` | Chip runtime dynamic/static/total watts, area, ROI energy and provenance |

Average chip power is **Runtime Dynamic + Subthreshold Leakage + Gate Leakage**; `Peak Dynamic` is not the ROI average. Exact tick-derived elapsed time is used for normalization, rather than summing core cycles. The converter accounts for the integer-MHz McPAT clock representation and documented O3 idle-cycle bookkeeping surplus without changing ROI duration. Architectural instructions and micro-ops remain distinct.

The model uses 45 nm/1 V and fixed leakage at 350 K. The shared 64-entry IQ becomes 32 integer + 32 FP entries. SIMD uses scalar-equivalent FPU activity, cache demand read/write splits use measured load/store ratios, and additional coherence/fill/writeback activity is incomplete. SimpleNetwork becomes an approximate physical router/flit/buffer model. Generic memory-controller/PHY power is included; DRAM chips and board power are excluded. All current cases report L2 latency-constraint warnings; physical timing closure and absolute power/area are not validated.

## HotSpot mapping

The thermal converter reads the completed ROI and McPAT XML, mapping, detailed report and power summary. [hotspot/model-v1.json](hotspot/model-v1.json) controls geometry, cooling and conservation tolerance.

| Thermal block | Power/area source |
| --- | --- |
| `coreN_logic` | Core N minus its separately counted Instruction Cache and Data Cache |
| `coreN_l1i`, `coreN_l1d` | The core's private caches |
| `l2_bankN` | Each shared L2 bank |
| `noc` | Whole equivalent network |
| `memory_controllers` | Aggregate `Total MCs`, avoiding mixed per-controller/aggregate detail |

Parent totals are checks, not additional heat sources. Blocks must conserve processor area and each power component within printed rounding allowance (relative `5e-5`, absolute `1e-6`); discrepancies are not silently rescaled. Four cores produce 16 blocks: 12 core/L1 blocks, two L2 banks, network and controllers.

The square die uses summed modeled areas, with no whitespace. Recursive area bisection keeps each core's logic/L1 group together. Dimensions convert from mm² to m²; overlap, incomplete coverage and package-fit checks are enforced. Cooling uses 45 °C ambient, 0.15 mm silicon, a 50 mm spreader, a 70 mm heat sink and `r_convec=0.1 K/W`. Geometry and adjacency can change between configurations. Network/controllers are spatially lumped.

| Output under a case's `hotspot/` | Contents |
| --- | --- |
| `floorplan.flp` | Block dimensions and coordinates in meters |
| `power.ptrace` | Matching block names and one row of ROI-average watts, without a time column |
| `hotspot.config` | Explicit package/material/cooling settings |
| `mapping.json` | Block sources, geometry, power, conservation checks and hashes |
| `steady.temp` | Solver temperatures in Kelvin, including package layers |
| `temperature.json` | Named silicon-block Celsius/Kelvin values and silicon-only maximum |
| `hotspot.stdout.txt`, `hotspot.stderr.txt` | Solver logs |

The floorplan, power input, configuration and mapping are generated without running the solver; `temperature.json` requires successful solver-output validation. Celsius is Kelvin minus 273.15, with original text precision 0.01 K. Interface/spreader/sink/internal package nodes are excluded from the headline maximum.

Steady state assumes average power applied indefinitely; it is not end-of-ROI temperature. Fixed leakage is not recalculated from HotSpot temperatures, and there is no thermal throttling. Geometry/cooling are surrogate assumptions, not a real CPU layout or calibrated package. Both model JSON files record these limitations; see the [full analysis](../non_warm_up_analysis.md) for their implications and the [scripts guide](../scripts/README.md#one-case-through-the-flow) for conversion commands.

## Changes and compatibility

Policies and input hashes are saved with checkpoints/results. Editing a JSON does not update existing measurements; incompatible artifacts are rejected. Keep the completed reference intact when planning warm-up, use separate result/checkpoint destinations and record a new policy identity for a changed measurement method. Stop active stages before editing their inputs or code.

Start with the [project setup](../README.md#getting-started) and [scripts guide](../scripts/README.md) for single-case commands, batch execution and retries.
