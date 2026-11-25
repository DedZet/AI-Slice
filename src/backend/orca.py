"""Write plate-space geometry and run a locally installed OrcaSlicer."""
import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path
from xml.etree import ElementTree
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from contracts import WorkflowError, load_profile

TIMEOUT_SECONDS = 300
CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PROGRAM_FILES_KEYS = ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)")


def write_plate(path, meshes):
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
            '</Types>'
        ))
        archive.writestr("_rels/.rels", (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="r0" Target="/3D/3dmodel.model" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
            '</Relationships>'
        ))
        with archive.open("3D/3dmodel.model", "w") as model:
            model.write(f'<model xmlns="{CORE}" unit="millimeter"><resources>'.encode())
            for number, mesh in enumerate(meshes, 1):
                model.write(f'<object id="{number}" type="model" name="AI-SLICE {number}"><mesh><vertices>'.encode())
                for x, y, z in mesh.vertices:
                    model.write(f'<vertex x="{x:.12g}" y="{y:.12g}" z="{z:.12g}"/>'.encode())
                model.write(b"</vertices><triangles>")
                for a, b, c in mesh.faces:
                    model.write(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>'.encode())
                model.write(b"</triangles></mesh></object>")
            model.write(b"</resources><build>")
            for number in range(1, len(meshes) + 1):
                model.write(f'<item objectid="{number}"/>'.encode())
            model.write(b"</build></model>")


def metrics(path):
    with path.open("rb") as stream:
        prefix = stream.read(32_768)
        stream.seek(max(0, stream.seek(0, os.SEEK_END) - 262_144))
        text = (prefix + b"\n" + stream.read()).decode("utf-8", errors="replace")

    result = {}
    for unit, key in (("g", "filament_g"), ("mm", "filament_mm")):
        match = re.search(rf"^;\s*(?:total )?filament used\s*\[{unit}\]\s*=\s*(\d+(?:\.\d+)?)\s*$", text, re.M)
        result[key] = float(match[1]) if match else None

    time = re.search(
        r"^;\s*(?:estimated printing time \(normal mode\)|estimated printing time|total estimated time|model printing time[^=\n;]*)\s*[:=]\s*([^;\r\n]+)",
        text, re.M | re.I,
    )
    result["estimated_seconds"] = None
    if time:
        units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
        parts = re.findall(r"(\d+)\s*([dhms])\b", time[1])
        if parts:
            result["estimated_seconds"] = sum(int(value) * units[unit] for value, unit in parts)
    return result


def find_orca() -> Path:
    configured = os.getenv("ORCA_SLICER_BIN", "").strip()
    if configured:
        path = Path(configured)
        if path.is_absolute() and path.is_file() and os.access(path, os.X_OK):
            return path
        raise WorkflowError(
            "ORCA_NOT_CONFIGURED",
            "ORCA_SLICER_BIN должен указывать абсолютный путь к исполняемому файлу OrcaSlicer.",
        )

    name = "orca-slicer.exe" if os.name == "nt" else "orca-slicer"
    directories = [Path(entry) for entry in os.get_exec_path()]
    if os.name == "nt":
        for key in PROGRAM_FILES_KEYS:
            if root := os.getenv(key):
                directories.append(Path(root) / "OrcaSlicer")
    for directory in directories:
        if directory.is_absolute():
            path = directory / name
            if path.is_file() and os.access(path, os.X_OK):
                return path
    raise WorkflowError(
        "ORCA_NOT_CONFIGURED",
        "Установите OrcaSlicer на машине с MCP или укажите путь в ORCA_SLICER_BIN.",
    )


def validate_batch(archive, model_count, profile):
    try:
        plate = json.loads(archive.read("Metadata/plate_1.json"))
        report = ElementTree.fromstring(archive.read("Metadata/slice_info.config"))
        flags = [
            entry.get("value") for entry in report.findall("./plate/metadata")
            if entry.get("key") == "outside"
        ]
        left, bottom, right, top = plate["bbox_all"]
        x, y = profile.bed_origin_mm
        width, depth, _ = profile.bed_size_mm
        valid = (
            len(plate["bbox_objects"]) == model_count
            and plate["is_seq_print"] is False and flags == ["false"]
            and all(math.isfinite(value) for value in (left, bottom, right, top))
            and x - .05 <= left <= right <= x + width + .05
            and y - .05 <= bottom <= top <= y + depth + .05
        )
    except (KeyError, ValueError, TypeError, ElementTree.ParseError):
        valid = False
    if not valid:
        raise WorkflowError(
            "ORCA_INVALID_PLATE",
            "OrcaSlicer не подтвердил, что все детали помещаются на одной пластине и печатаются по слоям.",
        )


def run_orca(folder, variant_id, *, model_count=1):
    executable = find_orca()
    output = folder / "output"
    output.mkdir(exist_ok=True)
    archive_path = output / "result.gcode.3mf"
    gcode_path = output / "result.gcode"
    archive_path.unlink(missing_ok=True)
    gcode_path.unlink(missing_ok=True)
    args = [
        executable, "plate.3mf",
        "--load-settings", f"profile/process-{variant_id}.json;profile/machine.json",
        "--load-filaments", "profile/filament.json",
        "--datadir", "orca-data", "--arrange", "0", "--orient", "0",
        "--slice", "0", "--outputdir", "output", "--export-3mf", archive_path.name,
    ]
    profile = load_profile(folder / "profile")
    if profile.bed_type:
        args.append(f"--curr-bed-type={profile.bed_type}")
    if model_count > 1:
        args.append("--print-sequence=by layer")
    try:
        with (folder / "orca.log").open("wb") as log:
            result = subprocess.run(
                args, cwd=folder, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, shell=False, timeout=TIMEOUT_SECONDS,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
    except subprocess.TimeoutExpired as exc:
        raise WorkflowError("ORCA_TIMEOUT", "OrcaSlicer превысил лимит времени нарезки.") from exc
    except OSError as exc:
        raise WorkflowError("ORCA_START_FAILED", "Не удалось запустить OrcaSlicer.") from exc
    if result.returncode:
        code = result.returncode
        if os.name == "nt" and code >= 2**31:
            code -= 2**32
        raise WorkflowError("ORCA_FAILED", f"OrcaSlicer завершился с кодом {code}.")
    try:
        with ZipFile(archive_path) as archive:
            entries = [item for item in archive.infolist() if item.filename.lower().endswith(".gcode")]
            if len(entries) != 1 or not 0 < entries[0].file_size <= 512 * 1024**2:
                raise ValueError("Expected one nonempty plate G-code")
            if model_count > 1:
                validate_batch(archive, model_count, profile)
            with archive.open(entries[0]) as source, gcode_path.open("wb") as sink:
                shutil.copyfileobj(source, sink)
    except (BadZipFile, OSError, ValueError) as exc:
        raise WorkflowError("ORCA_NO_RESULT", "OrcaSlicer не вернул нарезанную пластину с G-кодом.") from exc
    return metrics(gcode_path)
