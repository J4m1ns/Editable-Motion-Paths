"""Blender's own transform settings applied to path keys: pivot point and orientations."""

import bpy
from mathutils import Matrix, Vector

from . import core, state

ORIENTATIONS = [
    ('GLOBAL', "Global", "World axes"),
    ('LOCAL', "Local", "Axes of each key's bone (its channel axes) or object (turned with it)"),
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
    """Local axes of a key in world space: a bone's channel axes; an object's own axes, turned
    with it on the key's frame (like Blender's Local orientation - its location channels are in
    its parent's space)."""
    if cache.ptype == core.LOCATION and not tg.is_bone and cache.circle is None:
        rot = tg.rot.values(k.t)
        return _normalized((cache.B_at(k.t) @ tg.basis((0.0, 0.0, 0.0), rot, (1.0, 1.0, 1.0))).to_3x3())
    if cache.ptype != core.ROTATION:
        return _normalized(cache.B_at(k.t).to_3x3())
    M, _u = cache.rot_frame(tg, k.t)
    q = core.vals_to_quat(k.vals, tg.rot_mode)
    return _normalized(M.to_3x3() @ q.to_matrix())


# RY: twist; RB: the twist handle on the rotation ribbon; RV: the reach circle (view axis)
RING_AXES = {'RX': 0, 'RY': 1, 'RZ': 2, 'RV': 3, 'RB': 4}


def rot_rings(cache, tg, k, segments=48):
    """Rotation paths: {'RX', 'RY', 'RZ': points} circles around the bone head, perpendicular
    to the bone's X, Y and Z axes: X and Z pass through the key's tip (the ways the tip turns),
    Y is the equator around the bone (twisting it). Also the head and the radius."""
    import math
    center = cache.rot_frame(tg, k.t)[0].translation.copy()
    b = element_basis(cache, tg, k)
    r = (k.co - center).length
    out = {}
    for part, (u, v) in (('RX', (b.col[1], b.col[2])), ('RY', (b.col[2], b.col[0])),
                         ('RZ', (b.col[1], b.col[0]))):
        out[part] = [center + (u * math.cos(a * 2 * math.pi / segments) +
                               v * math.sin(a * 2 * math.pi / segments)) * r for a in range(segments + 1)]
    return out, center, r


# the move gizmo of location paths: (axis, in its plane - the square across that axis)
MOVE_AXES = {'MX': (0, False), 'MY': (1, False), 'MZ': (2, False),
             'MPX': (0, True), 'MPY': (1, True), 'MPZ': (2, True)}


def move_gizmo(context, cache, k, region, rv3d):
    """Location paths: the move gizmo at key k, like Blender's - arrows along the axes of the
    header's orientation {'MX', 'MY', 'MZ': (start, tip, alpha)} (from a gap around the key, so
    the key itself stays free to drag) and squares to move in the plane across an axis {'MPX',
    'MPY', 'MPZ': ([4 corners], alpha)}. As big on the screen as Blender's gizmo (Preferences >
    Viewport > Gizmo Size) at any zoom; an arrow pointing at the view / a square seen edge-on
    fades out."""
    from . import compat, pick
    if region is None or rv3d is None:
        return {}
    us = compat.ui_scale()
    per_px = pick.handle_min_world(region, rv3d, k.co) / (pick.MIN_HANDLE_PX * us)
    if per_px <= 0.0:
        return {}
    L = context.preferences.view.gizmo_size * us * per_px
    A = orientation_matrix(context, orientation_type(context), cache, k)
    if rv3d.is_perspective:
        view = (k.co - rv3d.view_matrix.inverted().translation).normalized()
    else:
        view = (rv3d.view_rotation @ Vector((0.0, 0.0, -1.0))).normalized()
    out = {}
    for i, part in enumerate(('MX', 'MY', 'MZ')):
        ax = A.col[i].normalized()
        d = abs(ax.dot(view))
        alpha = 1.0 if d < 0.9 else max(0.0, (0.98 - d) / 0.08)
        if alpha > 0.05:
            out[part] = (k.co + ax * (L * 0.22), k.co + ax * L, alpha)
    for i, part in enumerate(('MPX', 'MPY', 'MPZ')):
        u, v = A.col[(i + 1) % 3].normalized(), A.col[(i + 2) % 3].normalized()
        d = abs(A.col[i].normalized().dot(view))
        alpha = 1.0 if d > 0.3 else max(0.0, (d - 0.12) / 0.18)
        if alpha > 0.05:
            a0, a1 = L * 0.3, L * 0.48
            out[part] = ([k.co + u * a0 + v * a0, k.co + u * a1 + v * a0, k.co + u * a1 + v * a1,
                          k.co + u * a0 + v * a1], alpha)
    return out


def reach_circle(center, r, rv3d, segments=64):
    """The circle facing the view that shows how far the tip reaches."""
    import math
    right = rv3d.view_rotation @ Vector((1.0, 0.0, 0.0))
    up = rv3d.view_rotation @ Vector((0.0, 1.0, 0.0))
    return [center + (right * math.cos(a * 2 * math.pi / segments) +
                      up * math.sin(a * 2 * math.pi / segments)) * r for a in range(segments + 1)]


def parent_basis(cache, tg, k):
    """Axes of the parent (like Blender's Parent orientation): a bone's parent bone (the
    armature for a root bone), an object's parent object - on the key's frame. The channel space
    also holds a bone's rest offset from its parent / an object's parent inverse: taken out."""
    B = cache.B_at(k.t) if cache.ptype != core.ROTATION else cache.rot_frame(tg, k.t)[0]
    B = B.to_3x3()
    try:
        if tg.is_bone:
            bone = tg.owner.bone
            # (a location path of a bone with Local Location off: its space is the parent's already)
            if cache.ptype != core.LOCATION or bone.use_local_location:
                off = bone.matrix_local.to_3x3().inverted()
                if bone.parent is not None:
                    off = off @ bone.parent.matrix_local.to_3x3()
                B = B @ off
        elif tg.owner.parent is not None:
            B = B @ tg.owner.matrix_parent_inverse.to_3x3().inverted()
    except (ValueError, AttributeError):
        pass            # (a singular rest / parent inverse: the channel space as it is)
    return _normalized(B)


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
