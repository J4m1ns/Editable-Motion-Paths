"""Hit testing and selection helpers."""

import math

import bpy
from bpy_extras.view3d_utils import location_3d_to_region_2d, region_2d_to_location_3d
from mathutils import Vector

from . import compat, core, draw, space, state


class Hit:
    __slots__ = ("cache", "key", "part", "t", "dist")

    def __init__(self, cache, key, part, dist, t=None):
        self.cache = cache
        self.key = key
        self.part = part      # 'KEY', 'L', 'R' or 'LINE'
        self.dist = dist
        self.t = key.t if key is not None else t


def _radius():
    return 12.0 * compat.ui_scale()


MIN_HANDLE_PX = 36.0


def handle_min_world(region, rv3d, co):
    """World length that corresponds to the minimum on-screen handle length at `co`."""
    if region is None or rv3d is None or region.width < 1:
        return 0.0
    # a pixel at the depth of co, in world units: 2 w / (width * window[0][0]) - w is the clip
    # depth (1 in an orthographic view). (Called for every key with handles: no projections.)
    P = rv3d.perspective_matrix
    w = P[3][0] * co[0] + P[3][1] * co[1] + P[3][2] * co[2] + P[3][3]
    s = rv3d.window_matrix[0][0]
    if w <= 1e-6 or abs(s) < 1e-12:
        return 0.0
    return 2.0 * w / (region.width * abs(s)) * MIN_HANDLE_PX * compat.ui_scale()


def display_handle(k, side, m):
    """On-screen handle end: true length L is shown as sqrt(L^2 + m^2), so short or
    zero-length handles stay grabbable while long ones look exactly as they are."""
    h = k.hr if side == 'R' else k.hl
    if h is None:
        return None
    v = h - k.co
    ln = v.length
    d = v / ln if ln > 1e-9 else (k.dir_r if side == 'R' else k.dir_l)
    return k.co + d * math.sqrt(ln * ln + m * m)


def _polyline_distance(region, rv3d, pts, m):
    best = None
    prev = None
    for p in pts:
        s = location_3d_to_region_2d(region, rv3d, p)
        if s is not None and prev is not None:
            seg = s - prev
            ln = seg.length_squared
            f = 0.0 if ln < 1e-9 else max(0.0, min(1.0, (m - prev).dot(seg) / ln))
            d = (prev + seg * f - m).length
            best = d if best is None else min(best, d)
        prev = s
    return best


def _in_quad(q, m):
    """Is the screen point m inside the convex quad q (4 screen points, either winding)?"""
    sides = [(q[(i + 1) % 4] - q[i]).cross(m - q[i]) for i in range(4)]
    return all(s >= 0.0 for s in sides) or all(s <= 0.0 for s in sides)


def hit_test(context, mx, my, radius=None):
    """Nearest key point or visible handle end under the mouse."""
    from . import draw as _draw
    _draw.new_pass()
    region = context.region
    rv3d = context.region_data
    if region is None or rv3d is None or state.overlays_hidden():
        return None
    st = state.settings(context.scene)
    radius = radius or _radius()
    m = Vector((mx, my))
    best = None
    best_key = None
    # inside the drawn disc of a key, the key always wins over handles (a handle seen
    # end-on can be drawn right on top of its key)
    key_r = st.key_size * 0.5 * compat.ui_scale() + 2.0
    for cache in [c for c in state.visible_caches(context.scene) if not state.seen_from_itself(c, context)]:
        if not cache.refresh():
            continue
        editable = cache.owner_selected()
        lo, hi = draw.path_window(context.scene, st, cache)
        gk = draw.gizmo_key(cache)
        for k in cache.keys:
            if k.hidden or not (lo - 1e-4 <= k.t <= hi + 1e-4):
                continue
            cands = [('KEY', k.co, 0.0)]
            if editable and cache.ptype == core.ROTATION and k is gk and (st.show_rot_rings or st.show_reach):
                rings, _c, _r = space.rot_rings(cache, cache.target(), k, 32)
                for part, pts in (rings.items() if st.show_rot_rings else ()):
                    # (only the half drawn: the one facing the view)
                    ds = [_polyline_distance(region, rv3d, run, m) for run in draw.front_runs(pts, _c, rv3d)]
                    ds = [d for d in ds if d is not None]
                    d = min(ds) if ds else None
                    if d is not None and d <= radius * 0.8:
                        cands.append((part, None, d + 0.8))
                if st.show_reach:
                    # the faint reach circle: turn around the view axis (like R)
                    d = _polyline_distance(region, rv3d, space.reach_circle(_c, _r, rv3d, 48), m)
                    if d is not None and d <= radius * 0.8:
                        cands.append(('RV', None, d + 1.2))
            if editable and cache.ptype == core.LOCATION and k is gk and st.show_move_gizmo and \
                    cache.circle is None:
                for part, g in space.move_gizmo(context, cache, k, region, rv3d).items():
                    if part in ('MX', 'MY', 'MZ'):
                        d = _polyline_distance(region, rv3d, [g[0], g[1]], m)
                        if d is not None and d <= radius * 0.8:
                            cands.append((part, None, d + 0.6))
                    else:
                        corners = [location_3d_to_region_2d(region, rv3d, p) for p in g[0]]
                        if all(s is not None for s in corners) and _in_quad(corners, m):
                            cands.append((part, None, 0.4))
            if editable and cache.ptype == core.SCALE and k is gk:
                cands += [(part, p, 0.3) for part, p in
                          draw.scale_gizmo_points(st, cache, k, region, rv3d).items() if part != 'GYb']
                cands += [(part, end, 0.2) for part, _pts, end in draw.scale_whiskers(st, cache, k)]
            if draw.handles_shown(st, cache, editable, k):
                mw = handle_min_world(region, rv3d, k.co)
                if k.hl is not None:
                    cands.append(('L', display_handle(k, 'L', mw), 0.5))
                if k.hr is not None:
                    cands.append(('R', display_handle(k, 'R', mw), 0.5))
            for part, p, bias in cands:
                if p is None:
                    d = bias            # rings: the distance is measured already
                else:
                    s = location_3d_to_region_2d(region, rv3d, p)
                    if s is None:
                        continue
                    d = (s - m).length + bias
                if d <= radius and (best is None or d < best.dist):
                    best = Hit(cache, k, part, d)
                if part == 'KEY' and d <= key_r and (best_key is None or d < best_key.dist):
                    best_key = Hit(cache, k, part, d)
    if best_key is None and best is None:
        return _dot_hit(context, region, rv3d, st, m)
    return best_key or best


def _dot_hit(context, region, rv3d, st, m):
    """A frame dot (not a key) of an editable path under the mouse: part 'DOT'."""
    if not st.show_frames:
        return None
    r = 6.0 * compat.ui_scale()
    best = None
    for cache in [c for c in state.editable_caches(context.scene) if not state.seen_from_itself(c, context)]:
        lo, hi = draw.path_window(context.scene, st, cache)
        keys = {round(k.t, 3) for k in cache.keys}
        n = len(cache.times)
        vis = cache.vis if len(cache.vis) == n else [True] * n
        rep = cache.rep if len(cache.rep) == n else [False] * n
        for t, p, v, r_ in zip(cache.times, cache.points, vis, rep):
            if not v or r_ or not (lo <= t <= hi) or abs(t - round(t)) > 1e-4 or round(t, 3) in keys:
                continue
            s = location_3d_to_region_2d(region, rv3d, p)
            if s is None:
                continue
            d = (s - m).length
            if d <= r and (best is None or d < best.dist):
                best = Hit(cache, None, 'DOT', d, float(round(t)))
    return best


def line_hit(context, mx, my, radius=None):
    """Nearest point on an editable path line: returns Hit with part 'LINE' and frame t."""
    region = context.region
    rv3d = context.region_data
    st = state.settings(context.scene)
    radius = radius or _radius()
    m = Vector((mx, my))
    best = None
    for cache in [c for c in state.editable_caches(context.scene) if not state.seen_from_itself(c, context)]:
        lo, hi = draw.path_window(context.scene, st, cache)
        prev = None
        n = len(cache.times)
        vis = cache.vis if len(cache.vis) == n else [True] * n
        rep = cache.rep if len(cache.rep) == n else [False] * n
        for t, p, v, r_ in zip(cache.times, cache.points, vis, rep):
            if not (lo <= t <= hi) or not v or r_:
                prev = None
                continue
            s = location_3d_to_region_2d(region, rv3d, p)
            if s is None:
                prev = None
                continue
            if prev is not None:
                t0, s0 = prev
                seg = s - s0
                ln = seg.length_squared
                f = 0.0 if ln < 1e-9 else max(0.0, min(1.0, (m - s0).dot(seg) / ln))
                d = (s0 + seg * f - m).length
                if d <= radius and (best is None or d < best.dist):
                    best = Hit(cache, None, 'LINE', d, t0 + (t - t0) * f)
            prev = (t, s)
    return best


# ----------------------------------------------------------------------------
# Selection (stored in the F-Curve keyframe selection, so undo and the
# Graph Editor stay in sync)
# ----------------------------------------------------------------------------

def tag_anim_editors():
    wm = bpy.context.window_manager
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type in {'VIEW_3D', 'GRAPH_EDITOR', 'DOPESHEET_EDITOR', 'TIMELINE'}:
                area.tag_redraw()


def deselect_all(caches):
    changed = False
    for cache in caches:
        tg = cache.target()
        if tg is None:
            continue
        for fc in tg.channels(cache.ptype).fcurves:
            if fc is None:
                continue
            kps = fc.keyframe_points
            n = len(kps)
            if not n:
                continue
            for attr in ("select_control_point", "select_left_handle", "select_right_handle"):
                cur = [False] * n
                kps.foreach_get(attr, cur)
                if any(cur):
                    kps.foreach_set(attr, [False] * n)
                    changed = True
        cache.dirty = True
    state.forget_selected([c.key for c in caches])
    return changed


def set_key_select(cache, k, point=None, left=None, right=None):
    tg = cache.target()
    if tg is None:
        return
    ch = tg.channels(cache.ptype)
    for c, fc in enumerate(ch.fcurves):
        idx = k.idx[c] if c < len(k.idx) else -1
        if fc is None or idx < 0:
            continue
        kp = fc.keyframe_points[idx]
        if point is not None:
            kp.select_control_point = point
        if left is not None:
            kp.select_left_handle = left
        if right is not None:
            kp.select_right_handle = right
    cache.dirty = True


def select_part(cache, k, part, value=True):
    if value and k.hidden:
        return
    if value:
        state.note_selected(cache.key)      # (the order paths get selected in: Offset Cascade)
    if part == 'KEY':
        set_key_select(cache, k, value, value, value)
    elif part == 'L':
        set_key_select(cache, k, left=value)
    elif part == 'R':
        set_key_select(cache, k, right=value)


def part_selected(k, part):
    return k.sel if part == 'KEY' else (k.sel_l if part == 'L' else k.sel_r)


def select_owner(context, cache, extend):
    """Select the bone/object a path belongs to (making its path editable)."""
    obj, owner = core.resolve(cache.obj_name, cache.bone_name)
    if owner is None:
        return False
    if cache.bone_name:
        if obj.mode != 'POSE':
            return False
        if not extend:
            for o in context.objects_in_mode or [obj]:
                if o.pose:
                    for pb in o.pose.bones:
                        if compat.bone_selected(pb):
                            compat.bone_select_set(pb, False)
        compat.bone_select_set(owner, True)
        obj.data.bones.active = owner.bone
        return True
    if context.mode != 'OBJECT':
        return False
    if not extend:
        for o in context.selected_objects:
            o.select_set(False)
    obj.select_set(True)
    context.view_layer.objects.active = obj
    return True


def selected_count(caches):
    n = 0
    for c in caches:
        for k in c.keys:
            if k.sel or (k.sel_l and k.hl is not None) or (k.sel_r and k.hr is not None):
                n += 1
    return n
