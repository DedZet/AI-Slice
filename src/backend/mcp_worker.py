"""MCP adapter. Each stdio process handles one API-created job."""
import os
from functools import wraps
from typing import Annotated, Any
from uuid import UUID

import numpy as np
from mcp.server.mcpserver import MCPServer
from pydantic import Field

from contracts import Slug, WorkflowError, job_dir, load_profile, process_summary, read_json, save_json
from geometry import EPS, inspect, mesh_from_stl, orientations
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


@operation
def inspect_model(job_id: UUID) -> dict[str, Any]:
    """Validate STL and measure model bounds, volume, mass center and sampled walls."""
    folder = folder_for(job_id)
    data = inspect(mesh_from_stl(folder.parent / "source.stl"), load_profile(folder / "profile"))
    save_json(folder / "geometry.json", data)
    return data


@operation
def analyze_orientations(
    job_id: UUID, load_model_xyz: tuple[float, float, float] = (0, 0, 0),
) -> dict[str, Any]:
    """Rank printable plate poses. Use model coordinates for load; zero means unknown."""
    folder = folder_for(job_id)
    previous(folder, "geometry.json")
    profile = load_profile(folder / "profile")
    mesh = mesh_from_stl(folder.parent / "source.stl")
    choices = orientations(mesh, profile, load_model_xyz)
    save_json(folder / "orientations.json", {"load_model_xyz": load_model_xyz, "choices": choices})
    brief = [{key: value for key, value in item.items() if key != "model_to_plate"} for item in choices]
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
    mesh = mesh_from_stl(folder.parent / "source.stl")
    mesh.apply_transform(np.asarray(project["orientation"]["model_to_plate"]))
    write_plate(folder / "plate.3mf", mesh)
    mesh.export(folder / "preview.stl")
    risks = []
    if geometry["thin_wall_samples"]:
        risks.append("В выборке найдены участки тоньше порога профиля.")
    if project["orientation"]["contact_mm2"] <= EPS:
        risks.append("Плоский контакт со столом практически отсутствует.")
    bridge = project["orientation"]["bridge"]
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
    data = {"metrics": run_orca(folder, project["variant"]["id"])}
    if any(value is None for value in data["metrics"].values()):
        data["slice_warning"] = "OrcaSlicer не предоставил все метрики в распознаваемом формате."
    save_json(folder / "result.json", data)
    return data


if __name__ == "__main__":
    mcp.run()
