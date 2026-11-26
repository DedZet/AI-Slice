"""Find collision-free placements using the full XY silhouette of each mesh."""
import itertools
import math
from dataclasses import dataclass
from time import monotonic

import numpy as np
import shapely
from shapely.affinity import rotate, translate
import trimesh

from contracts import WorkflowError
from geometry import EPS

MIN_GAP_MM = 2.
SUPPORT_RESERVE_MM = 2.
ANGLE_ORDER = (0, 90, 180, 270, *[angle for angle in range(0, 360, 15) if angle % 90])
BEAM_SIZE = 24
SEARCH_SECONDS = 25


@dataclass
class Candidate:
    pose: dict
    yaw: int
    matrix: np.ndarray
    footprint: shapely.Geometry
    width: float
    height: float
    reserve: float


@dataclass
class Placement:
    model_id: int
    candidate: Candidate
    x: float
    y: float
    footprint: shapely.Geometry

    def bounds(self):
        return self.x, self.y, self.x + self.candidate.width, self.y + self.candidate.height

    def report(self, shift):
        matrix = self.candidate.matrix.copy()
        matrix[:2, 3] += np.array([self.x, self.y]) + shift
        pose = self.candidate.pose
        # Compute the new center from the original pose frame, including the yaw.
        center = np.linalg.solve(np.asarray(pose["model_to_plate"]), [*pose["center_of_mass_plate_mm"], 1.])
        return {
            **pose,
            "model_id": self.model_id,
            "model_to_plate": matrix.tolist(),
            "center_of_mass_plate_mm": (matrix @ center)[:3].tolist(),
            "yaw_deg": self.candidate.yaw,
            "extents_mm": [self.candidate.width, self.candidate.height, pose["extents_mm"][2]],
            "bounds_xy_mm": (np.array(self.bounds()) + np.tile(shift, 2)).tolist(),
        }


def project(mesh, matrix):
    vertices = trimesh.transform_points(mesh.vertices, matrix)
    triangles = vertices[mesh.faces, :2]
    edges = triangles[:, 1:] - triangles[:, :1]
    area2 = edges[:, 0, 0] * edges[:, 1, 1] - edges[:, 0, 1] * edges[:, 1, 0]
    # The union of projected faces keeps the actual notches and holes.
    polygons = shapely.polygons(triangles[np.abs(area2) > 1e-12])
    return shapely.union_all(polygons, grid_size=1e-6)


def candidates(mesh, poses, size, deadline):
    options = []
    for pose in poses:
        if monotonic() >= deadline:
            break
        original = np.asarray(pose["model_to_plate"])
        outline = project(mesh, original)
        if outline.is_empty:
            continue
        reserve = SUPPORT_RESERVE_MM if pose["overhang_mm2"] > EPS else 0.
        centroid = outline.centroid.coords[0]
        outline = translate(outline, -centroid[0], -centroid[1])
        matrix = original.copy()
        matrix[:2, 3] -= centroid
        for angle in ANGLE_ORDER:
            rotated = rotate(outline, angle, origin=(0, 0))
            left, bottom, right, top = rotated.bounds
            width, height = right - left, top - bottom
            if width + 2 * reserve > size[0] + EPS or height + 2 * reserve > size[1] + EPS:
                continue
            transform = trimesh.transformations.rotation_matrix(math.radians(angle), [0, 0, 1]) @ matrix
            transform[:2, 3] -= [left, bottom]
            options.append(Candidate(
                pose, angle, transform, translate(rotated, -left, -bottom), width, height, reserve,
            ))
    return options


def start_points(candidate, placed, bed, grid):
    margin = candidate.reserve
    left, bottom = bed[0] + margin, bed[1] + margin
    right, top = bed[2] - candidate.width - margin, bed[3] - candidate.height - margin
    if right < left - EPS or top < bottom - EPS:
        return
    right, top = max(left, right), max(bottom, top)
    center_x, center_y = (left + right) / 2, (bottom + top) / 2
    xs, ys = [left, right, center_x], [bottom, top, center_y]
    for item in placed:
        a, b, c, d = item.bounds()
        gap = MIN_GAP_MM + margin + item.candidate.reserve
        xs.extend((c + gap, a - candidate.width - gap, a, c - candidate.width, (a + c - candidate.width) / 2))
        ys.extend((d + gap, b - candidate.height - gap, b, d - candidate.height, (b + d - candidate.height) / 2))

    def ordered_points(values, low, high):
        return list(dict.fromkeys(round(float(value), 6) for value in values if low - EPS <= value <= high + EPS))

    anchors_x, anchors_y = ordered_points(xs, left, right), ordered_points(ys, bottom, top)
    yield from itertools.product(anchors_x, anchors_y)
    if grid:
        scan_x = np.linspace(left, right, min(31, math.ceil((right - left) / .25) + 1))
        scan_y = np.linspace(bottom, top, min(31, math.ceil((top - bottom) / .25) + 1))
        axial_x = ordered_points([*xs, *scan_x], left, right)
        axial_y = ordered_points([*ys, *scan_y], bottom, top)
        for x, y in itertools.product(axial_x, axial_y):
            if x not in anchors_x or y not in anchors_y:
                yield x, y


def find_placements(model_id, candidate, placed, bed, grid, deadline, limit):
    found = 0
    for x, y in start_points(candidate, placed, bed, grid):
        if monotonic() >= deadline:
            return
        near = []
        for item in placed:
            left, bottom, right, top = item.bounds()
            dx = max(left - x - candidate.width, x - right, 0.)
            dy = max(bottom - y - candidate.height, y - top, 0.)
            gap = MIN_GAP_MM + candidate.reserve + item.candidate.reserve
            if math.hypot(dx, dy) < gap:
                near.append((item, gap))
        footprint = translate(candidate.footprint, x, y)
        if any(shapely.dwithin(footprint, other.footprint, gap - 1e-5) for other, gap in near):
            continue
        yield Placement(model_id, candidate, x, y, footprint)
        found += 1
        if found >= limit:
            return


def compactness(placements):
    bounds = np.array([item.bounds() for item in placements])
    width, height = bounds[:, 2:].max(axis=0) - bounds[:, :2].min(axis=0)
    return -sum(item.candidate.pose["score"] for item in placements), width * height


def center_layout(placements, bed):
    boxes = np.array([item.bounds() for item in placements])
    reserves = np.array([item.candidate.reserve for item in placements])[:, None]
    low = (boxes[:, :2] - reserves).min(axis=0)
    high = (boxes[:, 2:] + reserves).max(axis=0)
    shift = (np.array(bed[:2]) + bed[2:] - low - high) / 2
    return [item.report(shift) for item in sorted(placements, key=lambda item: item.model_id)]


def search(options, order, bed, grid, deadline):
    states = [()]
    for index, model_id in enumerate(order):
        last = index == len(order) - 1
        next_states = []
        for placed in states:
            for candidate in options[model_id]:
                if monotonic() >= deadline:
                    return None
                if not grid and candidate.yaw % 90:
                    continue
                for item in find_placements(model_id, candidate, placed, bed, grid, deadline, 1 if last else 3):
                    if last:
                        return (*placed, item)
                    next_states.append((*placed, item))
        states = sorted(next_states, key=compactness)[:BEAM_SIZE]
        if not states:
            break
    return None


def pack(meshes, poses, profile):
    """Preserve stable poses where possible; refine yaw and positions for tight fits."""
    left, bottom = profile.bed_origin_mm
    width, depth, _ = profile.bed_size_mm
    margin = profile.margin_mm
    bed = left + margin, bottom + margin, left + width - margin, bottom + depth - margin
    size = bed[2] - bed[0], bed[3] - bed[1]
    deadline = monotonic() + SEARCH_SECONDS
    options = [candidates(mesh, choices[:3], size, deadline) for mesh, choices in zip(meshes, poses, strict=True)]
    if all(options) and sum(min(item.footprint.area for item in pool) for pool in options) <= size[0] * size[1] + EPS:
        order = sorted(range(len(options)), key=lambda index: -options[index][0].footprint.area)
        # Test inexpensive arrangements first. For tight fits the full contour
        # search includes overlapping bounding rectangles and intermediate angles.
        best = [[item for item in pool if item.pose["id"] == pool[0].pose["id"]] for pool in options]
        started = monotonic()
        remaining = max(0., deadline - started)
        for pool, sequence, grid, fraction in (
            (best, order, False, .1),
            (best, order, True, .5),
            (options, order, False, .65),
            (options, order[::-1], True, 1.),
        ):
            result = search(pool, sequence, bed, grid, started + remaining * fraction)
            if result:
                return center_layout(result, bed)
    raise WorkflowError(
        "PLATE_LAYOUT_NOT_FOUND",
        "Не удалось разместить все детали на одной пластине с безопасным зазором. "
        "Выберите печатный стол побольше или разбейте модели на несколько заданий.",
    )
