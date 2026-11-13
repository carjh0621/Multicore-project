# Scripts and execution flow

These scripts orchestrate cases, convert outputs between tools and extract the final analysis. Run commands from the repository root. The gem5 entry point is [configs/gem5/run_parsec.py](../configs/gem5/run_parsec.py), executed by **gem5**, which supplies the `m5` API. Batch/converter/analysis scripts use **host Python**. `run_parsec.py --describe` exits before gem5 imports, so inspection also works with host Python.

## Connections

```text
configs/experiment-startup.json
                 │
         batch_common.py ← shared by the four stage entry points
                 │
    1. batch_prepare.py → gem5 run_parsec.py → ROI checkpoint
    2. batch_restore.py → gem5 run_parsec.py → measured ROI
                           └─ imports roi_control.py
    3. batch_mcpat.py → gem5_to_mcpat.py → McPAT results
    4. batch_hotspot.py → mcpat_to_hotspot.py → HotSpot results
                 │
         saved outputs + stage logs
                 ├─ batch_status.py → progress table
                 └─ analyze_non_warm_up.py → CSVs, plots and report
```

Both prepare and restore use `run_parsec.py` and `roi_control.py`. Each batch command waits for its selected jobs to finish. The intended ordering is prepare **all checkpoints**, restore **all ROIs**, run **all McPAT cases**, then **all HotSpot cases**.

## Files

| Script | Role and outputs |
| --- | --- |
| [batch_prepare.py](batch_prepare.py) | Stage 1: prepare/reuse 12 checkpoints under `checkpoints/v1/<benchmark>-cN/` |
| [batch_restore.py](batch_restore.py) | Stage 2: measure 20 O3 ROIs under `results/v1/<benchmark>-cN-wW/` |
| [batch_mcpat.py](batch_mcpat.py) | Stage 3: convert and run McPAT for each case, creating `mcpat/` |
| [batch_hotspot.py](batch_hotspot.py) | Stage 4: convert and run steady-state HotSpot, creating `hotspot/` |
| [batch_common.py](batch_common.py) | Shared matrix, prerequisites, completion checks, locks, subprocess scheduling, logs and retries |
| [batch_status.py](batch_status.py) | Read-only metadata/log viewer; prints per-case states or JSON |
| [roi_control.py](roi_control.py) | Imported helper for ROI events, CPU switching, stats reset/capture and checkpoint compatibility |
| [gem5_to_mcpat.py](gem5_to_mcpat.py) | Completed ROI/config/statistics + power policy → XML, mapping, detailed report and `power.json` |
| [mcpat_to_hotspot.py](mcpat_to_hotspot.py) | McPAT results + thermal policy → floorplan, block power, cooling config and temperatures |
| [analyze_non_warm_up.py](analyze_non_warm_up.py) | All 20 completed cases → extracted metrics, 23 figure sets, offline HTML and Markdown analysis |
| [analyze_warm_up.py](analyze_warm_up.py) | Both 20-case datasets → warm report/23 figure sets, matched comparison/15 figure sets, CSVs, validation and offline HTML |

The batch/control/converter scripts use the Python standard library. Analysis additionally needs [requirements-analysis.txt](../requirements-analysis.txt). Tools are built separately using the [project setup](../README.md#getting-started).

## One case through the flow

The commands below use already built tools and fresh destinations for blackscholes C4/W4. On first download omit `--offline` from preparation; afterward it uses the cached kernel/disk.

### 1. Prepare

```bash
external/gem5/build/X86_MESI_Two_Level/gem5.opt \
  -d results/v1/blackscholes-c4-prepare \
  configs/gem5/run_parsec.py \
  --mode prepare --benchmark blackscholes --cores 4 --width 4 \
  --checkpoint-dir checkpoints/v1/blackscholes-c4 \
  --boot-cpu atomic --offline
```

Atomic cores boot the guest and initialize PARSEC. At `WORKBEGIN`, the ROI controller requests a stop and the runner saves architectural state/compatibility metadata. This does not measure a detailed ROI or run a physical model.

### 2. Restore

```bash
external/gem5/build/X86_MESI_Two_Level/gem5.opt \
  -d results/v1/blackscholes-c4-w4 \
  configs/gem5/run_parsec.py \
  --mode restore --benchmark blackscholes --cores 4 --width 4 \
  --checkpoint-dir checkpoints/v1/blackscholes-c4 \
  --boot-cpu atomic --offline
```

The runner restores the boundary, completes startup without advancing guest time, switches to O3 and resets statistics. `WORKEND` triggers a dump/snapshot and `roi.json`. The complete interval includes cold-start effects. The same four-core architectural checkpoint can serve widths 2/8 using separate measured output directories.

### 3. Convert and run McPAT

```bash
python3 scripts/gem5_to_mcpat.py \
  --run-dir results/v1/blackscholes-c4-w4 --run-mcpat
```

The converter selects the text-statistics dump matching the ROI, checks its JSON snapshot and reads actual hardware from `config.json`. It emits `mcpat/input.xml` plus `mapping.json`; McPAT produces `mcpat.txt`. The converter extracts Runtime Dynamic, leakage, area and average watts into `power.json`. See the [power mapping](../configs/README.md#mcpat-mapping).

### 4. Convert and run HotSpot

```bash
python3 scripts/mcpat_to_hotspot.py \
  --run-dir results/v1/blackscholes-c4-w4 --run-hotspot
```

The converter subtracts private-cache children from core totals, then counts separate L1/L2/network/controller blocks once. It checks power/area conservation, creates an area-derived floorplan, writes block watts/cooling settings and invokes HotSpot. `temperature.json` contains silicon-block temperatures and their maximum. See the [thermal mapping](../configs/README.md#hotspot-mapping).

Neither converter reruns gem5. Omitting `--run-mcpat` or `--run-hotspot` generates inputs only. Existing named outputs require `--force` to replace; batch retries instead archive incomplete/stale target directories. Use separate outputs when comparing changed models.

## Batches and progress

For all cases:

```bash
python3 scripts/batch_prepare.py --jobs 2 && \
python3 scripts/batch_restore.py --jobs 2 && \
python3 scripts/batch_mcpat.py --jobs 2 && \
python3 scripts/batch_hotspot.py --jobs 2
```

Each entry point calls `stage_main()` in `batch_common.py`. It constructs the matrix, deduplicates checkpoint targets, checks all prerequisites and skips compatible completed work. It runs up to `--jobs N` independent processes, with separate output/working directories and isolated McPAT scratch files. A shared lock prevents overlapping stages, and frozen input hashes/completion artifacts are checked during execution.

```bash
python3 scripts/batch_status.py --details
python3 scripts/batch_status.py --verify
```

The viewer reads metadata/logs without signaling gem5 or launching simulations; `--verify` checks hashes/artifacts more deeply. Published reports do not make a fresh clone's absent local runs appear complete. The preparation summary counts 12 checkpoint targets, even though widths share preparation status across 20 rows.

### Options and retries

| Option on each batch entry point | Purpose |
| --- | --- |
| `--jobs N` | Maximum independent concurrent processes; default 2, use 1 for serial execution |
| `--policy PATH` | Select experiment JSON; defaults to non-warm-up `configs/experiment-startup.json` |
| `--dry-run` | Print `RUN`, `SKIP`, `BLOCKED` and planned commands without creating outputs or launching jobs |
| `--retry` | Archive incomplete/stale target directories, then retry; keep validated completed work |
| `--benchmarks blackscholes canneal` | Select a workload subset |
| `--results-root PATH` | Override `results/v1`; use consistently between stages |
| `--checkpoints-root PATH` | Override `checkpoints/v1`; keep separate from results |
| `--gem5-binary`, `--mcpat-binary`, `--hotspot-binary` | Select built executables |

Inspect a plan before execution:

```bash
python3 scripts/batch_prepare.py --dry-run
```

An existing directory alone does not prove completion. Preparation checks checkpoint identity/files; restore checks completed ROI metadata, timing and snapshots; converters check model/input/binary hashes and result consistency. If partial or stale output blocks a stage, inspect its log and fix the cause before retrying:

```bash
python3 scripts/batch_restore.py --jobs 2 --retry
```

Retry renames incomplete/stale targets to `.incomplete-<timestamp>-<pid>` siblings; it does not delete them. Preparation can archive both checkpoint and preparation output; converter stages archive only their own output directories. Changed upstream artifacts can make downstream outputs stale. If a job fails, active siblings can finish, pending jobs remain unstarted and the stage returns failure so `&&` does not advance. Stop active stages before changing their inputs/code. The current width-2 workaround is already applied.

### Logs and progress states

```text
results/v1/batch_logs/<stage>/<timestamp>-<batch-pid>/
├── status.json
└── <case>.log
```

The terminal prints each child PID and log path. `status.json` stores commands, outputs, states, timestamps, return codes and input hashes. `DONE` means completion checks pass; `PENDING` means no output; `INCOMPLETE` means partial output; `STALE` means identity/model mismatch. Failures/interruption are recorded separately. `RUNNING`/`QUEUED` reflect the latest batch log and do not prove a PID remains alive after a hard kill. The guest console is each case's `board.pc.com_1.device` file.

For an SSH session, run the sequence inside tmux if installed:

```bash
tmux new -s multicore
```

Run the four-stage sequence there; detach with `Ctrl+B`, then `D`, and reconnect using `tmux attach -t multicore`. The host/WSL must remain running and awake. Interrupting a foreground batch stops its active child process groups. Concurrent cases use private outputs/copy-on-write state and read shared checkpoints without modifying them; gem5 listeners are disabled in batch jobs to avoid port contention. Concurrency affects host execution time, not the simulated-time metric.

## Warm-up policy

All four batch scripts, the status viewer and both converters support `--policy configs/experiment-warmup.json`. The gem5 runner supports the same selector. The policy's revision sets default local roots to `results/warmup-v1/` and `checkpoints/warmup-v1/`; pass the selector consistently at every stage.

Unlike the default workflow, warm-up preparation saves at the PARSEC launch marker. Restore switches to O3 before application initialization, captures its statistics at `WORKBEGIN`, resets counters while retaining state, then measures the full ROI. An old at-ROI checkpoint cannot supply this earlier execution. See the [configuration guide](../configs/README.md#warm-up-policy) for identities and validation.

Start with the [single C4/W4 pilot](../README.md#warm-up-evaluation). After its measured ROI finishes, run its converters:

```bash
python3 scripts/gem5_to_mcpat.py --policy configs/experiment-warmup.json \
  --run-dir results/warmup-v1/blackscholes-c4-w4 --run-mcpat
python3 scripts/mcpat_to_hotspot.py --policy configs/experiment-warmup.json \
  --run-dir results/warmup-v1/blackscholes-c4-w4 --run-hotspot
```

When the pilot and its warm-up/ROI artifacts pass validation, use the same stage ordering for all 20 cases:

```bash
python3 scripts/batch_prepare.py --policy configs/experiment-warmup.json --jobs 2 && \
python3 scripts/batch_restore.py --policy configs/experiment-warmup.json --jobs 2 && \
python3 scripts/batch_mcpat.py --policy configs/experiment-warmup.json --jobs 2 && \
python3 scripts/batch_hotspot.py --policy configs/experiment-warmup.json --jobs 2
```

Compatible completed pilot work is skipped. The matrix still needs 12 preparations and 20 measurements. The earlier checkpoints omit repeated boot, but O3 initialization repeats per hardware case and can be expensive; it is excluded from reported ROI time, energy and power. Its duration/counters remain in `warmup.json` and `warmup_stats.json` for inspection.

```bash
python3 scripts/batch_status.py --policy configs/experiment-warmup.json --details
python3 scripts/batch_status.py --policy configs/experiment-warmup.json --verify
```

The completed non-warm-up reference and report stay intact. Both scenarios are complete; use the separate warm-analysis entry point below. The `analyze_non_warm_up.py` CLI remains dedicated to the original policy.

## Analysis

With analysis dependencies installed and all 20 cases complete:

```bash
.venv-analysis/bin/python scripts/analyze_non_warm_up.py
```

Analysis imports shared validators and converter parsers, verifies saved binary identities, rereads ROI activity/disjoint powers and normalizes both sweeps. It does not invoke simulator executables. The [reports guide](../reports/README.md) explains source artifacts, derived tables, figure families and provenance requirements.

For the completed warm scenario and its matched cold comparison:

```bash
.venv-analysis/bin/python scripts/analyze_warm_up.py
```

This explicitly supplies the warm policy to shared extraction, re-extracts the cold reference into the comparison directory and checks resolved hardware and paired geometry. It writes `reports/warm_up/` and `reports/warm_up_comparison/` only. Matching uses workload/core/width identity; each scenario retains its own normalization baseline. Quiesce/active-cycle diagnostics help distinguish inactive time from changes in throughput while active, without claiming a specific scheduler cause.

`--extract-only` validates and writes tables without plots/reports. `--reuse-extracted` rechecks all input hashes and shared-extractor identity before reusing parsed data and recalculating the comparison. The interpretation template describes the current v1 dataset; review its narrative if replacing measurements. Published CSVs alone cannot satisfy the full source/binary-provenance checks.
