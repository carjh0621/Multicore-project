"""PARSEC full-system pilot for the gem5 commit pinned by this repository.

Run with gem5, not the host Python. --describe works with host Python.
"""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from roi_control import RoiControl, checkpoint_identity, validate_checkpoint
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--benchmark", default="blackscholes")
parser.add_argument("--cores", type=int, default=4)
parser.add_argument("--width", type=int, default=4)
parser.add_argument("--boot-cpu", choices=("atomic", "kvm"), default="atomic")
parser.add_argument("--mode", choices=("direct", "prepare", "restore"), default="direct")
parser.add_argument("--checkpoint-dir", type=Path)
parser.add_argument("--offline", action="store_true",
                    help="Use the existing pinned resource files without catalog/hash checks")
parser.add_argument("--smoke-ticks", type=int, default=0,
                    help="Diagnostic only: stop after this many ticks; never write ROI results")
parser.add_argument("--describe", action="store_true", help="Print settings without running gem5")
args = parser.parse_args()
if args.mode != "direct" and args.checkpoint_dir is None:
    parser.error("--checkpoint-dir is required for prepare/restore")
if args.mode == "direct" and args.checkpoint_dir is not None:
    parser.error("--checkpoint-dir is only used by prepare/restore")
if args.mode != "direct" and args.boot_cpu != "atomic":
    parser.error("Checkpoint modes currently require --boot-cpu atomic")
if args.smoke_ticks < 0:
    parser.error("--smoke-ticks must be positive when specified")
if args.smoke_ticks and (args.cores != 1 or args.boot_cpu != "atomic"):
    parser.error("Early-boot smoke tests require --cores 1 --boot-cpu atomic")
if args.checkpoint_dir is not None:
    args.checkpoint_dir = args.checkpoint_dir.resolve()
policy_path = ROOT / "configs/experiment-startup.json"
policy = json.loads(policy_path.read_text())
if args.benchmark not in policy["benchmarks"]:
    parser.error("Benchmark is not part of the startup experiment")
if not (args.smoke_ticks and args.width in policy["widths"]) and not (
    (args.cores in policy["core_counts"] and args.width == policy["fixed_width"])
    or (args.cores == policy["fixed_cores"] and args.width in policy["widths"])
):
    parser.error("Configuration is not part of either startup experiment sweep")

run = {
    "policy": policy,
    "policy_sha256": hashlib.sha256(policy_path.read_bytes()).hexdigest(),
    "benchmark": args.benchmark,
    "cores": args.cores,
    "width": args.width,
    "worker_threads": args.cores,
    "boot_cpu": args.boot_cpu,
    "warmup": policy["execution"]["warmup"],
    "mode": args.mode,
    "checkpoint_dir": str(args.checkpoint_dir) if args.checkpoint_dir else None,
    "smoke_ticks": args.smoke_ticks,
    "resource_source": "local_files_without_hash_check" if args.offline else "gem5_catalog",
}
if args.describe:
    print(json.dumps(run, indent=2))
    raise SystemExit(0)

import m5
from m5.objects import IQUnit

from gem5.coherence_protocol import CoherenceProtocol
from gem5.components.boards.x86_board import X86Board
from gem5.components.cachehierarchies.ruby.mesi_two_level_cache_hierarchy import (
    MESITwoLevelCacheHierarchy,
)
from gem5.components.memory import DualChannelDDR4_2400
from gem5.components.processors.cpu_types import CPUTypes
from gem5.components.processors.simple_switchable_processor import SimpleSwitchableProcessor
from gem5.isas import ISA
from gem5.resources.resource import obtain_resource, KernelResource, DiskImageResource
from gem5.simulate.exit_event import ExitEvent
from gem5.simulate.simulator import Simulator
from gem5.utils.requires import requires

requires(
    isa_required=ISA.X86,
    coherence_protocol_required=CoherenceProtocol.MESI_TWO_LEVEL,
    kvm_required=args.boot_cpu == "kvm",
)
m5.ticks.setGlobalFrequency("1THz")
outdir = Path(m5.options.outdir)
outdir.mkdir(parents=True, exist_ok=True)
for filename in ("run_manifest.json", "roi.json", "roi_stats.json"):
    if (outdir / filename).exists():
        parser.error("Output directory contains a previous run; use a fresh -d directory")
run["tool_commits"] = {
    name: subprocess.check_output(
        ["git", "-C", str(ROOT / "external" / name), "rev-parse", "HEAD"], text=True
    ).strip()
    for name in ("gem5", "mcpat", "hotspot")
}
checkpoint_metadata = None
if args.mode == "prepare" and args.checkpoint_dir.exists():
    parser.error("Checkpoint destination already exists; choose a fresh directory")
if args.mode == "restore":
    if not (args.checkpoint_dir / "m5.cpt").is_file():
        parser.error("Checkpoint directory has no m5.cpt")
    try:
        checkpoint_metadata = json.loads(
            (args.checkpoint_dir / "project_checkpoint.json").read_text()
        )
        validate_checkpoint(checkpoint_metadata, run)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
(outdir / "run_manifest.json").write_text(json.dumps(run, indent=2) + "\n")

processor = SimpleSwitchableProcessor(
    starting_core_type=CPUTypes.KVM if args.boot_cpu == "kvm" else CPUTypes.ATOMIC,
    switch_core_type=CPUTypes.O3,
    isa=ISA.X86,
    num_cores=args.cores,
    clk_freq=policy["clock"],
)
# This gem5 revision stores the inactive O3 cores under this key. Access the
# mapping because the SimObject child name "switch" also names a Python method.
for core in processor._switchable_cores["switch"]:
    cpu = core.get_simobject()
    for name, value in policy["roi_cpu"].items():
        setattr(cpu, name, value)
    for name in policy["pipeline_width_parameters"]:
        setattr(cpu, name, args.width)
    cpu.instQueues = [IQUnit(numEntries=policy["iq_entries"])]
    # Narrow writeback can schedule load completions beyond the default
    # five-cycle simulator window; preserve the requested two-wide datapath.
    if args.width == 2:
        cpu.forwardComSize = 128
processor.voltage_domain.voltage = f"{policy['power']['voltage_v']}V"

board = X86Board(
    clk_freq=policy["clock"],
    processor=processor,
    memory=DualChannelDDR4_2400(size=policy["memory"]["size"]),
    cache_hierarchy=MESITwoLevelCacheHierarchy(**policy["cache"]),
)
board.cache_line_size = policy["cache_line_bytes"]
# The guest launch marker only reports a milestone. Keep the fast CPU active
# through application setup; switch to O3 at WORKBEGIN.
guest_command = f"""#!/bin/bash
set -e
trap 'm5 exit' EXIT
cd /home/gem5/parsec-benchmark
source env.sh
export OMP_NUM_THREADS={args.cores}
export OMP_DYNAMIC=FALSE
export OMP_PROC_BIND=TRUE
export OMP_PLACES=cores
m5 exit
parsecmgmt -a run -p {args.benchmark} -c {policy['parsec_build']} -i {policy['input']} -n {args.cores}
"""
resources = ROOT / "resources"
resources.mkdir(exist_ok=True)


def resource(spec):
    if args.offline:
        path = resources / f"{spec['id']}-{spec['version']}"
        if not path.is_file():
            parser.error(f"Offline resource missing: {path}")
        if spec is policy["kernel"]:
            return KernelResource(local_path=str(path))
        return DiskImageResource(local_path=str(path), root_partition="1")
    return obtain_resource(
        spec["id"], resource_version=spec["version"], resource_directory=str(resources)
    )


board.set_kernel_disk_workload(
    kernel=resource(policy["kernel"]),
    disk_image=resource(policy["disk"]),
    readfile_contents=guest_command,
    checkpoint=args.checkpoint_dir if args.mode == "restore" else None,
)


def capture_roi_stats():
    shutil.copyfile(outdir / "stats.json", outdir / "roi_stats.json")


control = RoiControl(args.mode, m5, processor, capture_roi_stats)


def on_max_tick():
    if not args.smoke_ticks:
        raise RuntimeError("Unexpected tick limit before ROI completion")
    # Synthetic boundary for testing serialization only; never a production ROI.
    control.checkpoint_ready = args.mode == "prepare"
    return True


simulator = Simulator(
    board=board,
    on_exit_event={
        ExitEvent.EXIT: control.on_exit,
        ExitEvent.WORKBEGIN: control.on_workbegin,
        ExitEvent.WORKEND: control.on_workend,
        ExitEvent.MAX_TICK: on_max_tick,
    },
)
simulator.add_json_stats_output(str(outdir / "stats.json"))
if args.mode == "restore":
    # Pinned v26 API: instantiate/restore without advancing guest instructions.
    # Complete startup before takeOverFrom() initializes O3 pipeline structures.
    # A zero-tick simulate performs startup without advancing simulated time.
    # The saved WORKBEGIN event has already been consumed; do not wait for it.
    simulator._instantiate()
    restored_tick = m5.curTick()
    m5.simulate(0)
    if m5.curTick() != restored_tick:
        raise RuntimeError("Startup unexpectedly advanced checkpoint time")
    control.restore_at_boundary()
simulator.run(max_ticks=args.smoke_ticks or None)
if args.mode == "prepare":
    if not control.checkpoint_ready:
        raise RuntimeError("Simulation stopped before checkpoint boundary")
    print("Checkpoint boundary reached; draining and serializing", flush=True)
    simulator.save_checkpoint(args.checkpoint_dir)
    metadata = {
        "identity": checkpoint_identity(run),
        "created_with_width": args.width,
        "saved_tick": m5.curTick(),
        "run": run,
    }
    (args.checkpoint_dir / "project_checkpoint.json").write_text(
        json.dumps(metadata, indent=2) + "\n"
    )
    print(f"Checkpoint saved: {args.checkpoint_dir}", flush=True)
    raise SystemExit(0)
if args.smoke_ticks:
    (outdir / "smoke.json").write_text(json.dumps({
        "status": "checkpoint_plumbing_only_not_roi",
        "mode": args.mode,
        "final_tick": m5.curTick(),
    }, indent=2) + "\n")
    print("Smoke test finished; no benchmark ROI result was produced.")
    raise SystemExit(0)
if control.begin is None or control.end is None:
    raise RuntimeError("Simulation ended without a complete ROI")
roi_ticks = control.end - control.begin
if roi_ticks <= 0:
    raise RuntimeError("ROI duration must be positive")
summary = {
    "status": "roi_complete",
    "begin_tick": control.begin,
    "end_tick": control.end,
    "ticks_per_second": 10**12,
    "roi_seconds": roi_ticks / 10**12,
    "benchmark": args.benchmark,
    "cores": args.cores,
    "width": args.width,
    "policy_sha256": run["policy_sha256"],
    "mode": args.mode,
    "warmup": run["warmup"],
}
(outdir / "roi.json").write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps(summary, indent=2))
