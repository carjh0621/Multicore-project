# Reports and extracted data

This directory contains **shareable analysis outputs**, rather than raw simulator runs. The current study is the full-ROI, non-warm-up `v1` experiment: four PARSEC workloads × five hardware configurations = 20 measurements.

Read [non_warm_up_AI_analysis.md](non_warm_up/non_warm_up_AI_analysis.md) for the findings, metric definitions, complete case table and limitations. Its plots live here so relative links work after cloning.

## Contents

```text
reports/
├── README.md
└── non_warm_up/
    ├── non_warm_up_AI_analysis.md
    ├── data/
    │   ├── summary.csv
    │   ├── sweeps.csv
    │   ├── power_components.csv
    │   ├── per_core.csv
    │   ├── thermal_blocks.csv
    │   ├── power_scaling_intervals.csv
    │   ├── mcpat_warnings.csv
    │   ├── input_sha256.json
    │   └── validation.json
    ├── figures/                    # Figures 01–23, each as PNG/SVG/PDF
    └── interactive_tradeoffs.html
```

Download the [interactive chart](non_warm_up/interactive_tradeoffs.html) and open it in a browser for hoverable metrics and workload selection. Plotly is bundled for offline use. The report embeds PNGs; SVG/PDF versions are provided for presentations and documents.

## Sources and extraction

[Scripts/analyze_non_warm_up.py](../scripts/analyze_non_warm_up.py) reads completed cases from local `results/v1/<benchmark>-cN-wW/`. Raw results are excluded from Git; this directory contains derived tables/figures.

| Source artifact for each case | Extracted information |
| --- | --- |
| `roi.json` | Completed ROI identity, exact tick bounds and simulated duration |
| `run_manifest.json` | Workload/configuration, policy hash, execution method and upstream tool commits |
| `config.json` | Actual cores, clocks, queues, caches, network and memory structures |
| `stats.txt` | Committed instructions/μops, cache activity, OpClasses, network/memory counters and queue-pressure events |
| `roi_stats.json` | Snapshot for cross-checking ROI boundaries and root statistics |
| `mcpat/input.xml`, `mapping.json`, `mcpat.txt`, `power.json` | Mapping provenance, detailed and aggregate power/area, model warnings |
| `hotspot/mapping.json`, `floorplan.flp`, `steady.temp`, `temperature.json` | Disjoint block geometry/power, thermal provenance and silicon temperatures |

The extraction process:

1. Validate all 20 completed gem5/McPAT/HotSpot cases against policy, models and recorded provenance.
2. Select the text-statistics dump whose `finalTick` and `simTicks` match the completed ROI. Intermediate progress dumps do not become measurements. Named activity comes from text because this gem5 revision's JSON visitor omits some groups/labels.
3. Compute ROI duration from ticks, and extract McPAT Runtime Dynamic plus fixed subthreshold/gate leakage. Calculate total power, energy and EDP.
4. Separate detailed core logic, L1I, L1D, L2, network and controller power without double counting. Check chip-total conservation within printed rounding tolerance.
5. Extract maximum named silicon-block temperature and per-block values, excluding interface/package nodes from the headline maximum.
6. Normalize the core sweep to C1/W4 and width sweep to C4/W2, independently for each workload. Generate CSVs, plots, interactive HTML and the analysis report.

Performance uses **simulated ROI time**, not host execution time or `hostInstRate`. Energy is `average power × ROI seconds`; normalized energy is `power ratio / speedup`, and normalized EDP is `power ratio / speedup²`. Temperature changes use °C differences, not Celsius percentages.

## Data tables

| File | Contents |
| --- | --- |
| [summary.csv](non_warm_up/data/summary.csv) | 20 unique cases: time, power, energy, EDP, area, temperature and diagnostic counters |
| [sweeps.csv](non_warm_up/data/sweeps.csv) | 24 normalized rows with sweep/baseline identities, speedup, cost ratios and scaling efficiency |
| [power_components.csv](non_warm_up/data/power_components.csv) | Disjoint component area, dynamic/leakage/total watts and chip-power share |
| [per_core.csv](non_warm_up/data/per_core.csv) | Retired work, throughput, activity share, power and maximum core/L1 temperature per core |
| [thermal_blocks.csv](non_warm_up/data/thermal_blocks.csv) | Block dimensions/coordinates, area, power and Celsius temperature |
| [power_scaling_intervals.csv](non_warm_up/data/power_scaling_intervals.csv) | Adjacent-setting power ratios, elasticity, incremental watts and speedup |
| [mcpat_warnings.csv](non_warm_up/data/mcpat_warnings.csv) | The 40 recorded L2 timing warnings across 20 cases |
| [input_sha256.json](non_warm_up/data/input_sha256.json) | Paths/hashes of 260 source artifacts used in analysis |
| [validation.json](non_warm_up/data/validation.json) | Case counts, policy/tool identities, analysis-source hash, package versions and check summary |

The sweep table has 24 rows because C4/W4 appears in both 12-point sweeps. It does **not** represent 24 runs or statistical repetitions.

Units appear in column names: `_w` is watts, `_j` joules, `_j_s` energy-delay product, `_mm2` area, `_m` lengths and `_c` Celsius. Timing is in seconds/milliseconds. `uipc_per_core_wall` divides committed μops by core count and elapsed cycles, including idle time. Architectural instructions, issued operations and committed μops are distinct; queue-full event counts are not additive stall-cycle fractions.

## Figures

| Figures | Subject |
| --- | --- |
| 01–02 | Core/width speedup and scaling efficiency |
| 03–04 | Absolute power and peak equilibrium temperature |
| 05–06 | Normalized power and static-power fraction |
| 07–08 | Disjoint component power in each sweep |
| 09–10 | Dynamic versus static chip/component power |
| 11–12 | Normalized energy and energy-delay product |
| 13–14 | Speedup/power tradeoffs, including temperature and modeled area |
| 15–16 | Executed work, throughput, cache misses and network activity |
| 17 | Interval power elasticity |
| 18 | Modeled area and static-power cost |
| 19 | Temperature versus power and average power density |
| 20–21 | Example thermal floorplans using common length/color scales |
| 22 | Eight-core retired-work shares and core temperatures |
| 23 | Issued-operation mix and IQ-full rename events |

Captions and PNG/SVG/PDF download links for all 23 figures are in the [full report](non_warm_up/non_warm_up_AI_analysis.md). Connecting lines do not imply measurements at intermediate configurations.

## Regenerate and share

From the project root, after completing all 20 cases:

```bash
python3 -m venv .venv-analysis
.venv-analysis/bin/python -m pip install -r requirements-analysis.txt
.venv-analysis/bin/python scripts/analyze_non_warm_up.py
```

Optional `--results-root PATH` and `--output-dir PATH` select other locations. The script regenerates analysis assets and the analysis report; it does not run/rebuild simulators or modify raw results/checkpoints.

Full regeneration requires raw source artifacts, matching policies/tool revisions and McPAT/HotSpot executables matching the recorded binary hashes. Rebuilding with another compiler/environment may change those hashes even at the same source commit. CSVs support independent metric calculations, but the current script does not regenerate the complete report from CSVs alone. Either execute a new full flow with local builds or obtain matching original artifacts.

Commit this directory together with `configs/`, `scripts/`, dependency file and submodule references. Keep raw outputs, checkpoints, downloaded resources and binaries outside normal Git history; share them separately if needed for reproduction. Paths in `input_sha256.json` identify original raw inputs and need not exist in a fresh clone.

The study has one observation per case. Power/area depend on approximate analytical mappings and need calibration, particularly cache/NoC costs. Temperature is equilibrium under sustained average power and assumed geometry/cooling, with fixed leakage and no thermal feedback. The report separates observed behavior, model-dependent estimates and hypotheses requiring further evidence.
