"""ROI transitions and checkpoint compatibility, independent of gem5 imports."""

import hashlib
import json
import math


def pre_roi_warmup(policy):
    """Accept only the two explicitly implemented measurement boundaries."""
    execution = policy.get("execution", {})
    boundary = execution.get("checkpoint_boundary", "workbegin_before_o3_switch")
    expected = {"workbegin_before_o3_switch": "workbegin",
                "parsec_launch_before_o3_switch": "parsec_launch"}
    if boundary not in expected or execution.get("fast_forward_until", expected[boundary]) != expected[boundary]:
        raise ValueError("Unsupported or inconsistent fast-forward/checkpoint boundary")
    return boundary == "parsec_launch_before_o3_switch"


def validate_warmup(warmup, snapshot, roi):
    """A warmed full ROI must follow a separately recorded, excluded O3 interval."""
    if warmup.get("status") != "warmup_complete" or warmup.get("method") != "pre_roi_initialization":
        raise ValueError("Missing completed pre-ROI warm-up")
    for key in ("benchmark", "cores", "width", "policy_sha256", "ticks_per_second"):
        if warmup.get(key) != roi.get(key):
            raise ValueError("Warm-up/ROI mismatch: " + key)
    begin, end = warmup["begin_tick"], warmup["end_tick"]
    if not 0 <= begin < end == roi["begin_tick"]:
        raise ValueError("Warm-up must end exactly where the full ROI begins")
    if (snapshot["simulated_begin_time"] != begin or snapshot["simulated_end_time"] != end
            or snapshot["simTicks"]["value"] != end-begin
            or snapshot["finalTick"]["value"] != end
            or snapshot["simFreq"]["value"] != roi["ticks_per_second"]
            or snapshot["simInsts"]["value"] <= 0):
        raise ValueError("Warm-up snapshot does not match its detailed interval")
    seconds = (end-begin)/roi["ticks_per_second"]
    if not math.isfinite(warmup["warmup_seconds"]) or abs(warmup["warmup_seconds"]-seconds) > max(1e-15, seconds*1e-12):
        raise ValueError("Warm-up duration mismatch")


def validate_reference_policy(policy, root):
    """Keep hardware and inputs equal to the frozen non-warm-up reference."""
    if not pre_roi_warmup(policy):
        return
    reference = policy["reference"]
    source = root / reference["policy"]
    if hashlib.sha256(source.read_bytes()).hexdigest() != reference["policy_sha256"]:
        raise ValueError("Warm-up reference policy hash differs")
    baseline = json.loads(source.read_text())
    ignored = {"revision", "execution", "reference"}
    hardware = lambda p: {k: v for k, v in p.items() if k not in ignored}
    if hardware(policy) != hardware(baseline):
        raise ValueError("Warm-up hardware/workload policy differs from the reference")
    execution = policy["execution"]
    if (execution.get("warmup_method") != "pre_roi_initialization"
            or execution.get("measurement_begin") != "workbegin"
            or execution.get("measurement_end") != "workend"):
        raise ValueError("Warm-up must preserve the full WORKBEGIN-to-WORKEND ROI")


def checkpoint_identity(run):
    """Widths may vary; architectural state, workload, and gem5 must match."""
    return {
        "format": 1,
        "policy_sha256": run["policy_sha256"],
        "benchmark": run["benchmark"],
        "cores": run["cores"],
        "worker_threads": run["worker_threads"],
        "boot_cpu": run["boot_cpu"],
        "gem5_commit": run["tool_commits"]["gem5"],
        "boundary": ("smoke" if run["smoke_ticks"] else
                     "parsec_launch" if pre_roi_warmup(run.get("policy", {})) else "workbegin"),
    }


def validate_checkpoint(saved, run):
    if not isinstance(saved, dict) or not isinstance(saved.get("identity"), dict):
        raise ValueError("Checkpoint metadata has no valid identity")
    expected = checkpoint_identity(run)
    if saved.get("identity") != expected:
        keys = [
            key for key, value in expected.items()
            if saved.get("identity", {}).get(key) != value
        ]
        raise ValueError("Checkpoint mismatch: " + ", ".join(keys))


class RoiControl:
    def __init__(self, mode, m5, processor, capture, warmup_before_roi=False, capture_warmup=None):
        self.mode = mode
        self.m5 = m5
        self.processor = processor
        self.capture = capture
        self.warmup_before_roi = warmup_before_roi
        self.capture_warmup = capture_warmup
        self.detailed_started = False
        self.warmup_begin = None
        self.warmup_end = None
        self.launch_seen = False
        self.boundary_seen = False
        self.checkpoint_ready = False
        self.begin = None
        self.end = None

    def on_exit(self):
        if self.launch_seen or self.boundary_seen:
            raise RuntimeError("Guest exited before completing the expected ROI")
        self.launch_seen = True
        if self.warmup_before_roi:
            if self.mode == "prepare":
                self.checkpoint_ready = True
                print("Reached PARSEC launch marker; stopping to save pre-ROI checkpoint", flush=True)
                return True
            self.start_warmup()
            return False
        print("PARSEC launch marker: continuing fast-forward until WORKBEGIN", flush=True)
        return False

    def start_warmup(self):
        if self.detailed_started:
            raise RuntimeError("O3 warm-up already started")
        self.processor.switch()
        self.detailed_started = True
        self.warmup_begin = self.m5.curTick()
        self.m5.stats.reset()
        print("Warm-up begin: O3 initialization until WORKBEGIN; measurement has not started", flush=True)

    def start_detailed(self):
        self.processor.switch()
        self.detailed_started = True
        # Switching may drain pending work; start timing after the switch.
        self.begin = self.m5.curTick()
        self.m5.stats.reset()
        print("ROI begin: switched to O3 and reset statistics", flush=True)

    def on_workbegin(self):
        if not self.launch_seen or self.boundary_seen:
            raise RuntimeError("Unexpected ROI entry")
        self.boundary_seen = True
        if self.warmup_before_roi:
            if self.mode == "prepare" or not self.detailed_started or self.capture_warmup is None:
                raise RuntimeError("WORKBEGIN without a valid detailed warm-up")
            self.warmup_end = self.m5.curTick()
            if self.warmup_end <= self.warmup_begin:
                raise RuntimeError("Pre-ROI warm-up interval is empty")
            self.m5.stats.dump()
            self.capture_warmup()
            # Reset counters only: retain the warmed O3/cache state for the full ROI.
            self.begin = self.m5.curTick()
            self.m5.stats.reset()
            print("ROI begin: warm-up captured; statistics reset with O3/cache state retained", flush=True)
            return False
        if self.mode == "prepare":
            self.checkpoint_ready = True
            print("Reached WORKBEGIN on fast CPU; stopping to save checkpoint", flush=True)
            return True
        self.start_detailed()
        return False

    def restore_at_boundary(self):
        # The saved marker was already consumed; do not wait for it a second time.
        self.launch_seen = True
        if self.warmup_before_roi:
            # This checkpoint consumed the launch marker, not WORKBEGIN.
            self.start_warmup()
            return
        self.boundary_seen = True
        self.start_detailed()

    def on_workend(self):
        if self.begin is None or self.end is not None:
            raise RuntimeError("Unexpected ROI exit")
        self.end = self.m5.curTick()
        self.m5.stats.dump()
        self.capture()
        print("ROI end: captured statistics", flush=True)
        return True
