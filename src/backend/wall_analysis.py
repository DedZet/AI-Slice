"""Estimate local normal thickness across the entire triangle mesh."""
import numpy as np

from contracts import WorkflowError

MAX_EXTRA_PROBES = 1_000_000
BATCH_SIZE = 8192


def probe_density(triangles, step):
    edges = np.linalg.norm(triangles - np.roll(triangles, 1, axis=1), axis=2).max(axis=1)

    def limits(spacing):
        count = np.maximum(1, np.ceil(edges / spacing).astype(np.int64))
        extras = np.where(count > 1, count * (count + 1) // 2, 0)
        return count, extras.sum()

    count, extras = limits(step)
    if extras <= MAX_EXTRA_PROBES:
        return count, step
    low, high = step, max(step, edges.max())
    for _ in range(20):
        middle = (low + high) / 2
        _, extras = limits(middle)
        if extras > MAX_EXTRA_PROBES:
            low = middle
        else:
            high = middle
    return limits(high)[0], high


def probe_batches(mesh, density):
    for start in range(0, len(mesh.faces), BATCH_SIZE):
        ids = np.arange(start, min(start + BATCH_SIZE, len(mesh.faces)))
        yield ids, mesh.triangles_center[ids]
    for count in np.unique(density):
        if count == 1:
            continue
        face_ids = np.flatnonzero(density == count)
        i, j = np.tril_indices(count)
        u, v = (j + 1 / 3) / count, (i - j + 1 / 3) / count
        weights = np.column_stack((1 - u - v, u, v))
        points_per_face = len(weights)
        faces_per_batch = max(1, BATCH_SIZE // points_per_face)
        for start in range(0, len(face_ids), faces_per_batch):
            ids = face_ids[start:start + faces_per_batch]
            points = (weights @ mesh.triangles[ids]).reshape(-1, 3)
            yield np.repeat(ids, points_per_face), points


def measure_walls(mesh, minimum_mm):
    try:
        from trimesh.ray.ray_pyembree import RayMeshIntersector
    except ImportError as exc:
        raise WorkflowError(
            "WALL_ANALYSIS_DEPENDENCY",
            "Для проверки стенок установите зависимости из src/backend/requirements.txt.",
        ) from exc

    intersector = RayMeshIntersector(mesh)
    density, spacing = probe_density(mesh.triangles, minimum_mm / 2)
    offset = max(1e-7, mesh.scale * 2e-7)
    minimum = np.inf
    measured = thin = total = 0
    covered = np.zeros(len(mesh.faces), dtype=bool)

    for face_ids, points in probe_batches(mesh, density):
        total += len(points)
        for start in range(0, len(points), BATCH_SIZE):
            origins = points[start:start + BATCH_SIZE]
            ids = face_ids[start:start + BATCH_SIZE]
            inward = -mesh.face_normals[ids]
            hit, rays, coordinates = intersector.intersects_id(
                origins + offset * inward, inward, multiple_hits=False, return_locations=True,
            )
            thickness = np.einsum("ij,ij->i", coordinates - origins[rays], inward[rays])
            exiting = np.einsum("ij,ij->i", mesh.face_normals[hit], inward[rays]) > 0.
            valid = exiting & (hit != ids[rays]) & (thickness > 0.) & np.isfinite(thickness)
            values = thickness[valid]
            covered[ids[rays[valid]]] = True
            measured += len(values)
            thin += np.count_nonzero(values < minimum_mm)
            minimum = min(minimum, np.min(values, initial=np.inf))

    return {
        "wall_samples": measured,
        "min_sampled_wall_mm": float(minimum) if measured else None,
        "thin_wall_samples": int(thin),
        "wall_faces_probed": len(mesh.faces),
        "wall_faces_measured": int(np.count_nonzero(covered)),
        "wall_unresolved_samples": total - measured,
        "wall_probe_step_mm": float(spacing),
    }
