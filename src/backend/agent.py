"""Bridge a Chat Completions tool request to the job-scoped MCP process."""
import json
import os
import sys
from pathlib import Path

import httpx2
from mcp import Client, StdioServerParameters

from contracts import JOBS, WorkflowError, load_profile

STEPS = ("inspect_model", "analyze_orientations", "set_parameters", "slice_model", "check_result")


def local_arguments(step, results, variants):
    if step != "set_parameters":
        return {}
    for pose in results["analyze_orientations"]["orientations"]:
        eligible = [
            variant for variant in variants
            if variant.supports == "tree" or pose["overhang_mm2"] <= 1e-4
        ]
        if eligible:
            variant = min(eligible, key=lambda item: (item.priority != "balanced", item.supports == "tree"))
            return {
                "orientation_id": pose["id"], "variant_id": variant.id,
                "rationale": "Выбрана ориентация по геометрической оценке и доступный вариант профиля.",
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


async def run_workflow(job_id, folder, prompt, progress):
    profile = load_profile(folder / "profile")
    transport = StdioServerParameters(
        command=sys.executable,
        args=["-B", str(Path(__file__).with_name("mcp_worker.py"))],
        env={
            "AISLICE_JOB_ID": str(job_id), "JOBS_ROOT": str(JOBS),
            "ORCA_SLICER_BIN": os.getenv("ORCA_SLICER_BIN", ""),
        },
    )
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
            "Если направление не задано, используй [0,0,0]. "
            "Поясняй выбор по-русски и отмечай компромиссы. Каталог: "
            + json.dumps({
                "printer": profile.printer, "material": profile.material,
                "variants": [v.model_dump(exclude={"process"}) for v in profile.variants],
            }, ensure_ascii=False)
        )},
        {"role": "user", "content": prompt},
    ]
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
            if enabled:
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
                args = local_arguments(step, outputs, profile.variants)
            result = await mcp.call_tool(step, {**args, "job_id": str(job_id)})
            if result.is_error or not isinstance(result.structured_content, dict):
                raise WorkflowError("MCP_TOOL_FAILED", f"Ошибка инструмента {step}.")
            output = result.structured_content
            if "error" in output:
                raise WorkflowError(output["error"]["code"], output["error"]["message"])
            outputs[step] = output
            if enabled:
                messages.append({
                    "role": "tool", "tool_call_id": call_id,
                    "content": json.dumps(output, ensure_ascii=False),
                })
        message = outputs["set_parameters"]["rationale"]
        if enabled:
            messages.append({"role": "user", "content": "Кратко объясни параметры и риски по результатам инструментов."})
            try:
                message = (await ask(llm, messages)).get("content") or message
            except WorkflowError:
                pass  # The selected parameters and output already exist.
    return {
        "message": message, "mode": mode, "geometry": outputs["inspect_model"],
        "project": outputs["set_parameters"], **outputs["check_result"],
    }
