"""Bridge a Chat Completions tool request to the job-scoped MCP process."""
import json
import os
import re
import sys
from pathlib import Path

import httpx2
from mcp import Client, StdioServerParameters

from contracts import JOBS, WorkflowError, load_profile, process_summary
from orca import PROGRAM_FILES_KEYS

STEPS = ("inspect_model", "analyze_orientations", "set_parameters", "preview_model")


def local_arguments(step, results, variants, prompt, previous):
    text = prompt.lower()
    if step == "analyze_orientations":
        direction = re.search(r"(?:вдоль|по оси)\s*(?:оси\s*)?([xyzхуз])\b", text)
        if direction:
            axis = "xyz".index(direction[1].translate(str.maketrans("хуз", "xyz")))
            return {"load_model_xyz": [int(index == axis) for index in range(3)]}
        return {"load_model_xyz": previous["load_model_xyz"]} if previous else {}
    if step != "set_parameters":
        return {}
    priority = previous["variant"]["priority"] if previous else "balanced"
    target = previous["variant"]["infill_pct"] if previous else None
    for key, pattern in (
        ("strength", r"прочн|креп|strength"),
        ("quality", r"качеств|детал|гладк|уменьш\w* высот\w* слоя|quality"),
        ("speed", r"быстр|скорост|speed"),
        ("balanced", r"баланс|balanced"),
    ):
        if re.search(pattern, text):
            priority = key
            target = None
            break
    percentage = re.search(r"(\d{1,3})\s*%", text)
    if percentage:
        target = int(percentage[1])
    support = "none" if re.search(r"без поддерж|отключ\w* поддерж", text) else None
    if support is None and re.search(r"древовид|включ\w* поддерж", text):
        support = "tree"
    poses = results["analyze_orientations"]["orientations"]
    if support == "none" and any(variant.supports == "none" for variant in variants):
        poses = sorted(poses, key=lambda pose: pose["overhang_mm2"] > 1e-4)
    for pose in poses:
        eligible = [
            variant for variant in variants
            if variant.supports == "tree" or pose["overhang_mm2"] <= 1e-4
        ]
        if eligible:
            variant = min(eligible, key=lambda item: (
                support is not None and item.supports != support,
                abs(item.infill_pct - target) if target is not None else item.priority != priority,
                previous is not None and support is None and item.supports != previous["variant"]["supports"],
                item.supports == "tree",
            ))
            supports = "древовидные" if variant.supports == "tree" else "выключены"
            change = ""
            if previous:
                change = (
                    f" В предыдущем плане было {previous['variant']['infill_pct']}% заполнения "
                    f"и ориентация {previous['orientation']['id']}."
                )
            if percentage and target != variant.infill_pct:
                change += f" Для запроса {target}% выбрано ближайшее доступное заполнение."
            if support and variant.supports != support:
                change += " Запрошенный режим поддержек недоступен для выбранной ориентации."
            reasons = {
                "strength": "Больше материала во внутренних слоях и периметрах.",
                "quality": "Меньшая высота слоя помогает передать детали поверхности.",
                "speed": "Меньше проходов за счёт более высокого слоя и меньшего заполнения.",
                "balanced": "Средние значения высоты слоя и заполнения.",
            }
            return {
                "orientation_id": pose["id"], "variant_id": variant.id,
                "rationale": f"Выбран вариант «{variant.id}» с заполнением {variant.infill_pct}%, "
                f"поддержки {supports}. Контакт со столом {pose['contact_mm2']:.1f} мм². "
                f"{reasons[variant.priority]} Ориентация выбрана по геометрической оценке.{change}",
            }
    raise WorkflowError("SUPPORT_REQUIRED", "Нет подходящего варианта профиля с поддержками.")


async def ask(client, messages, spec=None):
    data = {"model": os.environ["LLM_MODEL"], "messages": messages}
    if spec:
        data.update(tools=[spec], tool_choice={"type": "function", "function": {"name": spec["function"]["name"]}})
    try:
        response = await client.post("chat/completions", json=data)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]
    except (httpx2.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise WorkflowError("LLM_FAILED", "Провайдер LLM не вернул корректный ответ.") from exc


def worker_transport(job_id, revision):
    return StdioServerParameters(
        command=sys.executable,
        args=["-B", str(Path(__file__).with_name("mcp_worker.py"))],
        env={
            "AISLICE_JOB_ID": str(job_id), "JOBS_ROOT": str(JOBS),
            "AISLICE_REVISION": str(revision),
            "ORCA_SLICER_BIN": os.getenv("ORCA_SLICER_BIN", ""),
            **{key: os.environ[key] for key in PROGRAM_FILES_KEYS if key in os.environ},
        },
    )


async def call_worker(mcp, step, job_id, args):
    result = await mcp.call_tool(step, {**args, "job_id": str(job_id)})
    if result.is_error or not isinstance(result.structured_content, dict):
        raise WorkflowError("MCP_TOOL_FAILED", f"Ошибка инструмента {step}.")
    output = result.structured_content
    if "error" in output:
        raise WorkflowError(output["error"]["code"], output["error"]["message"])
    return output


async def slice_workflow(job_id, revision):
    async with Client(worker_transport(job_id, revision), read_timeout_seconds=330) as mcp:
        return await call_worker(mcp, "slice_model", job_id, {})


async def run_workflow(job_id, folder, revision, prompt, progress, previous=None, history=()):
    profile_folder = folder / f"rev-{revision}" / "profile"
    profile = load_profile(profile_folder)
    transport = worker_transport(job_id, revision)
    mode = os.getenv("LLM_MODE", "local")
    enabled = mode == "api"
    if mode not in {"local", "api"} or (enabled and not all(
        os.getenv(key) for key in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")
    )):
        raise WorkflowError("LLM_NOT_CONFIGURED", "Проверьте конфигурацию провайдера LLM.")
    messages = [
        {"role": "system", "content": (
            "Помоги подготовить FDM-печать. Используй текущий инструмент. "
            "Выбирай только измеренные ориентации и утверждённые варианты. "
            "Для нависаний нужен вариант с древовидными поддержками. "
            "Нагрузку указывай трёхмерным вектором в осях исходного STL. "
            "При правке сохраняй ранее заданное направление, если его не просят менять. "
            "Если направление неизвестно, используй [0,0,0]. "
            "Если точного процента заполнения нет в каталоге, выбери ближайший совместимый вариант и объясни это. "
            "Поясняй выбор по-русски и отмечай компромиссы. Каталог: "
            + json.dumps({
                "printer": profile.printer, "material": profile.material,
                "variants": [
                    {**variant.model_dump(exclude={"process"}), **process_summary(profile_folder, variant.id)}
                    for variant in profile.variants
                ],
            }, ensure_ascii=False)
        )},
        *[entry for entry in history if entry.get("content")],
        {"role": "user", "content": prompt},
    ]
    if previous:
        messages.insert(1, {"role": "system", "content": "Предыдущий план для сравнения: " +
            json.dumps(previous, ensure_ascii=False)})
    outputs = {}
    headers = {"Authorization": "Bearer " + os.getenv("LLM_API_KEY", "")} if enabled else {}
    async with (
        Client(transport, read_timeout_seconds=330) as mcp,
        httpx2.AsyncClient(
            base_url=os.getenv("LLM_BASE_URL") or "http://127.0.0.1", headers=headers, timeout=35,
        ) as llm,
    ):
        tools = {tool.name: tool for tool in (await mcp.list_tools()).tools}
        for step in STEPS:
            progress(step)
            if enabled and step != "preview_model":
                tool = tools[step]
                schema = dict(tool.input_schema)
                schema["properties"] = {k: v for k, v in schema["properties"].items() if k != "job_id"}
                schema["required"] = [k for k in schema.get("required", []) if k != "job_id"]
                schema["additionalProperties"] = False
                spec = {"type": "function", "function": {
                    "name": step, "description": tool.description, "parameters": schema,
                }}
                message = await ask(llm, messages, spec)
                try:
                    call, = message["tool_calls"]
                    if call["function"]["name"] != step:
                        raise ValueError("Unexpected tool")
                    args = json.loads(call["function"]["arguments"])
                    if not isinstance(args, dict) or args.keys() - schema["properties"].keys():
                        raise ValueError("Unexpected arguments")
                    call_id = call["id"]
                except (ValueError, KeyError, TypeError) as exc:
                    raise WorkflowError("LLM_TOOL_CALL", f"Некорректные аргументы этапа {step}.") from exc
                messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": [call]})
            else:
                args = local_arguments(step, outputs, profile.variants, prompt, previous)
            output = await call_worker(mcp, step, job_id, args)
            outputs[step] = output
            if enabled and step != "preview_model":
                messages.append({
                    "role": "tool", "tool_call_id": call_id,
                    "content": json.dumps(output, ensure_ascii=False),
                })
        project = outputs["set_parameters"]
        variant = project["variant"]
        message = project["rationale"] or (
            f"Из доступных вариантов выбран «{variant['id']}» с заполнением {variant['infill_pct']}%. "
            f"Выбранная ориентация даёт {project['orientation']['contact_mm2']:.1f} мм² контакта со столом."
        )
        if enabled:
            messages.append({"role": "system", "content": json.dumps(outputs["preview_model"], ensure_ascii=False)})
            messages.append({"role": "user", "content": "Объясни выбранные настройки и причины, отличия от предыдущего плана и риски. План ожидает подтверждения, нарезка ещё не началась."})
            try:
                message = (await ask(llm, messages)).get("content") or message
            except WorkflowError:
                pass  # The selected parameters and output already exist.
    return {
        "message": message, "mode": mode, "geometry": outputs["inspect_model"],
        "project": outputs["set_parameters"], **outputs["preview_model"],
    }
