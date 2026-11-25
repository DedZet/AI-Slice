"""Mesh analysis in model coordinates and orientation scoring in plate millimeters."""
import numpy as np
import trimesh

from contracts import Profile, WorkflowError

try:
    from igl import ray_mesh_intersect
except ImportError:
    ray_mesh_intersect = None

EPS = 1e-4  # mm
UP = np.array([0., 0., 1.])
AXES = np.vstack((np.eye(3), -np.eye(3)))


def mesh_from_stl(path):
    try:
        mesh = trimesh.load_mesh(path, file_type="stl", process=False)
    except (ValueError, OSError, TypeError, IndexError, OverflowError) as exc:
        raise WorkflowError("INVALID_STL", "Не удалось прочитать STL.") from exc
    if (
        not 4 <= len(mesh.faces) <= 250_000
        or not np.isfinite(mesh.vertices).all()
        or np.abs(mesh.vertices).max() > 100_000
    ):
        raise WorkflowError("INVALID_STL", "Недопустимые координаты или число треугольников STL.")
    mesh.merge_vertices()
    mesh.update_faces(mesh.nondegenerate_faces() & mesh.unique_faces())
    mesh.remove_unreferenced_vertices()
    if mesh.is_watertight and mesh.is_winding_consistent and mesh.volume < 0:
        mesh.invert()
    if len(mesh.faces) < 4 or not mesh.is_volume or not single_shell(mesh):
        raise WorkflowError("INVALID_MESH", "Требуется одна замкнутая сетка с положительным объёмом.")
    return mesh


def single_shell(mesh):
    neighbors = [[] for _ in mesh.faces]
    for left, right in mesh.face_adjacency:
        neighbors[left].append(right)
        neighbors[right].append(left)
    visited = {0}
    pending = [0]
    while pending:
        for adjacent in neighbors[pending.pop()]:
            if adjacent not in visited:
                visited.add(adjacent)
                pending.append(adjacent)
    return len(visited) == len(mesh.faces)


def first_hit(mesh, origin, ray):
    """Nearest intersection, with an optional libigl backend."""
    if ray_mesh_intersect is not None:
        hits = ray_mesh_intersect(origin, ray, mesh.vertices, mesh.faces)
        return min((distance for _, distance, _, _ in (hits or []) if distance > EPS), default=np.inf)

    triangles = mesh.triangles
    edge1 = triangles[:, 1] - triangles[:, 0]
    edge2 = triangles[:, 2] - triangles[:, 0]
    cross = np.cross(ray, edge2)
    det = np.einsum("ij,ij->i", edge1, cross)
    keep = np.abs(det) > 1e-10
    relative = origin - triangles[keep, 0]
    reciprocal = 1 / det[keep]
    u = np.einsum("ij,ij->i", relative, cross[keep]) * reciprocal
    perpendicular = np.cross(relative, edge1[keep])
    v = np.einsum("ij,j->i", perpendicular, ray) * reciprocal
    t = np.einsum("ij,ij->i", edge2[keep], perpendicular) * reciprocal
    return float(np.min(t[(u >= 0) & (v >= 0) & (u + v <= 1) & (t > EPS)], initial=np.inf))


def sample(indices, count):
    return indices[np.linspace(0, len(indices) - 1, min(count, len(indices)), dtype=int)]


def inspect(mesh, profile: Profile):
    thickness = []
    for index in sample(np.arange(len(mesh.faces)), 48):
        inward = -mesh.face_normals[index]
        hit = first_hit(mesh, mesh.triangles_center[index] + inward * EPS, inward)
        if np.isfinite(hit):
            thickness.append(float(hit + EPS))
    return {
        "faces": len(mesh.faces),
        "bounds_model_mm": mesh.bounds.tolist(),
        "center_of_mass_model_mm": mesh.center_mass.tolist(),
        "volume_mm3": float(mesh.volume),
        "wall_samples": len(thickness),
        "min_sampled_wall_mm": min(thickness, default=None),
        "thin_wall_samples": sum(value < profile.min_wall_mm for value in thickness),
    }


def bridge_candidates(mesh, suspended, profile):
    # Sample horizontal underside faces. Look for solid anchor evidence on
    # opposite sides at one layer below the underside, along plate X or Y.
    horizontal = np.flatnonzero(suspended & (mesh.face_normals[:, 2] < -0.999))
    spans = []
    for index in sample(horizontal, 4):
        origin = mesh.triangles_center[index] - UP * min(profile.min_wall_mm / 4, 0.15)
        for direction in AXES[:2]:
            span = first_hit(mesh, origin, direction) + first_hit(mesh, origin, -direction)
            if span <= profile.max_bridge_mm + EPS:
                spans.append(float(span))
                break
    return {"bridge_samples": len(spans), "max_sampled_bridge_mm": max(spans, default=None)}


def pose(mesh, profile, rotation):
    matrix = np.eye(4)
    matrix[:3, :3] = rotation
    rotated = mesh.vertices @ rotation.T
    low, high = rotated.min(axis=0), rotated.max(axis=0)
    center = np.asarray(profile.bed_origin_mm) + np.asarray(profile.bed_size_mm[:2]) / 2
    matrix[:2, 3] = center - (low[:2] + high[:2]) / 2
    matrix[2, 3] = -low[2]
    return matrix


def orientations(mesh, profile, load_model_xyz):
    load = np.asarray(load_model_xyz, dtype=float)
    length = np.linalg.norm(load)
    if not np.isfinite(length) or length > 1e6 or (0 < length < EPS):
        raise WorkflowError("INVALID_LOAD", "Задайте направление нагрузки в координатах STL.")
    if length:
        load /= length
    capacity = np.asarray(profile.bed_size_mm) - [2 * profile.margin_mm, 2 * profile.margin_mm, 0]
    choices = []
    for index, up in enumerate(AXES):
        align = trimesh.geometry.align_vectors(up, UP)[:3, :3]
        for quarter_turn in range(2):
            turn = trimesh.transformations.rotation_matrix(quarter_turn * np.pi / 2, UP)[:3, :3]
            rotation = turn @ align
            matrix = pose(mesh, profile, rotation)
            placed = mesh.copy()
            placed.apply_transform(matrix)
            triangles = placed.triangles
            contact = (triangles[:, :, 2] <= EPS).all(axis=1)
            down = -placed.face_normals[:, 2]
            suspended = (~contact) & (down > np.sin(np.deg2rad(profile.overhang_deg_from_vertical)))
            contact_area = float(np.sum(placed.area_faces[contact] * np.abs(down[contact])))
            overhang_area = float(np.sum(placed.area_faces[suspended]))
            load_z = float(abs((rotation @ load)[2]))
            contact_ratio = min(1., contact_area / max(EPS, np.prod(placed.extents[:2])))
            score = 2 * contact_ratio - 3 * overhang_area / placed.area - 2 * load_z
            if not (placed.extents <= capacity + EPS).all():
                continue
            choices.append({
                "id": f"pose-{index}-{quarter_turn}",
                "model_to_plate": matrix.tolist(),
                "extents_mm": placed.extents.tolist(),
                "bridge": bridge_candidates(placed, suspended, profile),
                "contact_mm2": contact_area,
                "overhang_mm2": overhang_area,
                "load_normal_fraction": load_z,
                "center_of_mass_plate_mm": placed.center_mass.tolist(),
                "score": float(score),
            })
    valid = sorted(choices, key=lambda item: (-item["score"], item["extents_mm"][2]))
    if not valid:
        raise WorkflowError("DOES_NOT_FIT", "Модель не помещается в пределах профиля ни в одной ориентации.")
    return valid
