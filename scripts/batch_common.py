"""Shared planning, validation and process control for the four experiment stages."""

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import time

from gem5_to_mcpat import ROOT, ConversionError, parse_processor_power, read_json, require, sha256
from mcpat_to_hotspot import parse_temperatures
from roi_control import validate_checkpoint


STAGES = ("prepare", "restore", "mcpat", "hotspot")
INVALID = (OSError, ValueError, KeyError, TypeError)


@dataclass(frozen=True)
class Case:
    benchmark: str
    cores: int
    width: int

    @property
    def name(self):
        return f"{self.benchmark}-c{self.cores}-w{self.width}"

    @property
    def checkpoint_name(self):
        return f"{self.benchmark}-c{self.cores}"


def case_matrix(policy, benchmarks=None):
    """Configuration outer loop, benchmark inner loop; shared baseline occurs once."""
    configs = list(dict.fromkeys(
        [(c, policy["fixed_width"]) for c in policy["core_counts"]] +
        [(policy["fixed_cores"], w) for w in policy["widths"]]))
    return [Case(b, c, w) for c, w in configs for b in (benchmarks or policy["benchmarks"])]


@dataclass
class Job:
    key: str
    case: Case
    state: str
    reason: str
    output: Path
    command: list = field(default_factory=list)
    cwd: Path = ROOT
    archive: list = field(default_factory=list)


def file_required(path):
    require(path.is_file() and path.stat().st_size > 0, f"Missing/empty completion artifact: {path}")


class Context:
    def __init__(self, args):
        self.args = args
        self.policy_path = ROOT / "configs/experiment-startup.json"
        self.policy = read_json(self.policy_path)
        self.policy_sha = sha256(self.policy_path)
        self.results = (args.results_root or ROOT / "results" / self.policy["revision"]).resolve()
        self.checkpoints = (args.checkpoints_root or ROOT / "checkpoints" / self.policy["revision"]).resolve()
        require(self.results != self.checkpoints and self.results not in self.checkpoints.parents
                and self.checkpoints not in self.results.parents, "Results/checkpoint roots must be separate, non-nested directories")
        benchmarks = args.benchmarks or self.policy["benchmarks"]
        require(len(set(benchmarks)) == len(benchmarks) and set(benchmarks) <= set(self.policy["benchmarks"]),
                "Select unique benchmarks from the experiment policy")
        self.cases = case_matrix(self.policy, benchmarks)
        self.gem5 = args.gem5_binary.resolve()
        self.mcpat = args.mcpat_binary.resolve()
        self.hotspot = args.hotspot_binary.resolve()
        self.commits = {name: subprocess.check_output(
            ["git", "-C", str(ROOT / "external" / name), "rev-parse", "HEAD"], text=True).strip()
            for name in ("gem5", "mcpat", "hotspot")}
        self.hash_cache = {}

    def digest(self, path):
        path = Path(path)
        stat = path.stat()
        key = (path.resolve(), stat.st_mtime_ns, stat.st_size)
        if key not in self.hash_cache:
            self.hash_cache[key] = sha256(path)
        return self.hash_cache[key]

    def run_path(self, case):
        return self.results / case.name

    def checkpoint_path(self, case):
        return self.checkpoints / case.checkpoint_name

    def check_roi(self, case):
        path = self.run_path(case)
        for name in ("roi.json", "roi_stats.json", "run_manifest.json", "config.json", "stats.txt"):
            file_required(path / name)
        roi, manifest = read_json(path / "roi.json"), read_json(path / "run_manifest.json")
        require(roi["status"] == "roi_complete", "ROI has not completed")
        for key, expected in (("benchmark", case.benchmark), ("cores", case.cores),
                              ("width", case.width), ("policy_sha256", self.policy_sha)):
            require(roi[key] == manifest[key] == expected, f"ROI/manifest mismatch: {case.name}.{key}")
        require(manifest["tool_commits"]["gem5"] == self.commits["gem5"] and not manifest["smoke_ticks"],
                f"Wrong gem5 revision or diagnostic run: {case.name}")
        require(roi["ticks_per_second"] > 0 and roi["end_tick"] > roi["begin_tick"] >= 0, "Invalid ROI ticks")
        seconds = (roi["end_tick"]-roi["begin_tick"])/roi["ticks_per_second"]
        require(math.isfinite(seconds) and seconds > 0 and math.isclose(seconds, roi["roi_seconds"], rel_tol=1e-12),
                "Invalid ROI timing")
        snapshot = read_json(path / "roi_stats.json")
        require(snapshot["simulated_begin_time"] == roi["begin_tick"] and snapshot["simulated_end_time"] == roi["end_tick"],
                "ROI snapshot bounds differ")
        return roi

    def check_checkpoint(self, case):
        path = self.checkpoint_path(case)
        file_required(path / "m5.cpt")
        file_required(path / "project_checkpoint.json")
        metadata = read_json(path / "project_checkpoint.json")
        validate_checkpoint(metadata, {"policy_sha256": self.policy_sha, "benchmark": case.benchmark,
                                       "cores": case.cores, "worker_threads": case.cores, "boot_cpu": "atomic",
                                       "tool_commits": self.commits, "smoke_ticks": 0})
        refs = re.findall(r"^(?:filename|cowFilename)=(.+)$", (path / "m5.cpt").read_text(), re.M)
        require(len(refs) >= 2, "Checkpoint lacks memory/disk references")
        for name in refs:
            file_required(path / name.strip())
        file_required(path / "board.cache_hierarchy.ruby_system.cache.gz")

    def verify_input_hashes(self, recorded, known):
        """Accept moved checkouts: associate recorded paths with known input names."""
        for actual in known:
            candidates = [digest for name, digest in recorded.items() if Path(name).name == actual.name]
            require(candidates and self.digest(actual) in candidates, f"Stale input/model: {actual}")

    def check_mcpat(self, case):
        run, roi = self.run_path(case), self.check_roi(case)
        path = run / "mcpat"
        for name in ("input.xml", "mapping.json", "mcpat.txt", "power.json"):
            file_required(path / name)
        power, mapping = read_json(path / "power.json"), read_json(path / "mapping.json")
        require(mapping["roi"] == roi and power["roi_seconds"] == roi["roi_seconds"], "McPAT ROI mismatch")
        require(power["xml_sha256"] == mapping["xml_sha256"] == self.digest(path / "input.xml"), "McPAT XML changed")
        require(power["mcpat_binary_sha256"] == self.digest(self.mcpat), "McPAT binary changed")
        self.verify_input_hashes(mapping["inputs"], [self.policy_path, ROOT / "configs/mcpat/model-v1.json"] +
                                 [run / n for n in ("roi.json", "roi_stats.json", "config.json", "stats.txt")])
        parsed = parse_processor_power((path / "mcpat.txt").read_text())
        require(all(parsed[k] == power[k] for k in parsed), "Power JSON/report mismatch")

    def check_hotspot(self, case):
        self.check_mcpat(case)
        path = self.run_path(case) / "hotspot"
        for name in ("mapping.json", "temperature.json", "steady.temp", "floorplan.flp", "power.ptrace", "hotspot.config"):
            file_required(path / name)
        temp, mapping = read_json(path / "temperature.json"), read_json(path / "mapping.json")
        require(temp["mapping_sha256"] == self.digest(path / "mapping.json")
                and temp["steady_file_sha256"] == self.digest(path / "steady.temp"), "Thermal output changed")
        require(temp["hotspot_binary_sha256"] == self.digest(self.hotspot), "HotSpot binary changed")
        self.verify_input_hashes(mapping["inputs"], [self.policy_path, ROOT / "configs/hotspot/model-v1.json",
                                 self.run_path(case) / "roi.json"] +
                                 [self.run_path(case) / "mcpat" / n for n in ("input.xml", "mapping.json", "power.json", "mcpat.txt")])
        for name, digest in mapping["generated_inputs_sha256"].items():
            require(self.digest(path / name) == digest, f"Thermal input changed: {name}")
        parsed = parse_temperatures((path / "steady.temp").read_text(), mapping["blocks"], self.policy["thermal"]["ambient_k"])
        require(all(parsed[k] == temp[k] for k in parsed), "Temperature JSON/solver output mismatch")

    def complete(self, stage, case):
        {"prepare": self.check_checkpoint, "restore": self.check_roi,
         "mcpat": self.check_mcpat, "hotspot": self.check_hotspot}[stage](case)


def success(check, case):
    try:
        check(case)
        return True
    except INVALID:
        return False


def make_plan(ctx, stage):
    cases = ctx.cases
    if stage == "prepare":
        cases = list({(c.benchmark, c.cores): Case(c.benchmark, c.cores, ctx.policy["fixed_width"])
                      for c in cases}.values())
    jobs = []
    for case in cases:
        output = (ctx.results / (case.checkpoint_name+"-prepare") if stage == "prepare" else
                  ctx.run_path(case) if stage == "restore" else ctx.run_path(case) / stage)
        key = case.checkpoint_name if stage == "prepare" else case.name
        job = Job(key, case, "RUN", "pending", output)
        jobs.append(job)
        if stage == "prepare" and all(success(ctx.check_roi, c) for c in ctx.cases
                                       if (c.benchmark, c.cores) == (case.benchmark, case.cores)):
            job.state, job.reason = "SKIP", "all dependent ROI runs already complete"
            continue
        target = ctx.checkpoint_path(case) if stage == "prepare" else output
        if target.exists():
            try:
                ctx.complete(stage, case)
                job.state, job.reason = "SKIP", "validated completed artifacts"
                continue
            except INVALID as exc:
                job.reason = str(exc)
                if not ctx.args.retry:
                    job.state = "BLOCKED"
                    job.reason += "; inspect it or use --retry to archive and retry"
                    continue
                job.archive.append(target)
        if stage == "prepare" and output.exists():
            if not ctx.args.retry:
                job.state, job.reason = "BLOCKED", "prepare output already exists; inspect it or use --retry"
                continue
            job.archive.append(output)
        try:
            if stage == "restore":
                ctx.check_checkpoint(case)
            elif stage == "mcpat":
                ctx.check_roi(case)
            elif stage == "hotspot":
                ctx.check_mcpat(case)
        except INVALID as exc:
            job.state, job.reason = "BLOCKED", f"prerequisite is not ready: {exc}"
            continue
        if stage in ("prepare", "restore"):
            job.command = [str(ctx.gem5), "--listener-mode=off", "-d", str(output),
                           str(ROOT / "configs/gem5/run_parsec.py"), "--mode", stage,
                           "--benchmark", case.benchmark, "--cores", str(case.cores), "--width", str(case.width),
                           "--checkpoint-dir", str(ctx.checkpoint_path(case)), "--boot-cpu", "atomic", "--offline"]
        elif stage == "mcpat":
            job.command = [sys.executable, str(ROOT / "scripts/gem5_to_mcpat.py"), "--run-dir", str(ctx.run_path(case)),
                           "--mcpat-binary", str(ctx.mcpat), "--run-mcpat"]
            job.cwd = output  # Isolate CACTI's potential out.csv and all relative scratch files.
        else:
            job.command = [sys.executable, str(ROOT / "scripts/mcpat_to_hotspot.py"), "--run-dir", str(ctx.run_path(case)),
                           "--hotspot-binary", str(ctx.hotspot), "--run-hotspot"]
        job.reason = "will archive invalid/incomplete artifacts before retry" if job.archive else "pending"
    return jobs


def archive_path(path):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = path.with_name(path.name+f".incomplete-{stamp}-{os.getpid()}")
    path.rename(destination)
    return str(destination)


def freeze_inputs(ctx, stage):
    require(ctx.digest(ctx.policy_path) == ctx.policy_sha, "Policy changed during preflight")
    paths = [ctx.policy_path, ROOT / "configs/gem5/run_parsec.py", ROOT / "scripts/roi_control.py",
             ROOT / "scripts/batch_common.py", ROOT / f"scripts/batch_{stage}.py",
             ROOT / "scripts/gem5_to_mcpat.py", ROOT / "scripts/mcpat_to_hotspot.py",
             ROOT / "configs/mcpat/model-v1.json", ROOT / "configs/hotspot/model-v1.json"]
    if stage in ("prepare", "restore"):
        for spec in (ctx.policy["kernel"], ctx.policy["disk"]):
            file_required(ROOT / "resources" / f"{spec['id']}-{spec['version']}")
        require(os.access(ctx.gem5, os.X_OK), f"Build gem5 first: {ctx.gem5}")
        paths.append(ctx.gem5)
    if stage == "mcpat":
        require(os.access(ctx.mcpat, os.X_OK), f"Build McPAT first: {ctx.mcpat}")
        paths.append(ctx.mcpat)
    if stage == "hotspot":
        require(os.access(ctx.hotspot, os.X_OK), f"Build HotSpot first: {ctx.hotspot}")
        paths.append(ctx.hotspot)
    return {str(p): ctx.digest(p) for p in paths}


def inputs_unchanged(ctx, frozen):
    require(all(ctx.digest(Path(p)) == digest for p, digest in frozen.items()),
            "Policy, model, script or binary changed during this stage; pending jobs stopped")


def execute_jobs(ctx, stage, jobs):
    """Process-level concurrency; stop launching after failure, finish already-running jobs."""
    frozen = freeze_inputs(ctx, stage)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    logs = ctx.results / "batch_logs" / stage / (stamp+f"-{os.getpid()}")
    logs.mkdir(parents=True)
    report_path = logs / "status.json"
    records = {j.key: {"state": j.state, "reason": j.reason, "command": j.command, "output": str(j.output),
                       "cwd": str(j.cwd)} for j in jobs}
    report = {"stage": stage, "jobs_limit": ctx.args.jobs, "policy_sha256": ctx.policy_sha,
              "tool_commits": ctx.commits, "frozen_inputs_sha256": frozen, "cases": records}
    def save():
        tmp = report_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(report, indent=2)+"\n")
        tmp.replace(report_path)
    pending = [j for j in jobs if j.state == "RUN"]
    active, failed = {}, False
    save()
    print(f"Status: {report_path}", flush=True)
    try:
        while pending or active:
            while pending and len(active) < ctx.args.jobs and not failed:
                inputs_unchanged(ctx, frozen)
                job = pending.pop(0)
                row = records[job.key]
                row["archives"] = [archive_path(p) for p in job.archive if p.exists()]
                job.output.parent.mkdir(parents=True, exist_ok=True)
                if job.cwd != ROOT:
                    job.cwd.mkdir(parents=True, exist_ok=True)
                log_path = logs / (job.key+".log")
                try:
                    with log_path.open("w") as log:
                        process = subprocess.Popen(job.command, cwd=job.cwd, stdout=log, stderr=subprocess.STDOUT,
                                                   stdin=subprocess.DEVNULL, start_new_session=True)
                except OSError as exc:
                    row.update(state="FAILED", reason=str(exc), log=str(log_path))
                    failed = True
                    save()
                    print(f"FAILED {job.key}: {exc}", flush=True)
                    break
                active[process] = job
                row.update(state="RUNNING", pid=process.pid, log=str(log_path), started_at=time.time())
                print(f"START {job.key} pid={process.pid} log={log_path}", flush=True)
                save()
            for process, job in list(active.items()):
                code = process.poll()
                if code is None:
                    continue
                row = records[job.key]
                row.update(returncode=code, ended_at=time.time())
                try:
                    require(code == 0, f"Process exited with code {code}")
                    inputs_unchanged(ctx, frozen)
                    ctx.complete(stage, job.case)
                    row.update(state="DONE", reason="process and artifact validation succeeded")
                except INVALID as exc:
                    row.update(state="FAILED", reason=str(exc))
                    failed = True
                del active[process]
                print(f"{row['state']} {job.key}: {row['reason']}", flush=True)
                save()
            if failed and not active:
                break
            if active:
                time.sleep(0.2)
    except BaseException as exc:
        report["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        report["error"] = str(exc)
        for process in active:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for process, job in active.items():
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            records[job.key].update(state="INTERRUPTED", returncode=process.returncode, ended_at=time.time())
        for row in records.values():
            if row["state"] == "RUN":
                row["state"] = "NOT_STARTED"
        save()
        raise
    for job in pending:
        records[job.key]["state"] = "NOT_STARTED"
    report["status"] = "failed" if failed else "complete"
    save()
    print(f"Stage {stage}: {report['status']}. Status: {report_path}", flush=True)
    return 1 if failed else 0


def stage_main(stage, argv=None):
    parser = argparse.ArgumentParser(description=f"Batch stage {stage}: five configurations × four workloads; skip validated results.")
    parser.add_argument("--jobs", type=int, default=2, help="Maximum concurrent processes (default: 2; use 1 for serial)")
    parser.add_argument("--dry-run", action="store_true", help="Inspect plan only; create no files or processes")
    parser.add_argument("--retry", action="store_true", help="Archive incomplete/invalid target directories, then retry; never delete them")
    parser.add_argument("--benchmarks", nargs="+", help="Optional subset of policy benchmarks")
    parser.add_argument("--results-root", type=Path)
    parser.add_argument("--checkpoints-root", type=Path)
    parser.add_argument("--gem5-binary", type=Path, default=ROOT / "external/gem5/build/X86_MESI_Two_Level/gem5.opt")
    parser.add_argument("--mcpat-binary", type=Path, default=ROOT / "external/mcpat/mcpat")
    parser.add_argument("--hotspot-binary", type=Path, default=ROOT / "external/hotspot/hotspot")
    args = parser.parse_args(argv)
    try:
        require(stage in STAGES and args.jobs >= 1, "Select a valid stage and --jobs >= 1")
        ctx = Context(args)
        def plan():
            jobs = make_plan(ctx, stage)
            for job in jobs:
                print(f"{job.state:7} {job.key}: {job.reason}")
                if args.dry_run and job.command:
                    print("        "+shlex.join(job.command))
            counts = {s: sum(j.state == s for j in jobs) for s in ("RUN", "SKIP", "BLOCKED")}
            print(f"Stage {stage}: {len(jobs)} tasks; {counts}; concurrency={args.jobs}", flush=True)
            return jobs, counts
        if args.dry_run:
            plan()
            return 0
        # One stage at a time, even if two batch commands are accidentally launched.
        lock_dir = ROOT / "results"
        lock_dir.mkdir(exist_ok=True)
        with (lock_dir / ".batch-stage.lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ConversionError("Another batch stage is running; wait for it to finish") from exc
            jobs, counts = plan()
            require(counts["BLOCKED"] == 0, "Stage prerequisites/outputs need attention; nothing was launched")
            if counts["RUN"] == 0:
                print("All tasks already complete; nothing to run.")
                return 0
            def interrupt(*_):
                raise KeyboardInterrupt()
            old_handler = signal.signal(signal.SIGTERM, interrupt)
            try:
                return execute_jobs(ctx, stage, jobs)
            finally:
                signal.signal(signal.SIGTERM, old_handler)
    except KeyboardInterrupt:
        print("Batch interrupted; active child process groups stopped. Use --dry-run to inspect partial outputs.", file=sys.stderr)
        return 130
    except INVALID as exc:
        print(f"Batch failed: {exc}", file=sys.stderr)
        return 1
