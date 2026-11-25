import asyncio
import logging
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from pydantic import Field

from agent import run_workflow, slice_workflow
from contracts import ARTIFACTS, Contract, JOBS, Slug, WorkflowError, checked_profile, job_dir, profile_manifests, read_json, save_json, snapshot_profile

MAX_UPLOAD = 16 * 1024**2
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    JOBS.mkdir(parents=True, exist_ok=True)
    app.state.worker = asyncio.Semaphore(1)
    for path in JOBS.glob("*/status.json"):
        state = read_json(path)
        if state["status"] in {"uploading", "queued", "running", "slicing"}:
            interrupted_slice = state["status"] == "slicing"
            save_json(path, {**state, "status": "review" if interrupted_slice else "failed", "error": {
                "code": "INTERRUPTED", "message": (
                    "Нарезка прервана. Можно подтвердить план снова." if interrupted_slice
                    else "Подготовка прервана перезапуском сервера."
                ),
            }})
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
def read_root():
    return {"message": "AI-SLICE API"}


@app.get("/api/health")
def health_check():
    return {"status": "ok"}


@app.get("/api/profiles")
def list_profiles():
    profiles = []
    for profile_id, manifest in profile_manifests():
        try:
            profile, _, _, _ = checked_profile(manifest.parent, manifest.name)
        except WorkflowError:
            logger.warning("Invalid profile catalog entry: %s", manifest)
            continue
        profiles.append({
            "id": profile_id, "printer": profile.printer, "material": profile.material,
            "variants": [value.model_dump(exclude={"process"}) for value in profile.variants],
        })
    return sorted(profiles, key=lambda value: (value["printer"], value["material"] != "PLA", value["material"]))


def job_state(job_id: UUID):
    try:
        return read_json(job_dir(job_id) / "status.json")
    except FileNotFoundError as exc:
        raise HTTPException(404, "Задача не найдена.") from exc


async def prepare(job_id: UUID, slicing: bool = False):
    folder = job_dir(job_id)
    path = folder / "status.json"
    state = read_json(path)

    def progress(step):
        save_json(path, {**state, "status": "running", "step": step})

    revision = state["revision"]
    failure_status = "review" if slicing else "failed"
    async with app.state.worker:
        try:
            if slicing:
                sliced = await slice_workflow(job_id, revision)
                save_json(path, {
                    **state, "status": "approved", "result": {**state["result"], **sliced},
                    "artifacts": list(ARTIFACTS),
                })
            else:
                result = await run_workflow(
                    job_id, folder, revision, state["prompt"], progress,
                    previous=state.get("result", {}).get("project"), history=state.get("history", []),
                )
                save_json(path, {**state, "status": "review", "result": {**result, "revision": revision}})

        except* WorkflowError as errors:
            exc = errors.exceptions[0]
            while isinstance(exc, BaseExceptionGroup):
                exc = exc.exceptions[0]
            save_json(path, {**state, "status": failure_status, "error": {"code": exc.code, "message": str(exc)}})
        except* Exception:
            logger.exception("Job %s failed", job_id)
            save_json(path, {**state, "status": failure_status, "error": {
                "code": "INTERNAL", "message": "Внутренняя ошибка подготовки модели.",
            }})


@app.post("/api/jobs", status_code=202)
async def create_job(
    background: BackgroundTasks,
    model: Annotated[UploadFile, File()],
    profile_id: Annotated[Slug, Form()],
    prompt: Annotated[str, Form(min_length=1, max_length=4000)],
):
    if Path(model.filename or "").suffix.lower() != ".stl":
        raise HTTPException(415, "Поддерживается STL. Координаты интерпретируются как миллиметры.")
    if not prompt.strip():
        raise HTTPException(422, "Введите пожелания к печати.")

    job_id = uuid4()
    folder = job_dir(job_id)
    folder.mkdir()
    state = {"job_id": str(job_id), "status": "uploading", "revision": 1, "profile_id": profile_id, "prompt": prompt.strip()}
    path = folder / "status.json"
    save_json(path, state)
    try:
        if model.size is not None and model.size > MAX_UPLOAD:
            raise HTTPException(413, "Лимит загрузки STL составляет 16 МиБ.")
        source = folder / "source.stl"
        total = 0
        with source.open("wb") as stream:
            while chunk := await model.read(256 * 1024):
                total += len(chunk)
                if total > MAX_UPLOAD:
                    raise HTTPException(413, "Лимит загрузки STL составляет 16 МиБ.")
                stream.write(chunk)
        if total == 0:
            raise HTTPException(422, "Файл STL пуст.")
        revision_folder = folder / "rev-1"
        revision_folder.mkdir()
        await run_in_threadpool(snapshot_profile, profile_id, revision_folder / "profile")
    except WorkflowError as exc:
        save_json(path, {**state, "status": "failed", "error": {"code": exc.code, "message": str(exc)}})
        raise HTTPException(422, {"code": exc.code, "message": str(exc)}) from exc
    except HTTPException as exc:
        (folder / "source.stl").unlink(missing_ok=True)
        save_json(path, {**state, "status": "failed", "error": {"code": "UPLOAD_REJECTED", "message": exc.detail}})
        raise
    finally:
        await model.close()
    state["status"] = "queued"
    save_json(path, state)
    background.add_task(prepare, job_id)
    return state


@app.get("/api/jobs/{job_id}")
async def status(job_id: UUID):
    return job_state(job_id)


class Approval(Contract):
    revision: Annotated[int, Field(ge=1)]


class RevisionRequest(Approval):
    prompt: Annotated[str, Field(min_length=1, max_length=4000)]
    profile_id: Slug | None = None


@app.post("/api/jobs/{job_id}/revise", status_code=202)
async def revise(job_id: UUID, change: RevisionRequest, background: BackgroundTasks):
    state = job_state(job_id)
    if state["status"] not in {"review", "failed"} or state.get("revision") != change.revision:
        raise HTTPException(409, "Обновите задачу, чтобы редактировать текущую версию.")
    if not change.prompt.strip():
        raise HTTPException(422, "Опишите, что нужно изменить.")
    if change.revision >= 20:
        raise HTTPException(409, "Достигнут лимит версий задачи.")
    history = [*state.get("history", []), {"role": "user", "content": state["prompt"]}]
    result = state.get("result")
    if result and result["revision"] == change.revision:
        history.append({"role": "assistant", "content": result["message"]})
    revision = change.revision + 1
    folder = job_dir(job_id)
    revision_folder = folder / f"rev-{revision}"
    revision_folder.mkdir(exist_ok=True)
    current_profile = folder / f"rev-{change.revision}" / "profile"
    profile_id = change.profile_id or state["profile_id"]
    try:
        if profile_id == state["profile_id"] and current_profile.is_dir():
            shutil.copytree(current_profile, revision_folder / "profile")
        else:
            snapshot_profile(profile_id, revision_folder / "profile")
    except WorkflowError as exc:
        raise HTTPException(422, {"code": exc.code, "message": str(exc)}) from exc
    state.pop("error", None)
    state.pop("approved_revision", None)
    state.update(status="queued", revision=revision, profile_id=profile_id, prompt=change.prompt.strip(), history=history)
    save_json(folder / "status.json", state)
    background.add_task(prepare, job_id)
    return state


@app.post("/api/jobs/{job_id}/approve", status_code=202)
async def approve(job_id: UUID, approval: Approval, background: BackgroundTasks):
    state = job_state(job_id)
    if state.get("revision") != approval.revision:
        raise HTTPException(409, "Версия плана изменилась. Проверьте актуальный предпросмотр.")
    if state["status"] in {"slicing", "approved"}:
        return state
    if state["status"] != "review":
        raise HTTPException(409, "План ещё недоступен для подтверждения.")
    state.pop("error", None)
    state.update(status="slicing", approved_revision=approval.revision, step="slice_model")
    save_json(job_dir(job_id) / "status.json", state)
    background.add_task(prepare, job_id, slicing=True)
    return state


@app.get("/api/jobs/{job_id}/preview/{revision}.stl")
async def preview(job_id: UUID, revision: int):
    state = job_state(job_id)
    if not 1 <= revision <= state.get("result", {}).get("revision", 0):
        raise HTTPException(404, "Предпросмотр не найден.")
    path = job_dir(job_id) / f"rev-{revision}" / "preview.stl"
    if not path.is_file():
        raise HTTPException(404, "Предпросмотр не найден.")
    return FileResponse(path, media_type="model/stl")


@app.get("/api/jobs/{job_id}/artifacts/{name}")
async def artifact(job_id: UUID, name: str):
    state = job_state(job_id)
    if name not in ARTIFACTS:
        raise HTTPException(404, "Артефакт не найден.")
    if state["status"] != "approved":
        raise HTTPException(409, "Подтвердите результат перед скачиванием.")
    folder = job_dir(job_id)
    if "revision" in state:
        folder /= f"rev-{state['revision']}"
    path = folder / "output" / name
    if not path.is_file():
        raise HTTPException(404, "Артефакт не найден.")
    return FileResponse(path, filename=name, media_type="application/octet-stream")



@app.websocket("/api/jobs/{job_id}/events")
async def events(socket: WebSocket, job_id: UUID):
    await socket.accept()
    previous = None
    try:
        while True:
            state = job_state(job_id)
            if state != previous:
                await socket.send_json(state)
                previous = state
            if state["status"] in {"review", "approved", "failed"}:
                await socket.close()
                return
            try:
                await asyncio.wait_for(socket.receive_text(), timeout=1)
            except TimeoutError:
                pass
    except HTTPException:
        await socket.close(code=1008, reason="Unknown job")
    except WebSocketDisconnect:
        pass
