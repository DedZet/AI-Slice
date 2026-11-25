import unittest

import numpy as np
import trimesh

from wall_analysis import measure_walls


class WallAnalysisTest(unittest.TestCase):
    def test_detects_local_thinning_and_measures_every_face(self):
        box = trimesh.creation.box(extents=(20, 20, 2)).subdivide().subdivide()
        center = np.all(np.isclose(box.vertices, [0, 0, -1]), axis=1)
        box.vertices[center, 2] = .8
        result = measure_walls(box, .8)
        self.assertGreater(result["wall_faces_probed"], 48)
        self.assertEqual(result["wall_faces_measured"], len(box.faces))
        self.assertGreater(result["wall_samples"], len(box.faces))
        self.assertGreater(result["thin_wall_samples"], 0)
        self.assertAlmostEqual(result["min_sampled_wall_mm"], .2333333333, places=5)
