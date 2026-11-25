"""MCP adapter. Each stdio process handles one API-created job."""
import os
from functools import wraps
from typing import Annotated, Any, Literal
from uuid import UUID

import numpy as np
import trimesh
from mcp.server.mcpserver import MCPServer
from pydantic import Field

from contracts import Slug, WorkflowError, job_dir, load_profile, process_summary, read_json, save_json
from geometry import EPS, inspect, measure_bridges, mesh_from_stl, orientations
from model_import import normalize_model, source_path
from orca import run_orca, write_plate

mcp = MCPServer("AI-SLICE")


def operation(function):
    @wraps(function)
    def invoke(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except WorkflowError as exc:
            return {"error": {"code": exc.code, "message": str(exc)}}
    return mcp.tool(structured_output=True)(invoke)


def folder_for(job_id: UUID):
    folder = job_dir(job_id) / f"rev-{int(os.environ['AISLICE_REVISION'])}"
    if str(job_id) != os.getenv("AISLICE_JOB_ID") or not (folder / "profile/profile.json").is_file():
        raise WorkflowError("JOB_NOT_FOUND", "Задача недоступна этому процессу MCP.")
    return folder


def previous(folder, filename):
    try:
        return read_json(folder / filename)
    except FileNotFoundError as exc:
        raise WorkflowError("STEP_ORDER", "Сначала выполните предыдущий этап.") from exc


def model_meshes(folder):
    state = previous(folder.parent, "status.json")
    names = state.get("models", ["Модель 1"])
    suffixes = state.get("model_suffixes", [".stl"] * len(names))
    for index, (name, suffix) in enumerate(zip(names, suffixes, strict=True)):
        target = source_path(folder.parent, index)
        try:
            normalize_model(source_path(folder.parent, index, suffix), target)
            yield name, mesh_from_stl(target)
        except WorkflowError as exc:
            raise WorkflowError(exc.code, f"Модель {index + 1} ({name}). {exc}") from exc


@operation
def inspect_model(job_id: UUID) -> dict[str, Any]:
    """Validate geometry and measure bounds, volume, mass center and wall thickness across all faces."""
    folder = folder_for(job_id)
    profile = load_profile(folder / "profile")
    models = [
        {"model_id": index, "name": name, **inspect(mesh, profile)}
        for index, (name, mesh) in enumerate(model_meshes(folder))
    ]
    if len(models) == 1:
        data = models[0]
    else:
        walls = [model["min_sampled_wall_mm"] for model in models if model["min_sampled_wall_mm"] is not None]
        data = {
            "models": models,
            "min_sampled_wall_mm": min(walls, default=None),
            "thin_wall_samples": sum(model["thin_wall_samples"] for model in models),
            "wall_faces_probed": sum(model["wall_faces_probed"] for model in models),
            "wall_faces_measured": sum(model["wall_faces_measured"] for model in models),
            "wall_unresolved_samples": sum(model["wall_unresolved_samples"] for model in models),
            "wall_probe_step_mm": max(model["wall_probe_step_mm"] for model in models),
        }
    save_json(folder / "geometry.json", data)
    return data


@operation
def analyze_orientations(
    job_id: UUID, load_model_xyz: tuple[float, float, float] = (0, 0, 0),
    priority: Literal["balanced", "stability", "min-support"] = "balanced",
) -> dict[str, Any]:
    """Rank poses by contact, stability and overhang; pack multiple models on one plate."""
    folder = folder_for(job_id)
    previous(folder, "geometry.json")
    profile = load_profile(folder / "profile")
    models = list(model_meshes(folder))
    multi = len(models) > 1
    ranked = [
        orientations(mesh, profile, load_model_xyz, free_yaw=multi, priority=priority)
        for _, mesh in models
    ]
    choices = ranked[0]
    if multi:
        from nesting import MIN_GAP_MM, pack

        parts = pack([mesh for _, mesh in models], ranked, profile)
        for part, (name, _) in zip(parts, models, strict=True):
            part["name"] = name
        choices = [{
            "id": "layout-1", "parts": parts, "minimum_gap_mm": MIN_GAP_MM,
            **{key: sum(part[key] for part in parts) for key in
               ("contact_mm2", "overhang_mm2", "support_proxy_mm3", "score")},
        }]
    save_json(folder / "orientations.json", {"load_model_xyz": load_model_xyz, "priority": priority, "choices": choices})
    brief = []
    for choice in choices:
        item = {key: value for key, value in choice.items() if key not in {"model_to_plate", "parts"}}
        if multi:
            item["parts"] = [
                {key: value for key, value in part.items() if key != "model_to_plate"} for part in choice["parts"]
            ]
        brief.append(item)
    return {"orientations": brief}


@operation
def set_parameters(
    job_id: UUID, orientation_id: Slug, variant_id: Slug,
    rationale: Annotated[str, Field(max_length=1000)] = "",
) -> dict[str, Any]:
    """Select a measured pose and one process variant from the approved catalog."""
    folder = folder_for(job_id)
    data = previous(folder, "orientations.json")
    profile = load_profile(folder / "profile")
    pose = next((item for item in data["choices"] if item["id"] == orientation_id), None)
    variant = next((item for item in profile.variants if item.id == variant_id), None)
    if pose is None or variant is None:
        raise WorkflowError("INVALID_SELECTION", "Выберите доступную ориентацию и вариант профиля.")
    if pose["overhang_mm2"] > EPS and variant.supports == "none":
        raise WorkflowError("SUPPORT_REQUIRED", "Для выбранной ориентации требуется профиль с поддержками.")
    project = {
        "material": profile.material, "printer": profile.printer, "bed_type": profile.bed_type,
        "variant": variant.model_dump(mode="json", exclude={"process"}), "orientation": pose,
        "load_model_xyz": data["load_model_xyz"], "rationale": rationale,
        "layout_priority": data.get("priority", "balanced"),
        "bed_origin_mm": profile.bed_origin_mm,
        "bed_size_mm": profile.bed_size_mm,
        "settings": process_summary(folder / "profile", variant.id),
    }
    save_json(folder / "project.json", project)
    return project


@operation
def preview_model(job_id: UUID) -> dict[str, Any]:
    """Prepare the oriented mesh and flag risks before asking the user to approve."""
    folder = folder_for(job_id)
    geometry = previous(folder, "geometry.json")
    project = previous(folder, "project.json")
    orientation = project["orientation"]
    parts = orientation.get("parts", [orientation])
    profile = load_profile(folder / "profile")
    meshes = []
    bridges = []
    for part, (_, mesh) in zip(parts, model_meshes(folder), strict=True):
        mesh.apply_transform(np.asarray(part["model_to_plate"]))
        bridges.append(measure_bridges(mesh, profile))
        meshes.append(mesh)
    write_plate(folder / "plate.3mf", meshes)
    trimesh.util.concatenate(meshes).export(folder / "preview.stl")
    risks = []
    if geometry["thin_wall_samples"]:
        risks.append("Найдены участки стенок тоньше порога профиля.")
    if geometry.get("wall_unresolved_samples"):
        risks.append("Для части точек не удалось измерить толщину. Проверьте эти участки перед печатью.")
    if any(part["contact_mm2"] <= EPS for part in parts):
        risks.append("У одной или нескольких деталей почти нет плоского контакта со столом.")
    spans = [entry["max_sampled_bridge_mm"] for entry in bridges if entry["max_sampled_bridge_mm"] is not None]
    bridge = {"bridge_samples": sum(entry["bridge_samples"] for entry in bridges),
              "max_sampled_bridge_mm": max(spans, default=None)}
    if bridge["bridge_samples"]:
        risks.append("Найдены возможные мосты. Проверьте их после нарезки.")
    return {"risks": risks, "bridge": bridge}


@operation
def slice_model(job_id: UUID) -> dict[str, Any]:
    """Slice a user-approved revision using its previously prepared plate."""
    folder = folder_for(job_id)
    project = previous(folder, "project.json")
    state = previous(folder.parent, "status.json")
    if state.get("approved_revision") != state.get("revision") or state["status"] != "slicing" or state["revision"] != int(os.environ["AISLICE_REVISION"]):
        raise WorkflowError("APPROVAL_REQUIRED", "Эта версия плана ещё не подтверждена.")
    count = len(project["orientation"].get("parts", [project["orientation"]]))
    data = {"metrics": run_orca(folder, project["variant"]["id"], model_count=count)}
    if any(value is None for value in data["metrics"].values()):
        data["slice_warning"] = "OrcaSlicer не предоставил все метрики в распознаваемом формате."
    save_json(folder / "result.json", data)
    return data


if __name__ == "__main__":
    mcp.run()
