"""Configuration and on-disk contracts shared by the API and MCP worker."""
import json
import math
import os
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

SRC = Path(__file__).resolve().parents[1]
load_dotenv(SRC / ".env")


def configured_path(key: str, default: str) -> Path:
    return (SRC / os.getenv(key, default)).resolve()


JOBS = configured_path("JOBS_ROOT", "jobs")
PROFILES = configured_path("PROFILES_ROOT", "profiles")
ARTIFACTS = ("result.gcode.3mf", "result.gcode")
BED_TEMPERATURES = {
    "Cool Plate": "cool_plate_temp",
    "Textured PEI Plate": "textured_plate_temp",
    "Engineering Plate": "eng_plate_temp",
    "High Temp Plate": "hot_plate_temp",
}
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
Positive = Annotated[float, Field(gt=0, le=2000)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Variant(Contract):
    id: Slug
    process: str
    infill_pct: Annotated[int, Field(ge=0, le=100)]
    supports: Literal["none", "tree"]
    priority: Literal["balanced", "strength", "quality", "speed"]


class Profile(Contract):
    printer: str
    material: Literal["PLA", "PETG", "ABS", "ASA", "TPU", "PC", "PA"]
    bed_type: Literal["Cool Plate", "Textured PEI Plate", "Engineering Plate", "High Temp Plate"] | None = None
    machine: str
    filament: str
    # A verified printable rectangle, in the Orca plate coordinate system.
    bed_origin_mm: tuple[float, float]
    bed_size_mm: tuple[Positive, Positive, Positive]
    margin_mm: Annotated[float, Field(ge=0, le=50)]
    overhang_deg_from_vertical: Annotated[float, Field(gt=0, lt=90)]
    min_wall_mm: Positive
    max_bridge_mm: Positive
    variants: Annotated[list[Variant], Field(min_length=1, max_length=32)]


class WorkflowError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def job_dir(job_id: UUID) -> Path:
    return JOBS / str(job_id)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_json(path: Path, value) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def load_profile(folder: Path) -> Profile:
    return Profile.model_validate(read_json(folder / "profile.json"))


def process_summary(folder: Path, variant_id: str) -> dict:
    process = read_json(folder / f"process-{variant_id}.json")
    return {key: process.get(key) for key in ("layer_height", "wall_loops", "sparse_infill_pattern", "outer_wall_speed")}


def preset(folder: Path, filename: str, kind: str) -> dict:
    if Path(filename).name != filename or Path(filename).suffix != ".json" or ";" in filename:
        raise ValueError("Invalid preset filename")
    path = folder / filename
    if path.resolve().parent != folder.resolve():
        raise ValueError("Preset must be inside the profile directory")
    settings = read_json(path)
    if settings["type"] != kind or settings["from"] not in {"system", "User", "user"}:
        raise ValueError(f"Expected {kind} Orca preset")
    if settings.get("inherits") or settings.get("post_process"):
        raise ValueError("Use resolved presets without post-processing scripts")
    return settings


def validate_bed(profile: Profile, machine: dict) -> None:
    points = {tuple(map(float, point.split("x"))) for point in machine["printable_area"]}
    xs, ys = (sorted(set(axis)) for axis in zip(*points, strict=True))
    height = float(machine["printable_height"])
    if (
        len(xs) != 2 or len(ys) != 2 or len(points) != 4 or not math.isfinite(height)
        or not all(math.isfinite(value) for value in xs + ys)
        or len(machine["nozzle_diameter"]) != 1
        or machine["printer_technology"] != "FFF"
    ):
        raise ValueError("Expected a rectangular single-nozzle FFF build volume")
    x, y = profile.bed_origin_mm
    width, depth, z = profile.bed_size_mm
    if not (xs[0] <= x < x + width <= xs[1] and ys[0] <= y < y + depth <= ys[1] and z <= height):
        raise ValueError("Catalog build volume exceeds Orca machine preset")


def profile_manifests():
    for manifest in sorted(PROFILES.glob("*/profile*.json")):
        suffix = manifest.stem.removeprefix("profile")
        if not suffix or suffix.startswith("-"):
            yield manifest.parent.name + suffix, manifest


def checked_profile(folder: Path, manifest_name: str = "profile.json") -> tuple[Profile, dict, dict, dict[str, dict]]:
    try:
        profile = Profile.model_validate(read_json(folder / manifest_name))
        machine = preset(folder, profile.machine, "machine")
        filament = preset(folder, profile.filament, "filament")
        validate_bed(profile, machine)
        if filament["filament_type"] != [profile.material]:
            raise ValueError("Filament type differs from catalog")
        if profile.bed_type and float(filament[BED_TEMPERATURES[profile.bed_type]][0]) <= 0:
            raise ValueError("Filament preset does not support this build plate")
        if 2 * profile.margin_mm >= min(profile.bed_size_mm[:2]):
            raise ValueError("Margin leaves no printable area")
        processes = {}
        for variant in profile.variants:
            process = preset(folder, variant.process, "process")
            expected_support = "1" if variant.supports == "tree" else "0"
            if (
                variant.id in processes
                or process["sparse_infill_density"] != f"{variant.infill_pct}%"
                or process["enable_support"] != expected_support
                or (expected_support == "1" and (
                    process["support_type"] != "tree(auto)"
                    or float(process["support_threshold_angle"]) != 90 - profile.overhang_deg_from_vertical
                ))
            ):
                raise ValueError("Variant metadata differs from the Orca process preset")
            processes[variant.id] = process
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        raise WorkflowError("INVALID_PROFILE", "Профиль отсутствует или содержит несогласованные настройки.") from exc
    return profile, machine, filament, processes


def snapshot_profile(profile_id: str, destination: Path) -> None:
    matches = [path for key, path in profile_manifests() if key == profile_id]
    if len(matches) != 1:
        raise WorkflowError("INVALID_PROFILE", "Профиль отсутствует или его идентификатор продублирован.")
    manifest = matches[0]
    profile, machine, filament, processes = checked_profile(manifest.parent, manifest.name)
    destination.mkdir()
    snapshot = profile.model_dump(mode="json")
    snapshot.update(machine="machine.json", filament="filament.json")
    for variant in snapshot["variants"]:
        variant["process"] = f"process-{variant['id']}.json"
    save_json(destination / "profile.json", snapshot)
    save_json(destination / "machine.json", machine)
    save_json(destination / "filament.json", filament)
    for variant_id, process in processes.items():
        save_json(destination / f"process-{variant_id}.json", process)
