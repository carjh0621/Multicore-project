"""ROI transitions and checkpoint compatibility, independent of gem5 imports."""


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
        "boundary": "smoke" if run["smoke_ticks"] else "workbegin",
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
    def __init__(self, mode, m5, processor, capture):
        self.mode = mode
        self.m5 = m5
        self.processor = processor
        self.capture = capture
        self.launch_seen = False
        self.boundary_seen = False
        self.checkpoint_ready = False
        self.begin = None
        self.end = None

    def on_exit(self):
        if self.launch_seen or self.boundary_seen:
            raise RuntimeError("Guest exited before completing the expected ROI")
        self.launch_seen = True
        print("PARSEC launch marker: continuing fast-forward until WORKBEGIN", flush=True)
        return False

    def start_detailed(self):
        self.processor.switch()
        # Switching may drain pending work; start timing after the switch.
        self.begin = self.m5.curTick()
        self.m5.stats.reset()
        print("ROI begin: switched to O3 and reset statistics", flush=True)

    def on_workbegin(self):
        if not self.launch_seen or self.boundary_seen:
            raise RuntimeError("Unexpected ROI entry")
        self.boundary_seen = True
        if self.mode == "prepare":
            self.checkpoint_ready = True
            print("Reached WORKBEGIN on fast CPU; stopping to save checkpoint", flush=True)
            return True
        self.start_detailed()
        return False

    def restore_at_boundary(self):
        # The WORKBEGIN operation already happened when the checkpoint was made.
        # Do not wait for it to happen a second time after restore.
        self.launch_seen = True
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
