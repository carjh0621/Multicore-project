#!/usr/bin/env python3
"""Validate and analyze the v1 full-ROI cold-start experiment; never launch simulators."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
from types import SimpleNamespace
import xml.etree.ElementTree as ET

os.environ.setdefault("MPLCONFIGDIR", "/tmp/multicore-analysis-mplconfig")
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import Normalize
from matplotlib.patches import Rectangle
from matplotlib.lines import Line2D
import numpy as np
import pandas as pd

from batch_common import Context
from gem5_to_mcpat import ROOT, Converter, objects, partition_ops, read_json, require, sha256
from mcpat_to_hotspot import parse_mcpat, disjoint_blocks

BENCHMARKS = ["blackscholes", "canneal", "freqmine", "streamcluster"]
COLORS = dict(zip(BENCHMARKS, ["#0072B2", "#D55E00", "#009E73", "#CC79A7"]))
CONFIGS = [(1, 4), (4, 2), (4, 4), (4, 8), (8, 4)]
COMPONENTS = ["core_logic", "l1i", "l1d", "l2", "interconnect", "memory_controller"]
COMP_LABELS = ["Core logic", "L1I", "L1D", "Shared L2", "Interconnect", "Memory controllers"]
COMP_COLORS = ["#4C78A8", "#EBCB65", "#F28E2B", "#59A14F", "#B07AA1", "#76B7B2"]
SWEEP_SPECS = {"tlp": ("cores", [1, 4, 8], "Core count (width fixed at 4)"),
               "ilp": ("width", [2, 4, 8], "Pipeline width (cores fixed at 4)")}


def geo_mean(values):
    values = np.asarray(values, dtype=float)
    require(np.all(np.isfinite(values)) and np.all(values > 0), "Geometric means require positive finite ratios")
    return float(np.exp(np.mean(np.log(values))))


def component_key(block):
    if block["category"] == "l1_cache":
        return "l1i" if block["name"].endswith("_l1i") else "l1d"
    return {"l2_cache": "l2"}.get(block["category"], block["category"])


def load_data(results_root, output):
    args = SimpleNamespace(results_root=results_root, checkpoints_root=None, benchmarks=None,
                           gem5_binary=ROOT/"external/gem5/build/X86_MESI_Two_Level/gem5.opt",
                           mcpat_binary=ROOT/"external/mcpat/mcpat", hotspot_binary=ROOT/"external/hotspot/hotspot")
    ctx = Context(args)
    require(ctx.policy["benchmarks"] == BENCHMARKS, "This analysis expects the four v1 workloads")
    require(set((c.cores, c.width) for c in ctx.cases) == set(CONFIGS), "Unexpected hardware matrix")
    require(ctx.policy["execution"]["warmup"] == "no_excluded_warmup; cold-start effects included in full ROI",
            "This report is specifically for the non-warm-up policy")
    records, components, core_rows, blocks, provenance, warnings = [], [], [], [], [], []
    manifests = []
    for case in ctx.cases:
        print(f"Validate/extract {case.name}", flush=True)
        ctx.check_hotspot(case)
        run = ctx.run_path(case)
        c = Converter(run, ctx.policy_path, ROOT/"configs/mcpat/model-v1.json")
        _, generated = c.build()  # Exercise the full mapping checks against the completed ROI.
        power, temperature = read_json(run/"mcpat/power.json"), read_json(run/"hotspot/temperature.json")
        mapping = read_json(run/"hotspot/mapping.json")
        parsed = parse_mcpat((run/"mcpat/mcpat.txt").read_text())
        disjoint, _, totals = disjoint_blocks(parsed, case.cores, len(c.l2), mapping["conservation"]["tolerance"])
        require(math.isclose(totals["runtime_dynamic_w"], power["runtime_dynamic_w"], rel_tol=5e-5, abs_tol=1e-6),
                "Detailed runtime power does not conserve processor power")
        seconds, insts, uops = c.seconds, c.stat("simInsts"), c.stat("simOps")
        identity = {"case": case.name, "benchmark": case.benchmark, "cores": case.cores, "width": case.width}
        row = {**identity, "roi_seconds": seconds, "roi_ms": seconds*1000,
               "runtime_dynamic_w": power["runtime_dynamic_w"], "static_w": power["static_w"],
               "subthreshold_leakage_w": power["subthreshold_leakage_w"], "gate_leakage_w": power["gate_leakage_w"],
               "total_power_w": power["total_average_w"], "area_mm2": power["area_mm2"],
               "temperature_c": temperature["maximum_steady_state_silicon_celsius"],
               "temperature_rise_k": temperature["maximum_steady_state_silicon_kelvin"]-temperature["ambient_k"],
               "hottest_block": temperature["hottest_block"], "ambient_c": temperature["ambient_k"]-273.15,
               "energy_j": power["total_average_w"]*seconds,
               "edp_j_s": power["total_average_w"]*seconds**2,
               "architectural_instructions": insts, "committed_micro_ops": uops,
               "elapsed_cycles_per_core": c.native_cycles,
               "arch_ipc_per_core_wall": insts/(case.cores*c.native_cycles),
               "uipc_per_core_wall": uops/(case.cores*c.native_cycles),
               "committed_uop_width_fraction": uops/(case.cores*c.native_cycles*case.width),
               "native_clock_hz": c.hz, "clock_period_ticks": c.period,
               "mcpat_warning_count": len(power.get("mcpat_warnings", [])),
               "cycle_counter_note_count": len(generated.get("cycle_counter_notes", [])),
               "disjoint_power_w": sum(b["total_average_w"] for b in disjoint)}
        row["static_fraction"] = row["static_w"]/row["total_power_w"]
        row["chip_power_density_w_mm2"] = row["total_power_w"]/row["area_mm2"]
        row["effective_peak_thermal_resistance_k_w"] = row["temperature_rise_k"]/row["total_power_w"]
        row["dynamic_energy_j"] = row["runtime_dynamic_w"]*seconds
        row["static_energy_j"] = row["static_w"]*seconds
        for name, cache_name, controllers in (("l1i", "L1Icache", c.l1), ("l1d", "L1Dcache", c.l1), ("l2", "L2cache", c.l2)):
            for suffix in ("hits", "misses", "accesses"):
                row[f"{name}_demand_{suffix}"] = sum(c.stat(ctrl[cache_name]["path"]+".m_demand_"+suffix) for ctrl in controllers)
            require(row[f"{name}_demand_hits"]+row[f"{name}_demand_misses"] == row[f"{name}_demand_accesses"], "Cache counter mismatch")
            row[f"{name}_miss_rate_pct"] = 100*row[f"{name}_demand_misses"]/row[f"{name}_demand_accesses"]
            row[f"{name}_mpki"] = 1000*row[f"{name}_demand_misses"]/insts
        net = c.ruby["network"]
        row["network_hop_messages"] = sum(v for k, v in c.stats.items() if k.startswith(net["path"]+".routers") and k.endswith(".total_msg_count"))
        row["network_hop_bytes"] = sum(v for k, v in c.stats.items() if k.startswith(net["path"]+".routers") and k.endswith(".total_msg_bytes"))
        row["routers"] = len(objects(net["routers"]))
        row["directed_internal_links"] = len(objects(net["int_links"]))
        xml = ET.parse(run/"mcpat/input.xml")
        row["equivalent_noc_hop_flits"] = float(xml.find(".//component[@id='system.NoC0']/stat[@name='total_accesses']").get("value"))
        row["network_hop_flits_per_second"] = row["equivalent_noc_hop_flits"]/seconds
        mc = objects(c.board["memory"]["mem_ctrl"])
        row["memory_read_requests"] = sum(c.stat(x["path"]+".readReqs") for x in mc)
        row["memory_write_requests"] = sum(c.stat(x["path"]+".writeReqs") for x in mc)
        row["memory_requests"] = row["memory_read_requests"]+row["memory_write_requests"]
        user_insts = 0
        mixes = dict(integer_arithmetic=0, fp_simd=0, memory=0, other=0)
        for target, suffix in (("branch_correction_events", "branchPred.corrected_0::total"),
                               ("iq_full_events", "rename.IQFullEvents"), ("rob_full_events", "rename.ROBFullEvents"),
                               ("lq_full_events", "rename.LQFullEvents"), ("sq_full_events", "rename.SQFullEvents")):
            row[target] = sum(c.stat(x["path"]+"."+suffix) for x in c.cores)
        row["branch_corrections_per_1000_arch_insts"] = row["branch_correction_events"]*1000/insts
        for i, cpu in enumerate(c.cores):
            prefix = cpu["path"]+"."
            ci, cu = c.stat(prefix+"commitStats0.numInsts"), c.stat(prefix+"commitStats0.numOps")
            user_insts += c.stat(prefix+"commitStats0.numUserInsts")
            mix = c.vector(prefix+"issuedInstType_0::")
            part = partition_ops(mix)
            mem = sum(v for k, v in mix.items() if k != "total" and ("Mem" in k or "Load" in k or "Store" in k or k == "InstPrefetch"))
            mixes["integer_arithmetic"] += part["alu"]+part["mul"]
            mixes["fp_simd"] += part["fpu"]
            mixes["memory"] += mem
            mixes["other"] += mix["total"]-part["alu"]-part["mul"]-part["fpu"]-mem
            cblocks = [b for b in mapping["blocks"] if b["name"].startswith(f"core{i}_")]
            temp_by_name = {x["name"]: x["temperature_c"] for x in temperature["blocks"]}
            core_rows.append({**identity, "core_id": i, "architectural_instructions": ci, "committed_micro_ops": cu,
                              "uops_fraction": cu/uops, "arch_ipc_wall": ci/c.native_cycles, "uipc_wall": cu/c.native_cycles,
                              "active_tick_fraction": (c.stat(prefix+"numCycles")-c.stat(prefix+"idleCycles"))/c.native_cycles,
                              "total_power_w": sum(b["total_average_w"] for b in cblocks),
                              "max_temperature_c": max(temp_by_name[b["name"]] for b in cblocks),
                              "forwardComSize": cpu["forwardComSize"], "backComSize": cpu["backComSize"]})
        row["user_architectural_instructions"] = user_insts
        row["user_instruction_fraction"] = user_insts/insts
        for key, value in mixes.items():
            row["issued_"+key+"_ops"] = value
        total_issued = sum(mixes.values())
        for key, value in mixes.items():
            row["issued_"+key+"_fraction"] = value/total_issued
        selected = [x for x in core_rows if x["case"] == case.name]
        row["per_core_uop_cv"] = float(np.std([x["committed_micro_ops"] for x in selected])/np.mean([x["committed_micro_ops"] for x in selected]))
        for b in mapping["blocks"]:
            blocks.append({**identity, **b, "temperature_c": temp_by_name[b["name"]]})
        for key in COMPONENTS:
            subset = [b for b in disjoint if component_key(b) == key]
            metrics = {f: sum(b[f] for b in subset) for f in
                       ("area_mm2", "runtime_dynamic_w", "subthreshold_leakage_w", "gate_leakage_w", "static_w", "total_average_w")}
            components.append({**identity, "component": key, **metrics,
                               "chip_power_share": metrics["total_average_w"]/row["disjoint_power_w"]})
        for warning in power.get("mcpat_warnings", []):
            warnings.append({"case": case.name, "warning": warning})
        for n in ("roi.json", "run_manifest.json", "config.json", "stats.txt", "roi_stats.json",
                  "mcpat/input.xml", "mcpat/mapping.json", "mcpat/power.json", "mcpat/mcpat.txt",
                  "hotspot/mapping.json", "hotspot/temperature.json", "hotspot/steady.temp", "hotspot/floorplan.flp"):
            path = run/n
            provenance.append({"case": case.name, "file": str(path.relative_to(ROOT)), "sha256": ctx.digest(path)})
        records.append(row)
        manifests.append(read_json(run/"run_manifest.json")["tool_commits"])
    df = pd.DataFrame(records)
    require(len(df) == 20 and df["case"].is_unique, "Expected exactly 20 distinct completed measurements")
    require(all(x == manifests[0] for x in manifests), "Mixed upstream tool versions")
    comp, core, block = pd.DataFrame(components), pd.DataFrame(core_rows), pd.DataFrame(blocks)
    for (nc, w), group in df.groupby(["cores", "width"]):
        for field in ("area_mm2", "static_w"):
            require(np.allclose(group[field], group[field].iloc[0], rtol=5e-5), f"Unexpected workload-dependent hardware {field}")
    sweep_rows = []
    for sweep, (axis, values, _) in SWEEP_SPECS.items():
        part = df[df["width"].eq(4)] if sweep == "tlp" else df[df["cores"].eq(4)]
        for bench in BENCHMARKS:
            group = part[part["benchmark"].eq(bench)].sort_values(axis)
            require(group[axis].tolist() == values, "Incomplete sweep")
            base = group.iloc[0]
            for _, r in group.iterrows():
                ratios = {"speedup": base.roi_seconds/r.roi_seconds, "power_ratio": r.total_power_w/base.total_power_w,
                          "dynamic_power_ratio": r.runtime_dynamic_w/base.runtime_dynamic_w,
                          "static_power_ratio": r.static_w/base.static_w, "energy_ratio": r.energy_j/base.energy_j,
                          "edp_ratio": r.edp_j_s/base.edp_j_s, "area_ratio": r.area_mm2/base.area_mm2,
                          "work_ratio": r.committed_micro_ops/base.committed_micro_ops,
                          "instruction_ratio": r.architectural_instructions/base.architectural_instructions,
                          "l2_miss_ratio": r.l2_demand_misses/base.l2_demand_misses,
                          "flit_ratio": r.equivalent_noc_hop_flits/base.equivalent_noc_hop_flits,
                          "temperature_delta_c": r.temperature_c-base.temperature_c}
                scale = r[axis]/base[axis]
                ratios.update(nominal_scale=scale, scaling_efficiency=ratios["speedup"]/scale,
                              performance_per_watt_ratio=ratios["speedup"]/ratios["power_ratio"],
                              performance_per_area_ratio=ratios["speedup"]/ratios["area_ratio"])
                require(math.isclose(ratios["energy_ratio"], ratios["power_ratio"]/ratios["speedup"], rel_tol=1e-12), "Energy ratio identity failed")
                require(math.isclose(ratios["edp_ratio"], ratios["power_ratio"]/ratios["speedup"]**2, rel_tol=1e-12), "EDP ratio identity failed")
                sweep_rows.append({**r.to_dict(), "sweep": sweep, "baseline_case": base["case"], **ratios})
    sweeps = pd.DataFrame(sweep_rows)
    require(len(sweeps) == 24, "Two 12-point sweeps should share four measured C4/W4 configurations")
    slopes = []
    for (sw, bench), group in sweeps.groupby(["sweep", "benchmark"]):
        axis = SWEEP_SPECS[sw][0]
        group = group.sort_values(axis)
        for i in (0, 1):
            a, b = group.iloc[i], group.iloc[i+1]
            slopes.append({"sweep": sw, "benchmark": bench, "from": int(a[axis]), "to": int(b[axis]),
                           "power_ratio": b.total_power_w/a.total_power_w,
                           "power_elasticity": math.log(b.total_power_w/a.total_power_w)/math.log(b[axis]/a[axis]),
                           "incremental_watts_per_unit": (b.total_power_w-a.total_power_w)/(b[axis]-a[axis]),
                           "incremental_speedup": a.roi_seconds/b.roi_seconds})
    slopes = pd.DataFrame(slopes)
    datadir = output/"data"
    datadir.mkdir(parents=True, exist_ok=True)
    for name, frame in (("summary", df), ("sweeps", sweeps), ("power_components", comp), ("per_core", core),
                        ("thermal_blocks", block), ("power_scaling_intervals", slopes), ("mcpat_warnings", pd.DataFrame(warnings))):
        frame.to_csv(datadir/(name+".csv"), index=False, float_format="%.12g")
    (datadir/"input_sha256.json").write_text(json.dumps(provenance, indent=2)+"\n")
    audit = {"status": "validated", "unique_cases": len(df), "sweep_points": len(sweeps),
             "policy_sha256": ctx.policy_sha, "tool_commits": manifests[0],
             "python": platform.python_version(), "numpy": np.__version__, "pandas": pd.__version__,
             "matplotlib": matplotlib.__version__, "warmup": ctx.policy["execution"]["warmup"],
             "cases_with_mcpat_timing_warnings": int((df.mcpat_warning_count > 0).sum()),
             "cases_with_cycle_bookkeeping_notes": int((df.cycle_counter_note_count > 0).sum()),
             "max_component_power_relative_rounding_difference": float(np.max(np.abs(df.disjoint_power_w-df.total_power_w)/df.total_power_w)),
             "analysis_script_sha256": sha256(Path(__file__)), "source_files_hashed": len(provenance),
             "read_only_simulation_inputs": True, "replicates_per_case": 1}
    (datadir/"validation.json").write_text(json.dumps(audit, indent=2)+"\n")
    return df, sweeps, comp, core, block, slopes, audit


def style():
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.titlesize": 12,
                         "axes.labelsize": 10, "figure.titlesize": 16, "axes.spines.top": False,
                         "axes.spines.right": False, "axes.grid": True, "grid.alpha": .2,
                         "legend.frameon": False, "svg.fonttype": "none", "pdf.fonttype": 42,
                         "savefig.facecolor": "white"})


def save_figure(fig, output, name):
    folder = output/"figures"
    folder.mkdir(parents=True, exist_ok=True)
    for ext in ("png", "svg", "pdf"):
        fig.savefig(folder/f"{name}.{ext}", dpi=190, bbox_inches="tight")
    plt.close(fig)


def sweep_lines(ax, sweeps, sw, field, ylabel, factor=1, ideal=None):
    axis, vals, xlabel = SWEEP_SPECS[sw]
    for bench in BENCHMARKS:
        g = sweeps[sweeps.sweep.eq(sw) & sweeps.benchmark.eq(bench)].sort_values(axis)
        ax.plot(g[axis], g[field]*factor, "o-", color=COLORS[bench], label=bench, lw=2, ms=6)
    if ideal == "scale":
        ax.plot(vals, np.array(vals)/vals[0], "--", color="#555555", label="Proportional reference", lw=1.4)
    ax.set(xlabel=xlabel, ylabel=ylabel, xticks=vals)
    return ax


def paired_lines(output, sweeps, sw, fields, name, title, ideals=None):
    fig, axes = plt.subplots(1, 2, figsize=(11.8, 4.3), layout="constrained")
    for i, (field, label, multiplier) in enumerate(fields):
        sweep_lines(axes[i], sweeps, sw, field, label, multiplier, None if ideals is None else ideals[i])
    axes[0].legend(fontsize=8)
    fig.suptitle(title)
    save_figure(fig, output, name)


def component_bars(output, sweeps, comp, sw, name):
    axis, vals, _ = SWEEP_SPECS[sw]
    fig, axes = plt.subplots(2, 2, figsize=(12, 8), layout="constrained")
    for bench, ax in zip(BENCHMARKS, axes.flat):
        group = sweeps[sweeps.sweep.eq(sw) & sweeps.benchmark.eq(bench)].sort_values(axis)
        bottom = np.zeros(3)
        for key, label, color in zip(COMPONENTS, COMP_LABELS, COMP_COLORS):
            values = [float(comp[comp["case"].eq(c) & comp.component.eq(key)].total_average_w.iloc[0]) for c in group["case"]]
            ax.bar(np.arange(3), values, bottom=bottom, color=color, label=label, width=.65)
            bottom += values
        ax.set(title=bench, ylabel="ROI-average component power (W)", xticks=np.arange(3),
               xticklabels=[f"{'C' if sw == 'tlp' else 'W'}{x}" for x in vals])
        for i, total in enumerate(bottom):
            ax.text(i, total+max(bottom)*.015, f"{total:.1f}", ha="center", fontsize=9)
        ax.set_ylim(0, max(bottom)*1.16)
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="outside lower center", ncol=3, fontsize=9)
    fig.suptitle(f"{'Core' if sw == 'tlp' else 'Width'} sweep: disjoint power components\nDynamic + fixed-reference leakage; parents are not added again")
    save_figure(fig, output, name)


def make_figures(output, df, sweeps, comp, core, blocks, slopes):
    style()
    paired_lines(output, sweeps, "tlp", [("speedup", "Speedup vs C1/W4", 1), ("scaling_efficiency", "Core scaling efficiency (%)", 100)],
                 "01_tlp_speedup", "TLP: core scaling at width 4", ["scale", None])
    paired_lines(output, sweeps, "ilp", [("speedup", "Speedup vs C4/W2", 1), ("scaling_efficiency", "Speedup / nominal width ratio (%)", 100)],
                 "02_ilp_speedup", "ILP: pipeline-width scaling at 4 cores", ["scale", None])
    for sw, number in (("tlp", "03"), ("ilp", "04")):
        paired_lines(output, sweeps, sw, [("total_power_w", "ROI-average chip power (W)", 1), ("temperature_c", "Maximum modeled steady-state silicon (°C)", 1)],
                     number+"_"+sw+"_power_temperature", f"{sw.upper()}: power and modeled equilibrium temperature")
    for sw, number in (("tlp", "05"), ("ilp", "06")):
        paired_lines(output, sweeps, sw, [("power_ratio", "Total-power ratio vs sweep baseline", 1), ("static_fraction", "Static leakage / total power (%)", 100)],
                     number+"_"+sw+"_normalized_power", f"{sw.upper()}: proportional-power reference and static fraction", ["scale", None])
    component_bars(output, sweeps, comp, "tlp", "07_tlp_component_power")
    component_bars(output, sweeps, comp, "ilp", "08_ilp_component_power")
    fig, axes = plt.subplots(2, 1, figsize=(13, 8), layout="constrained")
    for sw, ax in zip(("tlp", "ilp"), axes):
        axis = SWEEP_SPECS[sw][0]
        rows = pd.concat([sweeps[sweeps.sweep.eq(sw) & sweeps.benchmark.eq(b)].sort_values(axis) for b in BENCHMARKS])
        x = np.arange(len(rows))
        ax.bar(x, rows.runtime_dynamic_w, label="Runtime dynamic", color="#3F8FC5")
        ax.bar(x, rows.subthreshold_leakage_w, bottom=rows.runtime_dynamic_w, label="Subthreshold leakage", color="#89959F")
        ax.bar(x, rows.gate_leakage_w, bottom=rows.runtime_dynamic_w+rows.subthreshold_leakage_w, label="Gate leakage", color="#D3D9DE")
        ax.set(title=sw.upper(), ylabel="Average power (W)", xticks=x,
               xticklabels=[f"{r.benchmark[:5]}\n{'C' if sw=='tlp' else 'W'}{int(r[axis])}" for _, r in rows.iterrows()])
        ax.set_xlim(-.65, len(rows)-.35)
        for boundary in (2.5, 5.5, 8.5):ax.axvline(boundary, color="#777777", lw=.8)
    axes[0].legend(ncol=3, fontsize=9)
    fig.suptitle("Dynamic versus static power: Runtime Dynamic + subthreshold + gate leakage")
    save_figure(fig, output, "09_dynamic_static_power")
    fig, axes = plt.subplots(2, 2, figsize=(12, 8), layout="constrained")
    for bench, ax in zip(BENCHMARKS, axes.flat):
        g=comp[comp["case"].eq(f"{bench}-c4-w4")].set_index("component").loc[COMPONENTS]
        x=np.arange(len(COMPONENTS))
        ax.bar(x-.18, g.runtime_dynamic_w, .36, color="#3F8FC5", label="Dynamic")
        ax.bar(x+.18, g.static_w, .36, color="#89959F", label="Static")
        ax.set(title=bench+" · C4/W4", ylabel="Component power (W)", xticks=x,
               xticklabels=["Logic", "L1I", "L1D", "L2", "NoC", "MC"])
    axes[0,0].legend(ncol=2)
    fig.suptitle("Which structures dissipate dynamic versus leakage power?")
    save_figure(fig, output, "10_component_dynamic_static")
    for sw, number in (("tlp", "11"), ("ilp", "12")):
        fig, axes = plt.subplots(1,2,figsize=(11.8,4.3),layout="constrained")
        sweep_lines(axes[0],sweeps,sw,"energy_ratio","ROI energy / baseline energy")
        sweep_lines(axes[1],sweeps,sw,"edp_ratio","Energy-delay product / baseline EDP")
        for ax in axes:ax.axhline(1,color="#555555",ls="--",lw=1)
        axes[0].legend(fontsize=8)
        fig.suptitle(sw.upper()+": energy and energy-delay cost (lower is better)")
        save_figure(fig,output,number+"_"+sw+"_energy_edp")
    norm=Normalize(df.temperature_c.min(),df.temperature_c.max())
    for sw, number in (("tlp","13"),("ilp","14")):
        fig,axes=plt.subplots(2,2,figsize=(11,8.5),layout="constrained")
        axis=SWEEP_SPECS[sw][0]
        for bench,ax in zip(BENCHMARKS,axes.flat):
            g=sweeps[sweeps.sweep.eq(sw)&sweeps.benchmark.eq(bench)].sort_values(axis)
            sc=ax.scatter(g.speedup,g.power_ratio,c=g.temperature_c,cmap="inferno",norm=norm,
                          s=90+100*g.area_ratio,edgecolors="#333333",lw=.7)
            ax.plot(g.speedup,g.power_ratio,color="#AAAAAA",lw=1,zorder=0)
            for _,r in g.iterrows():ax.annotate(f"{'C' if sw=='tlp' else 'W'}{int(r[axis])}",(r.speedup,r.power_ratio),xytext=(6,6),textcoords="offset points")
            lim=max(g.speedup.max(),g.power_ratio.max())*1.15
            ax.plot([0,lim],[0,lim],"--",color="#888888",lw=1,label="Equal ROI energy")
            ax.set(title=bench,xlabel="Normalized speedup",ylabel="Normalized average power",xlim=(0,lim),ylim=(0,lim))
        fig.colorbar(sc,ax=axes,label="Peak modeled silicon temperature (°C)",shrink=.7)
        fig.suptitle(f"{sw.upper()} tradeoff: below the diagonal saves ROI energy\nMarker area grows with modeled die area")
        save_figure(fig,output,number+"_"+sw+"_tradeoff")
    for sw, number in (("tlp","15"),("ilp","16")):
        fig,axes=plt.subplots(2,2,figsize=(12,8),layout="constrained")
        for ax,field,label in zip(axes.flat,["work_ratio","uipc_per_core_wall","l2_miss_ratio","flit_ratio"],
                                ["Committed micro-ops / baseline","Committed μops / core / elapsed cycle","L2 demand misses / baseline","Equivalent hop flits / baseline"]):
            sweep_lines(ax,sweeps,sw,field,label)
            if field in ("l2_miss_ratio","flit_ratio") and sw=="tlp":ax.set_yscale("log")
        axes[0,0].legend(fontsize=8)
        fig.suptitle(sw.upper()+": executed work, per-core throughput and memory/network activity")
        save_figure(fig,output,number+"_"+sw+"_work_diagnostics")
    fig,axes=plt.subplots(1,2,figsize=(12,4.8),layout="constrained")
    for sw,ax in zip(("tlp","ilp"),axes):
        g=slopes[slopes.sweep.eq(sw)]
        for j,interval in enumerate([(1,4),(4,8)] if sw=="tlp" else [(2,4),(4,8)]):
            vals=[g[g.benchmark.eq(b)&g["from"].eq(interval[0])].power_elasticity.iloc[0] for b in BENCHMARKS]
            ax.bar(np.arange(4)+(j-.5)*.35,vals,.35,color=["#4C78A8","#F28E2B"][j],label=f"{interval[0]} → {interval[1]}")
        ax.axhline(1,ls="--",color="#444444",label="Proportional growth")
        ax.set(title=sw.upper(),xticks=np.arange(4),xticklabels=[b[:6] for b in BENCHMARKS],ylabel="Local power elasticity: log(P₂/P₁) / log(x₂/x₁)")
        ax.legend(fontsize=8)
    fig.suptitle("Power proportionality depends on the interval; three points do not establish a power law")
    save_figure(fig,output,"17_power_elasticity")
    fig,axes=plt.subplots(1,2,figsize=(12,4.8),layout="constrained")
    for ax,metric,label in zip(axes,["area_mm2","static_w"],["Modeled area (mm²)","Static power at 350 K (W)"]):
        bottom=np.zeros(5)
        for key,clabel,color in zip(COMPONENTS,COMP_LABELS,COMP_COLORS):
            values=[comp[comp["case"].eq(f"blackscholes-c{n}-w{w}")&comp.component.eq(key)][metric].iloc[0] for n,w in CONFIGS]
            ax.bar(np.arange(5),values,bottom=bottom,color=color,label=clabel)
            bottom+=values
        ax.set(xticks=np.arange(5),xticklabels=[f"C{n}/W{w}" for n,w in CONFIGS],ylabel=label)
    fig.legend(*axes[0].get_legend_handles_labels(),loc="outside lower center",ncol=3,fontsize=9)
    fig.suptitle("Hardware cost: static power and area are workload-independent for each configuration")
    save_figure(fig,output,"18_area_and_static_cost")
    fig,axes=plt.subplots(1,2,figsize=(12,4.8),layout="constrained")
    markers=["o","s","^","D","P"]
    for (n,w),marker in zip(CONFIGS,markers):
        for bench in BENCHMARKS:
            r=df[df.benchmark.eq(bench)&df.cores.eq(n)&df.width.eq(w)].iloc[0]
            axes[0].scatter(r.total_power_w,r.temperature_c,color=COLORS[bench],marker=marker,s=65)
            axes[1].scatter(r.chip_power_density_w_mm2,r.temperature_c,color=COLORS[bench],marker=marker,s=65)
    axes[0].set(xlabel="Chip average power (W)",ylabel="Maximum steady-state silicon (°C)")
    axes[1].set(xlabel="Chip average power density (W/mm²)",ylabel="Maximum steady-state silicon (°C)")
    handles=[Line2D([],[],color=COLORS[b],marker="o",ls="",label=b) for b in BENCHMARKS]
    handles += [Line2D([],[],color="#555555",marker=m,ls="",label=f"C{n}/W{w}") for (n,w),m in zip(CONFIGS,markers)]
    fig.legend(handles=handles,loc="outside lower center",ncol=5,fontsize=8)
    fig.suptitle("Thermal result depends on total power, spatial density, die area and fixed package")
    save_figure(fig,output,"19_temperature_power_density")
    for sw,number in (("tlp","20"),("ilp","21")):
        fig,axes=plt.subplots(2,3,figsize=(13.5,8.5),layout="constrained")
        cfgs=[(n,4) for n in [1,4,8]] if sw=="tlp" else [(4,w) for w in [2,4,8]]
        selected=[f"{b}-c{n}-w{w}" for b in ["blackscholes","streamcluster"] for n,w in cfgs]
        subset=blocks[blocks["case"].isin(selected)]
        maxedge=max(subset.x_m.add(subset.width_m).max(),subset.y_m.add(subset.height_m).max())*1000
        for ax,case in zip(axes.flat,selected):
            g=blocks[blocks["case"].eq(case)]
            for _,r in g.iterrows():
                ax.add_patch(Rectangle((r.x_m*1000,r.y_m*1000),r.width_m*1000,r.height_m*1000,
                                       facecolor=plt.cm.inferno(norm(r.temperature_c)),edgecolor="white",lw=.5))
                label=r["name"].replace("core", "C").replace("_logic", "").replace("_l1i", "I").replace("_l1d", "D").replace("l2_bank", "L2-").replace("memory_controllers", "MC").replace("noc", "NoC")
                if r.width_m*1000>1.5 and r.height_m*1000>1.2:
                    ax.text((r.x_m+r.width_m/2)*1000,(r.y_m+r.height_m/2)*1000,label,ha="center",va="center",fontsize=6,
                            color="black" if norm(r.temperature_c)>.62 else "white")
            r=df[df["case"].eq(case)].iloc[0]
            ax.set(title=f"{case}\npeak {r.temperature_c:.2f} °C",xlim=(0,maxedge),ylim=(0,maxedge),xlabel="x (mm)",ylabel="y (mm)",aspect="equal")
            ax.set_facecolor("#EEEEEE")
            ax.grid(False)
        fig.colorbar(plt.cm.ScalarMappable(norm=norm,cmap="inferno"),ax=axes,label="Modeled silicon block temperature (°C)",shrink=.75)
        fig.suptitle(sw.upper()+": area-derived surrogate layouts, fixed cooling, common color/length scales")
        save_figure(fig,output,number+"_"+sw+"_thermal_floorplans")
    fig,axes=plt.subplots(1,2,figsize=(13,4.4),layout="constrained")
    eight=core[core.cores.eq(8)&core.width.eq(4)]
    for ax,field,title,cmap in zip(axes,["uops_fraction","max_temperature_c"],
                                  ["Share of committed micro-ops (%)","Peak of each core's logic/L1 blocks (°C)"],["Blues","inferno"]):
        data=np.array([eight[eight.benchmark.eq(b)].sort_values("core_id")[field].to_numpy() for b in BENCHMARKS])
        if field=="uops_fraction":data*=100
        im=ax.imshow(data,cmap=cmap,aspect="auto")
        ax.set(title=title,xticks=np.arange(8),xticklabels=np.arange(8),yticks=np.arange(4),yticklabels=BENCHMARKS,xlabel="Core ID")
        ax.grid(False)
        for i in range(4):
            for j in range(8):
                dark = im.norm(data[i,j])>.55 if cmap=="Blues" else im.norm(data[i,j])<.6
                ax.text(j,i,f"{data[i,j]:.1f}",ha="center",va="center",fontsize=8,color="white" if dark else "black")
        fig.colorbar(im,ax=ax,shrink=.8)
    fig.suptitle("Eight-core distributions: instruction shares are not direct useful-work or utilization measures")
    save_figure(fig,output,"22_per_core_work_and_temperature")
    fig,axes=plt.subplots(1,2,figsize=(12,4.8),layout="constrained")
    base=df[df.cores.eq(4)&df.width.eq(4)].set_index("benchmark").loc[BENCHMARKS]
    bottom=np.zeros(4)
    for key,label,color in zip(["integer_arithmetic","fp_simd","memory","other"],["Integer/branch arithmetic","FP/SIMD proxy","Memory OpClasses","Other"],["#4C78A8","#F28E2B","#59A14F","#B0B0B0"]):
        values=100*base["issued_"+key+"_fraction"].to_numpy()
        axes[0].bar(np.arange(4),values,bottom=bottom,color=color,label=label);bottom+=values
    axes[0].set(xticks=np.arange(4),xticklabels=[b[:6] for b in BENCHMARKS],ylabel="Share of issued OpClasses (%)",ylim=(0,100))
    for bench in BENCHMARKS:
        g=sweeps[sweeps.sweep.eq("ilp")&sweeps.benchmark.eq(bench)].sort_values("width")
        axes[1].plot(g.width,g.iq_full_events/g.committed_micro_ops*1000,"o-",color=COLORS[bench],label=bench)
    axes[1].set(xlabel="Width at 4 cores",ylabel="IQ-full rename events / 1,000 committed μops",xticks=[2,4,8])
    axes[1].legend(fontsize=8)
    fig.legend(*axes[0].get_legend_handles_labels(),loc="outside lower center",ncol=4,fontsize=8)
    fig.suptitle("Workload mix and fixed-IQ pressure: event counts are not additive stall-cycle fractions")
    save_figure(fig,output,"23_mix_and_iq_pressure")


def interactive_plot(output, sweeps):
    import plotly
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    fig=make_subplots(rows=1,cols=2,subplot_titles=("TLP · baseline C1/W4","ILP · baseline C4/W2"))
    for column,sw in enumerate(["tlp","ilp"],1):
        axis=SWEEP_SPECS[sw][0]
        for bench in BENCHMARKS:
            g=sweeps[sweeps.sweep.eq(sw)&sweeps.benchmark.eq(bench)].sort_values(axis)
            custom=np.column_stack([g["case"],g.roi_ms,g.total_power_w,g.energy_j,g.temperature_c,g.area_mm2,g.energy_ratio])
            fig.add_trace(go.Scatter(x=g.speedup,y=g.power_ratio,mode="markers+lines+text",name=bench,legendgroup=bench,
                showlegend=column==1,line=dict(color=COLORS[bench]),marker=dict(size=12),text=[f"{axis}={v}" for v in g[axis]],textposition="top center",
                customdata=custom,hovertemplate="%{customdata[0]}<br>Speedup %{x:.3f}×<br>Power ratio %{y:.3f}×<br>ROI %{customdata[1]:.3f} ms<br>Power %{customdata[2]:.3f} W<br>Energy %{customdata[3]:.4f} J<br>Tmax %{customdata[4]:.2f} °C<br>Area %{customdata[5]:.3f} mm²<br>Energy ratio %{customdata[6]:.3f}×<extra></extra>"),row=1,col=column)
        limit=max(sweeps[sweeps.sweep.eq(sw)].speedup.max(),sweeps[sweeps.sweep.eq(sw)].power_ratio.max())*1.1
        fig.add_trace(go.Scatter(x=[0,limit],y=[0,limit],mode="lines",line=dict(color="gray",dash="dash"),name="Equal ROI energy",showlegend=column==1,hoverinfo="skip"),row=1,col=column)
        fig.update_xaxes(title_text="Normalized speedup",row=1,col=column)
        fig.update_yaxes(title_text="Normalized average power",row=1,col=column)
    fig.update_layout(title="Non-warm-up ROI tradeoffs · hover points for metrics · below diagonal saves ROI energy",height=600,width=1200,template="plotly_white")
    fig.write_html(output/"interactive_tradeoffs.html",include_plotlyjs=True,full_html=True)
    return plotly.__version__


def markdown_table(frame, columns, formats=None):
    formats=formats or {}
    lines=["| "+" | ".join(columns.values())+" |", "| "+" | ".join("---" for _ in columns)+" |"]
    for _,r in frame.iterrows():
        values=[]
        for key in columns:
            value=r[key]
            values.append(formats[key](value) if key in formats else str(value))
        lines.append("| "+" | ".join(values)+" |")
    return "\n".join(lines)


def write_report(output, df, sweeps, comp, slopes, audit):
    """Render the interpretation alongside tables computed from the validated data."""
    base = Path(os.path.relpath(output, ROOT)).as_posix()
    def table(frame, columns, digits=3):
        formats = {k: (lambda v, d=digits: f"{v:.{d}f}") for k in columns
                   if pd.api.types.is_float_dtype(frame[k])}
        for k in ("temperature_c", "temperature_delta_c"):
            if k in columns:
                formats[k] = lambda v: f"{v:.2f}"
        if "efficiency_pct" in columns:
            formats["efficiency_pct"] = lambda v: f"{v:.1f}"
        return markdown_table(frame, columns, formats)
    def fig(name, caption):
        return f"![{caption}]({base}/figures/{name}.png)\n\n*{caption}*"
    endpoint_tables = {}
    for sw in ("tlp", "ilp"):
        axis = SWEEP_SPECS[sw][0]
        g = sweeps[sweeps.sweep.eq(sw)]
        mid = g[g[axis].eq(4)].set_index("benchmark")
        end = g[g[axis].eq(8)].set_index("benchmark").loc[BENCHMARKS].copy()
        end["middle_speedup"] = mid.speedup
        end["efficiency_pct"] = 100*end.scaling_efficiency
        end = end.reset_index()
        endpoint_tables[sw] = table(end, {"benchmark":"Benchmark", "middle_speedup":"Speedup at 4",
            "speedup":"Speedup at 8", "efficiency_pct":"Efficiency at 8 (%)", "power_ratio":"P₈/P₀",
            "energy_ratio":"E₈/E₀", "temperature_delta_c":"ΔT₈ (°C)"})
    hardware = df[df.benchmark.eq("blackscholes")].set_index(["cores", "width"]).loc[CONFIGS].reset_index()
    hardware["configuration"] = [f"C{n}/W{w}" for n,w in CONFIGS]
    hardware["noc_static_w"] = [comp[comp["case"].eq(f"blackscholes-c{n}-w{w}") & comp.component.eq("interconnect")].static_w.iloc[0] for n,w in CONFIGS]
    hardware_table = table(hardware, {"configuration":"Configuration", "area_mm2":"Area (mm²)",
        "static_w":"Chip static (W)", "noc_static_w":"NoC static (W)", "routers":"Routers", "directed_internal_links":"Directed internal links"})
    baseline = df[df.cores.eq(4)&df.width.eq(4)].set_index("benchmark").loc[BENCHMARKS].reset_index()
    for key, keys in (("logic", ["core_logic"]), ("l1", ["l1i","l1d"]), ("l2", ["l2"]),
                      ("noc", ["interconnect"]), ("mc", ["memory_controller"])):
        baseline[key+"_w"] = [comp[comp["case"].eq(c)&comp.component.isin(keys)].total_average_w.sum() for c in baseline["case"]]
    baseline["static_pct"] = 100*baseline.static_fraction
    component_table = table(baseline, {"benchmark":"Benchmark", "logic_w":"Core logic W", "l1_w":"L1I+D W",
        "l2_w":"Shared L2 W", "noc_w":"NoC W", "mc_w":"MC W", "static_pct":"Chip static %"}, 2)
    slope_records = []
    for b in BENCHMARKS:
        r={"benchmark":b}
        for sw, a, z in (("tlp",1,4),("tlp",4,8),("ilp",2,4),("ilp",4,8)):
            r[f"{sw}{a}{z}"] = slopes[slopes.sweep.eq(sw)&slopes.benchmark.eq(b)&slopes["from"].eq(a)].incremental_watts_per_unit.iloc[0]
        slope_records.append(r)
    slope_table = table(pd.DataFrame(slope_records), {"benchmark":"Benchmark", "tlp14":"C1→4 W/added core",
        "tlp48":"C4→8 W/added core", "ilp24":"W2→4 W/width unit", "ilp48":"W4→8 W/width unit"}, 2)
    energy_records = []
    for sw in ("tlp", "ilp"):
        axis = SWEEP_SPECS[sw][0]
        for b in BENCHMARKS:
            g=sweeps[sweeps.sweep.eq(sw)&sweeps.benchmark.eq(b)].sort_values(axis)
            r={"sweep":sw.upper(), "benchmark":b}
            for field,label in (("roi_seconds","fastest"),("energy_j","min_energy"),("edp_j_s","min_edp")):
                x=g.loc[g[field].idxmin()]
                r[label]=f"C{int(x.cores)}/W{int(x.width)}"
            r["energy_middle"]=g.iloc[1].energy_ratio
            r["energy_end"]=g.iloc[2].energy_ratio
            energy_records.append(r)
    energy_table=table(pd.DataFrame(energy_records), {"sweep":"Sweep", "benchmark":"Benchmark", "fastest":"Fastest",
        "min_energy":"Lowest energy", "min_edp":"Lowest EDP", "energy_middle":"E₄/E₀", "energy_end":"E₈/E₀"})
    summary_records=[]
    for sw in ("tlp","ilp"):
        axis=SWEEP_SPECS[sw][0]
        for v in (4,8):
            g=sweeps[sweeps.sweep.eq(sw)&sweeps[axis].eq(v)]
            summary_records.append({"setting":f"{sw.upper()} {'C' if sw=='tlp' else 'W'}{v}",
                **{k:geo_mean(g[k]) for k in ("speedup","power_ratio","energy_ratio","edp_ratio")}})
    suite_table=table(pd.DataFrame(summary_records), {"setting":"Setting", "speedup":"GM speedup",
        "power_ratio":"GM power ratio", "energy_ratio":"GM energy ratio", "edp_ratio":"GM EDP ratio"})
    raw=df.copy()
    raw["benchmark"]=pd.Categorical(raw.benchmark,categories=BENCHMARKS,ordered=True)
    raw=raw.sort_values(["benchmark","cores","width"])
    raw_table=table(raw, {"case":"Case", "roi_ms":"ROI (ms)", "runtime_dynamic_w":"Dynamic (W)",
        "static_w":"Static (W)", "total_power_w":"Total (W)", "energy_j":"Energy (J)", "temperature_c":"Tmax (°C)"})
    diagnostic=df[df.benchmark.eq("streamcluster")].set_index(["cores","width"]).loc[CONFIGS].reset_index()
    diagnostic["configuration"]=[f"C{n}/W{w}" for n,w in CONFIGS]
    diagnostic["arch_billion"]=diagnostic.architectural_instructions/1e9
    diagnostic["uop_billion"]=diagnostic.committed_micro_ops/1e9
    diagnostic["l2_million"]=diagnostic.l2_demand_misses/1e6
    diagnostic["flits_million"]=diagnostic.equivalent_noc_hop_flits/1e6
    diagnostic_table=table(diagnostic, {"configuration":"Configuration", "arch_billion":"Arch inst (B)",
        "uop_billion":"Committed μops (B)", "uipc_per_core_wall":"μops/core/cycle", "l2_million":"L2 misses (M)",
        "flits_million":"Hop flits (M)", "memory_requests":"Memory requests"})
    figures=sorted((output/"figures").glob("*.png"))
    gallery="\n".join(f"| {p.stem} | [PNG]({base}/figures/{p.name}) | [SVG]({base}/figures/{p.stem}.svg) | [PDF]({base}/figures/{p.stem}.pdf) |" for p in figures)
    text=f"""# Non-warm-up analysis: multicore out-of-order CPU, experiment v1

This report analyzes **20 completed PARSEC 3.0 `simsmall` ROI measurements** through gem5 → McPAT → HotSpot. All 20 pass the saved-artifact checks for completed ROI, configuration, provenance, power mapping and thermal output. Each benchmark/configuration has **one measured run**. Results below describe this simulator configuration and its explicit analytical models; validation of the conversion does not establish physical accuracy.

**Main finding:** adding cores benefits blackscholes and canneal strongly, whereas wider pipelines mainly benefit blackscholes. Freqmine has diminishing returns. Streamcluster reaches its best measured runtime at **C4/W4** and regresses at both C8/W4 and C4/W8. Increasing hardware increases modeled average power and equilibrium temperature in every benchmark. Faster execution sometimes saves energy, but the 8-core configuration consumes more ROI energy than the 1-core baseline in all four workloads.

The absolute watt, area and temperature estimates require particular care: all 20 McPAT runs report L2 latency-constraint warnings, and the surrogate NoC/cache models contribute substantial area and leakage. The performance trends come from gem5; the physical cost trends are conditional on these modeling choices.

Open the [interactive tradeoff plot]({base}/interactive_tradeoffs.html) in a browser for hoverable metrics and workload selection. All **23 figures** are available as PNG, SVG and PDF. The [complete measurements]({base}/data/summary.csv), [normalized sweeps]({base}/data/sweeps.csv), [component powers]({base}/data/power_components.csv) and [validation record]({base}/data/validation.json) are included for independent checking.

## 1. What was measured

### Experiment matrix and controlled settings

| Sweep | Configurations | Sweep baseline | Question |
| --- | --- | --- | --- |
| Thread-level parallelism (TLP) | C1/W4, C4/W4, C8/W4 | C1/W4, independently per benchmark | What happens when cores and worker threads increase? |
| Instruction-level parallelism (ILP) | C4/W2, C4/W4, C4/W8 | C4/W2, independently per benchmark | What happens when pipeline width increases? |

`C` denotes physical O3 cores, with one hardware thread per core; PARSEC worker count follows core count. `W` changes **fetch, decode, rename, dispatch, issue, writeback and commit widths together**. It is not an isolated issue-width experiment. Four workloads × five unique configurations = 20 measurements. C4/W4 belongs to both sweeps, so the normalized dataset has 24 rows but still only 20 unique measured runs. This is not a complete 3×3 core/width grid; interactions such as C8/W8 were not measured.

The [frozen experiment policy](configs/experiment-startup.json) specifies the same requested 3 GHz clock, 1 V, 45 nm analytical technology, ROB 192, shared IQ 64, 256 integer and 256 floating-point physical registers, and 32-entry load/store queues. Functional-unit counts stay fixed as width changes. McPAT represents the shared IQ as 32 integer + 32 FP entries and uses scalar-equivalent SIMD/FPU proxies.

Each core has private 32 KiB L1I and 32 KiB L1D caches, both 8-way. Two shared L2 banks provide a fixed total of 2 MiB, 16-way, with 64-byte cache lines. Memory is dual-channel DDR4-2400 with 3 GiB capacity. Thus, the core sweep also increases aggregate private L1 capacity and the number of coherence endpoints. It does not hold chip area, private-cache capacity or network hardware constant.

gem5 represents the requested clock by an integer period of **333 ticks at 10¹² ticks/s**, giving approximately **3.003003 GHz**, consistently across cases. McPAT uses 3003 MHz, with its time/cycle conversion adjusted to preserve the exact ROI duration. Speedup uses recorded ticks, never host runtime or rounded cycle counters. At width 2, `forwardComSize=128` is a simulator scheduling-window workaround; the requested two-wide writeback remains two-wide.

### ROI and non-warm-up meaning

The [runner](configs/gem5/run_parsec.py) and [ROI controller](scripts/roi_control.py) fast-forward the guest OS and PARSEC initialization using Atomic cores. With Ruby, this fast path bypasses caching. At the benchmark's `WORKBEGIN` hook, a checkpoint is saved before switching to O3. A measured restore switches to O3, resets statistics, and runs through `WORKEND`. The report uses that completed detailed interval, excluding boot, preparation and checkpoint creation.

There is **no excluded detailed warm-up interval**. O3/cache startup effects are included in the full measured ROI, after which structures warm naturally during execution. This is a full-ROI cold-start methodology, not a claim that caches stay cold for the entire ROI. Different configurations can have different startup sensitivity. The endpoint establishes completion of the hooked ROI; it does not independently verify any benchmark output checking that occurs after `WORKEND`.

## 2. Metrics and normalization

For each workload and sweep, let `t₀`, `P₀` and `A₀` be the smallest configuration's ROI time, average modeled power and modeled area. All ratios are workload-specific.

| Metric | Definition | Interpretation |
| --- | --- | --- |
| ROI time, `t` | `(end_tick − begin_tick) / ticks_per_second` | Simulated execution time, not wall-clock time to run gem5 |
| Speedup, `S` | `t₀ / t` | Larger is faster |
| Core scaling efficiency | `S / (N/N₀)` | Fraction of proportional core scaling |
| Width scaling fraction | `S / (W/W₀)` | Comparison with proportional width scaling; not a guaranteed attainable bound |
| Average chip power, `P` | McPAT **Runtime Dynamic + Subthreshold Leakage + Gate Leakage** | Whole modeled CPU, including caches, equivalent NoC and memory controllers |
| Static fraction | `(subthreshold + gate leakage) / P` | Contribution from fixed-reference leakage |
| ROI energy, `E` | `P × t` | Modeled chip joules for the measured interval |
| Normalized energy | `E/E₀ = (P/P₀)/S` | Below 1 saves energy |
| Energy-delay product | `EDP = E × t`; ratio `(P/P₀)/S²` | One explicit balance between time and energy; lower is better |
| Relative performance/watt | `S / (P/P₀) = E₀/E` | Reciprocal normalized energy for a fixed workload, not independent evidence |
| Temperature | Maximum named silicon-block steady-state temperature in °C | Excludes interface, spreader, heat sink and package nodes |
| Temperature change | `T − T₀`, in °C or equivalently K | Use differences; percentages of Celsius are not meaningful |
| Diagnostic throughput | `committed μops / (N × elapsed cycles)` | Wall-time-averaged per-core retirement throughput, including idle time |

The chip-power scope **includes modeled on-chip memory controllers/PHY but excludes DRAM chips, board power and peripherals**. No benchmark throughput or energy conclusion here is a full-system wall-plug claim. Leakage is evaluated at a fixed **350 K (76.85 °C)**, with no power gating, DVFS, thermal throttling or temperature/leakage feedback.

The solid lines connect three sampled settings; intermediate settings are not measurements. Each case has one observation, so there are no statistical confidence intervals or significance claims. Issued OpClass activity may include speculative/squashed work; committed instructions and micro-ops are separate metrics. Their counts are not interchangeable on x86.

## 3. Thread-level parallelism: more cores at width 4

{endpoint_tables['tlp']}

Here `P₈/P₀` and `E₈/E₀` compare C8/W4 with C1/W4; efficiency is `S₈/8`.

{fig('01_tlp_speedup', 'Figure 1. Core-count speedup and core scaling efficiency; the dashed line is proportional scaling.')}

### Speedup and scaling

**Blackscholes** scales from 29.094 ms to 8.380 ms to 4.200 ms: **3.472× at four cores and 6.928× at eight**, retaining 86.6% efficiency at eight cores. Doubling from four to eight cores yields another 1.995× speedup. Committed micro-op count grows only about 0.5% from one to eight cores, so the near-linear trend is accompanied by nearly constant executed work.

**Canneal** reaches **3.583× and 6.708×**, or 83.8% eight-core efficiency. Its low per-core retirement throughput does not prevent good thread scaling: independent threads can make concurrent progress even when each individual thread has limited instruction throughput. This distinction matters when comparing its strong TLP result with its weak width result.

**Freqmine** reaches **2.462× and 3.117×**. Doubling four to eight cores produces only 1.266× more speed. Eight-core efficiency falls to 39.0%; committed micro-op count increases by 9.9% relative to one core, while per-core throughput decreases. More hardware is doing both additional executed work and less work per core-cycle.

**Streamcluster** improves to **2.689× at four cores**, then falls to **1.936× at eight**. C8/W4 takes **38.9% longer than C4/W4**. This is a measured regression, not merely sublinear scaling. Section 7 examines the associated work and memory counters without assigning an unmeasured causal explanation.

### Power, energy and thermal cost

{fig('03_tlp_power_temperature', 'Figure 3. Absolute chip power and equilibrium silicon temperature in the core sweep.')}

At eight cores, power is **7.292×, 7.493×, 6.195× and 5.979×** the one-core power for blackscholes, canneal, freqmine and streamcluster. That cost buys very different amounts of useful runtime reduction. All four use more energy than their one-core baselines at eight cores: **+5.3%, +11.7%, +98.8% and +208.9%**, respectively.

Peak modeled equilibrium temperature rises by **34.76, 25.48, 24.81 and 29.35 °C** from one to eight cores. The hottest case in the entire dataset is blackscholes C8/W4 at **91.19 °C** and **335.18 W**. These are equilibrium model outputs under the assumed package, not measured temperatures at the end of a 4.2 ms execution.

**Answer for TLP:** more cores pay off strongly for blackscholes and canneal in runtime, moderately for freqmine, and only through four cores for streamcluster. Four cores save ROI energy for blackscholes/canneal. Eight cores are a speed-oriented choice for those two workloads, with increased modeled power, energy, area and temperature.

## 4. Instruction-level parallelism: wider pipelines at four cores

{endpoint_tables['ilp']}

The width sweep uses **C4/W2 as its baseline**. Its eight-wide nominal scale factor is 4, so efficiency here is `S₈/4`, not `S₈/8`.

{fig('02_ilp_speedup', 'Figure 2. Width-sweep speedup and speedup divided by the nominal width ratio.')}

**Blackscholes** benefits most: W2→W4 gives **1.957×**, and W2→W8 gives **3.212×** speedup. Its 80.3% fraction of proportional width scaling and nearly constant committed work are consistent with substantial exploitable instruction parallelism in this setup. W4→W8 still gives 1.642× speedup.

**Canneal** gains only **1.070× at W4 and 1.096× at W8**. The final doubling delivers just 2.4% additional speed. At W4 it retires about 0.369 μops/core/cycle, versus a four-wide datapath. L1D/L2 demand-miss densities are approximately 23.8/14.0 per thousand architectural instructions, much higher than blackscholes. These observations are consistent with important memory/dependency limits; they do not identify the precise limiting latency or prove bandwidth saturation.

**Freqmine** gains **1.275× at W4 and 1.463× at W8**. Width helps, but the measured gain remains well below proportional scaling. Fixed queues, functional units, dependencies and memory behavior are possible limits that this sweep does not isolate. W4→W8 gives 14.8% more speed for 8.7% more power, reducing energy by about 5.2%.

**Streamcluster** peaks at W4: **1.324×** speedup versus W2. W8 reaches only **1.173×**, taking **12.8% longer than W4**. Its committed micro-op count rises by 13.4% from W4 to W8 while per-core throughput changes by only about 0.5%. The extra retired activity, rather than a substantial throughput improvement, accompanies the runtime regression.

{fig('04_ilp_power_temperature', 'Figure 4. Power and equilibrium temperature as all seven pipeline widths increase.')}

W2→W8 increases average power by **79.6%, 4.4%, 17.5% and 28.2%**, and peak equilibrium temperature by **15.21, 0.42, 2.90 and 6.14 °C** for the four workloads. Energy changes by **−44.1%, −4.7%, −19.7% and +9.2%**. The larger blackscholes power increase is accompanied by much faster completion and less energy. For canneal, tiny extra speed beyond W4 does not offset the extra power: W8 consumes about 0.4% more energy than W4, a small single-run difference that should not be overstated.

**Answer for ILP:** widening helps when the workload can exploit it, most clearly blackscholes. It has diminishing value for freqmine, little value beyond W4 for canneal, and a measurable regression beyond W4 for streamcluster. This experiment changes throughput widths while keeping major queue and FU resources fixed; it does not evaluate fully redesigned commercial two-, four- and eight-wide cores.

## 5. Where does the power go?

### Component accounting without double counting

For every core, the decomposition is **core logic = detailed McPAT Core − Instruction Cache − Data Cache**. L1I/L1D are then added once, along with each shared L2 bank, the equivalent network and the aggregate memory-controller summary. The memory-controller summary is used because McPAT's detailed controller output mixes per-controller area/leakage with aggregate runtime power. Parent totals are never added to their children.

The largest relative difference between the sum of detailed block powers and the printed processor total is **{audit['max_component_power_relative_rounding_difference']:.2e}**, consistent with output rounding. All category powers include both runtime dynamic and fixed-reference static power unless explicitly separated.

At the common C4/W4 configuration:

{component_table}

{fig('07_tlp_component_power', 'Figure 7. Disjoint core, cache, network and controller power across the core sweep; axes differ by workload.')}

{fig('08_ilp_component_power', 'Figure 8. The same component accounting across the width sweep.')}

Core logic and private L1 dominate the baseline cases. For blackscholes, they contribute about **41.0% and 39.6%** of chip power; the network contributes 13.9%. For streamcluster, **L1 alone contributes 44.3%**, exceeding core logic's 35.8%. Canneal's modeled network share is 23.7%, largely leakage. Memory-controller power is small in this on-chip model, which says nothing about excluded off-chip DRAM energy.

These percentages are properties of the chosen mapping. In particular, McPAT estimates each 32 KiB L1D at **47.44 mm²**, versus **3.58 mm²** for each same-capacity L1I. Cache ports, buffers, cycle constraints and analytical implementation assumptions therefore deserve calibration before treating the cache-area or cache-power shares as realistic physical CPU measurements.

### Dynamic versus static

{fig('09_dynamic_static_power', 'Figure 9. Runtime dynamic, subthreshold leakage and gate leakage; both sweep panels reuse C4/W4 measurements.')}

{fig('10_component_dynamic_static', 'Figure 10. Per-component dynamic and static power at C4/W4.')}

At C4/W4, static power is approximately **76.20 W for every workload**. It accounts for **51.0%** of blackscholes, **81.9%** of canneal, **69.1%** of freqmine and **47.6%** of streamcluster power. Static power does not fall just because a workload retires fewer operations: voltage, hardware and reference leakage temperature remain fixed, with power gating disabled. Canneal therefore has a high leakage fraction despite relatively low dynamic power.

Most runtime dynamic power is in core logic and L1D in this mapping. At C4/W4, L2 and NoC power are largely static: blackscholes has only about **0.010 W L2 dynamic** and **0.093 W NoC dynamic**, compared with **7.40 W and 20.74 W static**. Streamcluster's modeled NoC dynamic is higher, **2.56 W**, but its NoC static remains 20.74 W.

At C8/W4, chip static power is **188.81 W**, accounting for **56.3–85.2%** of total power across these workloads. The equivalent NoC contributes **85.70 W of that static power**. Thus, a large network slice in a stacked plot cannot be read as direct evidence that message traffic is dissipating that much dynamic power.

### Hardware cost and the network assumption

{hardware_table}

{fig('18_area_and_static_cost', 'Figure 18. Area and fixed-reference leakage by disjoint component; all workloads share each configuration’s hardware.')}

The simulated SimplePt2Pt network has `N+6` routers/endpoints and a fully connected directed internal topology: 7, 10 and 14 routers, with 42, 90 and 182 directed internal links. The McPAT converter represents it as an **uncalibrated equivalent physical router network**, using port degree, three virtual networks and proxy buffers/flits. As endpoints increase, the modeled router complexity and leakage increase substantially. C1→C8 raises NoC static power **17.1×**, although core count rises 8×. The physical network is not a measured mesh layout, and the lumped HotSpot NoC block is not a recovered wire floorplan.

In contrast, W2→W8 keeps most hardware capacities and functional-unit counts fixed. Modeled chip area increases only **5.0%**, and static power only **1.35%**. This is the cost of the current width mapping and fixed-resource policy, not evidence that a physically validated eight-wide CPU costs only 5% more area than a two-wide CPU. Width also slightly changes equivalent network area through McPAT's geometry approximation, even though gem5's endpoint topology is unchanged.

## 6. Does power grow linearly?

“Linear” can mean either **proportional** (`P = kx`, through the origin) or **affine** (`P = a + bx`, with fixed overhead). These questions need different tests. A total-power ratio below the hardware-count ratio rejects proportional scaling over that interval, but does not by itself reject an affine relationship.

{fig('05_tlp_normalized_power', 'Figure 5. Core-sweep power normalized to C1/W4, with proportional reference and static fractions.')}

{fig('06_ilp_normalized_power', 'Figure 6. Width-sweep power normalized to C4/W2, with proportional reference and static fractions.')}

**Across C1→C8**, every workload's total-power ratio is below 8. **Across C4→C8**, however, blackscholes, canneal and freqmine increase by **2.242×, 2.381× and 2.139×**, exceeding the 2× increase in cores. Streamcluster increases by 1.908×. One cannot summarize this as power being uniformly sublinear in core count.

An affine relation would have constant incremental watts per added core or width unit. The measured interval slopes are:

{slope_table}

Blackscholes, canneal and freqmine have higher incremental watts/core in the C4→C8 interval, consistent with increasing network/static cost and workload-dependent activity. Streamcluster is **almost affine** over these samples: 36.36 versus 36.37 W per added core. It still has poor performance scaling; nearly affine power does not imply efficient scaling.

Every width interval has power elasticity below 1: widening is subproportional in total average power in this fixed-resource model. Canneal is nearly affine with a large fixed-power offset and a small added-width cost. The other workloads have smaller watts/width-unit in W4→W8 than W2→W4, although this does not imply a universal hardware power law.

{fig('17_power_elasticity', 'Figure 17. Interval power elasticity log(P₂/P₁)/log(x₂/x₁); the dashed level 1 denotes proportional growth.')}

Only three levels are sampled per sweep, with one observation each. No robust polynomial/power-law fit, significance test or extrapolation beyond this grid is justified. The interval comparisons answer the measured scaling question directly.

## 7. Why do the workloads behave differently?

The following are diagnostic associations. None is a source-level attribution to a particular lock, spin loop or data structure.

{fig('15_tlp_work_diagnostics', 'Figure 15. Core-sweep executed work, per-core retirement throughput, L2 misses and equivalent network hop flits; bottom axes are logarithmic.')}

{fig('16_ilp_work_diagnostics', 'Figure 16. Width-sweep diagnostics; per-core throughput counts all retired work, including synchronization.')}

**Blackscholes:** nearly unchanged committed work accompanies strong core and width scaling. This is the clearest case where additional execution capacity translates into lower time. It has very low L2 demand-miss activity compared with canneal/freqmine, while dynamic power rises as activity is completed faster.

**Canneal:** committed work is almost unchanged across either sweep. At four cores, wall-time-averaged per-core retirement throughput is only about **0.345, 0.369 and 0.378 μops/cycle** at W2, W4 and W8. More width barely changes that throughput; more independent cores still reduce completion time. Its demand-miss activity supports a memory/dependency bottleneck hypothesis, but latency and bandwidth measurements would be needed to separate those causes.

**Freqmine:** per-core throughput decreases as cores increase, and committed micro-op count grows about **9.9%** at eight cores versus one. The eight-core work shares are uneven: core 0 accounts for about **25%** of committed μops, compared with an equal-share 12.5%. That is evidence of uneven retired activity, not a direct measurement of useful-work balance; a thread doing synchronization can also retire many instructions.

### Streamcluster regression: what the counters actually show

{diagnostic_table}

For the core sweep, C1→C8 grows architectural instructions **3.271×** and committed μops **2.796×**. User-mode architectural instructions grow **3.127×**, so kernel execution alone cannot explain the work inflation. At the same time, μops/core/cycle falls from **2.625 to 1.777**. The exact accounting identity is:

`t = committed μops / (core count × clock frequency × wall-time-averaged μops/core/cycle)`.

Thus the measured speedup is consistent with both increased executed work and reduced per-core retirement throughput. It would be incorrect to interpret extra retired instructions as additional useful application work or to “correct” speedup by dividing away this overhead.

For W4→W8, committed μops increase **13.4%**, while throughput changes from **2.171 to 2.183 μops/core/cycle**. This corresponds to about **12.8% more time**, matching the measured regression. L2 demand misses actually decrease slightly in this width interval, while total hop-flit count is nearly unchanged. The observed slowdown therefore cannot be justified by simply claiming that this run has more cache misses or more total network traffic.

Core scaling does increase some memory-system activity: C4→C8 raises memory-controller requests from **25,760 to 213,362** and L2 demand misses from **1.593 M to 3.894 M**. Nevertheless, Ruby L2 demand misses are not equivalent to DRAM requests, and hop-flit counts are not stall cycles. These data alone do not establish bandwidth saturation, false sharing or lock contention. PC/source profiles, synchronization measurements, queue occupancy/latency and bandwidth counters would be appropriate follow-up evidence.

{fig('22_per_core_work_and_temperature', 'Figure 22. Eight-core retired-work shares and core-block temperatures; equal instruction shares do not guarantee equal useful progress.')}

{fig('23_mix_and_iq_pressure', 'Figure 23. Issued operation mix and normalized IQ-full rename events; events are not additive stall-cycle fractions.')}

The issued-operation mix is based on disjoint OpClass groups. The FP/SIMD category inherits the converter's scalar-equivalent SIMD approximation, including integer SIMD operations. IQ-full rename events grow with width for several workloads while IQ capacity remains 64, consistent with more queue pressure. These event counts do not supply a complete bottleneck breakdown and must not be summed into a percentage of execution time.

## 8. Energy and the speed–power–temperature tradeoff

Average watts answer a cooling/supply question; joules answer the cost of finishing the ROI. They can rank configurations differently. A point with higher average power can save energy if it completes sufficiently faster.

{energy_table}

Each “best” is **within the indicated sweep**, with no imposed temperature, area or power budget. EDP is one chosen objective, not a universally correct weighting. The canneal width-sweep energy difference between W4 and W8 is particularly small and has no replication-based uncertainty estimate.

{fig('11_tlp_energy_edp', 'Figure 11. Core-sweep normalized ROI energy and energy-delay product; lower is better.')}

{fig('12_ilp_energy_edp', 'Figure 12. Width-sweep normalized ROI energy and energy-delay product.')}

At C4/W4 versus C1/W4, blackscholes and canneal save **6.3% and 12.2%** energy, whereas freqmine and streamcluster consume **17.6% and 16.5%** more. At C8/W4, all four exceed baseline energy. Blackscholes/canneal still improve EDP at eight cores because of their large time reductions. Freqmine's EDP is best at four cores in the core sweep; streamcluster's eight-core EDP is worse than even its one-core baseline.

In the width sweep, W8 saves considerable energy for blackscholes and some for freqmine; canneal has its minimum at W4; streamcluster W8 uses more energy than both W2 and W4. Compared with C4/W4, streamcluster C8/W4 is worse in runtime, average power, energy and peak equilibrium temperature, as is C4/W8. C4/W4 therefore dominates those two sampled alternatives on these objectives.

{fig('13_tlp_tradeoff', 'Figure 13. Core-sweep speedup–power tradeoffs. Points below the diagonal save ROI energy; color encodes temperature and marker area grows with die area.')}

{fig('14_ilp_tradeoff', 'Figure 14. Width-sweep tradeoffs using the independent C4/W2 baseline.')}

Across the five measured configurations, blackscholes has its shortest time at C8/W4 but its lowest energy and EDP at C4/W8. Canneal's shortest time and lowest EDP occur at C8/W4, with lowest energy at C4/W4. Freqmine is fastest at C8/W4, most energy-efficient at C1/W4, and lowest in EDP at C4/W8. Streamcluster is fastest and lowest in EDP at C4/W4, with lowest energy at C1/W4. These choices are conditional on this model and the observed single runs.

### Optional equal-weight workload summary

{suite_table}

These are **geometric means of per-workload normalized ratios**, with equal weight per benchmark. They do not describe an actual simultaneously executing chip workload, a sum of application times or a population confidence interval. Celsius temperatures are not geometrically averaged. The core-sweep mean hides very different application behavior: C8 achieves 4.092× geometric-mean speedup but 1.639× geometric-mean energy. W8 achieves 1.568× speedup and 0.827× energy relative to W2, with most width benefit concentrated in blackscholes.

## 9. Temperature: interpretation and spatial patterns

The [thermal model](configs/hotspot/model-v1.json) applies each block's **ROI-average total power indefinitely** and solves for steady state. Ambient is **45 °C**, silicon thickness 0.15 mm, thermal interface thickness 20 μm, spreader side 50 mm, heat-sink side 70 mm, and convection resistance 0.1 K/W. These settings remain fixed across cases.

The square die uses the sum of disjoint McPAT block areas, with no added whitespace. A deterministic recursive bisection places core logic, L1I, L1D, two L2 banks, one whole-network block and one aggregate memory-controller block. Area and placement change across configurations, so the temperature comparison includes both different power and different surrogate geometry.

{fig('19_temperature_power_density', 'Figure 19. All 20 cases: equilibrium peak temperature versus total power and chip-average power density.')}

Temperature rises monotonically with both cores and width in every workload, but is not proportional to either hardware parameter. Higher total power raises the heat-removal burden; block density, adjacency, die area and package spreading also matter. Chip-average W/mm² alone does not determine the hottest block. For example, blackscholes C1/W4 has about **0.241 W/mm² and 56.43 °C**, while C4/W2 has a lower chip-average **0.216 W/mm²** but a higher peak **63.81 °C**, under the same package assumptions.

{fig('20_tlp_thermal_floorplans', 'Figure 20. Example core-sweep thermal maps for blackscholes and streamcluster, with common millimeter and temperature scales. Gray is outside the modeled die.')}

{fig('21_ilp_thermal_floorplans', 'Figure 21. Example width-sweep thermal maps under the same surrogate placement rule and cooling.')}

Map labels `Ck`, `CkI` and `CkD` denote core k's logic, L1I and L1D; `L2-0/1`, `NoC` and `MC` denote shared blocks. Small blocks may be unlabeled. The hottest named block can change: blackscholes C8/W4 peaks in core 4 logic, streamcluster C8/W4 in core 4 L1D, and canneal/freqmine C8/W4 in the **lumped NoC**. This last result is particularly dependent on the surrogate network leakage and placement.

The full ROI durations range from approximately **4.2 to 659.2 ms**. These steady-state maps do not predict the temperature reached after those short individual executions. No transient power trace or initial temperature history was used. Warm-up of simulated CPU/cache state and thermal warm-up are different concepts: excluding microarchitectural startup does not by itself make an equilibrium temperature physically occur during the ROI. Absolute values are not a safety-limit assessment, and within-block hotspots are unresolved.

## 10. Reliability, model limitations and what remains unresolved

The [validation record]({base}/data/validation.json) records 20 completed cases, 24 normalized sweep points and **260 SHA-256-hashed source artifacts**. The analysis rechecks stage provenance and the gem5→McPAT mapping, uses the completed ROI's statistics dump rather than arbitrary intermediate signal-triggered dumps, verifies cache hit/miss accounting and conserves detailed chip power. Two cases, canneal C1/W4 and freqmine C1/W4, have documented gem5 cycle-counter bookkeeping notes; exact tick duration is still used, with active cycles checked against elapsed time.

The following materially limit the conclusions:

1. **Single observation per configuration.** Guest scheduling, synchronization and initialization can influence results. Small differences, particularly canneal's width-energy ranking, need repetitions before being treated as stable design preferences.
2. **Cold-start full-ROI methodology.** O3 structures and caches are not preconditioned by detailed pre-ROI execution. The direction and magnitude of this bias can vary by workload and hardware size. Current data do not establish that warm-up would preserve every ranking.
3. **Physical timing is not validated.** Every McPAT run reports two `L2cache array structure cannot satisfy latency constraint` warnings, one per L2 bank. See [the complete warning list]({base}/data/mcpat_warnings.csv). gem5 timing is still the configured simulation timing; the analytical cache does not establish that those constraints are physically realizable at the modeled clock.
4. **Approximate power/activity mapping.** SIMD uses scalar-equivalent FPU activity; several register/storage structures lack separate representation; rename/CDB activity uses proxies. Cache demand read/write splitting is estimated, and explicit fill/writeback/coherence/directory activity is incomplete. Low modeled L2 dynamic power must be interpreted in that context. Details are recorded in the [McPAT policy](configs/mcpat/model-v1.json).
5. **Uncalibrated physical implementation.** Cache area/ports/buffers, equivalent NoC topology and controller models require calibration. Static and area growth, and hence thermal outcomes, strongly depend on them. NoC physical links and HotSpot adjacency are approximations of an abstract performance model.
6. **Fixed leakage and restricted energy scope.** Leakage is fixed at 350 K even when HotSpot predicts a different temperature. There is no electrothermal feedback, DVFS, gating, throttling or off-chip DRAM energy. Energy and EDP inherit these exclusions.
7. **Surrogate steady-state thermal model.** Power is spatially coarse and constant in time; geometry and cooling are assumed. The model does not supply transient temperatures, fine hotspots or a calibrated real package.
8. **Limited design/workload coverage.** Four `simsmall` workloads and two one-dimensional sweeps do not establish universal multicore scaling or the best joint core/width configuration. Architecture changes such as larger IQs, bandwidth or different network topologies were not tested.

The data support the observed speedup/regression patterns and the explicitly modeled cost tradeoffs. They do not yet establish exact source-level causes, manufacturing feasibility or real-chip temperature predictions.

## 11. Next study: a controlled warm-up comparison

No warm-up simulations were launched as part of this analysis. Keep this result set and its policies intact as the non-warm-up reference.

For the primary follow-up, keep **the same full `WORKBEGIN`→`WORKEND` ROI**, switch to the target O3 configuration **before `WORKBEGIN`**, run the selected pre-ROI initialization in detailed mode, and reset only statistics at `WORKBEGIN`. Verify that all target structures actually retain state into measurement and document exactly which pre-ROI interval was executed. Initialization is not automatically representative of steady-state ROI behavior; inspect misses/throughput over time or test more than one preconditioning interval where practical.

The current saved checkpoints are **at `WORKBEGIN`**, so they cannot recreate execution that happened earlier. A full-ROI, preconditioned run needs an earlier checkpoint or a fresh prepare path before the intended warm-up region. Existing at-ROI checkpoints can support warm-up **inside** the ROI followed by measuring the suffix, but that is a different experiment: work has already been consumed, so its elapsed time cannot be compared directly with this full-ROI baseline or called full-ROI speedup.

Use fresh result/checkpoint roots and explicit new policy identities. Preserve all five hardware configurations, four inputs, worker counts, frequency/voltage and ROI definition. Widths may share a compatible architectural fast-forward checkpoint at the same core/thread count, but target-specific microarchitectural state should be warmed for each target configuration. Checkpoint compatibility must be validated explicitly.

For each matched case, report `warm/full-ROI time ÷ cold/full-ROI time`, changes in demand misses, throughput, executed instruction counts, modeled dynamic power/energy and equilibrium temperature. Recompute both sweep speedups relative to their own warm baseline. If power/floorplan modeling is revised after calibration, reprocess **both** reference and warm datasets with the same revised models in separate outputs, so warm-up and model changes are not conflated. Repetitions are especially valuable for streamcluster's regressions and small canneal differences.

## 12. Reproduce, inspect and share

From the repository root, with completed `results/v1` artifacts and the same tool/model revisions:

```bash
python3 -m venv .venv-analysis
.venv-analysis/bin/python -m pip install -r requirements-analysis.txt
.venv-analysis/bin/python scripts/analyze_non_warm_up.py
```

The script validates existing results, regenerates the CSVs, plots, offline interactive HTML and this report. It **does not run gem5, McPAT or HotSpot**, and writes analysis artifacts only. `--results-root` and `--output-dir` select alternative artifact locations; this report specifically requires the current four-workload non-warm-up matrix and matching model provenance. The original simulation artifacts and compatible binaries must be available for a full rerun of the analysis.

The report's current analysis environment is Python **{audit['python']}**, NumPy **{audit['numpy']}**, pandas **{audit['pandas']}**, Matplotlib **{audit['matplotlib']}**, and Plotly **{audit['plotly']}**. Input policy SHA-256 is `{audit['policy_sha256']}`. Tool commits:

| Tool | Recorded commit |
| --- | --- |
| gem5 | `{audit['tool_commits']['gem5']}` |
| McPAT | `{audit['tool_commits']['mcpat']}` |
| HotSpot | `{audit['tool_commits']['hotspot']}` |

Share this Markdown file together with `reports/non_warm_up/` to preserve relative image/data links. Raw multi-gigabyte checkpoints are unnecessary for reading the report. For independent regeneration, provide the hashed source artifacts listed in [input_sha256.json]({base}/data/input_sha256.json), the matching configuration/converter sources and recorded tool revisions. Intermediate CSVs support independent metric checks without running the simulators. The HTML bundles Plotly and opens offline; static plots need no JavaScript.

### Complete case measurements

Values below are rounded for readability; CSVs retain additional digits. Temperature is maximum steady-state silicon-block temperature, with 0.01 K precision in the original HotSpot text output. Hardware area for each configuration is in Section 5.

{raw_table}

### Data files

| File | Contents |
| --- | --- |
| [summary.csv]({base}/data/summary.csv) | 20 unique cases: time, power, temperature, area, energy and diagnostic counters |
| [sweeps.csv]({base}/data/sweeps.csv) | 24 normalized rows with explicit sweep/baseline identity |
| [power_components.csv]({base}/data/power_components.csv) | Disjoint component area, dynamic/static power and chip share |
| [per_core.csv]({base}/data/per_core.csv) | Per-core retired work, throughput, power and temperature |
| [thermal_blocks.csv]({base}/data/thermal_blocks.csv) | Block geometry, power and temperature for every case |
| [power_scaling_intervals.csv]({base}/data/power_scaling_intervals.csv) | Adjacent-setting slopes, elasticity and speedup |
| [mcpat_warnings.csv]({base}/data/mcpat_warnings.csv) | All 40 recorded L2 timing warnings |
| [validation.json]({base}/data/validation.json) | Verification summary, policy/tool versions and analysis-source hash |
| [input_sha256.json]({base}/data/input_sha256.json) | Source artifact paths and hashes |

### Figure download index

PNG is convenient for Markdown; SVG and PDF preserve vector graphics for presentations and papers. Independent panel axes are labeled; use the common-color-scale floorplans when comparing thermal maps.

| Figure | Raster | Vector | Document |
| --- | --- | --- | --- |
{gallery}
"""
    (ROOT/"non_warm_up_analysis.md").write_text(text)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-root",type=Path,default=ROOT/"results/v1")
    parser.add_argument("--output-dir",type=Path,default=ROOT/"reports/non_warm_up")
    args=parser.parse_args()
    output=args.output_dir.resolve();output.mkdir(parents=True,exist_ok=True)
    df,sweeps,comp,core,blocks,slopes,audit=load_data(args.results_root,output)
    make_figures(output,df,sweeps,comp,core,blocks,slopes)
    audit["plotly"]=interactive_plot(output,sweeps)
    (output/"data/validation.json").write_text(json.dumps(audit,indent=2)+"\n")
    write_report(output,df,sweeps,comp,slopes,audit)
    print(f"Validated {len(df)} cases; generated 23 static figure sets, data tables and interactive tradeoffs in {output}")


if __name__=="__main__":
    main()
