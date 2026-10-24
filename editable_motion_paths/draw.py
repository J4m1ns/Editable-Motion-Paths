"""Viewport drawing of the editable paths.

Lines are drawn in 3D (POST_VIEW); points (frames, keys, handle ends) are drawn as
2D discs on top (POST_PIXEL) so they are always round, crisp and clickable.
"""

import math

import blf
import bpy
import gpu
from bpy_extras.view3d_utils import location_3d_to_region_2d
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

from . import compat, core, state

_handles = []
_error_reported = False

COL_KEY = (0.95, 0.95, 0.95, 1.0)
COL_KEY_SEL = (1.0, 0.55, 0.1, 1.0)
COL_KEY_LOCKED = (0.6, 0.6, 0.6, 0.85)
COL_OUTLINE = (0.0, 0.0, 0.0, 0.85)
COL_FRAME = (0.9, 0.9, 0.9, 0.8)
COL_CURRENT = (0.3, 1.0, 0.45, 1.0)
COL_GHOST = (0.65, 0.65, 0.65, 0.35)
COL_SPOKE = (1.0, 0.6, 0.2, 0.55)
HANDLE_COLORS = {
    'FREE': (0.1, 0.1, 0.1, 1.0),
    'ALIGNED': (0.9, 0.35, 0.75, 1.0),
    'VECTOR': (0.35, 0.85, 0.35, 1.0),
    'AUTO': (0.95, 0.85, 0.2, 1.0),
    'AUTO_CLAMPED': (0.95, 0.45, 0.4, 1.0),
}
COL_HANDLE_SEL = (1.0, 0.8, 0.5, 1.0)


def _speed_color(x):
    """0 = slow (blue), 0.5 = green/yellow, 1 = fast (red)."""
    stops = ((0.0, (0.2, 0.45, 1.0)), (0.35, (0.2, 0.9, 0.9)), (0.6, (0.4, 1.0, 0.3)),
             (0.8, (1.0, 0.85, 0.2)), (1.0, (1.0, 0.25, 0.2)))
    x = max(0.0, min(1.0, x))
    for (a, ca), (b, cb) in zip(stops, stops[1:]):
        if x <= b:
            f = (x - a) / (b - a)
            return tuple(ca[i] + (cb[i] - ca[i]) * f for i in range(3))
    return stops[-1][1]


COL_CALM = (0.5, 0.58, 0.68)


def _jitter_color(x):
    """0 = smooth (neutral gray-blue), 0.5 = yellow, 1 = rough (red)."""
    x = max(0.0, min(1.0, x))
    if x < 0.5:
        f = x / 0.5
        return tuple(a + (b - a) * f for a, b in zip(COL_CALM, (1.0, 0.85, 0.2)))
    f = (x - 0.5) / 0.5
    return tuple(a + (b - a) * f for a, b in zip((1.0, 0.85, 0.2), (1.0, 0.15, 0.1)))


def jitter_values(cache):
    """Roughness at every sample of the path: |4th difference| of the per-frame positions
    (zero for any smooth cubic motion) relative to the LOCAL speed, so fast but smooth parts
    (big spacing) stay calm. Near holds the speed used never drops below a tenth of the
    average, so tiny wobbles there don't light up."""
    cached = getattr(cache, "_jitter", None)
    if cached is not None and cached[0] is cache.sig:
        return cached[1]
    frames = [(t, p) for t, p in zip(cache.times, cache.points) if abs(t - round(t)) < 1e-6]
    P = [p for _t, p in frames]
    n = len(P)
    d4 = [0.0] * n
    for i in range(2, n - 2):
        d4[i] = (P[i - 2] - 4.0 * P[i - 1] + 6.0 * P[i] - 4.0 * P[i + 1] + P[i + 2]).length
    steps = [(b - a).length for a, b in zip(P, P[1:])]
    speed = sum(steps) / max(1, n - 1)
    floor = max(speed * 0.1, 1e-9)

    def local(i):
        s = steps[max(0, i - 2):i + 2]
        return max(sum(s) / len(s), floor) if s else floor
    rough = [max(d4[max(0, i - 1):i + 2]) / local(i) for i in range(n)]
    at = {round(t): r for (t, _p), r in zip(frames, rough)}
    out = [at.get(round(t), 0.0) for t in cache.times]
    cache._jitter = (cache.sig, out)
    return out


def path_window(scene, st, cache):
    """(lo, hi) frame window to display."""
    lo, hi = cache.fs, cache.fe
    rng = cache.circle_range()
    if rng is not None:
        lo, hi = max(lo, rng[0]), min(hi, rng[1])
    if st.use_window:
        cur = scene.frame_current
        lo = max(lo, cur - st.window_before)
        hi = min(hi, cur + st.window_after)
    return lo, hi


def handles_shown(st, cache, editable, k):
    if not editable or not st.show_handles:
        return False
    return st.show_all_handles or k.any_sel


def _in(t, lo, hi):
    return lo - 1e-4 <= t <= hi + 1e-4


# ----------------------------------------------------------------------------
# Primitives
# ----------------------------------------------------------------------------

def _lines(coords, colors, width, kind='LINES'):
    if len(coords) < 2:
        return
    region = bpy.context.region
    sh = gpu.shader.from_builtin('POLYLINE_SMOOTH_COLOR')
    sh.uniform_float("viewportSize", (region.width, region.height))
    sh.uniform_float("lineWidth", width)
    batch_for_shader(sh, kind, {"pos": coords, "color": colors}).draw(sh)


_CIRCLE = [(math.cos(a * 2 * math.pi / 16), math.sin(a * 2 * math.pi / 16)) for a in range(16)]


class Discs:
    """Collects 2D discs per color and draws them in one batch per color."""

    def __init__(self):
        self.groups = {}

    def add(self, p, radius, color):
        if p is None:
            return
        tris = self.groups.setdefault((radius, tuple(color)), [])
        x, y = p
        for i in range(16):
            a = _CIRCLE[i]
            b = _CIRCLE[(i + 1) % 16]
            tris += [(x, y), (x + a[0] * radius, y + a[1] * radius), (x + b[0] * radius, y + b[1] * radius)]

    def add_outlined(self, p, radius, color, outline=1.5):
        self.add(p, radius + outline, COL_OUTLINE)
        self.add(p, radius, color)

    def draw(self):
        if not self.groups:
            return
        sh = gpu.shader.from_builtin('UNIFORM_COLOR')
        # bigger (outline) discs first
        for (radius, color), tris in sorted(self.groups.items(), key=lambda kv: -kv[0][0]):
            sh.uniform_float("color", color)
            batch_for_shader(sh, 'TRIS', {"pos": tris}).draw(sh)


# ----------------------------------------------------------------------------
# 3D pass
# ----------------------------------------------------------------------------

def draw_view():
    global _error_reported
    try:
        _draw_view()
    except Exception as ex:
        if not _error_reported:
            _error_reported = True
            import traceback
            traceback.print_exc()
            print("Editable Motion Paths: drawing failed:", ex)


COL_HIDDEN = (0.7, 0.7, 0.7, 0.4)


def _sample_flags(cache):
    n = len(cache.times)
    vis = cache.vis if len(cache.vis) == n else [True] * n
    rep = cache.rep if len(cache.rep) == n else [False] * n
    return vis, rep


def _path_colors(st, cache, editable, cur, lo, hi):
    """Runs of (points, colors, kind): 'main', 'repeat' (a loop outside its keys, faint) and
    'hidden' (next to hidden keys: a faint dashed ghost)."""
    runs = []
    speeds = cache.speeds
    vmax = 0.0
    if st.color_mode == 'SPEED' and speeds:
        s = sorted(speeds)
        vmax = s[min(len(s) - 1, int(len(s) * 0.95))] or max(s) or 1.0
    base = tuple(st.color)
    alpha = 1.0 if editable else 0.6
    rough = jitter_values(cache) if st.color_mode == 'JITTER' else None
    vis, rep = _sample_flags(cache)
    cur_kind, pts, cols = None, [], []
    for i, (t, p) in enumerate(zip(cache.times, cache.points)):
        if not _in(t, lo, hi):
            kind = None
        elif not vis[i]:
            kind = 'hidden'
        elif rep[i]:
            kind = 'repeat' if st.show_repeats else None
        else:
            kind = 'main'
        if kind != cur_kind:
            if cur_kind is not None and len(pts) > 1:
                runs.append((pts, cols, cur_kind))
            # runs touch, so the line has no gaps where its kind changes
            pts = [pts[-1]] if pts and kind is not None and cur_kind is not None else []
            cols = [cols[-1]] if cols and pts else []
            cur_kind = kind
        if kind is None:
            continue
        if kind == 'hidden':
            col = COL_HIDDEN
        else:
            if rough is not None:
                c = _jitter_color(rough[i] / max(st.jitter_threshold, 1e-6))
            elif st.color_mode == 'SPEED':
                sp = speeds[min(i, len(speeds) - 1)] if speeds else 0.0
                c = _speed_color(sp / vmax if vmax > 0 else 0.0)
            elif st.color_mode == 'TIME':
                c = base if t >= cur else tuple(x * 0.45 for x in base)
            else:
                c = base
            if not editable:
                c = tuple(0.35 + 0.4 * x for x in c)
            col = (c[0], c[1], c[2], alpha * (0.3 if kind == 'repeat' else 1.0))
        pts.append(p)
        cols.append(col)
    if cur_kind is not None and len(pts) > 1:
        runs.append((pts, cols, cur_kind))
    return runs


def _draw_path_runs(st, cache, editable, cur, lo, hi, us):
    width = st.line_width * us * (1.0 if editable else 0.75)
    for pts, cols, kind in _path_colors(st, cache, editable, cur, lo, hi):
        if kind == 'hidden':
            dashed, dcols = [], []
            for i in range(0, len(pts) - 1, 2):
                dashed += [pts[i], pts[i + 1]]
                dcols += [cols[i], cols[i + 1]]
            _lines(dashed, dcols, 1.2 * us)
        else:
            _lines(pts, cols, width * (0.7 if kind == 'repeat' else 1.0), 'LINE_STRIP')


COL_RIBBON = (1.0, 0.75, 0.3)


COL_SCALE_LOW = (0.3, 0.6, 1.0)
COL_SCALE_HIGH = (1.0, 0.25, 0.35)
GIZMO_COLORS = {'GX': (1.0, 0.25, 0.3, 1.0), 'GY': (0.45, 0.9, 0.2, 1.0), 'GZ': (0.25, 0.5, 1.0, 1.0),
                'GU': (1.0, 1.0, 1.0, 1.0)}
GIZMO_COLORS['GXn'] = GIZMO_COLORS['GX']
GIZMO_COLORS['GZn'] = GIZMO_COLORS['GZ']


def scale_color(sy):
    """Ribbon color from the third scale axis: blue below 1, orange at 1, red above."""
    f = max(-1.0, min(1.0, math.log2(max(abs(sy), 1e-3))))
    other = COL_SCALE_HIGH if f > 0 else COL_SCALE_LOW
    return tuple(a + (b - a) * abs(f) for a, b in zip(COL_RIBBON, other))


def ribbon_half(st, cache):
    return cache.vgain * 0.12 * st.ribbon_width


def _draw_ribbon(st, cache, rv3d, lo, hi, editable, us):
    """Scale paths: two crossed ribbons along the target's own X and Z axes, as wide as the
    X and Z scale; their color shows the Y scale."""
    pts = cache.points
    n = len(pts)
    if len(cache.sample_vals) != n or len(cache.sample_axes) != n:
        return
    vis = cache.vis if len(cache.vis) == n else [True] * n
    half = ribbon_half(st, cache)
    alpha = 0.22 if editable else 0.1
    sh = gpu.shader.from_builtin('SMOOTH_COLOR')
    for axis, comp in ((0, 0), (2, 2)):
        run = []
        runs = []
        for i, t in enumerate(cache.times):
            if not _in(t, lo, hi) or not vis[i]:
                if len(run) > 1:
                    runs.append(run)
                run = []
                continue
            v = cache.sample_vals[i]
            w = half * abs(v[comp])
            d = cache.sample_axes[i][axis] * w
            col = scale_color(v[1]) if len(v) > 1 else COL_RIBBON
            run.append((pts[i] + d, pts[i] - d, col))
        if len(run) > 1:
            runs.append(run)
        for run in runs:
            tris, cols = [], []
            for (l0, r0, c0), (l1, r1, c1) in zip(run, run[1:]):
                tris += [l0, r0, l1, l1, r0, r1]
                ca, cb = c0 + (alpha,), c1 + (alpha,)
                cols += [ca, ca, cb, cb, ca, cb]
            batch_for_shader(sh, 'TRIS', {"pos": tris, "color": cols}).draw(sh)
            ea = 0.8 if editable else 0.35
            ecol = [c + (ea,) for _l, _r, c in run]
            _lines([l for l, _r, _c in run], ecol, 1.2 * us, 'LINE_STRIP')
            _lines([r for _l, r, _c in run], ecol, 1.2 * us, 'LINE_STRIP')
    if st.show_scale_box:
        _draw_scale_box(st, cache, us)


def _draw_scale_box(st, cache, us):
    """A box at the current frame, as big as the current scale."""
    scene = bpy.context.scene
    t = scene.frame_current + scene.frame_subframe
    if not cache.fs <= t <= cache.fe:
        return
    tg = cache.target()
    if tg is None:
        return
    c = cache.head(tg, t)
    ax = cache.axes(tg, t)
    s = tg.scale.values(t)
    h = ribbon_half(st, cache) * st.scale_box_size
    e = [ax[i] * (h * abs(s[i])) for i in range(3)]
    corners = {}
    for i in (-1, 1):
        for j in (-1, 1):
            for k in (-1, 1):
                corners[(i, j, k)] = c + e[0] * i + e[1] * j + e[2] * k
    lines = []
    for (i, j, k), p in corners.items():
        for q in ((-i, j, k), (i, -j, k), (i, j, -k)):
            if q > (i, j, k):
                lines += [p, corners[q]]
    _lines(lines, [(1.0, 1.0, 1.0, 0.7)] * len(lines), 1.3 * us)


def scale_gizmo_points(st, cache, k, region=None, rv3d=None):
    """Scale paths: gizmo handle ends of a selected key {'GX', 'GY', 'GZ', 'GU': world point}.
    Lengths follow the scale, with a minimum size on the screen."""
    from . import pick
    tg = cache.target()
    if tg is None or not k.vals or len(k.vals) < 3:
        return {}
    ax = cache.axes(tg, k.t)
    region = region or bpy.context.region
    rv3d = rv3d or bpy.context.region_data
    # X and Z sit on the edges of their ribbons (both sides), Y on the bone axis
    h = ribbon_half(st, cache)
    mn = pick.handle_min_world(region, rv3d, k.co) * 0.7
    out = {}
    for i, part in enumerate(('GX', 'GY', 'GZ')):
        ln = max(h * abs(k.vals[i]), mn)
        out[part] = k.co + ax[i] * ln
        if part != 'GY':
            out[part + 'n'] = k.co - ax[i] * ln
    u = (ax[0] + ax[1] + ax[2]).normalized()
    out['GU'] = k.co + u * max(h * 1.2 * sum(abs(v) for v in k.vals[:3]) / 3.0, mn * 1.3)
    return out


WHISKER_STEPS = 12


def scale_whiskers(st, cache, k):
    """Scale paths: the active channel's handles of key k, drawn like the ribbon edge they
    shape: [(part, [points of the whisker], end point)]. Time runs along the path, the value is
    the distance from the path (X, Z: on both edges, Y: on the path, up = bigger)."""
    c = {'X': 0, 'Y': 1, 'Z': 2}.get(st.scale_channel)
    tg = cache.target()
    if c is None or tg is None or c >= len(k.idx) or k.idx[c] < 0:
        return []
    fc = tg.scale.fcurves[c]
    kp = fc.keyframe_points[k.idx[c]]
    x, y = kp.co
    half = ribbon_half(st, cache)
    out = []
    for side, h in (('L', kp.handle_left), ('R', kp.handle_right)):
        dt = h[0] - x
        if abs(dt) < 1e-6:
            continue
        signs = (1.0, -1.0) if c != 1 else (1.0,)
        for s in signs:
            pts = []
            for i in range(WHISKER_STEPS + 1):
                f = i / WHISKER_STEPS
                t = k.t + dt * f
                v = y + (h[1] - y) * f
                ax = cache.axes(tg, t)
                if c == 1:
                    pts.append(cache.head(tg, t) + ax[2] * (half * (v - y)))
                else:
                    pts.append(cache.head(tg, t) + ax[c] * (s * half * abs(v)))
            out.append(("W%s%s" % (side, '+' if s > 0 else '-'), pts, pts[-1]))
    return out


COL_ROT_RIBBON = (0.72, 0.55, 1.0)


def rot_ribbon_span(st, cache):
    """(inner, outer) offsets along the bone's X axis of the rotation ribbon, in world units."""
    tg = cache.target()
    length = (tg.length if tg is not None else 1.0) * cache.rot_len
    w = 0.18 * length * st.rot_ribbon_width
    return {'POSITIVE': (0.0, w), 'NEGATIVE': (-w, 0.0)}.get(st.rot_ribbon_side, (-w * 0.5, w * 0.5))


def rot_ribbon_handle(st, cache, k):
    """World point of the twist handle on the ribbon's edge at key k."""
    i = next((i for i, t in enumerate(cache.times) if abs(t - k.t) < 1e-6), None)
    if i is None or i >= len(cache.sample_axes):
        return None
    lo, hi = rot_ribbon_span(st, cache)
    edge = hi if abs(hi) >= abs(lo) else lo
    return k.co + cache.sample_axes[i][0] * edge


def _draw_rot_ribbon(st, cache, lo, hi, editable, us):
    """Rotation paths: a ribbon along the path of the tip, across the bone's X axis, so the
    twist (roll around Y) shows as the ribbon turning."""
    pts = cache.points
    n = len(pts)
    if len(cache.sample_axes) != n:
        return
    vis = cache.vis if len(cache.vis) == n else [True] * n
    a0, a1 = rot_ribbon_span(st, cache)
    alpha = 0.2 if editable else 0.08
    tris, cols, e0, e1 = [], [], [], []
    prev = None
    for i, t in enumerate(cache.times):
        if not _in(t, lo, hi) or not vis[i]:
            prev = None
            continue
        x = cache.sample_axes[i][0]
        cur = (pts[i] + x * a0, pts[i] + x * a1)
        if prev is not None:
            tris += [prev[0], prev[1], cur[0], cur[0], prev[1], cur[1]]
            e0 += [prev[0], cur[0]]
            e1 += [prev[1], cur[1]]
        prev = cur
    if tris:
        sh = gpu.shader.from_builtin('SMOOTH_COLOR')
        cols = [COL_ROT_RIBBON + (alpha,)] * len(tris)
        batch_for_shader(sh, 'TRIS', {"pos": tris, "color": cols}).draw(sh)
        ea = 0.7 if editable else 0.3
        _lines(e0 + e1, [COL_ROT_RIBBON + (ea,)] * (len(e0) + len(e1)), 1.0 * us)


def _draw_scale_gizmos(st, cache, keys, us):
    lines, cols = [], []
    picked_sel = state.whisker_sel()
    for k in keys:
        if not k.sel:
            continue
        g = scale_gizmo_points(st, cache, k)
        for a, b in (('GXn', 'GX'), ('GZn', 'GZ')):
            if a in g:
                lines += [g[a], g[b]]       # across the ribbon: its width at this key
                cols += [GIZMO_COLORS[b]] * 2
        if 'GY' in g:
            lines += [k.co, g['GY']]
            cols += [GIZMO_COLORS['GY']] * 2
        part = {'X': 'GX', 'Y': 'GY', 'Z': 'GZ'}.get(st.scale_channel)
        ch = {'X': 0, 'Y': 1, 'Z': 2}.get(st.scale_channel)
        for p, pts, _end in scale_whiskers(st, cache, k):
            # a picked handle (V / T work on it) is drawn white
            picked = (cache.key, round(k.t, 3), ch, p[1]) in picked_sel
            for a, b in zip(pts, pts[1:]):
                lines += [a, b]
                cols += [(1.0, 1.0, 1.0, 1.0) if picked else GIZMO_COLORS[part]] * 2
    _lines(lines, cols, 2.0 * us)


def _draw_value_base(cache, lo, hi, us):
    """Value paths: the location path the value track floats above, and stems at the keys."""
    base = [p for t, p in zip(cache.times, cache.base_points) if _in(t, lo, hi)]
    _lines(base, [(0.6, 0.6, 0.6, 0.35)] * len(base), 1.0 * us, 'LINE_STRIP')
    stems = []
    by_t = dict(zip(cache.times, cache.base_points))
    for k in cache.keys:
        b = by_t.get(k.t)
        if b is not None and _in(k.t, lo, hi):
            stems += [b, k.co]
    _lines(stems, [(0.7, 0.7, 0.7, 0.45)] * len(stems), 1.0 * us)


RING_COLORS = {'RX': (1.0, 0.3, 0.35), 'RY': (0.45, 0.9, 0.3), 'RZ': (0.3, 0.55, 1.0)}


def _draw_rot_rings(st, cache, tg, keys, rv3d, us):
    from . import space
    if not (st.show_rot_rings or st.show_reach):
        return
    for k in keys:
        if not k.sel:
            continue
        rings, center, r = space.rot_rings(cache, tg, k)
        if st.show_reach:
            circ = space.reach_circle(center, r, rv3d)
            _lines(circ, [(1.0, 1.0, 1.0, 0.22)] * len(circ), 1.2 * us, 'LINE_STRIP')
        if st.show_rot_rings:
            for part, pts in rings.items():
                _lines(pts, [RING_COLORS[part] + (0.9,)] * len(pts), 2.0 * us, 'LINE_STRIP')


def _draw_view():
    from . import pick
    ctx = bpy.context
    scene = ctx.scene
    st = state.settings(scene)
    if st is None or not st.paths or state.overlays_hidden():
        return
    caches = state.visible_caches(scene)
    if not caches:
        return
    region, rv3d = ctx.region, ctx.region_data
    us = compat.ui_scale()
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE' if st.on_top else 'LESS_EQUAL')
    gpu.state.depth_mask_set(False)
    cur = scene.frame_current + scene.frame_subframe

    from . import onion
    onion.draw(scene, st, us)
    gpu.state.depth_test_set('NONE' if st.on_top else 'LESS_EQUAL')
    gpu.state.depth_mask_set(False)

    up = rv3d.view_rotation @ Vector((0.0, 1.0, 0.0))
    look = rv3d.view_rotation @ Vector((0.0, 0.0, -1.0))
    for cache in caches:
        if cache.ptype == core.VALUE and cache.circle is None and cache.track_mode != 'PATH' and \
                not state.modal_running and \
                (cache.vdir.angle(up, 0.0) > 0.01 or cache.vview.angle(look, 0.0) > 0.01):
            # the value track is placed relative to the screen; it turns with the view as it
            # turns (not while editing). The circle layout stays put.
            cache.vdir = up.normalized()
            cache.vview = look.normalized()
            cache.dirty = True
        if not cache.refresh():
            continue
        obj = bpy.data.objects.get(cache.obj_name)
        if obj is None or not obj.visible_get():
            continue
        editable = cache.owner_selected() and not cache.locked
        lo, hi = path_window(scene, st, cache)

        ghost = state.ghosts.get(cache.key)
        if ghost and st.show_ghost:
            _lines(ghost, [COL_GHOST] * len(ghost), st.line_width * us, 'LINE_STRIP')

        if cache.ptype == core.SCALE:
            _draw_ribbon(st, cache, rv3d, lo, hi, editable, us)
        elif cache.ptype == core.ROTATION and st.show_rot_ribbon:
            _draw_rot_ribbon(st, cache, lo, hi, editable, us)
        elif cache.ptype == core.VALUE:
            _draw_value_base(cache, lo, hi, us)
        _draw_path_runs(st, cache, editable, cur, lo, hi, us)

        if not editable:
            continue
        keys = [k for k in cache.keys if _in(k.t, lo, hi)]

        if cache.ptype == core.SCALE:
            _draw_scale_gizmos(st, cache, keys, us)

        if cache.ptype == core.ROTATION and st.show_spokes:
            tg = cache.target()
            if tg is not None:
                sp = []
                for k in keys:
                    if k.sel:
                        M, _u = cache.rot_frame(tg, k.t)
                        sp += [M.translation.copy(), k.co]
                _lines(sp, [COL_SPOKE] * len(sp), 1.2 * us)
                _draw_rot_rings(st, cache, tg, keys, rv3d, us)

        hl_coords, hl_cols = [], []
        for k in keys:
            if not handles_shown(st, cache, editable, k):
                continue
            m = pick.handle_min_world(region, rv3d, k.co)
            for side, htype in (('L', k.type_l), ('R', k.type_r)):
                h = pick.display_handle(k, side, m)
                if h is None:
                    continue
                col = HANDLE_COLORS.get(htype, HANDLE_COLORS['FREE'])
                hl_coords += [k.co, h]
                hl_cols += [col, col]
        _lines(hl_coords, hl_cols, 1.6 * us)

    if state.slide_extension is not None:
        dashed = []
        for line in state.slide_extension:
            for i in range(0, len(line) - 1, 2):
                dashed += [line[i], line[i + 1]]
        _lines(dashed, [(0.4, 0.85, 1.0, 0.6)] * len(dashed), 1.5 * us)

    if state.constraint is not None:
        p, ax, col = state.constraint
        ln = 1000.0
        _lines([p - ax * ln, p + ax * ln], [col, col], 1.5 * us)

    gpu.state.depth_test_set('NONE')
    gpu.state.blend_set('NONE')


# ----------------------------------------------------------------------------
# 2D pass
# ----------------------------------------------------------------------------

def draw_pixel():
    global _error_reported
    try:
        _draw_pixel()
    except Exception as ex:
        if not _error_reported:
            _error_reported = True
            import traceback
            traceback.print_exc()
            print("Editable Motion Paths: overlay drawing failed:", ex)


def _draw_points(ctx, st, us):
    from . import pick
    scene = ctx.scene
    region, rv3d = ctx.region, ctx.region_data
    discs = Discs()
    labels = []
    cur = scene.frame_current + scene.frame_subframe
    ks = st.key_size * 0.5 * us

    def proj(p):
        return location_3d_to_region_2d(region, rv3d, p)

    for cache in state.visible_caches(scene):
        if not cache.valid:
            continue
        obj = bpy.data.objects.get(cache.obj_name)
        if obj is None or not obj.visible_get():
            continue
        editable = cache.owner_selected() and not cache.locked
        lo, hi = path_window(scene, st, cache)

        if st.show_frames and cache.key not in state.sculpt_hide:
            col = COL_FRAME if editable else (0.7, 0.7, 0.7, 0.5)
            r = 1.8 * us
            vis, rep = _sample_flags(cache)
            for t, p, v, r_ in zip(cache.times, cache.points, vis, rep):
                if v and not r_ and _in(t, lo, hi) and abs(t - round(t)) < 1e-4:
                    discs.add(proj(p), r, col)

        if cache.fs <= cur <= cache.fe and _in(cur, lo, hi):
            tg = cache.target()
            if tg is not None:
                discs.add_outlined(proj(cache.point(tg, cur)), ks * 0.75, COL_CURRENT)

        active = state.get_active(scene)
        # dense keys (baked / mocap) get smaller dots, so the path stays readable
        scr = [proj(k.co) for k in cache.keys]
        last_label = None
        for i, k in enumerate(cache.keys):
            if k.hidden or not _in(k.t, lo, hi):
                continue
            s = scr[i]
            near = [(s - q).length for q in (scr[i - 1] if i > 0 else None,
                                             scr[i + 1] if i + 1 < len(scr) else None)
                    if q is not None and s is not None]
            gap = min(near) if near else 1e9
            kr = ks if gap >= 3.0 * ks else max(2.5 * us, round(gap * 0.33 * 2.0) / 2.0)
            ks_all = cache.keys
            if not k.sel and (i == 0 or k.t - ks_all[i - 1].t <= 2.0 + 1e-6) and \
                    (i + 1 == len(ks_all) or ks_all[i + 1].t - k.t <= 2.0 + 1e-6):
                kr = min(kr, 3.0 * us)      # baked keys (every frame or two): small dots
            if editable:
                if cache.ptype == core.SCALE and k.sel:
                    for part, p in scale_gizmo_points(st, cache, k).items():
                        g = proj(p)
                        if g is not None:
                            big = part == 'GU' or part[:2] == {'X': 'GX', 'Y': 'GY', 'Z': 'GZ'}.get(st.scale_channel)
                            discs.add_outlined(g, ks * (0.75 if big else 0.6), GIZMO_COLORS[part])
                    wcol = GIZMO_COLORS.get({'X': 'GX', 'Y': 'GY', 'Z': 'GZ'}.get(st.scale_channel), COL_HANDLE_SEL)
                    for _part, _pts, end in scale_whiskers(st, cache, k):
                        discs.add_outlined(proj(end), ks * 0.55, wcol)
                if handles_shown(st, cache, editable, k):
                    m = pick.handle_min_world(region, rv3d, k.co)
                    for side, htype, hsel in (('L', k.type_l, k.sel_l), ('R', k.type_r, k.sel_r)):
                        h = pick.display_handle(k, side, m)
                        if h is None:
                            continue
                        col = COL_HANDLE_SEL if hsel else HANDLE_COLORS.get(htype, HANDLE_COLORS['FREE'])
                        discs.add_outlined(proj(h), ks * 0.7, col)
                col = COL_KEY_SEL if k.sel else COL_KEY
                w = state.prop_weights.get((cache.key, round(k.t, 3)))
                if w and not k.sel:
                    # proportional editing influence: white -> orange
                    col = tuple(a + (b - a) * min(1.0, w) * 0.85 for a, b in zip(COL_KEY, COL_KEY_SEL))
                if k.sel and active is not None and active[0] == cache.key and abs(active[1] - k.t) < 1e-3:
                    discs.add(s, kr + 3.5 * us, (1.0, 1.0, 1.0, 1.0))     # active key: white ring
                if k.partial:
                    # keyed on some channels only: smaller, with a dark center
                    discs.add_outlined(s, kr * 0.8, col)
                    discs.add(s, kr * 0.35, COL_OUTLINE)
                else:
                    discs.add_outlined(s, kr, col)
            else:
                discs.add_outlined(s, kr * 0.7, COL_KEY_LOCKED, 1.0)
            show_val = cache.ptype in (core.SCALE, core.VALUE) and editable
            if (st.show_frame_numbers or state.force_numbers or show_val) and s is not None and \
                    (last_label is None or (s - last_label).length > 22.0 * us):
                txt = ""
                if st.show_frame_numbers or state.force_numbers:
                    txt = "%d" % round(k.t) if abs(k.t - round(k.t)) < 1e-3 else "%.2f" % k.t
                if show_val:
                    v = k.vals or []
                    val = ("%.2f" % v[0]) if len(v) == 1 or max(v) - min(v) < 1e-3 else \
                            " ".join("%.2f" % x for x in v)
                    txt = (txt + "  " if txt else "") + val
                labels.append((s, txt))
                last_label = s

    gpu.state.blend_set('ALPHA')
    discs.draw()
    if labels:
        font = 0
        blf.size(font, 11 * us)
        blf.color(font, 1.0, 1.0, 1.0, 0.9)
        blf.enable(font, blf.SHADOW)
        blf.shadow(font, 3, 0.0, 0.0, 0.0, 0.9)
        for s, txt in labels:
            blf.position(font, s.x + ks + 3 * us, s.y + ks * 0.5, 0)
            blf.draw(font, txt)
        blf.disable(font, blf.SHADOW)


def _draw_prop_circle(ctx, us):
    if state.prop_circle is None:
        return
    region, rv3d = ctx.region, ctx.region_data
    center, radius = state.prop_circle
    c = location_3d_to_region_2d(region, rv3d, center)
    right = rv3d.view_rotation @ Vector((1.0, 0.0, 0.0))
    e = location_3d_to_region_2d(region, rv3d, center + right * radius)
    if c is None or e is None:
        return
    r = (e - c).length
    pts = [(c.x + math.cos(a * 2 * math.pi / 64) * r, c.y + math.sin(a * 2 * math.pi / 64) * r)
           for a in range(64)]
    sh = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    sh.uniform_float("color", (1.0, 1.0, 1.0, 0.7))
    batch_for_shader(sh, 'LINE_LOOP', {"pos": pts}).draw(sh)


def _flat(pts, color, kind='LINES'):
    if len(pts) < 2:
        return
    sh = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    sh.uniform_float("color", color)
    batch_for_shader(sh, kind, {"pos": pts}).draw(sh)


def chart_span(region, us, area=None):
    """Left and right end of the timing chart, in the bottom right corner of the view (left of
    the sidebar when it overlaps the view); the speed graph above it is as wide."""
    right = region.width
    if area is not None:
        for r in area.regions:
            # an overlapping sidebar covers the right part of the view
            if r.type == 'UI' and r.width > 1 and region.x < r.x < region.x + region.width:
                right = min(right, r.x - region.x)
    x1 = right - 40.0 * us
    return max(x1 - 560.0 * us, 40.0 * us), x1


def speed_window(scene, st, cache):
    """Frames the speed graph shows: the selected keys or the keys around the current frame
    (Segment), the set frames (Range) or the scene's frame range (Timeline)."""
    if st.speed_range == 'RANGE':
        return float(st.speed_start), float(max(st.speed_end, st.speed_start + 1))
    if st.speed_range == 'AUTO':
        if scene.use_preview_range:
            return float(scene.frame_preview_start), float(scene.frame_preview_end)
        return float(scene.frame_start), float(scene.frame_end)
    sel = [k.t for k in cache.keys if k.sel]
    if len(sel) >= 2:
        return min(sel), max(sel)
    ks = [k.t for k in cache.keys]
    if len(ks) < 2:
        return None
    if len(sel) == 1:
        # one key: the segments on both sides of it
        i = ks.index(sel[0])
        return ks[max(i - 1, 0)], ks[min(i + 1, len(ks) - 1)]
    cur = scene.frame_current
    before = [t for t in ks if t <= cur]
    after = [t for t in ks if t > cur]
    if not before:
        return ks[0], ks[1]
    if not after:
        return ks[-2], ks[-1]
    return before[-1], after[0]


def _draw_chart(ctx, st, us):
    """Timing chart of the selected keys: a ruler with a tick where every frame is along the
    path between the first and the last selected key (like an animator's timing chart)."""
    state.chart = None
    if not st.show_timing_chart or ctx.area is None:
        return
    cache = next((c for c in state.editable_caches(ctx.scene) if sum(1 for k in c.keys if k.sel) >= 2), None)
    if cache is None:
        return
    sel = [k.t for k in cache.keys if k.sel]
    r0, r1 = min(sel), max(sel)
    pts = [(t, p) for t, p in zip(cache.times, cache.points) if r0 - 1e-6 <= t <= r1 + 1e-6]
    if len(pts) < 2:
        return
    arc = [0.0]
    for (_a, p), (_b, q) in zip(pts, pts[1:]):
        arc.append(arc[-1] + (q - p).length)
    total = arc[-1] or 1.0
    region = ctx.region
    x0, x1 = chart_span(region, us, ctx.area)
    y = 80.0 * us
    keys = {round(k.t, 3) for k in cache.keys}
    ticks = []
    lines = [(x0, y), (x1, y)]
    key_lines = []
    font = 0
    blf.size(font, 10 * us)
    blf.color(font, 1.0, 1.0, 1.0, 0.85)
    for (t, _p), s in zip(pts, arc):
        if abs(t - round(t)) > 1e-4:
            continue
        x = x0 + (x1 - x0) * s / total
        is_key = round(t, 3) in keys
        ticks.append((x, float(round(t)), is_key))
        if is_key:
            key_lines += [(x, y - 9 * us), (x, y + 9 * us)]
            blf.position(font, x - 6 * us, y + 12 * us, 0)
            blf.draw(font, "%d" % round(t))
        else:
            lines += [(x, y - 5 * us), (x, y + 5 * us)]
    _flat(lines, (1.0, 1.0, 1.0, 0.75))
    _flat(key_lines, (1.0, 0.6, 0.15, 1.0))
    blf.position(font, x0, y - 22 * us, 0)
    blf.draw(font, "Timing chart: drag a tick to move that frame along the path")
    state.chart = {"area": ctx.area.as_pointer(), "cache": cache.key, "r0": r0, "r1": r1,
                   "x0": x0, "x1": x1, "y": y, "ticks": ticks}


def _draw_speed_graph(ctx, st, us):
    """Speed of the active path (the one the timing chart shows, else the first editable one),
    right above the timing chart and as wide."""
    state.speed_graph = None
    if not st.show_speed_graph:
        return
    caches = state.editable_caches(ctx.scene)
    cache = next((c for c in caches if sum(1 for k in c.keys if k.sel) >= 2), None) or \
        next(iter(caches), None)
    if cache is None or not cache.speeds:
        return
    win = speed_window(ctx.scene, st, cache)
    if win is None:
        return
    lo, hi = win
    data = [(t, s) for t, s in zip(cache.times, cache.speeds) if lo - 1e-6 <= t <= hi + 1e-6]
    if len(data) < 2:
        return
    vmax = max(s for _t, s in data) or 1.0
    x0, x1 = chart_span(ctx.region, us, ctx.area)
    y0 = 118.0 * us
    state.speed_graph = (cache.key, lo, hi)
    w, h = x1 - x0, 60.0 * us
    t0, t1 = data[0][0], data[-1][0]

    pad = 8.0 * us                      # room for the key frame numbers at the bottom
    gy0, gh = y0 + 14.0 * us, h - 18.0 * us

    def X(t):
        return x0 + pad + (w - 2 * pad) * (t - t0) / max(t1 - t0, 1e-6)

    def Y(s):
        return gy0 + gh * s / (vmax * 1.2)       # some room above the fastest part
    # speed of a step, drawn in the middle of it
    pts = [((t + tn) * 0.5, s) for (t, s), (tn, _s) in zip(data, data[1:])]
    pts = [(t0, pts[0][1])] + pts + [(t1, pts[-1][1])]     # the curve reaches the end keys
    _rounded_rect(x0, y0, w, h, 7.0 * us, (0.08, 0.08, 0.1, 0.62))
    # soft fill under the curve, fading out at the bottom
    tris, cols = [], []
    top, bottom = (0.45, 0.8, 1.0, 0.32), (0.45, 0.8, 1.0, 0.03)
    for (ta, sa), (tb, sb) in zip(pts, pts[1:]):
        a0, a1, b0, b1 = (X(ta), gy0), (X(ta), Y(sa)), (X(tb), gy0), (X(tb), Y(sb))
        tris += [a0, a1, b1, a0, b1, b0]
        cols += [bottom, top, top, bottom, top, bottom]
    _smooth(tris, cols, 'TRIS')
    # keys: a faint line through the graph, a diamond on the curve, the frame below
    lines, diamonds, sel_diamonds = [], [], []
    font = 0
    blf.size(font, 9 * us)
    for k in cache.keys:
        if not t0 - 1e-6 <= k.t <= t1 + 1e-6:
            continue
        x = X(k.t)
        lines += [(x, gy0), (x, gy0 + gh)]
        s = _speed_at(pts, k.t)
        d = 4.0 * us
        y = Y(s)
        quad = [(x, y - d), (x + d, y), (x, y + d), (x - d, y)]
        (sel_diamonds if k.sel else diamonds).append(quad)
        blf.color(font, *((1.0, 0.75, 0.4, 0.9) if k.sel else (0.85, 0.85, 0.85, 0.7)))
        label = "%d" % round(k.t)
        tw = blf.dimensions(font, label)[0]
        blf.position(font, x - tw * 0.5, y0 + 3.0 * us, 0)
        blf.draw(font, label)
    _flat(lines, (1.0, 1.0, 1.0, 0.12))
    _flat([(X(t), Y(s)) for t, s in pts], (0.55, 0.85, 1.0, 1.0), 'LINE_STRIP')
    for quads, col in ((diamonds, (0.92, 0.92, 0.92, 1.0)), (sel_diamonds, (1.0, 0.62, 0.2, 1.0))):
        if quads:
            _flat([p for q in quads for p in (q[0], q[1], q[2], q[0], q[2], q[3])], col, 'TRIS')
    cur = ctx.scene.frame_current
    if t0 <= cur <= t1:
        x = X(cur)
        _flat([(x, gy0), (x, gy0 + gh)], (0.35, 1.0, 0.5, 0.8))
        y = Y(_speed_at(pts, cur))
        r = 3.0 * us
        _flat([(x + math.cos(a * math.pi / 6) * r, y + math.sin(a * math.pi / 6) * r) for a in range(12)],
              (0.35, 1.0, 0.5, 1.0), 'TRI_FAN')
    blf.size(font, 10 * us)
    blf.color(font, 1.0, 1.0, 1.0, 0.75)
    blf.position(font, x0 + 4 * us, y0 + h + 5 * us, 0)       # above the graph
    blf.draw(font, "Speed  \u00b7  %s" % cache.label)


def _speed_at(pts, t):
    """Speed at frame t from the step speeds (linear between them)."""
    if not pts:
        return 0.0
    if t <= pts[0][0]:
        return pts[0][1]
    for (ta, sa), (tb, sb) in zip(pts, pts[1:]):
        if ta <= t <= tb:
            return sa + (sb - sa) * ((t - ta) / (tb - ta) if tb > ta else 0.0)
    return pts[-1][1]


def _rounded_rect(x, y, w, h, r, col):
    r = min(r, w * 0.5, h * 0.5)
    pts = [(x + w * 0.5, y + h * 0.5)]
    for cx, cy, a0 in ((x + w - r, y + r, -90), (x + w - r, y + h - r, 0), (x + r, y + h - r, 90),
                       (x + r, y + r, 180)):
        for i in range(7):
            a = math.radians(a0 + 15 * i)
            pts.append((cx + math.cos(a) * r, cy + math.sin(a) * r))
    pts.append(pts[1])
    _flat(pts, col, 'TRI_FAN')


def _smooth(pts, cols, kind):
    if not pts:
        return
    sh = gpu.shader.from_builtin('SMOOTH_COLOR')
    batch_for_shader(sh, kind, {"pos": pts, "color": cols}).draw(sh)


def _draw_knife(ctx, us):
    """The knife's cut lines (kept in 3D, so they stay on the paths when the view turns), their
    points, the cursor and what it snapped to - like the mesh Knife."""
    lines3d, _area, cur, snap = state.knife[:4]
    open_cut = state.knife[4] if len(state.knife) > 4 else True
    region, rv3d = ctx.region, ctx.region_data
    lines = []
    for ln in lines3d:
        pts = [s for s in (location_3d_to_region_2d(region, rv3d, p) for p in ln) if s is not None]
        lines.append(pts)
    if cur is not None and lines3d and open_cut:
        lines[-1] = lines[-1] + [cur]
    gpu.state.blend_set('ALPHA')
    gpu.state.line_width_set(2.0)
    for pts in lines:
        if len(pts) >= 2:
            _flat([(p.x, p.y) for p in pts], (0.3, 1.0, 0.4, 0.9), 'LINE_STRIP')
    gpu.state.line_width_set(1.0)
    sq = 3.0 * us
    quads = []
    for pts in lines:
        for p in pts[:-1] if cur is not None and open_cut and pts is lines[-1] else pts:
            quads += [(p.x - sq, p.y - sq), (p.x + sq, p.y - sq), (p.x + sq, p.y + sq),
                      (p.x - sq, p.y - sq), (p.x + sq, p.y + sq), (p.x - sq, p.y + sq)]
    if quads:
        _flat(quads, (0.3, 1.0, 0.4, 1.0), 'TRIS')
    if cur is not None:
        r = (6.0 if snap else 4.0) * us
        col = (1.0, 0.85, 0.2, 1.0) if snap else (1.0, 1.0, 1.0, 0.9)
        ring = [(cur.x + math.cos(a * math.pi / 12) * r, cur.y + math.sin(a * math.pi / 12) * r) for a in range(25)]
        _flat(ring, col, 'LINE_STRIP')


def _draw_sculpt(ctx, us):
    """The sculpt brush: its circle (with the Sculpt Motion Path tool) and, while a timing
    brush paints, the new frame dots."""
    if ctx.area is None:
        return
    prev = state.sculpt_preview
    if prev is not None and prev[1] == ctx.area.as_pointer():
        discs = Discs()
        for p in prev[0]:
            discs.add(location_3d_to_region_2d(ctx.region, ctx.region_data, p), 2.4 * us, (1.0, 0.7, 0.25, 1.0))
        gpu.state.blend_set('ALPHA')
        discs.draw()
    tool = ctx.workspace.tools.from_space_view3d_mode(ctx.mode, create=False)
    if tool is None or tool.idname not in {"emp.sculpt_path_pose", "emp.sculpt_path_object"}:
        return
    from . import ops_sculpt
    ops_sculpt.draw_strip(ctx, us)
    rad = state.sculpt_radial
    if rad is not None and rad["area"] == ctx.area.as_pointer():
        _draw_radial(rad, us)
        return
    cur = state.sculpt_cursor
    if cur is None or cur[3] != ctx.area.as_pointer():
        return
    x, y, r, _a, painting = cur
    bs = getattr(ctx.window_manager, "emp_brush", None)
    if bs is not None:
        r = bs.radius * us          # the radius as it is now (changed from the header, F, ...)
    ring =[(x + math.cos(a * math.pi / 32) * r, y + math.sin(a * math.pi / 32) * r) for a in range(65)]
    gpu.state.line_width_set(1.5)
    _flat(ring, (1.0, 0.7, 0.25, 0.95) if painting else (1.0, 1.0, 1.0, 0.7), 'LINE_STRIP')
    gpu.state.line_width_set(1.0)


def _draw_radial(rad, us):
    """F / Shift+F like Blender's radial control: the brush circle, the strength as a filled
    disc inside it, a line to the mouse and the value in the middle."""
    x, y, r = rad["x"], rad["y"], rad["r"]

    def ring(rr):
        return [(x + math.cos(a * math.pi / 32) * rr, y + math.sin(a * math.pi / 32) * rr) for a in range(65)]
    gpu.state.blend_set('ALPHA')
    if rad["strength"] is not None:
        inner = r * rad["strength"]
        _flat([(x, y)] + ring(inner), (1.0, 0.7, 0.25, 0.28), 'TRI_FAN')
        _flat(ring(inner), (1.0, 0.7, 0.25, 0.9), 'LINE_STRIP')
    else:
        _flat([(x, y)] + ring(r), (1.0, 1.0, 1.0, 0.07), 'TRI_FAN')
    gpu.state.line_width_set(2.0)
    _flat(ring(r), (1.0, 1.0, 1.0, 0.9), 'LINE_STRIP')
    gpu.state.line_width_set(1.0)
    _flat([(x, y), rad["mouse"]], (1.0, 1.0, 1.0, 0.5))
    blf.size(0, 16 * us)
    w, h = blf.dimensions(0, rad["text"])
    blf.color(0, 1.0, 1.0, 1.0, 1.0)
    blf.enable(0, blf.SHADOW)
    blf.shadow(0, 3, 0.0, 0.0, 0.0, 0.9)
    blf.position(0, x - w * 0.5, y - h * 0.5, 0)
    blf.draw(0, rad["text"])
    blf.disable(0, blf.SHADOW)


def _draw_snap_point(ctx, us):
    if state.snap_point is None:
        return
    s = location_3d_to_region_2d(ctx.region, ctx.region_data, state.snap_point)
    if s is None:
        return
    r = 7 * us
    pts = [(s.x + math.cos(a * 2 * math.pi / 24) * r, s.y + math.sin(a * 2 * math.pi / 24) * r)
           for a in range(24)]
    sh = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.line_width_set(2.0)
    sh.uniform_float("color", (1.0, 1.0, 1.0, 0.95))
    batch_for_shader(sh, 'LINE_LOOP', {"pos": pts}).draw(sh)
    gpu.state.line_width_set(1.0)


def _draw_slide_marker(ctx, us):
    if state.slide_marker is None:
        return
    p, label = state.slide_marker
    s = location_3d_to_region_2d(ctx.region, ctx.region_data, p)
    if s is None:
        return
    gpu.state.blend_set('ALPHA')
    discs = Discs()
    discs.add(s, 9 * us, (0.3, 0.85, 1.0, 1.0))
    discs.add(s, 6 * us, COL_OUTLINE)
    discs.add(s, 3 * us, (0.3, 0.85, 1.0, 1.0))
    discs.draw()
    font = 0
    blf.size(font, 13 * us)
    blf.enable(font, blf.SHADOW)
    blf.shadow(font, 3, 0.0, 0.0, 0.0, 1.0)
    blf.color(font, 0.4, 0.9, 1.0, 1.0)
    blf.position(font, s.x + 12 * us, s.y - 18 * us, 0)
    blf.draw(font, label)
    blf.disable(font, blf.SHADOW)


def _tool_active(ctx):
    try:
        tool = ctx.workspace.tools.from_space_view3d_mode(ctx.mode, create=False)
    except Exception:
        return False
    return tool is not None and tool.idname in {"emp.edit_path_pose", "emp.edit_path_object"}


def _draw_mode_indicator(ctx, st, us):
    """Bottom-center hint: which trajectory is being edited, how to switch."""
    if not _tool_active(ctx):
        return
    region = ctx.region
    font = 0
    blf.size(font, 12 * us)
    m = st.edit_mode
    parts = [("1 Location", m == 'LOCATION'), ("   2 Rotation", m == 'ROTATION'),
             ("   3 Scale", m == 'SCALE'), ("   4 Value", m == 'VALUE')]
    ts = ctx.scene.tool_settings
    if ts.use_proportional_edit:
        name = ts.bl_rna.properties['proportional_edit_falloff'].enum_items[ts.proportional_edit_falloff].name
        parts.append(("      O Proportional: %s %.2f" % (name, st.proportional_size), True))
    hint = ""
    for cache in state.visible_caches(ctx.scene):
        if cache.locked and cache.owner_selected():
            hint = cache.lock_reason
            break
        if cache.ptype == core.ROTATION and st.show_rot_rings and cache.owner_selected() and \
                any(k.sel for k in cache.keys):
            hint = "Drag a ring: turn around that axis (green: twist)   drag the key: turn freely   " \
                   "the faint circle: around the view"
    widths = [blf.dimensions(font, t)[0] for t, _a in parts]
    x = (region.width - sum(widths)) * 0.5
    y = 24 * us
    blf.enable(font, blf.SHADOW)
    blf.shadow(font, 3, 0.0, 0.0, 0.0, 0.9)
    for (text, active), w in zip(parts, widths):
        if active:
            blf.color(font, 1.0, 0.65, 0.2, 1.0)
        else:
            blf.color(font, 0.75, 0.75, 0.75, 0.8)
        blf.position(font, x, y, 0)
        blf.draw(font, text)
        x += w
    if state.flash is not None:
        import time
        if time.time() < state.flash[1]:
            hint = state.flash[0]
        else:
            state.flash = None
    if hint:
        w = blf.dimensions(font, hint)[0]
        blf.color(font, 1.0, 0.85, 0.4, 1.0)
        blf.position(font, (region.width - w) * 0.5, y + 20 * us, 0)
        blf.draw(font, hint)
    blf.disable(font, blf.SHADOW)


def _draw_pixel():
    ctx = bpy.context
    st = state.settings(ctx.scene)
    us = compat.ui_scale()
    font = 0

    state.chart = None if state.chart is None or ctx.area is None or \
        state.chart["area"] != ctx.area.as_pointer() else state.chart
    if not state.overlays_hidden():
        _draw_sculpt(ctx, us)       # the brush strip and circle show even before there's a path
    if st is not None and st.paths and not state.overlays_hidden():
        _draw_points(ctx, st, us)
        _draw_chart(ctx, st, us)
        _draw_speed_graph(ctx, st, us)
        _draw_prop_circle(ctx, us)
        _draw_slide_marker(ctx, us)
        _draw_snap_point(ctx, us)
        _draw_mode_indicator(ctx, st, us)

    if state.box is not None and ctx.area is not None and ctx.area.as_pointer() == state.box_area:
        x0, y0, x1, y1, mode = state.box
        sh = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        coords = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        sh.uniform_float("color", (1.0, 1.0, 1.0, 0.06))
        batch_for_shader(sh, 'TRI_FAN', {"pos": coords}).draw(sh)
        sh.uniform_float("color", (1.0, 1.0, 1.0, 0.8) if mode != 'SUB' else (1.0, 0.4, 0.4, 0.8))
        batch_for_shader(sh, 'LINE_LOOP', {"pos": coords}).draw(sh)

    if state.knife is not None and ctx.area is not None and ctx.area.as_pointer() == state.knife[1]:
        _draw_knife(ctx, us)
    if state.markers is not None and ctx.area is not None and ctx.area.as_pointer() == state.markers[1]:
        discs = Discs()
        labels = state.markers[2] if len(state.markers) > 2 else None
        blf.size(font, 10 * us)
        blf.color(font, 0.6, 1.0, 0.65, 1.0)
        for i, p in enumerate(state.markers[0]):
            s = location_3d_to_region_2d(ctx.region, ctx.region_data, p)
            if s is None:
                continue
            discs.add_outlined(s, 4.5 * us, (0.3, 1.0, 0.4, 1.0))
            if labels and labels[i]:
                blf.position(font, s.x + 7 * us, s.y + 5 * us, 0)
                blf.draw(font, labels[i])
        gpu.state.blend_set('ALPHA')
        discs.draw()
    if state.circle is not None and ctx.area is not None and ctx.area.as_pointer() == state.circle[3]:
        cx, cy, r, _a = state.circle
        pts = [(cx + math.cos(a * 2 * math.pi / 48) * r, cy + math.sin(a * 2 * math.pi / 48) * r)
               for a in range(48)]
        sh = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        sh.uniform_float("color", (1.0, 1.0, 1.0, 0.8))
        batch_for_shader(sh, 'LINE_LOOP', {"pos": pts}).draw(sh)

    if state.hud is not None and ctx.area is not None and ctx.area.as_pointer() == state.hud_area:
        text, (x, y) = state.hud
        blf.size(font, 13 * us)
        w, h = blf.dimensions(font, text)
        sh = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        pad = 5 * us
        x += 18 * us
        y += 14 * us
        coords = [(x - pad, y - pad), (x + w + pad, y - pad), (x + w + pad, y + h + pad), (x - pad, y + h + pad)]
        sh.uniform_float("color", (0.0, 0.0, 0.0, 0.6))
        batch_for_shader(sh, 'TRI_FAN', {"pos": coords}).draw(sh)
        blf.color(font, 1.0, 1.0, 1.0, 1.0)
        blf.position(font, x, y, 0)
        blf.draw(font, text)
    gpu.state.blend_set('NONE')


def register():
    _handles.append(bpy.types.SpaceView3D.draw_handler_add(draw_view, (), 'WINDOW', 'POST_VIEW'))
    _handles.append(bpy.types.SpaceView3D.draw_handler_add(draw_pixel, (), 'WINDOW', 'POST_PIXEL'))


def unregister():
    for h in _handles:
        bpy.types.SpaceView3D.draw_handler_remove(h, 'WINDOW')
    _handles.clear()
