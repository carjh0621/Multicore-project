#!/usr/bin/env python3
"""Convert this project's completed gem5 O3/Ruby ROI into McPAT XML.

Only the pinned X86 / MESI_Two_Level / SimpleNetwork configuration is supported.
This is an explicitly approximate pilot model; see configs/mcpat/model-v1.json.
No upstream example workload statistics are copied into the generated XML.
"""

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]


class ConversionError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ConversionError(message)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path):
    with path.open() as stream:
        return json.load(stream)


def objects(value):
    return value if isinstance(value, list) else [value]


def walk_config(value):
    if isinstance(value, dict):
        if "path" in value and "type" in value:
            yield value
        for item in value.values():
            yield from walk_config(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_config(item)


def select_roi_dump(text, begin_tick, end_tick):
    """Select a complete matching dump, never an arbitrary last USR1 dump."""
    selected = None
    selected_index = None
    blocks = text.split("---------- Begin Simulation Statistics ----------")[1:]
    for index, block in enumerate(blocks, 1):
        if "---------- End Simulation Statistics" not in block:
            continue
        stats = {}
        for line in block.splitlines():
            fields = line.split()
            if len(fields) < 2 or fields[0].startswith(("#", "-")):
                continue
            try:
                number = float(fields[1])
            except ValueError:
                continue
            require(fields[0] not in stats, f"Duplicate statistic: {fields[0]}")
            stats[fields[0]] = number
        if (stats.get("finalTick") == end_tick
                and stats.get("simTicks") == end_tick - begin_tick):
            if selected is not None:
                # Consecutive dumps at the same tick can differ in host time only.
                keys = set(stats) | set(selected)
                require(all(stats.get(k) == selected.get(k)
                            or k.startswith("host")
                            or (math.isnan(stats.get(k, 0)) and math.isnan(selected.get(k, 0)))
                            for k in keys), "Conflicting completed ROI dumps")
            selected, selected_index = stats, index
    require(selected is not None, "No complete stats.txt dump matches the ROI tick bounds")
    return selected, selected_index, len(blocks)


def partition_ops(vector):
    """Return disjoint int/FP instruction counts and ALU/MUL/FPU accesses."""
    groups = dict(integer=0, floating=0, alu=0, mul=0, fpu=0)
    for op, count in vector.items():
        if op == "total":
            continue
        require(math.isfinite(count) and count >= 0, f"Invalid OpClass count: {op}")
        supported = (op in {"No_OpClass", "IntAlu", "IntMult", "IntDiv", "System", "InstPrefetch"}
                     or op.startswith(("Float", "Simd", "Mem", "Bf16")))
        require(supported or count == 0, f"Unsupported active OpClass: {op}")
        floating = op.startswith(("Float", "SimdFloat", "SimdBf16", "Bf16"))
        groups["floating" if floating else "integer"] += count
        memory = ("Mem" in op or "Load" in op or "Store" in op or op == "InstPrefetch")
        if memory or op in {"No_OpClass", "System", "SimdConfig"}:
            continue
        if floating or op.startswith("Simd"):
            # The shared SIMD FU group is represented structurally by FPU proxies,
            # including integer SIMD ops; instruction categories remain disjoint.
            groups["fpu"] += count
        elif "Mult" in op or "Div" in op:
            groups["mul"] += count
        else:
            groups["alu"] += count
    require(math.isclose(groups["integer"] + groups["floating"], vector["total"], abs_tol=0.01),
            "OpClass total does not equal its bins")
    return groups


class Converter:
    def __init__(self, run_dir, policy_path, model_path):
        self.run_dir = Path(run_dir)
        self.policy_path, self.model_path = Path(policy_path), Path(model_path)
        self.policy = read_json(self.policy_path)
        self.model = read_json(self.model_path)
        require(self.model["schema_version"] == 1, "Unsupported model schema")
        self.roi = read_json(self.run_dir / "roi.json")
        require(self.roi["status"] == "roi_complete", "Run has no completed ROI")
        require(self.roi["policy_sha256"] == sha256(self.policy_path),
                "Policy hash differs from the completed run; use its original policy file")
        r = self.roi
        require(r["end_tick"] > r["begin_tick"] >= 0 and r["ticks_per_second"] > 0,
                "Invalid ROI timing")
        self.seconds = (r["end_tick"] - r["begin_tick"]) / r["ticks_per_second"]
        require(math.isclose(self.seconds, r["roi_seconds"], rel_tol=1e-12), "ROI seconds mismatch")
        self.stats, selected, dumps = select_roi_dump(
            (self.run_dir / "stats.txt").read_text(), r["begin_tick"], r["end_tick"])
        snapshot = read_json(self.run_dir / "roi_stats.json")
        require(snapshot["simulated_begin_time"] == r["begin_tick"]
                and snapshot["simulated_end_time"] == r["end_tick"], "JSON ROI bounds mismatch")
        for key in ("simTicks", "finalTick", "simFreq", "simInsts", "simOps"):
            require(snapshot[key]["value"] == self.stat(key), f"JSON/text mismatch: {key}")
        require(self.stat("simFreq") == r["ticks_per_second"], "Tick frequency mismatch")
        self.config = read_json(self.run_dir / "config.json")
        self.board = self.config["board"]
        self.index = {x["path"]: x for x in walk_config(self.config)}
        self.cores = [x["core"] for x in objects(self.board["processor"]["switch"])]
        require(len(self.cores) == r["cores"], "ROI/config core count mismatch")
        self.ruby = self.board["cache_hierarchy"]["ruby_system"]
        self.l1 = objects(self.ruby["l1_controllers"])
        self.l2 = objects(self.ruby["l2_controllers"])
        require(len(self.l1) == len(self.cores), "Expected private L1 pair per core")
        require(all(x["type"] == "MESI_Two_Level_L1Cache_Controller" for x in self.l1),
                "Only MESI_Two_Level is supported")
        require(all(x["type"] == "MESI_Two_Level_L2Cache_Controller" for x in self.l2),
                "Only MESI_Two_Level L2 controllers are supported")
        for core in self.cores:
            require(core["type"] == "BaseO3CPU" and core["numThreads"] == 1,
                    "Only single-thread BaseO3CPU cores are supported")
            require(all(core[name] == r["width"] for name in self.policy["pipeline_width_parameters"]),
                    "ROI/config pipeline width mismatch")
            require(objects(core["isa"])[0]["type"] == "X86ISA", "Only X86 is supported")
            require(core["branchPred"]["conditionalBranchPred"]["type"] == "TournamentBP",
                    "Only TournamentBP is supported")
            require(core["branchPred"]["btb"]["type"] == "SimpleBTB", "Only SimpleBTB is supported")
        self.period = self.clock_period(self.cores[0])
        require(all(self.clock_period(c) == self.period for c in self.cores), "Mixed core clocks")
        self.hz = r["ticks_per_second"] / self.period
        # McPAT's XML parser uses atoi for clocks. Preserve ROI time after rounding MHz.
        self.mhz = round(self.hz / 1e6)
        self.cycles = self.seconds * self.mhz * 1e6
        self.native_cycles = (r["end_tick"] - r["begin_tick"]) / self.period
        self.mappings = []
        self.report = {
            "schema_version": 1, "status": "pilot_approximate", "model": self.model["name"],
            "roi": r, "roi_seconds": self.seconds,
            "selected_text_dump": selected, "text_dump_count": dumps,
            "native_clock_hz": self.hz, "mcpat_clock_mhz": self.mhz,
            "mcpat_total_cycles": self.cycles,
            "clock_note": "McPAT clocks are integer MHz; cycles are rescaled to preserve exact ROI seconds.",
            "scope": "Modeled chip: cores including L1, shared L2 with merged sharer bits, equivalent NoC, generic memory controllers including PHY. Excludes external DRAM and board/peripherals; see limitations for unmodeled structures.",
            "limitations": self.model["limitations"], "mapping": self.mappings,
            "inputs": {str(p): sha256(p) for p in [self.policy_path, self.model_path] +
                       [self.run_dir / f for f in ("roi.json", "roi_stats.json", "config.json", "stats.txt")]},
        }
        manifest_path = self.run_dir / "run_manifest.json"
        if manifest_path.is_file():
            manifest = read_json(manifest_path)
            require(all(manifest[k] == r[k] for k in ("benchmark", "cores", "width", "policy_sha256")),
                    "Run manifest/ROI mismatch")
            self.report["simulation_tool_commits"] = manifest["tool_commits"]
            self.report["inputs"][str(manifest_path)] = sha256(manifest_path)

    def clock_period(self, obj):
        clock = self.index[obj["clk_domain"]]
        require(clock["type"] == "SrcClockDomain" and len(clock["clock"]) == 1,
                "Only fixed source clocks are supported")
        return clock["clock"][0]

    def stat(self, name):
        require(name in self.stats, f"Missing required ROI statistic: {name}")
        value = self.stats[name]
        require(math.isfinite(value) and value >= 0, f"Invalid ROI statistic: {name}={value}")
        return value

    def vector(self, prefix):
        result = {k[len(prefix):]: v for k, v in self.stats.items() if k.startswith(prefix)}
        require("total" in result, f"Missing vector total: {prefix}")
        return result

    def add(self, component, kind, name, value, source):
        require(not any(e.tag == kind and e.get("name") == name for e in component),
                f"Duplicate XML field: {component.get('id')}.{name}")
        if isinstance(value, (float, int)):
            require(math.isfinite(value) and value >= 0, f"Invalid XML number: {name}={value}")
        encoded = ",".join(map(str, value)) if isinstance(value, (list, tuple)) else str(value)
        ET.SubElement(component, kind, name=name, value=encoded)
        self.mappings.append({"component": component.get("id"), "kind": kind,
                              "name": name, "value": value, "source": source})

    def fields(self, component, kind, values, source):
        for name, value in values.items():
            self.add(component, kind, name, value, source)

    def component(self, parent, name):
        ident = name if parent.get("id") == "root" else parent.get("id") + "." + name
        return ET.SubElement(parent, "component", id=ident, name=name)

    def mapped(self, component, target, stat_name):
        self.add(component, "stat", target, self.stat(stat_name), "stats.txt:" + stat_name)

    def cache(self, parent, name, config, controller, read_fraction):
        node = self.component(parent, name)
        level = "L2" if name.startswith("L2") else name
        cfg = self.model["cache"]
        require(not controller.get("enable_prefetch", False), "Prefetching is not modeled")
        require(self.clock_period(controller) == self.period, "Cache/core clocks differ")
        latency = controller["l2_response_latency" if level == "L2" else "l1_response_latency"]
        data = [config["size"], self.ruby["block_size_bytes"], config["assoc"],
                config["dataArrayBanks"], cfg["throughput_cycles"], latency, cfg["output_width_bits"],
                0 if level == "icache" else 1]
        self.add(node, "param", level + "_config", data,
                 f"config.json:{config['path']}; controller response latency; model.cache throughput/output width/write policy")
        self.add(node, "param", "buffer_sizes", cfg["buffer_sizes"], "model.cache: physical buffer approximation")
        if level == "L2":
            self.fields(node, "param", {"clockrate": self.mhz, "ports": cfg["ports"],
                                        "device_type": 0, "merged_dir": cfg["merged_dir"],
                                        "vdd": self.policy["power"]["voltage_v"]},
                        "model.cache + policy.power; clock from config.json")
        path = config["path"]
        hits, misses = self.stat(path + ".m_demand_hits"), self.stat(path + ".m_demand_misses")
        require(hits + misses == self.stat(path + ".m_demand_accesses"),
                f"Cache demand counter mismatch: {path}")
        accesses = hits + misses
        # Preserve exact total accesses/misses without pretending to know the split.
        values = {"read_accesses": accesses * read_fraction, "read_misses": misses * read_fraction,
                  "conflicts": 0}
        if level != "icache":
            values.update(write_accesses=accesses * (1-read_fraction), write_misses=misses * (1-read_fraction))
        if level == "L2":
            values["duty_cycle"] = min(1, accesses / self.native_cycles)
        self.fields(node, "stat", values,
                    f"stats.txt:{path}.m_demand_hits/m_demand_misses; read_fraction={read_fraction}; conflicts unavailable, set 0 (not measured)")
        return values["read_accesses"], values.get("write_accesses", 0)

    def core(self, system, index, config, l1):
        core = self.component(system, f"core{index}")
        self.fields(core, "param", self.model["core"], "model.core: explicit physical representation assumptions")
        p = config["path"] + "."
        params = {"clock_rate": self.mhz, "vdd": self.policy["power"]["voltage_v"],
                  "number_hardware_threads": config["numThreads"],
                  "fetch_width": config["fetchWidth"], "decode_width": config["decodeWidth"],
                  "issue_width": config["issueWidth"], "peak_issue_width": config["issueWidth"],
                  "commit_width": config["commitWidth"], "ROB_size": config["numROBEntries"],
                  "phy_Regs_IRF_size": config["numPhysIntRegs"], "phy_Regs_FRF_size": config["numPhysFloatRegs"],
                  "instruction_buffer_size": config["fetchQueueSize"],
                  "load_buffer_size": config["LQEntries"], "store_buffer_size": config["SQEntries"],
                  "RAS_size": config["branchPred"]["ras"]["numEntries"]}
        queues = objects(config["instQueues"])
        require(len(queues) == 1, "Only a single shared instruction queue is supported")
        iq = queues[0]["numEntries"]
        require(iq >= 2, "Instruction queue is too small to split")
        params.update(instruction_window_size=iq-iq//2, fp_instruction_window_size=iq//2)
        fus = objects(queues[0]["fuPool"]["FUList"])
        alu = mul = fpu = memory = 0
        for fu in fus:
            ops = {o["opClass"] for o in objects(fu["opList"])}
            if "IntAlu" in ops:
                alu += fu["count"]
            elif "IntMult" in ops:
                mul += fu["count"]
            elif any(o.startswith(("Float", "Simd")) and "Mem" not in o
                     and "Load" not in o and "Store" not in o for o in ops):
                # A predicate-only unit has no separate scalar-equivalent FU.
                if ops != {"SimdPredAlu"}:
                    fpu += fu["count"]
            if "MemRead" in ops or "MemWrite" in ops:
                memory += fu["count"]
        require(min(alu, mul, fpu, memory) > 0, "Unsupported FU pool")
        params.update(ALU_per_core=alu, MUL_per_core=mul, FPU_per_core=fpu,
                      fp_issue_width=min(config["issueWidth"], fpu), memory_ports=memory)
        self.fields(core, "param", params,
                    f"config.json:{config['path']}; shared IQ split equally; scalar/SIMD FU equivalents (see model limitations)")
        issued = partition_ops(self.vector(p + "issuedInstType_0::"))
        committed_vector = self.vector(p + "commitStats0.committedInstType::")
        committed = partition_ops(committed_vector)
        total_ops = self.stat(p + "commitStats0.numOps")
        require(total_ops == committed_vector["total"], "Committed micro-op counter mismatch")
        require(issued["integer"] + issued["floating"] == self.stat(p + "instsIssued"),
                "Issued micro-op counter mismatch")
        for name, source in {
            "total_instructions": "rename.renamedInsts", "branch_instructions": "executeStats0.numBranches",
            "branch_mispredictions": "branchPred.corrected_0::total",
            "load_instructions": "executeStats0.numLoadInsts", "store_instructions": "executeStats0.numStoreInsts",
            "committed_instructions": "commitStats0.numOps", "ROB_reads": "rob.reads", "ROB_writes": "rob.writes",
            "rename_reads": "rename.intLookups", "fp_rename_reads": "rename.fpLookups",
            "rename_writes": "executeStats0.numIntRegWrites", "fp_rename_writes": "executeStats0.numFpRegWrites",
            "inst_window_reads": "intInstQueueReads", "inst_window_writes": "intInstQueueWrites",
            "inst_window_wakeup_accesses": "intInstQueueWakeupAccesses",
            "fp_inst_window_reads": "fpInstQueueReads", "fp_inst_window_writes": "fpInstQueueWrites",
            "fp_inst_window_wakeup_accesses": "fpInstQueueWakeupAccesses",
            "float_regfile_reads": "executeStats0.numFpRegReads", "float_regfile_writes": "executeStats0.numFpRegWrites",
            "function_calls": "commitStats0.functionCalls",
        }.items():
            self.mapped(core, name, p + source)
        for action in ("Reads", "Writes"):
            value = self.stat(p + "executeStats0.numIntReg" + action) + self.stat(p + "executeStats0.numCCReg" + action)
            self.add(core, "stat", "int_regfile_" + action.lower(), value,
                     p + "executeStats0.numIntReg" + action + " + numCCReg" + action + " (CC folded into integer RF)")
        for name, value in {"int_instructions": issued["integer"], "fp_instructions": issued["floating"],
                            "committed_int_instructions": committed["integer"],
                            "committed_fp_instructions": committed["floating"],
                            "ialu_accesses": issued["alu"], "mul_accesses": issued["mul"], "fpu_accesses": issued["fpu"],
                            "cdb_alu_accesses": issued["alu"], "cdb_mul_accesses": issued["mul"],
                            "cdb_fpu_accesses": issued["fpu"]}.items():
            self.add(core, "stat", name, value, p + "issuedInstType_0 / commitStats0.committedInstType; partition/proxy in converter")
        observed = self.stat(p + "numCycles")
        idle = self.stat(p + "idleCycles")
        # O3 adds idle intervals to both numCycles and idleCycles in wakeCPU().
        # Their difference counts active ticks; numCycles alone is not a strict
        # identity with elapsed ROI cycles around idle/wakeup transitions.
        require(idle <= observed, "Invalid O3 cycle counters: idle exceeds numCycles")
        active_cycles = observed-idle
        require(active_cycles <= self.native_cycles + 2,
                "Invalid O3 cycle counters: active ticks exceed elapsed ROI cycles")
        if observed > self.native_cycles + 2:
            self.report.setdefault("cycle_counter_notes", []).append({
                "core": config["path"], "numCycles": observed, "idleCycles": idle,
                "active_tick_cycles": active_cycles, "elapsed_roi_cycles": self.native_cycles,
                "numCycles_minus_elapsed": observed-self.native_cycles,
                "note": "Idle/wakeup bookkeeping can make numCycles exceed elapsed cycles. "
                        "Validated active ticks = numCycles-idleCycles; McPAT duration remains exact ROI time."
            })
        busy_fraction = max(0, min(1, active_cycles/self.native_cycles))
        self.fields(core, "stat", {"total_cycles": self.cycles, "busy_cycles": self.cycles*busy_fraction,
                                   "idle_cycles": self.cycles*(1-busy_fraction),
                                   "pipeline_duty_cycle": min(1, total_ops/(self.native_cycles*config["issueWidth"])),
                                   "context_switches": 0},
                    "Exact ROI duration; numCycles-idleCycles estimates busy fraction, trailing unscheduled time idle; pipeline duty=committed uops/(elapsed cycles*width); windowed-register context switches N/A")
        self.fields(core, "stat", self.model["peak_activity"], "model.peak_activity: upstream example peak factors, not measured ROI activity")
        pred = self.component(core, "predictor")
        # The upstream parser recognizes this child's name, not its id.
        pred.set("name", "PBT")
        bp = config["branchPred"]["conditionalBranchPred"]
        self.fields(pred, "param", {
            "local_predictor_size": [math.ceil(math.log2(bp["localPredictorSize"])), bp["localCtrBits"]],
            "local_predictor_entries": bp["localHistoryTableSize"],
            "global_predictor_entries": bp["globalPredictorSize"], "global_predictor_bits": bp["globalCtrBits"],
            "chooser_predictor_entries": bp["choicePredictorSize"], "chooser_predictor_bits": bp["choiceCtrBits"]},
            "config.json:" + bp["path"] + "; McPAT local predictor table representation approximation")
        for name, table in (("itlb", "itb"), ("dtlb", "dtb")):
            tlb = config["mmu"][table]
            node = self.component(core, name)
            self.add(node, "param", "number_entries", tlb["size"], "config.json:" + tlb["path"] + ".size")
            for target, suffix in (("total_accesses", "Accesses"), ("total_misses", "Misses")):
                self.add(node, "stat", target, sum(self.stat(tlb["path"] + "." + a + suffix) for a in ("rd", "wr", "ex")),
                         tlb["path"] + ".(rd+wr+ex)" + suffix)
            self.add(node, "stat", "conflicts", 0, "Not separately available; no additional conflict term")
        # McPAT parses core children by name, while system children must be in schema order.
        ir, iw = self.cache(core, "icache", l1["L1Icache"], l1, 1)
        loads = self.stat(p + "executeStats0.numLoadInsts")
        stores = self.stat(p + "executeStats0.numStoreInsts")
        demand = self.stat(l1["L1Dcache"]["path"] + ".m_demand_accesses")
        require(loads+stores > 0 or demand == 0, "Cannot estimate nonzero L1D traffic without memory ops")
        dr, dw = self.cache(core, "dcache", l1["L1Dcache"], l1, loads/(loads+stores) if loads+stores else 1)
        btb = self.component(core, "BTB")
        config_btb = config["branchPred"]["btb"]
        entry_bytes = 2 ** math.ceil(math.log2(math.ceil((config_btb["tagBits"] + 64)/8)))
        self.add(btb, "param", "BTB_config", [config_btb["numEntries"]*entry_bytes, entry_bytes,
                                               config_btb["associativity"], 1, 1, max(1, config_btb["latency"])],
                 "config.json: BTB entries/assoc/tag; approximate 64-bit targets plus tag, padded to power-of-two bytes for CACTI; one-cycle minimum")
        self.mapped(btb, "read_accesses", p + "branchPred.BTBLookups")
        self.mapped(btb, "write_accesses", p + "branchPred.BTBUpdates")
        return ir+dr, iw+dw

    def network(self, system):
        net = self.ruby["network"]
        require(net["type"] == "SimpleNetwork", "Only Ruby SimpleNetwork is supported")
        require(self.clock_period(net) == self.period, "Network/core clocks differ")
        routers = objects(net["routers"])
        # Restrict to the fully connected topology used by the project.
        links = objects(net["int_links"])
        n = len(routers)
        require(len(links) == n*(n-1), "Expected fully connected SimplePt2Pt network")
        require({(x["src_node"], x["dst_node"]) for x in links} ==
                {(a["path"], b["path"]) for a in routers for b in routers if a != b},
                "Unexpected SimpleNetwork routing graph")
        node = self.component(system, "NoC0")
        node.set("name", "noc0")  # McPAT checks this case-sensitive name.
        params = dict(self.model["noc"])
        params.update(clockrate=self.mhz, vdd=self.policy["power"]["voltage_v"],
                      horizontal_nodes=n, vertical_nodes=1,
                      input_ports=n, output_ports=n,
                      virtual_channel_per_port=self.ruby["number_of_virtual_networks"])
        self.fields(node, "param", params, "config.json: router count/degree/vnets; model.noc equivalent physical network")
        flit_bytes = params["flit_bits"] / 8
        require(flit_bytes > 0, "Invalid flit width")
        count_keys = [k for k in self.stats if k.startswith(net["path"] + ".routers")
                      and re.search(r"\.throttle\d+\.total_msg_count$", k)]
        require(len(count_keys) == n*n, "Missing router output-link statistics")
        total_flits = 0
        for count_key in count_keys:
            prefix = count_key.removesuffix("total_msg_count")
            messages, byte_sum, flits = 0, 0, 0
            for key in self.stats:
                if not key.startswith(prefix + "msg_count."):
                    continue
                count = self.stat(key)
                byte_count = self.stat(key.replace(".msg_count.", ".msg_bytes."))
                require(count > 0 or byte_count == 0, "Bytes without messages")
                size = byte_count/count if count else 0
                require(size in (0, net["control_msg_size"], net["data_msg_size"]+net["control_msg_size"]),
                        f"Unexpected SimpleNetwork message size: {size}")
                flits += count*math.ceil(size/flit_bytes)
                messages += count
                byte_sum += byte_count
            require(messages == self.stat(count_key), f"Message bin mismatch: {prefix}")
            require(byte_sum == self.stat(prefix + "total_msg_bytes"), f"Message byte mismatch: {prefix}")
            total_flits += flits
        self.add(node, "stat", "total_accesses", total_flits,
                 "Sum over all router outputs and message bins: count*ceil(bytes_per_message/assumed_flit_bytes); hop-level flits, not injected packets")
        self.add(node, "stat", "duty_cycle", min(1, total_flits/(self.native_cycles*n*n)),
                 "Estimated output-link occupancy; used for peak activity only")

    def memory(self, system):
        controllers = objects(self.board["memory"]["mem_ctrl"])
        dram = controllers[0]["dram"]
        compare = ("tCK", "device_bus_width", "devices_per_rank", "ranks_per_channel", "read_buffer_size", "write_buffer_size")
        require(all(all(c["dram"][k] == dram[k] for k in compare) for c in controllers), "Mixed DRAM controllers")
        clock_mhz = round(self.roi["ticks_per_second"]/dram["tCK"]/1e6)
        bus_bits = dram["device_bus_width"]*dram["devices_per_rank"]
        params = dict(self.model["mc"])
        params.update(mc_clock=clock_mhz, peak_transfer_rate=round(clock_mhz*2*bus_bits/8),
                      block_size=self.ruby["block_size_bytes"], number_mcs=len(controllers),
                      number_ranks=dram["ranks_per_channel"], req_window_size_per_channel=dram["read_buffer_size"],
                      IO_buffer_size_per_channel=dram["write_buffer_size"], databus_width=bus_bits,
                      vdd=self.policy["power"]["voltage_v"])
        node = self.component(system, "mc")
        self.fields(node, "param", params, "config.json: DRAM timing/width/queues; model.mc generic DDR approximation; peak_transfer_rate in MB/s per channel")
        reads = sum(self.stat(c["path"] + ".readReqs") for c in controllers)
        writes = sum(self.stat(c["path"] + ".writeReqs") for c in controllers)
        self.fields(node, "stat", {"memory_accesses": reads+writes, "memory_reads": reads, "memory_writes": writes},
                    "stats.txt: sum MemCtrl.readReqs/writeReqs across all controllers (McPAT divides by number_mcs)")
        for name, count in (("niu", "number_units"), ("pcie", "number_units"), ("flashc", "number_flashcs")):
            self.add(self.component(system, name), "param", count, 0, "Excluded peripheral")

    def build(self):
        root = ET.Element("component", id="root", name="root")
        system = self.component(root, "system")
        self.fields(system, "param", self.model["system"], "model.system: scope and physical representation")
        p = self.policy["power"]
        require(not p["dvfs"] and not p["power_gating"] and not p["temperature_leakage_feedback"],
                "Only fixed voltage/temperature with no power gating is supported")
        require(300 <= p["reference_temperature_k"] <= 400 and p["reference_temperature_k"] % 10 == 0,
                "McPAT reference temperature must be 300..400 K in 10 K steps")
        self.fields(system, "param", {"number_of_cores": len(self.cores), "number_of_L2s": len(self.l2),
                                      "number_of_NoCs": 1, "core_tech_node": p["technology_nm"],
                                      "target_core_clockrate": self.mhz, "temperature": p["reference_temperature_k"],
                                      "vdd": p["voltage_v"]}, "config.json + experiment power policy")
        self.fields(system, "stat", {"total_cycles": self.cycles, "idle_cycles": 0, "busy_cycles": self.cycles},
                    "ROI seconds * rounded McPAT MHz * 1e6; elapsed wall cycles, not sum over cores; per-core idle separately")
        read, write = 0, 0
        insts, ops = 0, 0
        for i, (c, l1) in enumerate(zip(self.cores, self.l1)):
            r, w = self.core(system, i, c, l1)
            read, write = read+r, write+w
            insts += self.stat(c["path"] + ".commitStats0.numInsts")
            ops += self.stat(c["path"] + ".commitStats0.numOps")
        require(insts == self.stat("simInsts") and ops == self.stat("simOps"),
                "O3 core totals differ from ROI totals (boot CPU activity or bad stats reset)")
        self.report["architectural_instructions"] = insts
        self.report["committed_micro_ops"] = ops
        for i, ctrl in enumerate(self.l2):
            self.cache(system, f"L2{i}", ctrl["L2cache"], ctrl, read/(read+write) if read+write else 1)
        self.network(system)
        self.memory(system)
        ET.indent(root, space="  ")
        return ET.ElementTree(root), self.report


def parse_processor_power(text):
    """Read the Processor aggregate, never sum it with its children."""
    match = re.search(r"^Processor:\s*\n(.*?)(?=^\s*Total Cores:|\Z)", text, re.M | re.S)
    require(match is not None, "McPAT output has no Processor aggregate")
    metrics = {}
    for label in ("Area", "Runtime Dynamic", "Subthreshold Leakage", "Gate Leakage"):
        value = re.search(r"^\s*" + label + r"\s*=\s*([^\s]+)", match[1], re.M)
        require(value is not None, f"McPAT output missing {label}")
        try:
            number = float(value[1])
        except ValueError as exc:
            raise ConversionError(f"Invalid McPAT {label}") from exc
        require(math.isfinite(number) and number >= 0, f"Invalid McPAT {label}={number}")
        metrics[label] = number
    require(metrics["Area"] > 0 and metrics["Runtime Dynamic"] > 0, "Empty McPAT result")
    static = metrics["Subthreshold Leakage"] + metrics["Gate Leakage"]
    return {"area_mm2": metrics["Area"], "runtime_dynamic_w": metrics["Runtime Dynamic"],
            "subthreshold_leakage_w": metrics["Subthreshold Leakage"], "gate_leakage_w": metrics["Gate Leakage"],
            "static_w": static, "total_average_w": metrics["Runtime Dynamic"]+static}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path, help="Default: RUN_DIR/mcpat")
    parser.add_argument("--policy", type=Path, default=ROOT / "configs/experiment-startup.json")
    parser.add_argument("--model", type=Path, default=ROOT / "configs/mcpat/model-v1.json")
    parser.add_argument("--run-mcpat", action="store_true", help="Also run McPAT and extract aggregate average power")
    parser.add_argument("--mcpat-binary", type=Path, default=ROOT / "external/mcpat/mcpat")
    parser.add_argument("--force", action="store_true", help="Replace converter-owned files in output directory")
    args = parser.parse_args(argv)
    try:
        output = args.output_dir or args.run_dir / "mcpat"
        owned = [output / f for f in ("input.xml", "mapping.json", "mcpat.txt", "mcpat.stderr.txt", "power.json")]
        require(args.force or not any(p.exists() for p in owned), "Converter output exists; choose a fresh --output-dir or use --force")
        if args.run_mcpat:
            require(args.mcpat_binary.is_file(), "Build McPAT first; see README.md and configs/README.md#mcpat-mapping")
        converter = Converter(args.run_dir, args.policy, args.model)
        tree, report = converter.build()
        output.mkdir(parents=True, exist_ok=True)
        for path in owned:
            path.unlink(missing_ok=True)
        tree.write(output / "input.xml", encoding="utf-8", xml_declaration=True)
        report["xml_sha256"] = sha256(output / "input.xml")
        (output / "mapping.json").write_text(json.dumps(report, indent=2) + "\n")
        print(f"Created {output / 'input.xml'} ({len(converter.cores)} cores, {converter.roi['width']}-wide, ROI {converter.seconds:.12f} s)")
        print(f"Mapping and modeling limitations: {output / 'mapping.json'}", flush=True)
        print("Pilot model: review the recorded approximations before interpreting power estimates.", flush=True)
        if args.run_mcpat:
            command = [str(args.mcpat_binary.resolve()), "-infile", str((output / "input.xml").resolve()),
                       "-print_level", "5"]
            with (output / "mcpat.txt").open("w") as stdout, (output / "mcpat.stderr.txt").open("w") as stderr:
                result = subprocess.run(command, stdout=stdout, stderr=stderr, timeout=600, check=False)
            require(result.returncode == 0, f"McPAT failed ({result.returncode}); inspect {output / 'mcpat.stderr.txt'}")
            power = parse_processor_power((output / "mcpat.txt").read_text())
            power.update(status="pilot_approximate", roi_seconds=converter.seconds,
                         roi_energy_j=power["total_average_w"]*converter.seconds,
                         reference_temperature_k=converter.policy["power"]["reference_temperature_k"],
                         voltage_v=converter.policy["power"]["voltage_v"],
                         scope=report["scope"], limitations=report["limitations"],
                         xml_sha256=report["xml_sha256"], mcpat_binary_sha256=sha256(args.mcpat_binary),
                         command=command,
                         cycle_counter_notes=report.get("cycle_counter_notes", []),
                         mcpat_warnings=[line.strip() for line in (output / "mcpat.txt").read_text().splitlines()
                                         if line.strip().startswith("Warning:")])
            (output / "power.json").write_text(json.dumps(power, indent=2) + "\n")
            print(f"ROI average power: {power['total_average_w']:.6g} W = {power['runtime_dynamic_w']:.6g} W dynamic + {power['static_w']:.6g} W static")
        return 0
    except (ConversionError, OSError, KeyError, TypeError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(f"Conversion failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
