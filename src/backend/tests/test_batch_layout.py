"""A recessed outline must fit where the two bounding rectangles cannot."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from xml.etree import ElementTree
from zipfile import ZipFile

import numpy as np
import trimesh
from fastapi.testclient import TestClient

from contracts import WorkflowError
from geometry import orientations
from main import app
from nesting import MIN_GAP_MM, pack, project
from orca import find_orca


def gear(phase):
    angles = np.linspace(0, 2 * np.pi, 24, endpoint=False) + phase
    radii = np.tile([20, 12], 12)
    xy = np.column_stack((np.cos(angles) * radii, np.sin(angles) * radii))
    vertices = np.vstack((np.c_[xy, np.zeros(24)], np.c_[xy, np.full(24, 5.)], [0, 0, 0], [0, 0, 5]))
    faces = []
    for i in range(24):
        j = (i + 1) % 24
        faces.extend(((48, j, i), (49, i + 24, j + 24), (i, j, j + 24), (i, j + 24, i + 24)))
    return trimesh.Trimesh(vertices=vertices, faces=faces, process=False)


def profile_for(width, depth):
    return SimpleNamespace(
        bed_origin_mm=(50., 50.), bed_size_mm=(width + 10, depth + 10, 200.),
        margin_mm=5., overhang_deg_from_vertical=45.,
    )


def outline_layout(meshes, profile):
    choices = [orientations(mesh, profile, (0, 0, 0), free_yaw=True) for mesh in meshes]
    return pack(meshes, choices, profile)


class BatchLayoutTest(unittest.TestCase):
    def test_three_parts_and_impossible_fit(self):
        box = trimesh.creation.box(extents=[18, 18, 18])
        layout = outline_layout([box] * 3, profile_for(41, 41))
        pieces = [project(box, np.asarray(part["model_to_plate"])) for part in layout]
        self.assertEqual(len(pieces), 3)
        for i, piece in enumerate(pieces):
            self.assertTrue(all(piece.distance(other) >= MIN_GAP_MM - 1e-5 for other in pieces[:i]))
        with self.assertRaisesRegex(WorkflowError, "Не удалось разместить"):
            outline_layout([box] * 2, profile_for(19, 19))

    def test_tilted_face_is_considered_for_stability(self):
        model = trimesh.creation.box(extents=[30, 14, 4])
        model.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 4, [0, 1, 0]))
        chosen = orientations(model, profile_for(80, 80), (0, 0, 0), priority="stability")[0]
        self.assertAlmostEqual(chosen["contact_mm2"], 420.)
        self.assertAlmostEqual(chosen["extents_mm"][2], 4.)
        self.assertAlmostEqual(chosen["overhang_mm2"], 0.)

    def test_two_gears_nest_and_slice_together(self):
        try:
            executable = find_orca()
        except WorkflowError:
            self.skipTest("OrcaSlicer is required for the integration test")

        root = Path(__file__).resolve().parents[2]
        meshes = [gear(0), gear(.04)]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            jobs = directory / "jobs"
            profiles = directory / "profiles"
            target = profiles / "small-bed"
            shutil.copytree(root / "profiles" / "creality-ender-3-v3-se", target)
            manifest = target / "profile-pla.json"
            data = json.loads(manifest.read_text())
            data.update(bed_origin_mm=[50, 50], bed_size_mm=[85, 51, 200], margin_mm=5)
            manifest.write_text(json.dumps(data), encoding="utf-8")

            with (
                patch("main.JOBS", jobs), patch("agent.JOBS", jobs), patch("contracts.JOBS", jobs),
                patch("contracts.PROFILES", profiles),
                patch.dict(os.environ, {"ORCA_SLICER_BIN": str(executable), "LLM_MODE": "local"}),
                TestClient(app) as client,
            ):
                response = client.post(
                    "/api/jobs",
                    files=[("model", (f"gear-{i}.stl", mesh.export(file_type="stl"))) for i, mesh in enumerate(meshes)],
                    data={"prompt": "Печатай вместе, без поддержек", "profile_id": "small-bed-pla"},
                )
                self.assertEqual(response.status_code, 202, response.text)
                job_id = response.json()["job_id"]
                endpoint = f"/api/jobs/{job_id}"
                state = client.get(endpoint).json()
                self.assertEqual(state["status"], "review", state)
                parts = state["result"]["project"]["orientation"]["parts"]
                self.assertEqual(len(parts), 2)
                self.assertTrue(any(part["yaw_deg"] % 90 != 0 for part in parts))
                self.assertTrue(all(abs(part["extents_mm"][2] - 5.) < 1e-4 for part in parts))
                a, b = [project(mesh, np.asarray(part["model_to_plate"])) for mesh, part in zip(meshes, parts, strict=True)]
                self.assertGreaterEqual(a.distance(b), MIN_GAP_MM - 1e-5)
                self.assertGreater(a.envelope.intersection(b.envelope).area, 0.)
                for shape in (a, b):
                    left, bottom, right, top = shape.bounds
                    self.assertTrue(55 - 1e-5 <= left < right <= 130 + 1e-5)
                    self.assertTrue(55 - 1e-5 <= bottom < top <= 96 + 1e-5)
                self.assertEqual(client.get(endpoint + "/preview/1.stl").status_code, 200)
                self.assertFalse((jobs / job_id / "rev-1" / "orca.log").exists())
                with ZipFile(jobs / job_id / "rev-1" / "plate.3mf") as prepared:
                    scene = ElementTree.fromstring(prepared.read("3D/3dmodel.model"))
                    self.assertEqual(len(scene.findall("./{*}build/{*}item")), 2)

                self.assertEqual(client.post(endpoint + "/approve", json={"revision": 1}).status_code, 202)
                state = client.get(endpoint).json()
                self.assertEqual(state["status"], "approved", state)
                output = jobs / job_id / "rev-1" / "output" / "result.gcode.3mf"
                with ZipFile(output) as sliced:
                    plate = json.loads(sliced.read("Metadata/plate_1.json"))
                    self.assertEqual(len(plate["bbox_objects"]), 2)
                    self.assertFalse(plate["is_seq_print"])
                self.assertEqual(client.get(endpoint + "/artifacts/result.gcode").status_code, 200)
