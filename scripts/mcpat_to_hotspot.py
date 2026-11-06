#!/usr/bin/env python3
"""Convert the project's McPAT report to block-model HotSpot steady-state inputs."""

import argparse
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

from gem5_to_mcpat import ConversionError, ROOT, read_json, require, sha256


METRICS = {"Area": "area_mm2", "Runtime Dynamic": "runtime_dynamic_w",
           "Subthreshold Leakage": "subthreshold_leakage_w", "Gate Leakage": "gate_leakage_w"}


def metric_run(text):
    """Read a component's own metric paragraph, excluding all nested children."""
    values = {}
    indent = None
    for line in text.splitlines():
        if not line.strip():
            if values:
                break
            continue
        match = re.match(r"^(\s+)([^=]+?)\s*=\s*(\S+)\s+(\S+)\s*$", line)
        if match is None:
            if values:
                break
            require(line.strip().startswith("Device Type="), f"Expected McPAT metrics, found: {line.strip()}")
            continue
        label = match[2].strip()
        if indent is not None and len(match[1]) != indent:
            break
        if label not in METRICS:
            continue  # Peak Dynamic/Peak Power/Total Leakage are not added again.
        indent = len(match[1])
        require(label not in values, f"Duplicate McPAT metric: {label}")
        value = float(match[3])
        require(math.isfinite(value) and value >= 0, f"Invalid McPAT {label}: {value}")
        require(match[4] == ("mm^2" if label == "Area" else "W"), f"Wrong McPAT units for {label}")
        values[label] = value
    require(set(values) == set(METRICS), "McPAT component lacks area or runtime/leakage metrics; use -print_level 5")
    return {METRICS[k]: v for k, v in values.items()}


def parse_mcpat(text):
    """Parse repeated detailed sections and the Processor's disjoint summaries."""
    sections = {}
    allowed = {"Processor:", "Core:", "L2", "NOC", "Memory Controller:"}
    for chunk in re.split(r"^\*{10,}\s*$", text, flags=re.M):
        lines = chunk.strip("\r\n").splitlines()
        if not lines:
            continue
        header = lines[0].strip()
        if header in allowed:
            sections.setdefault(header, []).append("\n".join(lines[1:]))
        elif re.match(r"^(Core|L[123]|NOC|Memory Controller|L[12]Directory|NIU|PCIe|Flash)", header):
            raise ConversionError(f"Unsupported McPAT section: {header}")
    require(len(sections.get("Processor:", [])) == 1, "Expected one McPAT Processor aggregate")
    processor = sections["Processor:"][0]
    result = {"processor": metric_run(processor), "cores": [], "l2": [], "noc": []}
    for key, header in (("cores", "Core:"), ("l2", "L2"), ("noc", "NOC")):
        for chunk in sections.get(header, []):
            item = {"metrics": metric_run(chunk)}
            if key == "cores":
                for child, label in (("l1i", "Instruction Cache"), ("l1d", "Data Cache")):
                    match = re.search(r"^\s+" + label + r":\s*\n", chunk, re.M)
                    require(match is not None, f"Missing core {label}; use McPAT -print_level 5")
                    item[child] = metric_run(chunk[match.end():])
            result[key].append(item)
    for key, label in (("core_summary", "Total Cores:"), ("l2_summary", "Total L2s:"),
                       ("noc_summary", "Total NoCs (Network/Bus):"), ("mc_summary", "Total MCs:")):
        match = re.search(r"^  " + re.escape(label) + r"[^\n]*\n(.*?)(?=^  Total |\Z)", processor, re.M | re.S)
        require(match is not None, f"Missing McPAT summary {label}")
        result[key] = metric_run(match[1])
    require(sections.get("Memory Controller:"), "Missing memory-controller detail")
    return result


def close(actual, expected, tolerance, label):
    require(math.isclose(actual, expected, rel_tol=tolerance["relative_tolerance"],
                         abs_tol=tolerance["absolute_tolerance"]),
            f"Conservation mismatch for {label}: {actual} vs {expected}")


def sum_metrics(items):
    return {key: sum(x[key] for x in items) for key in METRICS.values()}


def make_block(name, category, metrics, source):
    require(metrics["area_mm2"] > 0, f"Block has no positive area: {name}")
    require(all(math.isfinite(v) and v >= 0 for v in metrics.values()), f"Invalid block metrics: {name}")
    static = metrics["subthreshold_leakage_w"] + metrics["gate_leakage_w"]
    return {"name": name, "category": category, **metrics, "static_w": static,
            "total_average_w": metrics["runtime_dynamic_w"]+static, "source": source}


def disjoint_blocks(parsed, ncores, nl2, tolerance):
    require(len(parsed["cores"]) == ncores, "McPAT detailed core count differs from input XML")
    require(len(parsed["l2"]) == nl2, "McPAT detailed L2 count differs from input XML")
    require(len(parsed["noc"]) == 1, "Expected one detailed NoC")
    for key, summary in (("cores", "core_summary"), ("l2", "l2_summary"), ("noc", "noc_summary")):
        sums = sum_metrics([x["metrics"] for x in parsed[key]])
        for metric, value in sums.items():
            close(value, parsed[summary][metric], tolerance, f"{key}.{metric}")
    blocks = []
    groups = []
    for i, core in enumerate(parsed["cores"]):
        logic = {}
        for key, total in core["metrics"].items():
            residual = total-core["l1i"][key]-core["l1d"][key]
            require(residual >= 0, f"Core {i} has negative residual {key}; cache/core mapping is inconsistent")
            logic[key] = residual
        group = [make_block(f"core{i}_logic", "core_logic", logic, f"Core[{i}] minus its Instruction Cache and Data Cache"),
                 make_block(f"core{i}_l1i", "l1_cache", core["l1i"], f"Core[{i}]/Instruction Cache"),
                 make_block(f"core{i}_l1d", "l1_cache", core["l1d"], f"Core[{i}]/Data Cache")]
        groups.append(group)
        blocks.extend(group)
    for i, item in enumerate(parsed["l2"]):
        block = make_block(f"l2_bank{i}", "l2_cache", item["metrics"], f"Detailed L2[{i}]")
        blocks.append(block)
        groups.append([block])
    for name, category, values, source in (
        ("noc", "interconnect", parsed["noc"][0]["metrics"], "Detailed NOC, whole equivalent network"),
        ("memory_controllers", "memory_controller", parsed["mc_summary"], "Processor/Total MCs aggregate; detail mixes per-controller area/leakage with aggregate runtime power"),
    ):
        block = make_block(name, category, values, source)
        blocks.append(block)
        groups.append([block])
    totals = sum_metrics(blocks)
    for key, value in totals.items():
        close(value, parsed["processor"][key], tolerance, f"chip.{key}")
    return blocks, groups, totals


def bisect_rectangles(items, area, bounds, place):
    """Ordered area-balanced cuts along the longer axis, with exact coverage."""
    if len(items) == 1:
        place(items[0], bounds)
        return
    weights = [area(x) for x in items]
    total = sum(weights)
    index = min(range(1, len(items)), key=lambda i: abs(sum(weights[:i])-total/2))
    fraction = sum(weights[:index])/total
    x, y, width, height = bounds
    if width >= height:
        left, right = (x, y, width*fraction, height), (x+width*fraction, y, width*(1-fraction), height)
    else:
        left, right = (x, y, width, height*fraction), (x, y+height*fraction, width, height*(1-fraction))
    bisect_rectangles(items[:index], area, left, place)
    bisect_rectangles(items[index:], area, right, place)


def floorplan(groups, model):
    cfg = model["floorplan"]
    require(cfg["method"] == "recursive_area_bisection", "Unsupported floorplan method")
    require(cfg["area_scale"] == 1, "This pilot requires unit area scale")
    require(math.isfinite(cfg["die_aspect_ratio"]) and cfg["die_aspect_ratio"] > 0, "Invalid die aspect ratio")
    area_m2 = sum(b["area_mm2"] for group in groups for b in group)*1e-6
    width = math.sqrt(area_m2*cfg["die_aspect_ratio"])
    height = area_m2/width
    limit = min(model["hotspot"]["s_spreader"], model["hotspot"]["s_sink"])
    require(max(width, height) < limit, "Area-derived die does not fit the fixed package; explicitly revise the thermal model for all configurations")
    def place_block(block, bounds):
        x, y, w, h = bounds
        block.update(x_m=x, y_m=y, width_m=w, height_m=h)
    def place_group(group, bounds):
        bisect_rectangles(group, lambda b: b["area_mm2"], bounds, place_block)
    bisect_rectangles(groups, lambda g: sum(b["area_mm2"] for b in g), (0, 0, width, height), place_group)
    validate_geometry([b for g in groups for b in g], width, height)
    return {"width_m": width, "height_m": height, "area_mm2": area_m2*1e6}


def validate_geometry(blocks, width, height):
    require(len({b["name"] for b in blocks}) == len(blocks), "Duplicate floorplan block name")
    for i, b in enumerate(blocks):
        x, y, w, h = (b[k] for k in ("x_m", "y_m", "width_m", "height_m"))
        require(all(math.isfinite(v) for v in (x, y, w, h)) and w > 0 and h > 0 and x >= 0 and y >= 0,
                f"Invalid floorplan rectangle: {b['name']}")
        require(x+w <= width+1e-12 and y+h <= height+1e-12, "Floorplan exceeds die bounds")
        require(math.isclose(w*h, b["area_mm2"]*1e-6, rel_tol=1e-10), "Floorplan block area mismatch")
        for other in blocks[:i]:
            overlap_x = min(x+w, other["x_m"]+other["width_m"])-max(x, other["x_m"])
            overlap_y = min(y+h, other["y_m"]+other["height_m"])-max(y, other["y_m"])
            require(overlap_x <= 1e-12 or overlap_y <= 1e-12, "Overlapping floorplan blocks")
    require(math.isclose(sum(b["width_m"]*b["height_m"] for b in blocks), width*height, rel_tol=1e-10),
            "Floorplan does not cover the die")


def parse_temperatures(text, blocks, ambient_k):
    values = {}
    names = {b["name"] for b in blocks}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split()
        require(len(fields) == 2, "Invalid HotSpot steady-state row")
        name, encoded = fields
        value = float(encoded)
        require(name not in values, f"Duplicate HotSpot temperature: {name}")
        require(math.isfinite(value) and value > 0, f"Invalid HotSpot temperature: {name}")
        require(name in names or any(name == prefix+n for prefix in ("iface_", "hsp_", "hsink_") for n in names)
                or re.fullmatch(r"inode_\d+", name), f"Unknown HotSpot temperature node: {name}")
        values[name] = value
    require(names <= set(values), "HotSpot output is missing silicon block temperatures")
    for name in names:
        require(values[name] >= ambient_k-0.02, f"Silicon below ambient with nonnegative power: {name}")
    peak = max((b["name"] for b in blocks), key=lambda n: values[n])
    per_block = [{"name": b["name"], "category": b["category"], "temperature_k": values[b["name"]],
                  "temperature_c": round(values[b["name"]]-273.15, 2)} for b in blocks]
    return {"maximum_steady_state_silicon_celsius": round(values[peak]-273.15, 2),
            "maximum_steady_state_silicon_kelvin": values[peak], "hottest_block": peak,
            "blocks": per_block, "temperature_precision_k": 0.01,
            "non_silicon_nodes_excluded_from_maximum": len(values)-len(names)}


class Converter:
    def __init__(self, run_dir, policy_path, model_path):
        self.run_dir = Path(run_dir)
        self.mcpat_dir = self.run_dir / "mcpat"
        self.policy_path, self.model_path = Path(policy_path), Path(model_path)
        self.policy, self.model = read_json(self.policy_path), read_json(self.model_path)
        require(self.model["schema_version"] == 1, "Unsupported thermal model schema")
        self.roi = read_json(self.run_dir / "roi.json")
        self.power = read_json(self.mcpat_dir / "power.json")
        self.mcpat_mapping = read_json(self.mcpat_dir / "mapping.json")
        require(self.roi["status"] == "roi_complete", "A completed gem5 ROI is required")
        require(self.roi["policy_sha256"] == sha256(self.policy_path), "Original simulation policy hash mismatch")
        require(self.mcpat_mapping["roi"] == self.roi, "McPAT mapping/ROI mismatch")
        require(self.mcpat_mapping["xml_sha256"] == self.power["xml_sha256"] == sha256(self.mcpat_dir / "input.xml"),
                "McPAT XML provenance mismatch")
        require(self.power["roi_seconds"] == self.roi["roi_seconds"], "McPAT/ROI duration mismatch")
        for key, field in (("reference_temperature_k", "reference_temperature_k"), ("voltage_v", "voltage_v")):
            require(self.power[key] == self.policy["power"][field], f"McPAT/policy mismatch: {key}")
        require(not self.policy["power"]["temperature_leakage_feedback"], "Only fixed-leakage thermal modeling is supported")
        thermal = self.policy["thermal"]
        require(thermal["model"] == "block" and thermal["power_input"] == "roi_average"
                and thermal["headline_metric"] == "maximum_steady_state_silicon_celsius", "Unsupported thermal policy")
        require(math.isfinite(thermal["ambient_k"]) and thermal["ambient_k"] > 0, "Invalid ambient temperature")
        tol = self.model["conservation"]
        require(0 <= tol["relative_tolerance"] <= 0.00005 and 0 <= tol["absolute_tolerance"] <= 0.000001,
                "Conservation tolerance must stay within McPAT printed-rounding allowance")
        config = self.model["hotspot"]
        require(config["model_type"] == "block" and all(config[k] == 0 for k in
                ("model_secondary", "dtm_used", "leakage_used", "package_model_used")), "Unsupported thermal feedback/package mode")
        require(config["block_omit_lateral"] == 0, "Lateral heat conduction must remain enabled")
        for key, value in config.items():
            if key.startswith(("t_", "k_", "p_", "s_", "r_", "c_")):
                require(math.isfinite(value) and value > 0, f"Invalid HotSpot physical parameter: {key}")
        require(config["s_sink"] > config["s_spreader"], "Heat sink must be larger than spreader")
        xml = ET.parse(self.mcpat_dir / "input.xml").getroot().find("component[@id='system']")
        require(xml is not None, "McPAT XML lacks system component")
        def param(node, name):
            found = node.find(f"param[@name='{name}']")
            require(found is not None, f"Missing XML parameter: {name}")
            return int(found.get("value"))
        ncores, nl2 = param(xml, "number_of_cores"), param(xml, "number_of_L2s")
        require(ncores == self.roi["cores"], "XML/ROI core count mismatch")
        require(param(xml, "number_of_NoCs") == 1, "Only a single NoC is supported")
        for name in ("number_of_L1Directories", "number_of_L2Directories", "number_of_L3s", "Private_L2",
                     "homogeneous_cores", "homogeneous_L2s", "longer_channel_device", "power_gating"):
            require(param(xml, name) == 0, f"Unsupported McPAT XML setting: {name}")
        for name, count in (("niu", "number_units"), ("pcie", "number_units"), ("flashc", "number_flashcs")):
            node = xml.find(f"component[@id='system.{name}']")
            require(node is not None and param(node, count) == 0, "Unmodeled peripheral in McPAT XML")
        mc = xml.find("component[@id='system.mc']")
        require(mc is not None and param(mc, "number_mcs") > 0, "Expected memory controllers")
        parsed = parse_mcpat((self.mcpat_dir / "mcpat.txt").read_text())
        for key, value in parsed["processor"].items():
            close(value, self.power[key], tol, f"power.json.{key}")
        expected_total = sum(parsed["processor"][k] for k in
                             ("runtime_dynamic_w", "subthreshold_leakage_w", "gate_leakage_w"))
        close(expected_total, self.power["total_average_w"], tol, "power.json.total_average_w")
        self.blocks, groups, totals = disjoint_blocks(parsed, ncores, nl2, tol)
        self.die = floorplan(groups, self.model)
        self.hotspot_config = {**config, "ambient": thermal["ambient_k"], "init_temp": thermal["ambient_k"]}
        all_limits = self.power["limitations"] + self.model["limitations"]
        self.report = {"schema_version": 1, "status": "pilot_approximate", "model": self.model["name"],
                       "roi": self.roi, "scope": self.power["scope"], "die": self.die,
                       "power_input": "roi_average_dynamic_plus_fixed_leakage_watts", "blocks": self.blocks,
                       "conservation": {"disjoint_totals": totals, "processor_totals": parsed["processor"],
                                        "difference": {k: totals[k]-parsed["processor"][k] for k in totals},
                                        "tolerance": tol, "note": "Printed rounding differences are retained; no power/area normalization."},
                       "hotspot_config": self.hotspot_config, "model_sources": self.model["sources"],
                       "limitations": all_limits, "mcpat_warnings": self.power.get("mcpat_warnings", []),
                       "inputs": {str(p): sha256(p) for p in [self.policy_path, self.model_path, self.run_dir / "roi.json"] +
                                  [self.mcpat_dir / n for n in ("power.json", "mapping.json", "input.xml", "mcpat.txt")]}}
        self.report["category_totals"] = {cat: {**sum_metrics([b for b in self.blocks if b["category"] == cat]),
                                               "total_average_w": sum(b["total_average_w"] for b in self.blocks if b["category"] == cat)}
                                          for cat in sorted({b["category"] for b in self.blocks})}

    def write(self, output):
        flp = ["# name width_m height_m left_x_m bottom_y_m; area-derived pilot floorplan"]
        for b in self.blocks:
            flp.append(b["name"] + "\t" + "\t".join(format(b[k], ".15g") for k in ("width_m", "height_m", "x_m", "y_m")))
        (output / "floorplan.flp").write_text("\n".join(flp)+"\n")
        (output / "power.ptrace").write_text("\t".join(b["name"] for b in self.blocks)+"\n" +
                                            "\t".join(format(b["total_average_w"], ".15g") for b in self.blocks)+"\n")
        (output / "hotspot.config").write_text("# Explicit fixed cooling; no leakage feedback or throttling\n" +
                                               "\n".join(f"-{k}\t{v}" for k, v in self.hotspot_config.items())+"\n")
        self.report["generated_inputs_sha256"] = {n: sha256(output / n) for n in ("floorplan.flp", "power.ptrace", "hotspot.config")}
        (output / "mapping.json").write_text(json.dumps(self.report, indent=2)+"\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path, help="Default: RUN_DIR/hotspot")
    parser.add_argument("--policy", type=Path, default=ROOT / "configs/experiment-startup.json")
    parser.add_argument("--model", type=Path, default=ROOT / "configs/hotspot/model-v1.json")
    parser.add_argument("--run-hotspot", action="store_true")
    parser.add_argument("--hotspot-binary", type=Path, default=ROOT / "external/hotspot/hotspot")
    parser.add_argument("--force", action="store_true", help="Replace only converter-owned output files")
    args = parser.parse_args(argv)
    try:
        output = args.output_dir or args.run_dir / "hotspot"
        owned = [output / n for n in ("floorplan.flp", "power.ptrace", "hotspot.config", "mapping.json",
                                      "steady.temp", "temperature.json", "hotspot.stdout.txt", "hotspot.stderr.txt")]
        require(output.resolve() != (args.run_dir / "mcpat").resolve(), "Output must not overwrite the McPAT directory")
        require(args.force or not any(p.exists() for p in owned), "Thermal output exists; choose a fresh --output-dir or use --force")
        if args.run_hotspot:
            require(args.hotspot_binary.is_file(), "Build HotSpot: make -C external/hotspot -j2 hotspot")
        converter = Converter(args.run_dir, args.policy, args.model)
        output.mkdir(parents=True, exist_ok=True)
        for path in owned:
            path.unlink(missing_ok=True)
        converter.write(output)
        total_power = sum(b["total_average_w"] for b in converter.blocks)
        print(f"Created {len(converter.blocks)} thermal blocks: {output / 'floorplan.flp'}", flush=True)
        print(f"ROI-average power {total_power:.6g} W; assumptions and conservation checks: {output / 'mapping.json'}", flush=True)
        if args.run_hotspot:
            command = [str(args.hotspot_binary.resolve()), "-c", str((output / "hotspot.config").resolve()),
                       "-f", str((output / "floorplan.flp").resolve()), "-p", str((output / "power.ptrace").resolve()),
                       "-steady_file", str((output / "steady.temp").resolve())]
            with (output / "hotspot.stdout.txt").open("w") as stdout, (output / "hotspot.stderr.txt").open("w") as stderr:
                result = subprocess.run(command, stdout=stdout, stderr=stderr, timeout=120, check=False, cwd=output.resolve())
            require(result.returncode == 0, f"HotSpot failed ({result.returncode}); inspect {output / 'hotspot.stderr.txt'}")
            temperature = parse_temperatures((output / "steady.temp").read_text(), converter.blocks,
                                             converter.hotspot_config["ambient"])
            temperature.update(status="pilot_approximate", thermal_model=converter.model["name"],
                               ambient_k=converter.hotspot_config["ambient"], total_average_power_w=total_power,
                               interpretation="Equilibrium under constant ROI-average power; maximum over modeled silicon blocks.",
                               limitations=converter.report["limitations"], mcpat_warnings=converter.report["mcpat_warnings"],
                               mapping_sha256=sha256(output / "mapping.json"), steady_file_sha256=sha256(output / "steady.temp"),
                               hotspot_binary_sha256=sha256(args.hotspot_binary), command=command)
            (output / "temperature.json").write_text(json.dumps(temperature, indent=2)+"\n")
            print(f"Maximum steady-state silicon temperature: {temperature['maximum_steady_state_silicon_celsius']:.2f} C ({temperature['hottest_block']})")
        return 0
    except (ConversionError, OSError, KeyError, TypeError, ValueError, ET.ParseError, subprocess.TimeoutExpired) as exc:
        print(f"Conversion failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
