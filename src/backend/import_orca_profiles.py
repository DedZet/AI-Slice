"""Build AI-SLICE presets from an OrcaSlicer v2.5.2 source checkout."""
import argparse
import json
import re
from functools import lru_cache
from pathlib import Path

MACHINES = {
    "BBL": ("Bambu Lab A1", "Bambu Lab A1 mini", "Bambu Lab P1S", "Bambu Lab X1 Carbon"),
    "Creality": ("Creality Ender-3 V3 SE", "Creality Ender-3 V3 KE", "Creality K1", "Creality K1C", "Creality K1 Max"),
    "Prusa": ("Prusa MK4S", "Prusa CORE One"),
    "Anycubic": ("Anycubic Kobra 2 Neo", "Anycubic Kobra 3", "Anycubic Kobra S1"),
    "Elegoo": ("Elegoo Neptune 4", "Elegoo Neptune 4 Pro", "Elegoo Centauri Carbon"),
}
ENCLOSED = {
    "Bambu Lab P1S", "Bambu Lab X1 Carbon", "Creality K1", "Creality K1C", "Creality K1 Max",
    "Prusa CORE One", "Anycubic Kobra S1", "Elegoo Centauri Carbon",
}
MATERIALS = ("PLA", "PETG", "ABS", "ASA", "TPU", "PC", "PA")
BED_TEMPS = {
    "Textured PEI Plate": "textured_plate_temp",
    "Cool Plate": "cool_plate_temp",
    "Engineering Plate": "eng_plate_temp",
    "High Temp Plate": "hot_plate_temp",
}
TIERS = {
    "balanced": (20, "0.20", "3", "gyroid"),
    "strength": (40, "0.20", "4", "gyroid"),
    "quality": (15, "0.16", "3", "gyroid"),
    "speed": (10, "0.24", "2", "grid"),
}
METADATA = {"name", "type", "from", "inherits", "include", "instantiation", "setting_id", "renamed_from", "description"}


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@lru_cache
def records(source, vendor, kind):
    found = {}
    for namespace in ("OrcaFilamentLibrary", vendor):
        for path in sorted((source / namespace / kind).rglob("*.json")):
            value = read(path)
            if "name" in value:
                found[value["name"]] = value
    return found


def resolve(source, vendor, kind, name, stack=()):
    if name in stack:
        raise ValueError(f"Cyclic Orca preset inheritance: {name}")
    data = records(source, vendor, kind)[name]
    merged = {}
    for parent in [data.get("inherits"), *data.get("include", [])]:
        if parent:
            merged.update(resolve(source, vendor, kind, parent, (*stack, name)))
    merged.update({key: value for key, value in data.items() if key not in METADATA})
    return merged


def points(values):
    if isinstance(values, str):
        values = values.split(",")
    return [tuple(float(number) for number in point.split("x")) for point in values]


def printable_rectangle(machine):
    bed = points(machine["printable_area"])
    xs, ys = [sorted(set(values)) for values in zip(*bed)]
    if len(xs) != 2 or len(ys) != 2 or len(set(bed)) != 4:
        raise ValueError("Nonrectangular print bed")
    excluded = points(machine.get("bed_exclude_area", []))
    # The stock Kobra 3 preset excludes the ring between the two closed rectangles.
    if machine.get("printer_model") == "Anycubic Kobra 3" and len(excluded) == 10:
        inner_x, inner_y = [sorted(set(values)) for values in zip(*excluded[5:])]
        return [inner_x[0], inner_y[0]], [
            inner_x[1] - inner_x[0], inner_y[1] - inner_y[0], float(machine["printable_height"]),
        ]
    candidates = [(xs[0], ys[0], xs[1], ys[1])]
    if excluded:
        (left, right), (bottom, top) = [(min(axis), max(axis)) for axis in zip(*excluded)]
        if left < right and bottom < top:
            candidates = [
                (xs[0], ys[0], min(xs[1], left), ys[1]),
                (max(xs[0], right), ys[0], xs[1], ys[1]),
                (xs[0], ys[0], xs[1], min(ys[1], bottom)),
                (xs[0], max(ys[0], top), xs[1], ys[1]),
            ]
    x, y, right, top = max(candidates, key=lambda r: max(0, r[2] - r[0]) * max(0, r[3] - r[1]))
    if right - x <= 10 or top - y <= 10:
        raise ValueError("No supported printable rectangle")
    return [x, y], [right - x, top - y, float(machine["printable_height"])]


def filaments_for(source, vendor, machine_name, model):
    candidates = {}
    for name, raw in records(source, vendor, "filament").items():
        if raw.get("instantiation") != "true":
            continue
        if machine_name not in raw.get("compatible_printers", []):
            continue
        try:
            filament = resolve(source, vendor, "filament", name)
        except KeyError:
            continue
        for material in MATERIALS:
            if filament.get("filament_type") != [material] or material in {"ABS", "ASA", "PC", "PA"} and model not in ENCLOSED:
                continue
            score = (not name.startswith(f"Generic {material}"), len(raw["compatible_printers"]), name)
            if material not in candidates or score < candidates[material][0]:
                candidates[material] = (score, filament)
    return candidates


def export_machine(source, destination, vendor, model):
    for name, raw in records(source, vendor, "machine").items():
        if raw.get("instantiation") != "true" or "0.4" not in name or raw.get("printer_model") != model:
            continue
        machine = resolve(source, vendor, "machine", name)
        if machine.get("nozzle_diameter") != ["0.4"] or machine.get("printer_technology") != "FFF":
            continue
        process_name = machine["default_print_profile"]
        if model == "Creality K1C":
            process_name = "0.20mm Standard @Creality K1C"
        process = resolve(source, vendor, "process", process_name)
        origin, size = printable_rectangle(machine)
        filaments = filaments_for(source, vendor, name, model)
        if not filaments:
            raise ValueError("No compatible filament presets")
        folder = destination / re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")
        folder.mkdir(parents=True, exist_ok=True)
        write(folder / "machine.json", {**machine, "type": "machine", "name": name, "from": "user"})
        variants = []
        for priority, (density, height, walls, pattern) in TIERS.items():
            for supports in ("none", "tree"):
                variant_id = priority if supports == "none" else f"{priority}-tree"
                filename = f"{variant_id}.json"
                write(folder / filename, {
                    **process, "type": "process", "name": f"AI-SLICE {model} {variant_id}", "from": "user",
                    "sparse_infill_density": f"{density}%", "sparse_infill_pattern": pattern,
                    "layer_height": height, "wall_loops": walls,
                    "enable_support": "1" if supports == "tree" else "0",
                    "support_type": "tree(auto)", "support_threshold_angle": "45",
                })
                variants.append({
                    "id": variant_id, "process": filename, "priority": priority,
                    "infill_pct": density, "supports": supports,
                })
        for material, (score, filament) in filaments.items():
            slug = material.lower()
            build_plates = list(BED_TEMPS)
            if vendor != "BBL":
                build_plates.insert(0, "Cool Plate")
            bed_type = next((key for key in build_plates if float(filament.get(BED_TEMPS[key], ["0"])[0]) > 0), None)
            write(folder / f"filament-{slug}.json", {
                **filament, "type": "filament", "name": score[2], "from": "user",
            })
            write(folder / f"profile-{slug}.json", {
                "printer": f"{model} · 0.4 mm", "material": material, "bed_type": bed_type,
                "machine": "machine.json", "filament": f"filament-{slug}.json",
                "bed_origin_mm": origin, "bed_size_mm": size, "margin_mm": 5,
                "overhang_deg_from_vertical": 45, "min_wall_mm": 0.8, "max_bridge_mm": 15,
                "variants": variants,
            })
        return sorted(filaments), process_name
    raise ValueError("0.4 mm machine preset not found")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Path to OrcaSlicer resources/profiles")
    parser.add_argument("destination", type=Path, help="Path to AI-SLICE src/profiles")
    args = parser.parse_args()
    for vendor, models in MACHINES.items():
        for model in models:
            try:
                materials, process = export_machine(args.source, args.destination, vendor, model)
            except (KeyError, ValueError) as exc:
                print(f"Skipped {model}: {exc}")
            else:
                print(f"{model}: {', '.join(materials)} [{process}]")


if __name__ == "__main__":
    main()
