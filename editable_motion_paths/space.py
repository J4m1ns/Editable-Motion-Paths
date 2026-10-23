"""Blender's own transform settings applied to path keys: pivot point and orientations."""

import bpy
from mathutils import Matrix, Vector

from . import core, state

ORIENTATIONS = [
    ('GLOBAL', "Global", "World axes"),
    ('LOCAL', "Local", "Channel axes of each key (what the F-Curves edit)"),
    ('NORMAL', "Path", "Along the path: Y = direction of motion, X = sideways, Z = up"),
    ('VIEW', "View", "Screen axes"),
    ('CURSOR', "Cursor", "Axes of the 3D cursor"),
    ('PARENT', "Parent", "Axes of the parent space"),
    ('CUSTOM', "Custom", "The custom transform orientation"),
]


def tool_settings(context=None):
    return (context or bpy.context).scene.tool_settings


def orientation_type(context=None):
    """Header transform orientation, mapped to the ones paths understand."""
    scene = (context or bpy.context).scene
    t = scene.transform_orientation_slots[0].type
    if t in {'GLOBAL', 'LOCAL', 'NORMAL', 'VIEW', 'CURSOR', 'PARENT'}:
        return t
    if t == 'GIMBAL':
        return 'LOCAL'
    return 'CUSTOM'


def _normalized(m3):
    cols = []
    for i in range(3):
        c = m3.col[i].copy()
        if c.length < 1e-9:
            c = Vector([1.0 if j == i else 0.0 for j in range(3)])
        cols.append(c.normalized())
    return Matrix((cols[0], cols[1], cols[2])).transposed()


def tangent(cache, t):
    """Direction of motion of the path at frame t."""
    tg = cache.target()
    if tg is None:
        return Vector((0.0, 1.0, 0.0))
    for e in (0.5, 2.0, 5.0):
        v = cache.point(tg, min(t + e, cache.fe)) - cache.point(tg, max(t - e, cache.fs))
        if v.length > 1e-7:
            return v.normalized()
    return Vector((0.0, 1.0, 0.0))


def path_frame(cache, t):
    """Y along the motion, X sideways (horizontal), Z up-ish."""
    y = tangent(cache, t)
    up = Vector((0.0, 0.0, 1.0))
    x = y.cross(up)
    if x.length < 1e-4:
        x = y.cross(Vector((0.0, -1.0, 0.0)))
    x.normalize()
    z = x.cross(y).normalized()
    return Matrix((x, y, z)).transposed()


def element_basis(cache, tg, k):
    """Channel axes of a key in world space."""
    if cache.ptype != core.ROTATION:
        return _normalized(cache.B_at(k.t).to_3x3())
    M, _u = cache.rot_frame(tg, k.t)
    q = core.vals_to_quat(k.vals, tg.rot_mode)
    return _normalized(M.to_3x3() @ q.to_matrix())


RING_AXES = {'RX': 0, 'RZ': 2, 'RV': 3}    # RV: the reach circle, turning around the view axis


def rot_rings(cache, tg, k, segments=48):
    """Rotation paths: {'RX': points, 'RZ': points} circles around the bone head through the
    key's tip, perpendicular to the bone's X and Z axes (the ways the tip turns), and the
    head and radius."""
    import math
    center = cache.rot_frame(tg, k.t)[0].translation.copy()
    b = element_basis(cache, tg, k)
    r = (k.co - center).length
    out = {}
    for part, (u, v) in (('RX', (b.col[1], b.col[2])), ('RZ', (b.col[1], b.col[0]))):
        out[part] = [center + (u * math.cos(a * 2 * math.pi / segments) +
                               v * math.sin(a * 2 * math.pi / segments)) * r for a in range(segments + 1)]
    return out, center, r


def reach_circle(center, r, rv3d, segments=64):
    """The circle facing the view that shows how far the tip reaches."""
    import math
    right = rv3d.view_rotation @ Vector((1.0, 0.0, 0.0))
    up = rv3d.view_rotation @ Vector((0.0, 1.0, 0.0))
    return [center + (right * math.cos(a * 2 * math.pi / segments) +
                      up * math.sin(a * 2 * math.pi / segments)) * r for a in range(segments + 1)]


def parent_basis(cache, tg, k):
    if cache.ptype != core.ROTATION:
        return _normalized(cache.B_at(k.t).to_3x3())
    return _normalized(cache.rot_frame(tg, k.t)[0].to_3x3())


def orientation_matrix(context, otype, cache=None, k=None):
    """World 3x3 axes of a transform orientation (at key k for the per-key ones)."""
    if otype == 'VIEW':
        rv3d = getattr(context, "region_data", None)
        if rv3d is not None:
            return rv3d.view_rotation.to_matrix()
        return Matrix.Identity(3)
    if otype == 'CURSOR':
        return _normalized(context.scene.cursor.matrix.to_3x3())
    if otype == 'CUSTOM':
        co = context.scene.transform_orientation_slots[0].custom_orientation
        return _normalized(co.matrix.copy()) if co is not None else Matrix.Identity(3)
    if cache is not None and k is not None:
        tg = cache.target()
        if tg is not None:
            if otype == 'LOCAL':
                return element_basis(cache, tg, k)
            if otype == 'PARENT':
                return parent_basis(cache, tg, k)
            if otype == 'NORMAL':
                return path_frame(cache, k.t)
    return Matrix.Identity(3)


def pivot_point(context, keys, handles=()):
    """(pivot, individual) from the header pivot setting.

    keys: list of (cache, k) of selected keys, handles: (cache, k) of selected handles only.
    individual = every key turns / scales around itself (its handles), like Individual
    Origins on curve points."""
    scene = context.scene
    mode = scene.tool_settings.transform_pivot_point
    pts = [k.co for _c, k in keys] or [k.co for _c, k in handles]
    if not pts:
        return scene.cursor.location.copy(), False
    if mode == 'CURSOR':
        return scene.cursor.location.copy(), False
    if mode == 'INDIVIDUAL_ORIGINS':
        return median(pts), True
    active = state.get_active(scene)
    if mode == 'ACTIVE_ELEMENT' and active is not None:
        for c, k in keys:
            if c.key == active[0] and abs(k.t - active[1]) < 1e-3:
                return k.co.copy(), False
    if mode == 'BOUNDING_BOX_CENTER':
        lo = Vector([min(p[i] for p in pts) for i in range(3)])
        hi = Vector([max(p[i] for p in pts) for i in range(3)])
        return (lo + hi) * 0.5, False
    return median(pts), False


def median(pts):
    p = Vector()
    for q in pts:
        p += q
    return p / len(pts)


def active_key(caches):
    """(cache, key) of the active (last clicked) key if it is selected."""
    active = state.get_active()
    if active is None:
        return None
    for c in caches:
        if c.key != active[0]:
            continue
        for k in c.keys:
            if k.sel and abs(k.t - active[1]) < 1e-3:
                return c, k
    return None
