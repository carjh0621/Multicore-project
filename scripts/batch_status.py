#!/usr/bin/env python3
"""Read-only experiment matrix: checkpoint, measured ROI, McPAT and HotSpot status."""

import argparse
import json
from pathlib import Path
import sys

from batch_common import Context, INVALID, ROOT, STAGES, file_required, read_json, require


class StaleResult(ValueError):
    pass


def identity(ctx, data, case, width=True):
    expected = {"benchmark": case.benchmark, "cores": case.cores, "policy_sha256": ctx.policy_sha}
    if width:
        expected["width"] = case.width
    for key, value in expected.items():
        if data.get(key) != value:
            raise StaleResult(f"Stored {key} differs from the current case/policy")


def paths(ctx, stage, case):
    if stage == "prepare":
        folder = ctx.checkpoint_path(case)
        return folder, folder / "project_checkpoint.json"
    folder = ctx.run_path(case)
    if stage == "restore":
        return folder, folder / "roi.json"
    folder /= stage
    return folder, folder / ("power.json" if stage == "mcpat" else "temperature.json")


def quick_check(ctx, stage, case):
    """Check completion metadata; --verify additionally uses full batch validators."""
    folder, marker = paths(ctx, stage, case)
    data = read_json(marker)
    if stage == "prepare":
        identity(ctx, data["identity"], case, width=False)
        require(data["identity"]["boundary"] == "workbegin", "Diagnostic checkpoint")
        if data["identity"]["gem5_commit"] != ctx.commits["gem5"]:
            raise StaleResult("Checkpoint gem5 revision differs")
        file_required(folder / "m5.cpt")
    elif stage == "restore":
        identity(ctx, data, case)
        require(data["status"] == "roi_complete", "ROI has not completed")
        for name in ("config.json", "stats.txt", "roi_stats.json", "run_manifest.json"):
            file_required(folder / name)
        manifest = read_json(folder / "run_manifest.json")
        identity(ctx, manifest, case)
        if manifest["tool_commits"]["gem5"] != ctx.commits["gem5"]:
            raise StaleResult("Measured ROI gem5 revision differs")
    else:
        require(data["status"] == "pilot_approximate", "Converter has no completed result")
        mapping = read_json(folder / "mapping.json")
        identity(ctx, mapping["roi"], case)
        model = ROOT / "configs" / stage / "model-v1.json"
        try:
            ctx.verify_input_hashes(mapping["inputs"], [ctx.policy_path, model])
        except INVALID as exc:
            raise StaleResult(str(exc)) from exc
        for name in (("input.xml", "mcpat.txt") if stage == "mcpat" else
                     ("floorplan.flp", "power.ptrace", "hotspot.config", "steady.temp")):
            file_required(folder / name)


def latest_records(ctx):
    """Last report for each stage/output, including cases from subset invocations."""
    records = {}
    for filename in sorted((ctx.results / "batch_logs").glob("*/*/status.json")):
        try:
            report = read_json(filename)
            stage = report["stage"]
            if stage not in STAGES or report["policy_sha256"] != ctx.policy_sha:
                continue
            for key, row in report["cases"].items():
                if not isinstance(row, dict):
                    continue
                records[(stage, key)] = {**row, "report": str(filename),
                                         "reported_at": row.get("ended_at", filename.stat().st_mtime)}
        except INVALID:
            continue
    return records


def cell_status(ctx, stage, case, records, verify=False):
    folder, marker = paths(ctx, stage, case)
    exists = folder.exists() or (stage == "prepare" and (ctx.results / (case.checkpoint_name+"-prepare")).exists())
    state, reason = ("INCOMPLETE", "Output exists without a completed result") if exists else ("PENDING", "No output yet")
    if marker.exists():
        try:
            quick_check(ctx, stage, case)
            if verify:
                ctx.complete(stage, case)
            state, reason = "DONE", "Completed output verified" if verify else "Completed output metadata present"
        except StaleResult as exc:
            state, reason = "STALE", str(exc)
        except INVALID as exc:
            state, reason = "INCOMPLETE", str(exc)
    key = case.checkpoint_name if stage == "prepare" else case.name
    record = records.get((stage, key), {})
    expected_output = ctx.results / (case.checkpoint_name+"-prepare") if stage == "prepare" else folder
    if record.get("output") and Path(record["output"]).resolve() != expected_output.resolve():
        record = {}
    logged = record.get("state")
    if logged in ("FAILED", "INTERRUPTED") and (state != "DONE" or record["reported_at"] >= marker.stat().st_mtime):
        state, reason = logged, record.get("reason", logged)
    elif logged == "RUNNING" and state in ("PENDING", "INCOMPLETE"):
        state, reason = "RUNNING", "Latest batch report says running"
    elif logged in ("RUN", "NOT_STARTED") and state == "PENDING":
        state, reason = ("QUEUED" if logged == "RUN" else "NOT_STARTED"), record.get("reason", "")
    elif logged in ("DONE", "SKIP") and state == "PENDING":
        if stage == "prepare" and record.get("reason") == "all dependent ROI runs already complete":
            state, reason = "NOT_NEEDED", record["reason"]
        else:
            state, reason = "INCOMPLETE", "Batch completion recorded but output is missing"
    return {"state": state, "reason": reason, "output": str(folder),
            **{k: record[k] for k in ("pid", "log", "report") if k in record}}


def collect(ctx, verify=False):
    records, rows, checkpoints = latest_records(ctx), [], {}
    for case in ctx.cases:
        statuses = {}
        for stage in STAGES:
            if stage == "prepare":
                if case.checkpoint_name not in checkpoints:
                    checkpoints[case.checkpoint_name] = cell_status(ctx, stage, case, records, verify)
                statuses[stage] = checkpoints[case.checkpoint_name]
            else:
                statuses[stage] = cell_status(ctx, stage, case, records, verify)
        rows.append({"case": case.name, "benchmark": case.benchmark, "cores": case.cores,
                     "width": case.width, "stages": statuses})
    summary = {"prepare": {"done": sum(c["state"] == "DONE" for c in checkpoints.values()), "total": len(checkpoints)}}
    for stage in STAGES[1:]:
        summary[stage] = {"done": sum(r["stages"][stage]["state"] == "DONE" for r in rows), "total": len(rows)}
    return {"results_root": str(ctx.results), "verified": verify, "summary": summary, "cases": rows}


def table(report, details=False):
    rows = [["Benchmark", "Cores", "Width", "Prepare", "gem5 ROI", "McPAT", "HotSpot"]]
    rows.extend([[r["benchmark"], str(r["cores"]), str(r["width"])] +
                 [r["stages"][s]["state"] for s in STAGES] for r in report["cases"]])
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = ["  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip() for row in rows]
    lines.insert(1, "  ".join("-"*w for w in widths))
    lines.append("\nCompleted: "+" | ".join(f"{s} {v['done']}/{v['total']}" for s, v in report["summary"].items()))
    lines.append("Prepare checkpoints are shared across widths. RUNNING/QUEUED are the latest batch-reported states.")
    if not report["verified"]:
        lines.append("DONE checks completion metadata; add --verify for full integrity/provenance checks.")
    if details:
        seen = set()
        for row in report["cases"]:
            for stage, value in row["stages"].items():
                key = (stage, value["output"])
                if key in seen or value["state"] in ("DONE", "PENDING", "QUEUED", "NOT_NEEDED"):
                    continue
                seen.add(key)
                lines.append(f"\n{stage} {row['case']}: {value['state']} — {value['reason']}")
                if "pid" in value:
                    lines.append(f"  Child PID (from batch report): {value['pid']}")
                if "log" in value:
                    lines.append(f"  Log: {value['log']}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="Fully validate completed artifacts (more disk I/O)")
    parser.add_argument("--details", action="store_true", help="Show active/failed/invalid case reasons, PIDs and logs")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.add_argument("--benchmarks", nargs="+")
    parser.add_argument("--results-root", type=Path)
    parser.add_argument("--checkpoints-root", type=Path)
    parser.add_argument("--gem5-binary", type=Path, default=ROOT / "external/gem5/build/X86_MESI_Two_Level/gem5.opt")
    parser.add_argument("--mcpat-binary", type=Path, default=ROOT / "external/mcpat/mcpat")
    parser.add_argument("--hotspot-binary", type=Path, default=ROOT / "external/hotspot/hotspot")
    args = parser.parse_args(argv)
    try:
        report = collect(Context(args), args.verify)
        print(json.dumps(report, indent=2) if args.json else table(report, args.details))
        return 0
    except INVALID as exc:
        print(f"Status unavailable: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
