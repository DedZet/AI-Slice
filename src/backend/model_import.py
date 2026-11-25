"""Convert supported CAD and mesh inputs to a local STL before analysis."""
import os
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZipFile

from contracts import WorkflowError

INPUT_SUFFIXES = {".stl", ".3mf", ".step", ".stp"}
MAX_3MF_EXPANDED = 128 * 1024**2
MAX_FACES = 250_000
IMPORT_TIMEOUT = 60


def source_path(folder, index, suffix=".stl"):
    stem = "source" if index == 0 else f"source-{index + 1}"
    return folder / f"{stem}{suffix}"


def validate_3mf(path):
    try:
        with ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 1024 or sum(entry.file_size for entry in entries) > MAX_3MF_EXPANDED:
                raise ValueError("3MF expanded size")
            if any(
                PurePosixPath(entry.filename).is_absolute()
                or ".." in PurePosixPath(entry.filename).parts
                or any(char in entry.filename for char in ("\\", ":"))
                or entry.flag_bits & 1
                for entry in entries
            ):
                raise ValueError("Invalid 3MF archive entry")
            if not any(entry.filename.lower().endswith(".model") for entry in entries):
                raise ValueError("Missing geometry")
    except (BadZipFile, OSError, ValueError) as exc:
        raise WorkflowError("INVALID_3MF", "Файл 3MF повреждён, слишком велик или не содержит модели.") from exc


def step_to_stl(source, target):
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.STEPControl import STEPControl_Reader
    from OCP.StlAPI import StlAPI_Writer

    reader = STEPControl_Reader()
    if reader.ReadFile(source) != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise ValueError("STEP read failed")
    reader.SetSystemLengthUnit(1.0)
    if reader.TransferRoots() < 1:
        raise ValueError("No STEP shape")
    shape = reader.OneShape()
    if shape.IsNull():
        raise ValueError("Empty STEP shape")
    mesher = BRepMesh_IncrementalMesh(shape, 0.05, False, 0.2, False)
    if not mesher.IsDone():
        raise ValueError("STEP tessellation failed")
    writer = StlAPI_Writer()
    writer.ASCIIMode = False
    if not writer.Write(shape, target):
        raise ValueError("STL export failed")


def normalize_model(source, target):
    if source == target or target.is_file():
        return
    if source.suffix == ".3mf":
        validate_3mf(source)
        from orca import find_orca
        orca = find_orca()
    try:
        with TemporaryDirectory(prefix="import-", dir=target.parent) as folder:
            work = Path(folder)
            # A fixed, local input filename avoids using user-controlled names in the converter.
            local_input = work / ("input" + source.suffix)
            shutil.copyfile(source, local_input)
            if source.suffix == ".3mf":
                command = [orca, local_input.name, "--datadir", "orca-import-data", "--arrange", "0", "--orient", "0", "--export-stl"]
            else:
                command = [sys.executable, "-B", Path(__file__).resolve(), local_input, "converted.stl"]
            result = subprocess.run(
                command, cwd=work, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=IMPORT_TIMEOUT, shell=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            converted = list((work / "stl").glob("*.stl")) if source.suffix == ".3mf" else [work / "converted.stl"]
            if result.returncode == 0 and len(converted) > 1:
                raise WorkflowError("MULTIPLE_SOLIDS", "Загрузите одну деталь в каждом файле. Отдельные детали можно выбрать одной партией.")
            if result.returncode != 0 or not converted or not converted[0].is_file():
                raise WorkflowError("MODEL_IMPORT_FAILED", f"Не удалось извлечь геометрию из {source.suffix[1:].upper()}.")
            if converted[0].stat().st_size > 84 + 50 * MAX_FACES:
                raise WorkflowError("MODEL_TOO_COMPLEX", "После импорта сетка превышает лимит в 250 000 треугольников.")
            converted[0].replace(target)
    except subprocess.TimeoutExpired as exc:
        raise WorkflowError("MODEL_IMPORT_TIMEOUT", "Истекло время преобразования модели.") from exc
    except OSError as exc:
        raise WorkflowError("MODEL_IMPORT_FAILED", "Не удалось преобразовать загруженную модель.") from exc


if __name__ == "__main__":
    step_to_stl(sys.argv[1], sys.argv[2])
