import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from agent import run_workflow
from contracts import ARTIFACTS, JOBS, PROFILES, Slug, WorkflowError, checked_profile, job_dir, read_json, save_json, snapshot_profile

MAX_UPLOAD = 16 * 1024**2
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    JOBS.mkdir(parents=True, exist_ok=True)
    app.state.worker = asyncio.Semaphore(1)
    for path in JOBS.glob("*/status.json"):
        state = read_json(path)
        if state["status"] in {"uploading", "queued", "running"}:
            save_json(path, {**state, "status": "failed", "error": {
                "code": "INTERRUPTED", "message": "Задача прервана перезапуском сервера. Загрузите модель повторно.",
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
    for folder in sorted(PROFILES.glob("*/profile.json")):
        try:
            profile, _, _, _ = checked_profile(folder.parent)
        except WorkflowError:
            logger.warning("Invalid profile catalog entry: %s", folder)
            continue
        profiles.append({
            "id": folder.parent.name, "printer": profile.printer, "material": profile.material,
            "variants": [value.model_dump(exclude={"process"}) for value in profile.variants],
        })
    return profiles


def job_state(job_id: UUID):
    try:
        return read_json(job_dir(job_id) / "status.json")
    except FileNotFoundError as exc:
        raise HTTPException(404, "Задача не найдена.") from exc


async def prepare(job_id: UUID, prompt: str):
    folder = job_dir(job_id)
    path = folder / "status.json"
    state = read_json(path)

    def progress(step):
        save_json(path, {**state, "status": "running", "step": step})

    async with app.state.worker:
        try:
            result = await run_workflow(job_id, folder, prompt, progress)
            save_json(path, {**state, "status": "review", "result": result})
        except* WorkflowError as errors:
            exc = errors.exceptions[0]
            while isinstance(exc, BaseExceptionGroup):
                exc = exc.exceptions[0]
            save_json(path, {**state, "status": "failed", "error": {"code": exc.code, "message": str(exc)}})
        except* Exception:
            logger.exception("Job %s failed", job_id)
            save_json(path, {**state, "status": "failed", "error": {
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
    state = {"job_id": str(job_id), "status": "uploading", "profile_id": profile_id, "prompt": prompt.strip()}
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
        await run_in_threadpool(snapshot_profile, profile_id, folder / "profile")
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
    background.add_task(prepare, job_id, state["prompt"])
    return state


@app.get("/api/jobs/{job_id}")
async def status(job_id: UUID):
    return job_state(job_id)


@app.post("/api/jobs/{job_id}/approve")
async def approve(job_id: UUID):
    state = job_state(job_id)
    if state["status"] not in {"review", "approved"}:
        raise HTTPException(409, "Результат ещё недоступен для подтверждения.")
    if state["status"] == "review":
        state["status"] = "approved"
        state["artifacts"] = list(ARTIFACTS)
        save_json(job_dir(job_id) / "status.json", state)
    return state


@app.get("/api/jobs/{job_id}/artifacts/{name}")
async def artifact(job_id: UUID, name: str):
    state = job_state(job_id)
    if name not in ARTIFACTS:
        raise HTTPException(404, "Артефакт не найден.")
    if state["status"] != "approved":
        raise HTTPException(409, "Подтвердите результат перед скачиванием.")
    path = job_dir(job_id) / "output" / name
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
