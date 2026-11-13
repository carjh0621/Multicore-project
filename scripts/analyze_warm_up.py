#!/usr/bin/env python3
"""Analyze completed full-ROI warm-up runs and their matched cold reference.

Writes reports/warm_up and reports/warm_up_comparison only; never runs simulators.
"""

import argparse
import json
import math
import os
from pathlib import Path
import re

os.environ.setdefault("MPLBACKEND", "Agg")

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.colors import Normalize, TwoSlopeNorm
from matplotlib.patches import Rectangle
from matplotlib.lines import Line2D

from analyze_non_warm_up import (BENCHMARKS, COLORS, CONFIGS, COMPONENTS, COMP_COLORS,
    COMP_LABELS, SWEEP_SPECS, ROOT, geo_mean, load_data, make_figures, interactive_plot,
    markdown_table, save_figure, style)
from gem5_to_mcpat import read_json, require, sha256

WARM = ROOT/"reports/warm_up"
COMPARE = ROOT/"reports/warm_up_comparison"
REFERENCE = COMPARE/"reference"
WARM_POLICY = ROOT/"configs/experiment-warmup.json"
FRAME_NAMES = ["summary", "sweeps", "power_components", "per_core", "thermal_blocks", "power_scaling_intervals"]
PAIR_KEYS = ["case", "benchmark", "cores", "width"]
LABELS = [f"C{n}/W{w}" for n,w in CONFIGS]


def restore_tables(output, policy):
    """Reuse parsing only when source hashes and extractor identity still match."""
    audit = read_json(output/"data/validation.json")
    require(audit["policy_sha256"] == sha256(policy), "Extracted policy changed; re-extract")
    require(audit["analysis_script_sha256"] == sha256(ROOT/"scripts/analyze_non_warm_up.py"),
            "Shared extractor changed; re-extract")
    for source in read_json(output/"data/input_sha256.json"):
        require(sha256(ROOT/source["file"]) == source["sha256"], "Source changed: "+source["file"])
    return (*[pd.read_csv(output/"data"/(n+".csv")) for n in FRAME_NAMES], audit)


def align_cases(df):
    return df.set_index(["benchmark","cores","width"]).loc[
        [(b,n,w) for b in BENCHMARKS for n,w in CONFIGS]].reset_index()


def paired_data(cold, warm):
    c,cs,cc,cr,cb,cl,ca = cold
    w,ws,wc,wr,wb,wl,wa = warm
    require(ca["tool_commits"] == wa["tool_commits"], "Upstream revisions differ between scenarios")
    require(len(c) == len(w) == 20 and c["case"].is_unique and w["case"].is_unique, "Expected 20 unique pairs")
    m = w.merge(c,on=PAIR_KEYS,suffixes=("_warm","_cold"),validate="one_to_one")
    require(len(m) == 20, "Missing matched configurations")
    hardware = []
    for _,r in m.iterrows():
        a,b = ROOT/"results/v1"/r["case"],ROOT/"results/warmup-v1"/r["case"]
        cfg_a,cfg_b = read_json(a/"config.json")["board"],read_json(b/"config.json")["board"]
        checks = {"resolved_o3_equal":cfg_a["processor"]["switch"]==cfg_b["processor"]["switch"],
                  "cache_hierarchy_equal":cfg_a["cache_hierarchy"]==cfg_b["cache_hierarchy"],
                  "memory_equal":cfg_a["memory"]==cfg_b["memory"],
                  "floorplan_equal":(a/"hotspot/floorplan.flp").read_bytes()==(b/"hotspot/floorplan.flp").read_bytes(),
                  "thermal_config_equal":(a/"hotspot/hotspot.config").read_bytes()==(b/"hotspot/hotspot.config").read_bytes()}
        require(all(checks.values()), "Unmatched resolved hardware/thermal settings: "+r["case"]+str(checks))
        hardware.append({"case":r["case"],**checks})
    for name in ["static_w","area_mm2","native_clock_hz"]:
        require(np.allclose(m[name+"_warm"],m[name+"_cold"],rtol=5e-12,atol=1e-12), "Paired fixed hardware differs: "+name)
    ratios = {"time_ratio":"roi_seconds","paired_speedup":"roi_seconds", "power_ratio":"total_power_w",
        "dynamic_power_ratio":"runtime_dynamic_w","static_power_ratio":"static_w","energy_ratio":"energy_j",
        "edp_ratio":"edp_j_s","uop_ratio":"committed_micro_ops","arch_instruction_ratio":"architectural_instructions",
        "user_instruction_ratio":"user_architectural_instructions","wall_throughput_ratio":"uipc_per_core_wall",
        "active_throughput_ratio":"uipc_per_active_cycle","active_fraction_ratio":"mean_active_tick_fraction",
        "l1d_miss_ratio":"l1d_demand_misses","l2_miss_ratio":"l2_demand_misses",
        "flit_ratio":"equivalent_noc_hop_flits","memory_request_ratio":"memory_requests"}
    for out,src in ratios.items():
        m[out] = m[src+"_warm"]/m[src+"_cold"]
        if out=="paired_speedup":m[out]=1/m[out]
    for name in ["time","power","dynamic_power","energy","edp","uop"]:
        m[name+"_change_pct"] = 100*(m[name+"_ratio"]-1)
    m["temperature_delta_c"] = m.temperature_c_warm-m.temperature_c_cold
    m["quiesce_delta_percentage_points"] = 100*(m.quiesce_cycles_per_elapsed_core_cycle_warm-m.quiesce_cycles_per_elapsed_core_cycle_cold)
    require(np.allclose(m.energy_ratio,m.time_ratio*m.power_ratio,rtol=2e-10), "Paired energy identity failed")
    require(np.allclose(m.time_ratio,m.uop_ratio/m.wall_throughput_ratio,rtol=2e-10), "Work/throughput identity failed")
    require(np.allclose(m.wall_throughput_ratio,m.active_fraction_ratio*m.active_throughput_ratio,rtol=2e-10),
            "Active/elapsed throughput identity failed")
    q = ws.merge(cs,on=PAIR_KEYS+["sweep"],suffixes=("_warm","_cold"),validate="one_to_one")
    require(len(q)==24 and (q.baseline_case_warm==q.baseline_case_cold).all(),"Sweep baselines differ")
    q["scaling_speedup_ratio"] = q.speedup_warm/q.speedup_cold
    q["scaling_efficiency_delta_pp"] = 100*(q.scaling_efficiency_warm-q.scaling_efficiency_cold)
    cp = wc.merge(cc,on=PAIR_KEYS+["component"],suffixes=("_warm","_cold"),validate="one_to_one")
    for field in ["runtime_dynamic_w","static_w","total_average_w"]:
        cp[field+"_delta"] = cp[field+"_warm"]-cp[field+"_cold"]
    require(np.allclose(cp.static_w_delta,0,atol=1e-12),"Paired component leakage changed")
    rows=[]
    for _,r in m.iterrows():
        g=cp[cp["case"].eq(r["case"])]
        delta=g.total_average_w_delta.sum()
        require(math.isclose(delta,r.total_power_w_warm-r.total_power_w_cold,rel_tol=5e-4,abs_tol=6e-4),
                "Paired component delta does not conserve power")
    for b in BENCHMARKS:
        for scenario,df in [("non_warm_up",c),("warm_up",w)]:
            g=df[df.benchmark.eq(b)]
            row={"benchmark":b,"scenario":scenario}
            for field,key in [("roi_seconds","fastest"),("energy_j","minimum_energy"),("edp_j_s","minimum_edp")]:
                row[key]=g.loc[g[field].idxmin(),"case"]
            rows.append(row)
    d=COMPARE/"data";d.mkdir(parents=True,exist_ok=True)
    for name,frame in [("matched_cases",align_cases(m)),("matched_sweeps",q),("component_changes",cp),
                       ("hardware_checks",pd.DataFrame(hardware)),("configuration_choices",pd.DataFrame(rows))]:
        frame.to_csv(d/(name+".csv"),index=False,float_format="%.12g")
    return align_cases(m),q,cp,pd.DataFrame(rows)


def independent_checks(cold,warm,matched):
    """Recalculate from raw tick/power JSON, independently of the shared extractor."""
    old=pd.read_csv(ROOT/"reports/non_warm_up/data/summary.csv").set_index("case")
    fresh=cold[0].set_index("case")
    for field in old.select_dtypes(include="number").columns:
        require(np.allclose(old.loc[fresh.index,field],fresh[field],rtol=2e-10,atol=1e-10),
                "Published cold reference changed: "+field)
    raw={}
    for scenario,base,dataset in [("cold",ROOT/"results/v1",cold),("warm",ROOT/"results/warmup-v1",warm)]:
        for _,r in dataset[0].iterrows():
            folder=base/r["case"];roi=read_json(folder/"roi.json");p=read_json(folder/"mcpat/power.json")
            t=(roi["end_tick"]-roi["begin_tick"])/roi["ticks_per_second"]
            watts=p["runtime_dynamic_w"]+p["subthreshold_leakage_w"]+p["gate_leakage_w"]
            for field,value in [("roi_seconds",t),("total_power_w",watts),("energy_j",watts*t),("edp_j_s",watts*t*t)]:
                require(math.isclose(r[field],value,rel_tol=2e-10,abs_tol=1e-12),"Raw metric mismatch: "+r["case"]+field)
            raw[(scenario,r["case"])]=(t,watts)
        for _,r in dataset[1].iterrows():
            t,power=raw[(scenario,r["case"])];bt,bp=raw[(scenario,r["baseline_case"])]
            expected=f"{r.benchmark}-c1-w4" if r.sweep=="tlp" else f"{r.benchmark}-c4-w2"
            require(r.baseline_case==expected and math.isclose(r.speedup,bt/t,rel_tol=2e-10)
                    and math.isclose(r.energy_ratio,power*t/(bp*bt),rel_tol=2e-10),"Wrong scenario baseline")
    for _,r in matched.iterrows():
        ct,cp=raw[("cold",r["case"])];wt,wp=raw[("warm",r["case"])]
        for field,value in [("time_ratio",wt/ct),("power_ratio",wp/cp),("energy_ratio",(wt*wp)/(ct*cp))]:
            require(math.isclose(r[field],value,rel_tol=2e-10),"Independent paired ratio mismatch")
    result={"status":"passed","raw_cases_checked":40,"matched_cases_checked":20,"sweep_rows_checked":48,
            "original_published_cold_numeric_columns_reproduced":True,"analysis_entrypoint_sha256":sha256(Path(__file__)),
            "checks":"Raw tick/power calculations, paired ratios, own-scenario baselines and published reference metrics"}
    (COMPARE/"data/independent_checks.json").write_text(json.dumps(result,indent=2)+"\n")


def figure_pair_sweeps(q,sw,field,ylabel,name,title,ideal=False):
    axis,values,_=SWEEP_SPECS[sw]
    fig,axes=plt.subplots(2,2,figsize=(11.5,8),layout="constrained")
    for b,ax in zip(BENCHMARKS,axes.flat):
        g=q[q.benchmark.eq(b)&q.sweep.eq(sw)].sort_values(axis)
        for suffix,color,label,ls in [("cold","#777777","Non-warm-up","--"),("warm",COLORS[b],"Pre-ROI warm-up","-")]:
            ax.plot(g[axis],g[field+"_"+suffix],"o",ls=ls,color=color,lw=2,label=label)
        if ideal:ax.plot(values,np.array(values)/values[0],":",color="#AAAAAA",label="Proportional reference")
        ax.set(title=b,xlabel="Core count at W4" if sw=="tlp" else "Width at C4",ylabel=ylabel,xticks=values)
        ax.legend(fontsize=8)
    fig.suptitle(title)
    save_figure(fig,COMPARE,name)


def matrix(m,field):
    return np.array([[m[m.benchmark.eq(b)&m.cores.eq(n)&m.width.eq(w)][field].iloc[0] for n,w in CONFIGS] for b in BENCHMARKS])


def heatmap(ax,data,title,percent=True):
    lim=max(float(np.max(np.abs(data))),1e-9)
    im=ax.imshow(data,cmap="RdBu_r",norm=TwoSlopeNorm(vcenter=0,vmin=-lim,vmax=lim),aspect="auto")
    ax.set(title=title,xticks=range(5),xticklabels=LABELS,yticks=range(4),yticklabels=BENCHMARKS)
    ax.tick_params(axis="x",labelrotation=25);ax.grid(False)
    for i in range(4):
        for j in range(5):
            ax.text(j,i,f"{data[i,j]:+.1f}"+("%" if percent else ""),ha="center",va="center",fontsize=8,
                    color="white" if abs(data[i,j])/lim>.65 else "black")
    return im


def comparison_figures(cold,warm,m,q,cp):
    c,cs,cc,cr,cb,cl,ca=cold;w,ws,wc,wr,wb,wl,wa=warm
    style()
    fig,axes=plt.subplots(2,2,figsize=(12,8),layout="constrained")
    for b,ax in zip(BENCHMARKS,axes.flat):
        g=m[m.benchmark.eq(b)].set_index(["cores","width"]).loc[CONFIGS]
        x=np.arange(5)
        ax.bar(x-.18,g.roi_ms_cold,.36,color="#999999",label="Non-warm-up")
        ax.bar(x+.18,g.roi_ms_warm,.36,color=COLORS[b],label="Pre-ROI warm-up")
        ax.set(title=b,ylabel="Full ROI time (ms)",xticks=x,xticklabels=LABELS);ax.tick_params(axis="x",labelrotation=20)
        ax.legend(fontsize=8)
    fig.suptitle("Matched full-ROI times: same hardware and workload")
    save_figure(fig,COMPARE,"01_paired_roi_times")
    fig,axes=plt.subplots(2,2,figsize=(14,8),layout="constrained")
    for ax,field,title,pct in zip(axes.flat,["time_change_pct","power_change_pct","energy_change_pct","temperature_delta_c"],
                                ["ROI time change (%) · lower is better","Average power change (%)","ROI energy change (%) · lower is better","Peak equilibrium temperature change (°C)"],[True,True,True,False]):
        im=heatmap(ax,matrix(m,field),title,pct);fig.colorbar(im,ax=ax,shrink=.75)
    fig.suptitle("Warm-up minus non-warm-up: percentages use matched cold cases")
    save_figure(fig,COMPARE,"02_paired_metric_changes")
    figure_pair_sweeps(q,"tlp","speedup","Speedup vs own C1/W4","03_tlp_speedup_comparison","Core scaling: each scenario uses its own one-core baseline",True)
    figure_pair_sweeps(q,"ilp","speedup","Speedup vs own C4/W2","04_ilp_speedup_comparison","Width scaling: each scenario uses its own two-wide baseline",True)
    for sw,num in [("tlp","05"),("ilp","06")]:
        fig,axes=plt.subplots(1,2,figsize=(12,4.6),layout="constrained")
        axis,values,xlabel=SWEEP_SPECS[sw]
        for b in BENCHMARKS:
            g=q[q.benchmark.eq(b)&q.sweep.eq(sw)].sort_values(axis)
            for suffix,ls,marker in [("cold","--","s"),("warm","-","o")]:
                for ax,field in zip(axes,["total_power_w","temperature_c"]):
                    ax.plot(g[axis],g[field+"_"+suffix],ls=ls,marker=marker,color=COLORS[b],lw=1.7)
        for ax,ylabel in zip(axes,["Average modeled chip power (W)","Peak equilibrium silicon temperature (°C)"]):
            ax.set(xlabel=xlabel,ylabel=ylabel,xticks=values)
        handles=[Line2D([],[],color=COLORS[b],label=b,lw=2) for b in BENCHMARKS]+[
            Line2D([],[],color="gray",ls="--",marker="s",label="Non-warm-up"),Line2D([],[],color="gray",marker="o",label="Warm-up")]
        fig.legend(handles=handles,loc="outside lower center",ncol=3,fontsize=8)
        fig.suptitle(sw.upper()+": power and equilibrium temperature with fixed paired geometry")
        save_figure(fig,COMPARE,num+"_"+sw+"_power_temperature")
    fig,axes=plt.subplots(2,2,figsize=(12,8),layout="constrained")
    for i,sw in enumerate(["tlp","ilp"]):
        axis,values,xlabel=SWEEP_SPECS[sw]
        for b in BENCHMARKS:
            g=q[q.benchmark.eq(b)&q.sweep.eq(sw)].sort_values(axis)
            for suffix,ls in [("cold","--"),("warm","-")]:
                for ax,field in zip(axes[i],["energy_ratio","edp_ratio"]):ax.plot(g[axis],g[field+"_"+suffix],"o",ls=ls,color=COLORS[b])
        for ax,field in zip(axes[i],["energy_ratio","edp_ratio"]):
            ax.axhline(1,color="#888888",lw=1);ax.set(title=sw.upper(),xlabel=xlabel,ylabel="Normalized "+("ROI energy" if field=="energy_ratio" else "EDP"),xticks=values)
    fig.legend(handles=handles,loc="outside lower center",ncol=3,fontsize=8)
    fig.suptitle("Energy/EDP scaling: each scenario uses its own sweep baseline")
    save_figure(fig,COMPARE,"07_energy_edp_comparison")
    fig,axes=plt.subplots(2,3,figsize=(17,8),layout="constrained")
    for ax,field,title in zip(axes.flat,["uop_ratio","wall_throughput_ratio","active_fraction_ratio","l1d_miss_ratio","l2_miss_ratio","flit_ratio"],
                             ["Committed μops","μops/core/elapsed cycle","Recorded active fraction","L1D demand misses","L2 demand misses","Equivalent hop flits"]):
        im=heatmap(ax,100*(matrix(m,field)-1),title+" change (%)");fig.colorbar(im,ax=ax,shrink=.75)
    fig.suptitle("Matched diagnostics: extra work, elapsed-time throughput and memory activity")
    save_figure(fig,COMPARE,"08_work_activity_changes")
    fig,axes=plt.subplots(2,2,figsize=(12,8),layout="constrained")
    for b,ax in zip(BENCHMARKS,axes.flat):
        pos,neg=np.zeros(5),np.zeros(5)
        for key,label,color in zip(COMPONENTS,COMP_LABELS,COMP_COLORS):
            vals=np.array([cp[cp.benchmark.eq(b)&cp.cores.eq(n)&cp.width.eq(width)&cp.component.eq(key)].total_average_w_delta.iloc[0] for n,width in CONFIGS])
            ax.bar(range(5),vals,bottom=np.where(vals>=0,pos,neg),color=color,label=label)
            pos+=np.maximum(vals,0);neg+=np.minimum(vals,0)
        ax.axhline(0,color="#333333",lw=.8);ax.set(title=b,ylabel="Component power change (W)",xticks=range(5),xticklabels=LABELS);ax.tick_params(axis="x",labelrotation=20)
    fig.legend(*axes[0,0].get_legend_handles_labels(),loc="outside lower center",ncol=3,fontsize=8)
    fig.suptitle("Power changes by disjoint component; fixed-reference leakage is unchanged")
    save_figure(fig,COMPARE,"09_component_power_changes")
    fig,axes=plt.subplots(1,2,figsize=(12,4.8),layout="constrained")
    for b in BENCHMARKS:
        g=m[m.benchmark.eq(b)]
        axes[0].scatter(g.time_ratio,g.dynamic_power_ratio,color=COLORS[b],s=60,label=b)
        axes[1].scatter(g.time_ratio,g.energy_ratio,color=COLORS[b],s=60)
        for _,r in g.iterrows():
            if abs(r.time_change_pct)>10:
                for ax,field in zip(axes,["dynamic_power_ratio","energy_ratio"]):ax.annotate(f"C{r.cores}/W{r.width}",(r.time_ratio,r[field]),xytext=(4,3),textcoords="offset points",fontsize=8)
    for ax,ylabel in zip(axes,["Runtime dynamic power: warm/cold","ROI energy: warm/cold"]):
        ax.axhline(1,color="gray",ls="--");ax.axvline(1,color="gray",ls="--");ax.set(xlabel="Full ROI time: warm/cold",ylabel=ylabel)
    axes[0].legend(fontsize=8);fig.suptitle("Lower average dynamic power need not mean less energy")
    save_figure(fig,COMPARE,"10_dynamic_power_time_energy")
    fig,axes=plt.subplots(1,2,figsize=(12,4.8),layout="constrained")
    for b in BENCHMARKS:
        g=w[w.benchmark.eq(b)].set_index(["cores","width"]).loc[CONFIGS]
        axes[0].plot(range(5),g.warmup_seconds,"o-",color=COLORS[b],label=b)
        axes[1].plot(range(5),g.warmup_to_roi_time_ratio,"o-",color=COLORS[b])
    for ax,ylabel in zip(axes,["Excluded O3 initialization (simulated s)","Initialization time / measured ROI time"]):
        ax.set(xticks=range(5),xticklabels=LABELS,ylabel=ylabel);ax.tick_params(axis="x",labelrotation=20)
    axes[1].set_yscale("log");axes[0].legend(fontsize=8)
    fig.suptitle("Pre-ROI coverage varies by workload and target width; not a convergence test")
    save_figure(fig,COMPARE,"11_initialization_coverage")
    cases=["blackscholes-c4-w8","streamcluster-c4-w8"]
    norm=Normalize(min(cb.temperature_c.min(),wb.temperature_c.min()),max(cb.temperature_c.max(),wb.temperature_c.max()))
    fig,axes=plt.subplots(2,3,figsize=(14,9),layout="constrained")
    for i,case in enumerate(cases):
        a=cb[cb["case"].eq(case)].set_index("name");b=wb[wb["case"].eq(case)].set_index("name")
        delta=b.temperature_c-a.temperature_c;lim=max(abs(delta).max(),.01);dn=TwoSlopeNorm(vcenter=0,vmin=-lim,vmax=lim)
        for j,(g,values,title) in enumerate([(a,a.temperature_c,"Non-warm-up"),(b,b.temperature_c,"Warm-up"),(b,delta,"Warm − cold")]):
            ax=axes[i,j];cmap=plt.cm.RdBu_r if j==2 else plt.cm.inferno;scale=dn if j==2 else norm
            for name,r in g.iterrows():
                ax.add_patch(Rectangle((r.x_m*1000,r.y_m*1000),r.width_m*1000,r.height_m*1000,
                                      facecolor=cmap(scale(values.loc[name])),edgecolor="white",lw=.5))
            edge=max((g.x_m+g.width_m).max(),(g.y_m+g.height_m).max())*1000
            ax.set(title=case+"\n"+title,xlim=(0,edge),ylim=(0,edge),aspect="equal",xlabel="x (mm)",ylabel="y (mm)");ax.grid(False)
            fig.colorbar(plt.cm.ScalarMappable(norm=scale,cmap=cmap),ax=ax,shrink=.75,label="Δ°C" if j==2 else "°C")
    fig.suptitle("Identical paired floorplans: changes reflect ROI power, not thermal preheating")
    save_figure(fig,COMPARE,"12_paired_thermal_maps")
    fig,axes=plt.subplots(2,2,figsize=(12,8),layout="constrained")
    for j,width in enumerate([4,8]):
        name=f"blackscholes-c4-w{width}"
        for data,offset,color,label in [(cr,-.18,"#999999","Non-warm-up"),(wr,.18,"#0072B2","Warm-up")]:
            g=data[data["case"].eq(name)].sort_values("core_id")
            axes[0,j].bar(g.core_id+offset,100*g.active_tick_fraction,.36,color=color,label=label)
            axes[1,j].bar(g.core_id+offset,g.uipc_wall/g.active_tick_fraction,.36,color=color,label=label)
        axes[0,j].set(title=name,ylabel="Recorded active cycles / elapsed cycles (%)",xticks=range(4),xlabel="Core ID")
        axes[1,j].set(ylabel="Committed μops / recorded active cycle",xticks=range(4),xlabel="Core ID")
    axes[0,0].legend(fontsize=8);fig.suptitle("Blackscholes: the W8 regression coincides with more inactive time")
    save_figure(fig,COMPARE,"13_blackscholes_active_throughput")
    fig,axes=plt.subplots(2,2,figsize=(12,8),layout="constrained")
    fields=[("uop_ratio","Executed work", "#4C78A8",1),("active_fraction_ratio","Active fraction", "#F28E2B",-1),
            ("active_throughput_ratio","Active-cycle throughput","#59A14F",-1)]
    for b,ax in zip(BENCHMARKS,axes.flat):
        g=m[m.benchmark.eq(b)].set_index(["cores","width"]).loc[CONFIGS];pos,neg=np.zeros(5),np.zeros(5)
        for field,label,color,sign in fields:
            vals=sign*np.log(g[field].to_numpy());ax.bar(range(5),vals,bottom=np.where(vals>=0,pos,neg),color=color,label=label)
            pos+=np.maximum(vals,0);neg+=np.minimum(vals,0)
        ax.plot(range(5),np.log(g.time_ratio),"ko",label="Observed log time ratio");ax.axhline(0,color="gray",lw=.8)
        ax.set(title=b,ylabel="Contribution to ln(warm time / cold time)",xticks=range(5),xticklabels=LABELS);ax.tick_params(axis="x",labelrotation=20)
    fig.legend(*axes[0,0].get_legend_handles_labels(),loc="outside lower center",ncol=2,fontsize=8)
    fig.suptitle("Accounting identity: time ∝ retired work / (active fraction × active-cycle throughput)")
    save_figure(fig,COMPARE,"14_time_ratio_accounting")
    fig,axes=plt.subplots(1,2,figsize=(13,4.8),layout="constrained")
    for sw,ax in zip(["tlp","ilp"],axes):
        axis=SWEEP_SPECS[sw][0]
        for b in BENCHMARKS:
            z=q[q.sweep.eq(sw)&q.benchmark.eq(b)].sort_values(axis)
            ax.plot(z[axis],z.scaling_speedup_ratio,"o-",color=COLORS[b],label=b)
        ax.axhline(1,color="gray",ls="--");ax.set(title=sw.upper(),xlabel="Core count" if sw=="tlp" else "Width",ylabel="Warm normalized speedup / cold normalized speedup",xticks=SWEEP_SPECS[sw][1])
    axes[0].legend(fontsize=8);fig.suptitle("Normalization matters: the sweep baseline changes between scenarios too")
    save_figure(fig,COMPARE,"15_change_in_scaling_conclusions")


def interactive_comparison(m):
    import plotly.express as px
    fig=px.scatter(m,x="time_change_pct",y="energy_change_pct",color="benchmark",hover_name="case",
        hover_data=["power_change_pct","temperature_delta_c","roi_ms_cold","roi_ms_warm","warmup_seconds"],
        color_discrete_map=COLORS,template="plotly_white",title="Warm-up effect on full ROI · matched cold case = 0%")
    fig.add_hline(y=0,line_dash="dash");fig.add_vline(x=0,line_dash="dash")
    fig.update_xaxes(title="ROI time change (%)");fig.update_yaxes(title="ROI energy change (%)")
    fig.write_html(COMPARE/"interactive_comparison.html",include_plotlyjs=True,full_html=True)


def report_table(frame, columns, digits=3):
    numeric=lambda v,d:f"{0 if abs(v)<.5*10**(-d) else v:.{d}f}"
    formats={k:(lambda v,d=digits:numeric(v,d)) for k in columns if pd.api.types.is_float_dtype(frame[k])}
    for k in columns:
        if "temperature" in k or k.endswith("_pct") or k.endswith("_pp"):
            formats[k]=lambda v:numeric(v,2)
    return markdown_table(frame,columns,formats)


def report_figure(name,caption,prefix=""):
    return f"![{caption}]({prefix}figures/{name}.png)\n\n*{caption}*"


def gallery(output):
    return "\n".join(f"| {p.stem} | [PNG](figures/{p.name}) | [SVG](figures/{p.stem}.svg) | [PDF](figures/{p.stem}.pdf) |"
                     for p in sorted((output/"figures").glob("*.png")))


def write_reports(cold,warm,m,q,cp,choices,audit):
    c,cs,cc,cr,cb,cl,ca=cold;w,ws,wc,wr,wb,wl,wa=warm
    get=lambda name:m[m["case"].eq(name)].iloc[0]
    black=get("blackscholes-c4-w8");can=get("canneal-c8-w4");stream=get("streamcluster-c4-w8")
    # This is a report for the frozen current dataset, not a generic causal classifier.
    require(black.time_ratio>1.5 and can.time_ratio>1 and stream.time_ratio<1,
            "Key observed patterns changed; revise the interpretation before publishing")
    endpoints={}
    for sw in ["tlp","ilp"]:
        axis=SWEEP_SPECS[sw][0];g=ws[ws.sweep.eq(sw)]
        end=g[g[axis].eq(8)].set_index("benchmark").loc[BENCHMARKS].copy()
        end["speedup_at_4"]=g[g[axis].eq(4)].set_index("benchmark").speedup
        end["efficiency_pct"]=100*end.scaling_efficiency
        endpoints[sw]=report_table(end.reset_index(),{"benchmark":"Workload","speedup_at_4":"Speedup at 4",
            "speedup":"Speedup at 8","efficiency_pct":"Scale fraction at 8 (%)","power_ratio":"P₈/P₀",
            "energy_ratio":"E₈/E₀","temperature_delta_c":"ΔT₈ (°C)"})
    raw=align_cases(w)
    raw_table=report_table(raw,{"case":"Case","roi_ms":"ROI ms","runtime_dynamic_w":"Dynamic W","static_w":"Static W",
        "total_power_w":"Total W","energy_j":"ROI J","temperature_c":"Peak °C","warmup_seconds":"Excluded init s"})
    comp_rows=[]
    for b in BENCHMARKS:
        g=wc[wc["case"].eq(f"{b}-c4-w4")]
        r={"benchmark":b}
        for name,keys in [("logic",["core_logic"]),("l1",["l1i","l1d"]),("l2",["l2"]),("noc",["interconnect"]),("mc",["memory_controller"])]:
            r[name]=g[g.component.isin(keys)].total_average_w.sum()
        r["static_pct"]=100*w[w["case"].eq(f"{b}-c4-w4")].static_fraction.iloc[0]
        comp_rows.append(r)
    comp_table=report_table(pd.DataFrame(comp_rows),{"benchmark":"Workload","logic":"Core logic W","l1":"L1I+D W",
        "l2":"L2 W","noc":"NoC W","mc":"MC W","static_pct":"Static %"},2)
    best_rows=[]
    for sw in ["tlp","ilp"]:
        for b in BENCHMARKS:
            g=ws[ws.sweep.eq(sw)&ws.benchmark.eq(b)];r={"sweep":sw.upper(),"benchmark":b}
            for metric,name in [("roi_seconds","fastest"),("energy_j","energy"),("edp_j_s","edp")]:
                z=g.loc[g[metric].idxmin()];r[name]=f"C{z.cores}/W{z.width}"
            best_rows.append(r)
    best_table=report_table(pd.DataFrame(best_rows),{"sweep":"Sweep","benchmark":"Workload","fastest":"Fastest","energy":"Least ROI energy","edp":"Least EDP"})
    init_table=report_table(raw,{"case":"Case","warmup_seconds":"Excluded init s","warmup_to_roi_time_ratio":"Init / ROI time",
        "warmup_architectural_instructions":"Init architectural instructions","warmup_l1d_demand_accesses":"Init L1D demands"})
    warm_only_choices=choices[choices.scenario.eq("warm_up")]
    warm_report=f"""# Warm-up AI analysis: full-ROI multicore evaluation

All **20 warm-up cases** completed gem5, McPAT and HotSpot and passed artifact/provenance checks. This study runs the same four PARSEC 3.0 `simsmall` workloads and five hardware configurations as the [non-warm-up reference](../non_warm_up/non_warm_up_AI_analysis.md). There is one observation per scenario/configuration. The [matched comparison](../warm_up_comparison/warm_up_vs_non_warm_up_AI_analysis.md) explains which conclusions change and why single-run effects must not be treated as cache-only causation.

**Main findings:** core scaling remains strongest for blackscholes and canneal. Freqmine has diminishing core returns. Streamcluster still reaches its best core-sweep runtime at four cores, but its best measured width is now eight. Blackscholes is fastest at width four within its width sweep: its eight-wide case has much more quiescent time. More hardware is therefore not uniformly faster, hotter or higher in average power in these particular runs. Model-dependent energy and thermal costs must be read alongside simulated runtime.

## 1. Method and scope

The [warm-up policy](../../configs/experiment-warmup.json) uses an earlier architectural checkpoint at the PARSEC launch marker. Restoring switches to the target O3 configuration, runs the launcher and application initialization in detailed mode, captures the excluded interval at `WORKBEGIN`, resets statistics only, and measures the **entire `WORKBEGIN`→`WORKEND` ROI**. O3/cache state is retained; there is no second CPU switch at ROI entry. This is pre-ROI initialization conditioning, not a fixed-length in-ROI warm-up and not a demonstrated convergence to steady-state application behavior.

Completion means that the hooked ROI was captured. Simulation stops at `WORKEND`, so this analysis does not independently establish benchmark output checks that may run afterward.

The core sweep is C1/C4/C8 at W4; the width sweep is W2/W4/W8 at C4. Baselines are **warm C1/W4** and **warm C4/W2**, independently for each workload. C4/W4 is shared, giving 20 actual measurements and 24 normalized sweep rows. Worker count follows core count, with one hardware thread per core. Fetch/decode/rename/dispatch/issue/writeback/commit widths change together; FU counts and major queue capacities remain fixed. No unmeasured C8/W8 result is implied.

Requested frequency is 3 GHz, represented by 333 ticks/cycle at 1 THz (approximately 3.003003 GHz); voltage is 1 V. The 45 nm McPAT model uses ROB 192, IQ 64 split into two 32-entry proxies, fixed FU counts, 256 integer/FP registers, LQ/SQ 32, private 32 KiB L1I/D per core, two shared 1 MiB L2 banks and dual-channel DDR4. Added cores also add private cache and network endpoints; this is not an iso-area comparison.

The same [power](../../configs/mcpat/model-v1.json) and [thermal](../../configs/hotspot/model-v1.json) model files are used in both scenarios. Paired resolved O3/cache/memory configurations, area, reference leakage, clocks, floorplans and thermal configs match; see [hardware checks](../warm_up_comparison/data/hardware_checks.csv). Changing pre-ROI execution can still change guest scheduling/history at ROI entry. These are matched-policy measurements, not identical architectural states with only cache contents altered.

## 2. Metrics

| Metric | Definition |
| --- | --- |
| ROI time | Exact measured tick difference divided by tick frequency; excludes boot and initialization |
| Speedup | Own scenario's smallest sweep configuration time divided by target time |
| Core scaling efficiency | Speedup divided by core-count ratio |
| Width scaling fraction | Speedup divided by width ratio; a proportional reference, not a strict upper bound |
| Average chip power | McPAT Runtime Dynamic + Subthreshold Leakage + Gate Leakage |
| ROI energy | Average chip power × measured ROI time |
| Normalized energy / EDP | Power ratio / speedup; power ratio / speedup² |
| Temperature | Maximum named silicon-block equilibrium temperature under sustained ROI-average power |
| Wall throughput | Committed μops / (cores × elapsed cycles) |
| Recorded active fraction | Sum(numCycles − idleCycles) / (cores × elapsed cycles) |
| Active-cycle throughput | Committed μops / sum(numCycles − idleCycles) |

Whole modeled chip power includes caches, equivalent NoC and on-chip controllers/PHY; it excludes DRAM chips, board power and peripherals. Leakage stays fixed at 350 K, with no gating, DVFS or electrothermal feedback. Temperature changes use °C differences, not Celsius percentages. Each case has one run; plots have no statistical error bars. Retired instructions are not a direct measure of useful work, and queue-event counters are not additive stall-cycle fractions.

## 3. Thread-level parallelism

{endpoints['tlp']}

Here the subscript 8 means eight cores and the baseline is warm C1/W4. Blackscholes reaches **7.014×** speedup at eight cores (87.7% efficiency), while canneal reaches **5.719×** (71.5%). Blackscholes C4→C8 adds 1.803× speed; canneal adds 1.558×. Freqmine reaches **3.165×**, with only 39.6% eight-core efficiency. Streamcluster reaches **2.558× at C4**, then drops to **2.175× at C8**; eight cores take about 17.6% longer than four in this sweep.

{report_figure('01_tlp_speedup','Warm figure 1. Core-sweep speedup and scaling efficiency.')}

{report_figure('03_tlp_power_temperature','Warm figure 3. Chip power and equilibrium temperature across core counts.')}

At eight cores, chip power is 7.329×/7.352×/6.212×/6.056× the one-core power for blackscholes/canneal/freqmine/streamcluster. ROI energy is **1.045×/1.286×/1.963×/2.784×** the one-core energy. Four cores save energy for blackscholes and canneal; eight cores exceed one-core energy for all four workloads. Strong speedup can improve EDP despite higher energy, but streamcluster C8 has worse EDP than its one-core baseline.

Peak modeled equilibrium temperature at C8 is **91.62 °C**, **76.83 °C**, **78.66 °C** and **87.47 °C**, respectively. Added hardware raises total leakage and changes die size/placement within the core sweep. The speed gains do not remove those supply/cooling costs.

## 4. Instruction-level parallelism

{endpoints['ilp']}

Here 8 denotes width eight at four cores, with warm C4/W2 as baseline. Blackscholes reaches **2.193× at W4**, then falls to **1.227× at W8**. The W2→W4 wall-time gain exceeds the nominal 2× width ratio; that ratio is not an architectural bound, and elapsed-time/scheduling differences contribute. It is not evidence that all underlying hardware structures scaled superlinearly.

Canneal changes little: **1.072× at W4, 1.097× at W8**. Freqmine improves moderately to **1.265× and 1.440×**. Streamcluster improves to **1.554× and 1.797×**; W8 is about 15.6% faster than W4 in its warm width sweep.

{report_figure('02_ilp_speedup','Warm figure 2. Width-sweep speedup; proportional width scaling is a reference, not a hard ceiling.')}

{report_figure('04_ilp_power_temperature','Warm figure 4. Width-sweep power and equilibrium temperature.')}

Blackscholes W4→W8 takes about **78.7% longer**, while average power falls by about **19.9%** and peak temperature falls from **71.46 to 65.89 °C**. This is not an energy win: ROI energy rises from **1.179 to 1.689 J**, about **43.2%**. The active/quiescent counter evidence is discussed below and in the comparison. It would be misleading to call this a physically cheaper eight-wide core; area and reference leakage still increase with width.

For streamcluster W4→W8, power rises from **157.17 to 178.56 W** and peak equilibrium temperature rises **3.87 °C**, while ROI energy decreases slightly, **14.486 to 14.231 J**. The observed width benefit includes a change in executed work and active fraction, so it cannot be interpreted as isolated instruction-level hardware throughput improvement.

## 5. Power breakdown and linearity

At warm C4/W4:

{comp_table}

Core logic is each detailed McPAT core minus its L1I/D children. Shared L2, whole equivalent NoC and aggregate memory controllers are counted once. This avoids adding parents and children together. Detailed sums conserve processor power within rounding; the largest warm relative discrepancy is **{wa['max_component_power_relative_rounding_difference']:.2e}**.

{report_figure('07_tlp_component_power','Warm figure 7. Disjoint component power in the core sweep.')}

{report_figure('08_ilp_component_power','Warm figure 8. Disjoint component power in the width sweep.')}

{report_figure('09_dynamic_static_power','Warm figure 9. Runtime dynamic, subthreshold and gate leakage contributions.')}

{report_figure('10_component_dynamic_static','Warm figure 10. Component-level dynamic versus static watts at C4/W4.')}

Reference static power is unchanged by warm-up: approximately **24.74 W at C1/W4**, **76.20 W at C4/W4** and **188.81 W at C8/W4**. The equivalent NoC alone contributes about **5.01, 20.74 and 85.70 W** static. Fully connected SimplePt2Pt endpoints grow with core count, and their physical router/buffer representation is uncalibrated. Large network power slices mostly reflect leakage in this model; they are not measurements of dynamic communication cost alone.

Chip power is below an 8× proportional increase over C1→C8 in all four workloads, but exceeds 2× over C4→C8 for blackscholes, canneal and freqmine. Incremental W/core increases from **37.45→44.80**, **21.27→31.00**, **24.03→31.57** and **35.35→38.13** across the two core intervals. Therefore a single through-origin linear rule does not describe the whole sweep. An affine rule includes fixed overhead; its slope need not be inferred from a total ratio alone.

Width-power growth is small for canneal/freqmine and larger for streamcluster, with a negative W4→W8 slope in the blackscholes anomaly. Runtime average power combines hardware and activity per second. It is not a structural cost curve independent of execution. Three settings and one observation each do not support reliable power-law fitting or extrapolation.

{report_figure('05_tlp_normalized_power','Warm figure 5. Normalized core power and leakage fraction.')}

{report_figure('06_ilp_normalized_power','Warm figure 6. Normalized width power and leakage fraction.')}

{report_figure('17_power_elasticity','Warm figure 17. Interval elasticity distinguishes proportional growth from observed activity-dependent changes.')}

{report_figure('18_area_and_static_cost','Warm figure 18. Modeled hardware area and static cost; identical to corresponding cold hardware.')}

The cache-area and network approximations remain material: each modeled 32 KiB L1D is about 47.44 mm² versus 3.58 mm² for L1I, and the eight-core die exceeds 1000 mm² in this analytical mapping. These numbers require calibration before physical implementation claims. All 20 warm McPAT runs retain two L2 latency warnings, one per bank; successful conversion does not establish timing closure.

## 6. Executed work and quiescent time

{report_figure('15_tlp_work_diagnostics','Warm figure 15. Executed work, wall throughput and memory/network activity across cores.')}

{report_figure('16_ilp_work_diagnostics','Warm figure 16. Equivalent diagnostics across pipeline widths.')}

{report_figure('22_per_core_work_and_temperature','Warm figure 22. Eight-core retired-work shares and core-block temperatures.')}

{report_figure('23_mix_and_iq_pressure','Warm figure 23. Issued OpClasses and normalized IQ-full events.')}

The blackscholes C4/W8 pair is especially informative. Cold/warm committed work differs by only **{100*(black.uop_ratio-1):.2f}%**. Wall throughput falls from **{black.uipc_per_core_wall_cold:.3f} to {black.uipc_per_core_wall_warm:.3f} μops/core/cycle**, while active-cycle throughput stays approximately **{black.uipc_per_active_cycle_cold:.3f} versus {black.uipc_per_active_cycle_warm:.3f}**. Recorded active fraction falls from **{100*black.mean_active_tick_fraction_cold:.1f}% to {100*black.mean_active_tick_fraction_warm:.1f}%**. The quiesce counter normalized to elapsed core-cycles rises from **{100*black.quiesce_cycles_per_elapsed_core_cycle_cold:.1f}% to {100*black.quiesce_cycles_per_elapsed_core_cycle_warm:.1f}%**.

gem5 defines `quiesceCycles` for quiesced/wait-for-interrupt time and `idleCycles` for unscheduled idle time; they are not synonymous. These statistics have boundary bookkeeping and should not be summed into an exact 100% partition. They strongly associate this regression with inactive/quiescent time rather than a large deterioration of throughput while active. They do not identify a particular guest scheduler decision, timer, lock or system call. The [comparison](../warm_up_comparison/warm_up_vs_non_warm_up_AI_analysis.md) provides direct paired plots and counter definitions.

Canneal C8 shows a similar active-fraction change with almost unchanged active-cycle throughput. Streamcluster changes its retired work substantially between some cases, consistent with path/synchronization variability. More instructions need not mean more useful application work. No source/PC, runnable-thread or synchronization profile was collected, so exact causes remain hypotheses.

## 7. Energy, EDP and configuration choices

{best_table}

These choices are within each sweep and the recorded single observations, with no external area/power/temperature budget. EDP weights time and energy through `E×t`; another objective can choose differently. Canneal's W4/W8 energy difference is small and should not be treated as statistically settled.

{report_figure('11_tlp_energy_edp','Warm figure 11. Normalized core-sweep ROI energy and EDP.')}

{report_figure('12_ilp_energy_edp','Warm figure 12. Normalized width-sweep ROI energy and EDP.')}

{report_figure('13_tlp_tradeoff','Warm figure 13. Core-sweep tradeoff; below the diagonal saves ROI energy.')}

{report_figure('14_ilp_tradeoff','Warm figure 14. Width-sweep tradeoff; all normalization uses warm baselines.')}

Across all five measured settings, blackscholes is fastest/lowest in EDP at C8/W4 and lowest in energy at C4/W4. Canneal is fastest/lowest in EDP at C8/W4 and lowest in energy at C4/W4. Freqmine is fastest at C8/W4, lowest in energy at C1/W4 and lowest in EDP at C4/W8. Streamcluster is fastest/lowest in EDP at C4/W8 and lowest in energy at C1/W4. No unmeasured joint core/width design is declared optimal.

## 8. Thermal meaning

{report_figure('19_temperature_power_density','Warm figure 19. Equilibrium temperature against total power and average die density.')}

{report_figure('20_tlp_thermal_floorplans','Warm figure 20. Core-sweep surrogate thermal maps under common cooling and color/length scales.')}

{report_figure('21_ilp_thermal_floorplans','Warm figure 21. Width-sweep surrogate thermal maps; blackscholes W8 is cooler because of its lower average ROI power.')}

The maximum is **91.62 °C at blackscholes C8/W4**. Core-sweep temperatures increase with cores, but the blackscholes width sweep is nonmonotonic. More width can produce a cooler yet slower, more energy-consuming run when activity per second falls and fixed leakage persists.

HotSpot uses 45 °C ambient, an area-derived square die with no whitespace, coarse uniform-power blocks, a 50 mm spreader/70 mm sink and fixed cooling. These are equilibrium predictions for average ROI power applied indefinitely. Detailed CPU/cache warm-up does not mean the silicon was thermally preheated in this model: pre-ROI temperature history is not an input. Temperature/leakage feedback and fine within-block hotspots remain unmodeled.

## 9. Warm-up coverage and reliability

Excluded initialization spans **{w.warmup_seconds.min():.3f}–{w.warmup_seconds.max():.3f} simulated seconds**, or **{w.warmup_to_roi_time_ratio.min():.2f}–{w.warmup_to_roi_time_ratio.max():.2f}×** each measured ROI duration. Its contents include the guest launcher and application setup, not solely useful application operations. Nonzero pre-ROI cache activity and committed instructions prove detailed execution occurred, not that ROI working sets/predictors converged. A longer warm-up is not automatically a better representative warm-up.

{init_table}

The [validation record](data/validation.json) covers 20 cases, 24 sweep rows and **300 hashed source artifacts**, including separate warm-up metadata/snapshots. The [mapping policies](../../configs/README.md) and [source manifests](data/input_sha256.json) allow auditing. Three warm cases have documented cycle bookkeeping notes; exact tick duration is always used. Zero-valued quiesce/idle-event counters may be absent because the pinned gem5 source declares `.prereq(stat)`; only these explicitly zero-suppressed counters are mapped to zero, not arbitrary missing activity.

Warm-up interpretation must account for scheduling/history changes, single observations, incomplete physical activity mappings, L2 timing warnings, fixed leakage and surrogate geometry/cooling. Preserve this full-ROI definition when repeating experiments. Priority repeats are blackscholes widths 4/8, canneal C8, and streamcluster widths 2/8, with both scenarios and effective thread-placement/idle/synchronization evidence. If identical restores reproduce exactly, copies of that deterministic result are not independent random trials; document controlled sensitivity conditions before assigning variability or confidence intervals. No new simulation was launched for this analysis.

## 10. Reproduction and files

From the repository root, with dependencies installed and both raw datasets plus matching binaries available:

```bash
.venv-analysis/bin/python scripts/analyze_warm_up.py
```

This writes `reports/warm_up/` and `reports/warm_up_comparison/`, preserving the original non-warm-up report/assets and all raw results. `--extract-only` validates/extracts data without figures. `--reuse-extracted` rechecks source hashes and the shared extractor identity before reusing tables; matched calculations and report generation still run again. The [comparison validation](../warm_up_comparison/data/validation.json) records the analysis/extractor/model hashes and upstream commits. The interactive [warm tradeoff plot](interactive_tradeoffs.html) opens offline.

Warm tables use the same schema as the [reference data guide](../README.md), with added pre-ROI fields, recorded active-cycle throughput and quiesce diagnostics. The complete case values follow; source values retain more digits than these rounded displays.

{raw_table}

### Figure downloads

| Figure | Raster | Vector | Document |
| --- | --- | --- | --- |
{gallery(WARM)}
"""
    (WARM/"warm_up_AI_analysis.md").write_text(warm_report)

    paired_table=report_table(m,{"case":"Case","roi_ms_cold":"Cold ms","roi_ms_warm":"Warm ms","time_change_pct":"Time Δ%",
        "power_change_pct":"Power Δ%","energy_change_pct":"Energy Δ%","temperature_delta_c":"Temperature Δ°C"})
    scaling=[]
    for sw in ["tlp","ilp"]:
        g=q[q.sweep.eq(sw)]
        for b in BENCHMARKS:
            row={"sweep":sw.upper(),"benchmark":b}
            for value in [4,8]:
                r=g[g.benchmark.eq(b)&g[SWEEP_SPECS[sw][0]].eq(value)].iloc[0]
                for scenario in ["cold","warm"]:row[f"s{value}_{scenario}"]=r["speedup_"+scenario]
            scaling.append(row)
    scaling_table=report_table(pd.DataFrame(scaling),{"sweep":"Sweep","benchmark":"Workload","s4_cold":"Cold S₄","s4_warm":"Warm S₄","s8_cold":"Cold S₈","s8_warm":"Warm S₈"})
    active_cases=m[m["case"].isin(["blackscholes-c4-w8","canneal-c8-w4"])].copy()
    for field in ["mean_active_tick_fraction","quiesce_cycles_per_elapsed_core_cycle"]:
        for suffix in ["cold","warm"]:active_cases[field+"_"+suffix]*=100
    active_table=report_table(active_cases,{"case":"Case","mean_active_tick_fraction_cold":"Cold active %",
        "mean_active_tick_fraction_warm":"Warm active %","uipc_per_active_cycle_cold":"Cold active μIPC",
        "uipc_per_active_cycle_warm":"Warm active μIPC","quiesce_cycles_per_elapsed_core_cycle_cold":"Cold quiesce/elapsed %",
        "quiesce_cycles_per_elapsed_core_cycle_warm":"Warm quiesce/elapsed %"})
    gm={f:geo_mean(m[f]) for f in ["time_ratio","paired_speedup","power_ratio","energy_ratio","edp_ratio"]}
    within=int((m.time_change_pct.abs()<1).sum())
    black_static_extra=black.static_energy_j_warm-black.static_energy_j_cold
    black_total_extra=black.energy_j_warm-black.energy_j_cold
    scope_table=report_table(choices,{"benchmark":"Workload","scenario":"Scenario","fastest":"Fastest","minimum_energy":"Minimum ROI energy","minimum_edp":"Minimum EDP"})
    cp_case=cp[cp["case"].eq("blackscholes-c4-w8")]
    black_power_table=report_table(cp_case,{"component":"Component","runtime_dynamic_w_cold":"Cold dynamic W",
        "runtime_dynamic_w_warm":"Warm dynamic W","static_w_cold":"Fixed static W","total_average_w_delta":"Total ΔW"})
    small=m[m["case"].ne("blackscholes-c4-w8")]
    comp_report=f"""# Warm-up versus non-warm-up AI analysis

This report compares **20 matched full-ROI cases per scenario**: PARSEC 3.0 `simsmall` blackscholes, canneal, freqmine and streamcluster, using five hardware configurations. Both gem5→McPAT→HotSpot datasets passed validation. The original [cold report](../non_warm_up/non_warm_up_AI_analysis.md) is preserved; the [warm report](../warm_up/warm_up_AI_analysis.md) analyzes the new scenario independently.

**Main finding:** pre-ROI O3 initialization does not produce a uniform improvement. **{int((m.time_ratio<1).sum())}/20** warm runs are faster, but **{within}/20** time changes are within ±1%, and there is only one observation per pair. Blackscholes C4/W8 is **{black.time_ratio:.3f}× as slow**, with **{black.energy_change_pct:.1f}% more energy**, despite lower modeled watts and temperature. Streamcluster C4/W8 is **{-stream.time_change_pct:.1f}% faster** and changes the preferred measured width from four to eight. Core-scaling conclusions mostly persist, with a material canneal C8 regression. The counter evidence points to substantial changes in inactive/quiescent time and, for streamcluster, executed work; cache-miss reductions alone do not explain the outcomes.

Open the [interactive comparison](interactive_comparison.html) in a browser for all paired effects. Positive time/energy percentage changes mean higher cost; negative changes mean reductions. Temperature uses absolute °C differences.

## 1. What is comparable, and what is not controlled

| Property | Non-warm-up | Pre-ROI warm-up |
| --- | --- | --- |
| Fast-forward/checkpoint boundary | `WORKBEGIN`, before O3 switch | Earlier PARSEC launch marker, before O3 initialization |
| Detailed CPU initialization | Begins at ROI entry | Runs through launcher/application initialization before ROI |
| Measured work | Full `WORKBEGIN`→`WORKEND` | Same full `WORKBEGIN`→`WORKEND` |
| Statistics reset | After O3 switch at entry | At entry with warmed O3/cache state retained |
| Hardware/workloads/physical models | Frozen v1 | Matching fields checked against v1 |
| Observations | One per case | One per case |

The [resolved hardware checks](data/hardware_checks.csv) show identical O3 cores, cache hierarchies, memory configuration, clocks, area, reference static power, floorplan files and thermal configs for all 20 pairs. Both use the same upstream revisions and model files. This makes matched physical-cost calculations consistent, but does not establish physical model accuracy.

Pre-ROI execution history is different. Guest scheduling, interrupt/wakeup phase, thread state and application execution paths may also differ at ROI entry. The warm-up is workload-dependent initialization, not a fixed instruction budget or demonstrated converged ROI state. This experiment compares **two execution methodologies**; it does not isolate cache contents as the only independent variable. A single paired observation cannot separate repeat variability from a repeatable methodological effect.

## 2. Paired metrics and complete results

For each identical workload/configuration, define `Rₜ=t_warm/t_cold`, `Rₚ=P_warm/P_cold`, `Rₑ=E_warm/E_cold=RₜRₚ` and `R_EDP=RₚRₜ²`. Paired speedup is `1/Rₜ`. Report percentage changes as `100(R−1)`. These paired ratios differ from the within-sweep speedups discussed next. Both scenarios exclude pre-ROI time/energy from ROI metrics; an end-to-end application energy comparison was not measured.

{paired_table}

{report_figure('01_paired_roi_times','Comparison figure 1. Full-ROI times for the same workload/hardware; per-workload axes differ.')}

{report_figure('02_paired_metric_changes','Comparison figure 2. Matched time, average power, energy and temperature effects.')}

Across all 20 equally weighted pairs, geometric-mean ratios are **{gm['time_ratio']:.3f}× time**, **{gm['power_ratio']:.3f}× power**, **{gm['energy_ratio']:.3f}× energy**, and **{gm['edp_ratio']:.3f}× EDP**. This is approximately +4.2% time, −2.0% watts, +2.1% energy and +6.4% EDP. These descriptive means do not represent an actual job mixture or uncertainty interval. They are sensitive to the blackscholes W8 observation: the other 19 cases' time-ratio geometric mean is **{geo_mean(small.time_ratio):.3f}×**. That sensitivity is a reason to show all cases, not to discard the outlier.

## 3. Do the scaling conclusions change?

{scaling_table}

Each scenario is normalized to **its own C1/W4** for core scaling and **its own C4/W2** for width scaling. A sweep speedup cannot be compared as if its denominator were unchanged. The same C4/W4 measurement appears in both sweeps; there are 24 paired sweep rows, not 24 independent cases.

{report_figure('03_tlp_speedup_comparison','Comparison figure 3. Core-count speedups with separate cold/warm one-core baselines.')}

**TLP:** blackscholes remains strong, reaching **7.014× warm versus 6.928× cold** at C8. Canneal falls from **6.708× to 5.719×**, although four-core scaling improves slightly. Freqmine stays around **3.12–3.16×** at C8. Streamcluster improves C8 speedup from **1.936× to 2.175×**, but C4 remains faster within its W4 core sweep. Warm-up therefore does not remove its multicore scaling limitation.

{report_figure('04_ilp_speedup_comparison','Comparison figure 4. Width speedups with separate cold/warm two-wide baselines.')}

**ILP:** blackscholes changes sharply: width-eight speedup falls from **3.212× to 1.227×**; warm W4 is fastest. Canneal remains near **1.096×** at W8 and freqmine near **1.44–1.46×**. Streamcluster W8 speedup improves from **1.173× to 1.797×**. Its warm W8 time improves 19.5%, while its warm W2 baseline is 23.3% slower; both endpoints contribute to the much larger normalized speedup. For blackscholes, W2 changes by less than 1%, so the width-eight speedup collapse primarily reflects the target case's regression.

{report_figure('15_change_in_scaling_conclusions','Comparison figure 15. Ratio of warm/cold normalized speedups; both target and baseline contribute.')}

The observed blackscholes W2→W4 warm speedup exceeds the nominal width ratio. This is a wall-time outcome that includes activity/scheduling differences, not proof of a universal throughput law. Width changes all seven pipeline widths while fixed queue/FU resources constrain available execution capacity.

## 4. Blackscholes C4/W8: slower, cooler, and more energy-consuming

Warm full ROI time is **{black.roi_ms_warm:.3f} ms versus {black.roi_ms_cold:.3f} ms**. Architectural inputs, resolved core configuration and modeled area/leakage match. Committed μops differ by only **{100*(black.uop_ratio-1):.2f}%**. L2 demand misses fall by about **{100*(1-black.l2_miss_ratio):.1f}%**, so a claim that additional L2 misses caused the regression is inconsistent with these counters.

{active_table}

Recorded active-cycle throughput is nearly unchanged, while wall throughput falls approximately **61.6%**. Active fraction falls from about **94.9% to 36.5%**. Quiesce cycles normalized to elapsed core-cycles rise from about **3.9% to 64.2%**. The [pinned gem5 CPU source](https://github.com/gem5/gem5/blob/{audit['tool_commits']['gem5']}/src/cpu/o3/cpu.cc) defines quiesce cycles for waiting/quiesced intervals, separately from idle cycles used when the CPU unschedules for idling.

{report_figure('13_blackscholes_active_throughput','Comparison figure 13. Per-core active fraction and active-cycle throughput for blackscholes widths 4/8.')}

This supports an association with inactive/quiescent periods rather than a large loss of throughput during active cycles. A guest scheduler/wakeup/synchronization explanation is plausible, but the exact cause is not established: no PC/source trace, runnable-thread profile or lock timing was collected. Counter bookkeeping at boundaries also means active/idle/quiesce ratios must not be added into an exact 100% partition.

Average power falls from **{black.total_power_w_cold:.2f} to {black.total_power_w_warm:.2f} W**, while energy rises from **{black.energy_j_cold:.3f} to {black.energy_j_warm:.3f} J**. Lower activity per second reduces runtime dynamic watts; fixed leakage keeps accumulating over the longer ROI. Of the **{black_total_extra:.3f} J** energy increase, about **{black_static_extra:.3f} J ({100*black_static_extra/black_total_extra:.1f}%)** comes from additional fixed-reference static energy. This accounting is conditional on no power gating and fixed leakage temperature.

{black_power_table}

Peak modeled equilibrium temperature decreases by **{-black.temperature_delta_c:.2f} °C**, despite increased execution energy. Cooling responds to sustained watts/distribution in this steady-state calculation, whereas energy integrates watts over the finite ROI. Cooler does not imply more efficient completion.

## 5. Other paired effects and diagnostic limits

**Canneal C8/W4:** full ROI time increases **{can.time_change_pct:.1f}%**, while committed work grows only **{100*(can.uop_ratio-1):.2f}%**. Active-cycle throughput remains approximately 0.362 μops/cycle; active fraction decreases from about 96.0% to 82.0%, with much more recorded quiescent time. Average power falls slightly, but energy increases **{can.energy_change_pct:.1f}%**. The TLP result remains useful but less efficient than the cold observation. This is another priority for repeat measurements and scheduler/wakeup evidence.

**Streamcluster C4/W8:** time decreases **{-stream.time_change_pct:.1f}%**, energy decreases **{-stream.energy_change_pct:.1f}%**, and committed μops decrease **{100*(1-stream.uop_ratio):.1f}%**. Wall throughput improves about 5.7%; active-cycle throughput is nearly unchanged. The improvement therefore includes less executed work and a higher active fraction. It is not a clean observation of a larger amount of useful work per instruction window.

**Streamcluster C4/W2:** time increases 23.3% and committed work increases about 15.2%; C4/W4 also becomes about 5.1% slower. These changes explain why wider-pipeline normalization changes substantially. C8/W4 becomes about 11.0% faster with about 9.9% fewer committed μops, but still loses to C4/W4 within the core sweep.

**Freqmine and one-core cases:** many changes are small. All one-core ROI changes are below 0.5% in magnitude, and most freqmine changes are near 1% or less except C8's roughly 1.5% improvement. Small single-observation deltas should not be advertised as statistically established gains.

{report_figure('08_work_activity_changes','Comparison figure 8. Work, throughput, active fraction, cache-miss and network-activity changes.')}

For identical `N` and clock, the exact accounting identity is `t = U/(N f a IPC_active)`, where `U` is retired μops and `a` the recorded active fraction. Thus `Rₜ = R_U/(R_a R_IPC_active)`. This decomposes the measured totals; it is not a causal model of useful work. More retired instructions may reflect synchronization, runtime/OS work or execution-path variation.

{report_figure('14_time_ratio_accounting','Comparison figure 14. Log-ratio accounting of work, active fraction and active-cycle throughput; dots are the observed time ratio.')}

L2 demand misses are not DRAM requests, and network hop flits are not stall cycles or injected packet counts. Changes in those counters support follow-up hypotheses but do not prove bandwidth saturation, false sharing, lock contention or a specific cache-warming mechanism.

## 6. Power, energy and scaling cost

{report_figure('05_tlp_power_temperature','Comparison figure 5. Paired core-sweep power and equilibrium temperatures.')}

{report_figure('06_ilp_power_temperature','Comparison figure 6. Paired width-sweep power and equilibrium temperatures.')}

Reference static power and component area are equal in every pair. Changes in average chip power therefore come from measured runtime dynamic power, within output precision. Core logic and L1 dominate dynamic changes; equivalent NoC and L2 retain large fixed-reference leakage contributions. The unchanged models use about 76.20 W static at C4/W4 and 188.81 W at C8/W4, including an 85.70 W static equivalent NoC at eight cores.

{report_figure('09_component_power_changes','Comparison figure 9. Signed per-component changes in average power, without parent/child double counting.')}

The C4→C8 warm intervals for blackscholes/canneal/freqmine still show more than 2× power growth, while C1→C8 ratios remain below 8. Distinguish proportional `P=kN` from affine `P=a+bN`; neither is established as a universal law by three levels. The blackscholes W4→W8 warm interval has decreasing average power because of lower activity per second, despite greater structural width and static cost. It is not a physical area/power optimization result independent of workload timing.

{report_figure('10_dynamic_power_time_energy','Comparison figure 10. Paired dynamic power and energy versus time; lower watts can accompany worse energy.')}

{report_figure('07_energy_edp_comparison','Comparison figure 7. Each scenario’s normalized energy and EDP curves.')}

At eight cores, all four workloads exceed their own one-core ROI energy in both scenarios. Warm-up therefore does not make maximum core count the energy-minimal choice. Canneal's warm W4/W8 energy difference remains small, despite consistent lowest observed energy at W4. EDP can favor a different design because it weights time again.

## 7. Temperature is not a thermal warm-up experiment

Paired floorplans and thermal configs are byte-identical. The HotSpot maximum is computed from named silicon blocks, under 45 °C ambient, fixed package/cooling and indefinitely sustained ROI-average block power. Pre-ROI thermal history is not supplied, so differences are driven by **ROI power distribution under the same paired geometry**, not by simulated heat accumulated during initialization.

{report_figure('12_paired_thermal_maps','Comparison figure 12. Identical geometry with cold/warm temperatures and their differences; each delta row has its own labeled scale.')}

The largest cooling change is blackscholes C4/W8 at −13.13 °C; streamcluster C4/W8 rises 1.96 °C while becoming faster and using less ROI energy. The warm maximum across the study is blackscholes C8/W4 at 91.62 °C versus 91.19 °C cold. Maximum block temperature depends on distribution as well as total watts; very small changes should also be read against 0.01 K output precision.

These are conditional equilibrium estimates, not peak temperatures reached during millisecond workloads or a real-chip safety-limit assessment. Fixed 350 K leakage, no thermal feedback/throttling, coarse blocks and uncalibrated cooling/layout remain limitations.

## 8. Which configuration is preferred now?

{scope_table}

These are the minima among **five observed designs**, not the unmeasured core×width grid. Blackscholes's energy/EDP preference changes away from C4/W8 because of its warm regression. Streamcluster's fastest/lowest-EDP setting changes from C4/W4 to C4/W8. Canneal/freqmine choices mostly persist. Any small difference needs repetitions before a stable design recommendation; objectives with a strict area/temperature budget may choose differently.

## 9. Initialization coverage and confidence

{report_figure('11_initialization_coverage','Comparison figure 11. Excluded initialization time and its ratio to measured ROI time; these are simulated durations.')}

Initialization spans roughly 0.332–1.411 simulated seconds and about 0.56–101.91× ROI time. It includes guest setup, data loading and runtime work, varies by target width/workload and is not a calibrated warm-up budget. No excluded-region power estimate was generated, so no end-to-end initialization energy benefit/cost is claimed. Host simulation duration is also not the benchmark performance metric.

All 40 cases passed completed-ROI and converter/thermal provenance checks. Comparison additionally verifies resolved hardware, constant area/leakage/clock, identical thermal geometry/settings, and energy/throughput accounting identities. [Independent checks](data/independent_checks.json) recalculate all 40 raw tick/power measurements, 20 paired effects and 48 scenario-specific sweep rows, and reproduce the original published cold numeric metrics. The combined source manifests hash **560 input artifacts**. See [validation](data/validation.json), [warm input hashes](../warm_up/data/input_sha256.json) and [re-extracted reference hashes](reference/data/input_sha256.json). The original cold report/data remain unchanged.

Both scenarios have 40 total L2 timing warnings each, approximate SIMD/register/cache/directory/network/controller mappings, fixed leakage and no external DRAM/board energy. Three warm and two cold cases have documented cycle bookkeeping notes; exact ROI ticks remain authoritative. Suppressed-zero quiesce counters are handled using the pinned gem5 registration rule, not by silently dropping unknown required counters.

The strongest defensible conclusions are the observed full-program scaling changes and the associated work/active/quiesce behavior under this model. They are not proof that warm-up itself causes a universal gain or slowdown. To strengthen the study, repeat both scenarios for anomalous blackscholes widths, canneal C8 and streamcluster width extremes; verify effective thread placement and collect scheduling/wakeup/synchronization/PC evidence. If identical restores reproduce exactly, repeated copies demonstrate reproducibility rather than independent random-trial variability. Check documented sensitivity to pre-ROI conditioning and execution conditions before assigning confidence intervals or using “steady-state” microarchitecture language. ROI completion also does not independently verify guest output checking after `WORKEND`, where these simulations stop. No new simulations were launched during this analysis.

## 10. Reproduce and inspect

With the raw datasets and matching model/tool binaries available, run from the repository root:

```bash
.venv-analysis/bin/python scripts/analyze_warm_up.py
```

Dependencies are in [requirements-analysis.txt](../../requirements-analysis.txt). This produces the warm report, 23 warm figure sets, 15 comparison figure sets, CSVs and two bundled offline interactive charts. It writes only the two new report directories. `--extract-only` omits figures/reports; `--reuse-extracted` checks recorded source hashes and extractor identity before reusing parsed tables. The current interpretation template describes this v1 dataset and guards its principal anomaly signs; review the narrative if measurements are replaced.

| File | Contents |
| --- | --- |
| [matched_cases.csv](data/matched_cases.csv) | 20 cold/warm pairs with absolute metrics, matched ratios and diagnostic deltas |
| [matched_sweeps.csv](data/matched_sweeps.csv) | 24 matched sweep rows using each scenario’s own baseline |
| [component_changes.csv](data/component_changes.csv) | Component dynamic/static/total power changes |
| [configuration_choices.csv](data/configuration_choices.csv) | Lowest observed time, energy and EDP per workload/scenario |
| [hardware_checks.csv](data/hardware_checks.csv) | Resolved hardware and thermal-input equality per pair |
| [validation.json](data/validation.json) | Policy, tool, model, extractor and analysis-source identities |
| [independent_checks.json](data/independent_checks.json) | Independent raw tick/power arithmetic, sweep-baseline checks and reference reproduction |
| [Warm tables](../warm_up/data/summary.csv) | Full warm ROI metrics, excluded initialization and counter diagnostics |
| [Reference tables](reference/data/summary.csv) | Independently re-extracted cold metrics; original publication is preserved |

### Figure downloads

| Figure | Raster | Vector | Document |
| --- | --- | --- | --- |
{gallery(COMPARE)}
"""
    (COMPARE/"warm_up_vs_non_warm_up_AI_analysis.md").write_text(comp_report)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reuse-extracted",action="store_true",help="Recheck hashes and reuse validated extraction tables")
    parser.add_argument("--extract-only",action="store_true",help="Validate/extract metrics and comparisons without plotting")
    args=parser.parse_args()
    for path in [WARM,COMPARE]:path.mkdir(parents=True,exist_ok=True)
    if args.reuse_extracted:
        cold=restore_tables(REFERENCE,ROOT/"configs/experiment-startup.json")
        warm=restore_tables(WARM,WARM_POLICY)
    else:
        cold=load_data(ROOT/"results/v1",REFERENCE)
        warm=load_data(ROOT/"results/warmup-v1",WARM,WARM_POLICY)
    m,q,cp,choices=paired_data(cold,warm)
    independent_checks(cold,warm,m)
    audit={"status":"validated","matched_cases":20,"matched_sweep_points":24,"replicates_per_scenario_case":1,
           "reference_policy_sha256":cold[-1]["policy_sha256"],"warm_policy_sha256":warm[-1]["policy_sha256"],
           "tool_commits":warm[-1]["tool_commits"],"analysis_entrypoint_sha256":sha256(Path(__file__)),
           "shared_extractor_sha256":sha256(ROOT/"scripts/analyze_non_warm_up.py"),
           "matched_static_area_clock_and_geometry":True,"simulation_sources_hashed":cold[-1]["source_files_hashed"]+warm[-1]["source_files_hashed"],
           "models_sha256":{n:sha256(ROOT/"configs"/n/"model-v1.json") for n in ["mcpat","hotspot"]}}
    audit["software"]={name:warm[-1][name] for name in ["python","numpy","pandas","matplotlib"]}
    audit["software"]["plotly"]=__import__("plotly").__version__
    for output,dataset in [(WARM,warm),(REFERENCE,cold)]:
        dataset[-1]["analysis_entrypoint_sha256"]=audit["analysis_entrypoint_sha256"]
        dataset[-1]["plotly"]=audit["software"]["plotly"]
        (output/"data/validation.json").write_text(json.dumps(dataset[-1],indent=2)+"\n")
    (COMPARE/"data/validation.json").write_text(json.dumps(audit,indent=2)+"\n")
    if args.extract_only:
        print('Validated and matched 40 measurements; extraction complete.');return
    make_figures(WARM,*warm[:5],warm[5])
    interactive_plot(WARM,warm[1],scenario="Pre-ROI warm-up")
    comparison_figures(cold,warm,m,q,cp)
    interactive_comparison(m)
    write_reports(cold,warm,m,q,cp,choices,audit)
    print('Generated warm-up and matched-comparison figures, tables and reports.')


if __name__=="__main__":
    main()
