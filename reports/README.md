# Reports and extracted data

This directory contains **shareable analysis outputs**, rather than raw simulator runs. Both full-ROI scenarios have completed: four PARSEC workloads × five hardware configurations × two methodologies = 40 measurements, with one observation per scenario/case.

| Report | Contents |
| --- | --- |
| [Non-warm-up analysis](non_warm_up/non_warm_up_AI_analysis.md) | Preserved original cold results and 23 figure sets |
| [Warm-up analysis](warm_up/warm_up_AI_analysis.md) | Pre-ROI initialization scenario, its scaling/costs and 23 figure sets |
| [Matched comparison](warm_up_comparison/warm_up_vs_non_warm_up_AI_analysis.md) | 20 paired cases, changed conclusions, activity/quiesce diagnostics and 15 figure sets |

Each report keeps its figures/data alongside it so relative links work after cloning. All figure sets include PNG, SVG and PDF. The original non-warm-up publication is unchanged.

## Contents

```text
reports/
├── README.md
├── non_warm_up/
│   ├── non_warm_up_AI_analysis.md
│   ├── data/                      # Original metrics and provenance
│   ├── figures/                   # Original 23 figure sets
│   └── interactive_tradeoffs.html
├── warm_up/
│   ├── warm_up_AI_analysis.md
│   ├── data/                      # ROI, initialization and active/quiesce metrics
│   ├── figures/                   # 23 figure sets
│   └── interactive_tradeoffs.html
└── warm_up_comparison/
    ├── warm_up_vs_non_warm_up_AI_analysis.md
    ├── data/                      # Matched metrics, hardware and independent checks
    ├── reference/data/            # Re-extracted cold data; originals preserved
    ├── figures/                   # 15 comparison figure sets
    └── interactive_comparison.html
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

## Warm-up and matched comparison

The [warm-up policy](../configs/experiment-warmup.json) uses separate `warmup-v1` raw-output/checkpoint roots and preserves the same hardware, workloads and full ROI. Detailed PARSEC initialization is excluded from measurement and recorded in `warmup.json`/`warmup_stats.json`. The final ROI statistics still feed the same physical-model JSON files and converters. See the [execution instructions](../scripts/README.md#warm-up-policy).

The [new analysis entry point](../scripts/analyze_warm_up.py) validates and extracts both completed scenarios, verifies matched hardware/geometry and generates the warm report and comparison:

```bash
.venv-analysis/bin/python scripts/analyze_warm_up.py
```

It writes only `warm_up/` and `warm_up_comparison/`. `--extract-only` omits plotting; `--reuse-extracted` validates source hashes and extractor identity before reusing tables. Raw results, model policies and matching recorded binaries are required. No simulator is run.

Warm tables extend the original schema with excluded initialization duration/activity, mean recorded active fraction, active-cycle throughput and quiesce diagnostics. Missing quiesce/event counters are treated as zero only where the pinned gem5 source explicitly suppresses zero values via `.prereq(stat)`. Active/idle/quiesce counters are diagnostic accounting measures, not a strict partition to sum to 100%.

| Comparison table | Contents |
| --- | --- |
| [matched_cases.csv](warm_up_comparison/data/matched_cases.csv) | All 20 cold/warm pairs, absolute metrics, paired ratios/deltas |
| [matched_sweeps.csv](warm_up_comparison/data/matched_sweeps.csv) | 24 paired sweep rows, using each scenario's own baseline |
| [component_changes.csv](warm_up_comparison/data/component_changes.csv) | Signed dynamic/static/component watt changes |
| [configuration_choices.csv](warm_up_comparison/data/configuration_choices.csv) | Lowest observed time, energy and EDP per workload/scenario |
| [hardware_checks.csv](warm_up_comparison/data/hardware_checks.csv) | Equality of resolved cores/caches/memory and thermal inputs |
| [independent_checks.json](warm_up_comparison/data/independent_checks.json) | Raw tick/power arithmetic, scenario-specific baselines and published-reference reproduction |
| [validation.json](warm_up_comparison/data/validation.json) | Policy/model/tool and analysis-source identities; 560 hashed source artifacts across both extractions |

The comparison's figures cover paired ROI times and cost changes (01–02), core/width scaling (03–04), power/temperature (05–06), normalized energy/EDP (07), activity changes (08), component watts (09), dynamic power versus time/energy (10), initialization coverage (11), paired thermal maps (12), blackscholes active-cycle evidence (13), time-ratio accounting (14) and changes in normalized scaling (15). Open the [interactive comparison](warm_up_comparison/interactive_comparison.html) offline in a browser.

Pre-ROI conditioning does not establish convergence, isolate cache effects or provide replication-based confidence. The report distinguishes changed executed work, activity/quiescence and possible scheduling/history effects; all physical-model limitations persist. No warm-up results overwrite the frozen cold publication.
