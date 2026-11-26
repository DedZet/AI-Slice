"""Regression test for revision review and the OrcaSlicer approval boundary."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx2
import trimesh
from fastapi.testclient import TestClient

from contracts import WorkflowError
from main import app
from orca import find_orca


class RevisionFlowTest(unittest.TestCase):
    def test_approve_only_the_latest_preview(self):
        try:
            orca = find_orca()
        except WorkflowError:
            self.skipTest("OrcaSlicer is required for the integration test")

        root = Path(__file__).resolve().parents[2]
        factory = httpx2.AsyncClient
        tool_calls = []

        def model(request):
            payload = json.loads(request.content)
            messages = payload["messages"]
            editing = any(item["content"] == "Уменьши высоту слоя" for item in messages)
            if editing:
                self.assertTrue(any(item["content"] == "Упор на прочность" for item in messages))
                self.assertTrue(any("Предыдущий план" in (item["content"] or "") for item in messages))
            if "tools" not in payload:
                message = {"content": "Выбран меньший слой." if editing else "Увеличены заполнение и число периметров."}
            else:
                name = payload["tools"][0]["function"]["name"]
                tool_calls.append(name)
                args = {}
                if name == "set_parameters":
                    orientations = json.loads(messages[-1]["content"])["orientations"]
                    args = {
                        "orientation_id": orientations[0]["id"],
                        "variant_id": "quality" if editing else "strength",
                    }
                message = {"content": None, "tool_calls": [{
                    "id": name, "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }]}
            return httpx2.Response(200, json={"choices": [{"message": message}]})

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        jobs = Path(temporary.name)
        with (
            patch("main.JOBS", jobs),
            patch("contracts.JOBS", jobs),
            patch("agent.JOBS", jobs),
            patch("contracts.PROFILES", root / "profiles"),
            patch.dict(os.environ, {
                "ORCA_SLICER_BIN": str(orca), "LLM_MODE": "api", "LLM_MODEL": "mock",
                "LLM_API_KEY": "mock", "LLM_BASE_URL": "https://example.test/v1/",
            }),
            patch("agent.httpx2.AsyncClient", lambda **kwargs: factory(**kwargs, transport=httpx2.MockTransport(model))),
            TestClient(app) as client,
        ):
            box = trimesh.creation.box(extents=(20, 15, 10)).export(file_type="stl")
            response = client.post("/api/jobs", files={"model": ("box.stl", box)}, data={
                "prompt": "Упор на прочность", "profile_id": "bambu-lab-p1s-pla",
            })
            self.assertEqual(response.status_code, 202, response.text)
            job_id = response.json()["job_id"]
            path = f"/api/jobs/{job_id}"
            state = client.get(path).json()
            self.assertEqual(state["status"], "review", state)
            self.assertEqual(state["result"]["project"]["variant"]["infill_pct"], 40)
            self.assertEqual(client.get(path + "/preview/1.stl").status_code, 200)
            self.assertNotIn("metrics", state["result"])

            response = client.post(path + "/revise", json={
                "revision": 1, "prompt": "Уменьши высоту слоя", "profile_id": "bambu-lab-p1s-petg",
            })
            self.assertEqual(response.status_code, 202, response.text)
            state = client.get(path).json()
            self.assertEqual(state["status"], "review", state)
            project = state["result"]["project"]
            self.assertEqual(project["material"], "PETG")
            self.assertEqual(project["settings"]["layer_height"], "0.16")
            self.assertEqual(client.get(path + "/preview/2.stl").status_code, 200)
            self.assertEqual(client.post(path + "/approve", json={"revision": 1}).status_code, 409)
            self.assertEqual(client.get(path + "/artifacts/result.gcode").status_code, 409)
            self.assertFalse(list((jobs / job_id).glob("rev-*/orca.log")))

            response = client.post(path + "/approve", json={"revision": 2})
            self.assertEqual(response.status_code, 202, response.text)
            state = client.get(path).json()
            self.assertEqual(state["status"], "approved", state)
            self.assertTrue(client.get(path + "/artifacts/result.gcode.3mf").content.startswith(b"PK"))
            self.assertFalse((jobs / job_id / "rev-1" / "orca.log").exists())
            self.assertEqual(tool_calls, ["inspect_model", "analyze_orientations", "set_parameters"] * 2)
