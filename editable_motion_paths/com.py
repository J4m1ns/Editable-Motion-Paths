"""Center of mass: where the character's weight is, from its deform bones, to check that a
pose is believable (a visual aid only; nothing is changed).

* The mass of every deform bone is worked out once from the meshes it moves: the onion proxies
  (the meshes melted down, with their bone weights), each vertex counting its area times its
  distance from the bone (about the volume around the bone), so a torso weighs more than a
  hand. A pose bone's custom property "com_mass" scales its mass by hand.
* The point follows the pose live (from the bone matrices only); a plumb line drops it to the
  floor.
* In the background, frame by frame over the paths' range (only the bones are evaluated, like
  the onion ghosts): the center's trajectory (optional), its velocity (an arrow: where the body
  is going) and the floor level. The support: the parts of the meshes touching the floor (their
  outline on it); the plumb point is green inside it (a stable pose), red outside (falling or
  moving), gray when nothing touches the floor (in the air)."""

import time

import bpy
import gpu
from gpu_extras.batch import batch_for_shader

from . import onion, state

_mass = {}          # armature name -> dict(sig, names, rest_inv, m, c, height)
_track = {}         # armature name -> {frame: (center (3,), lowest z)}
_redo = {}          # armature name -> frames to work out again (the old values stay meanwhile)
_timer_on = False
_busy = False
_now = {}           # armature name -> (key, center, contacts xy, lowest z) for the pose of now
status = ""


def busy():
    return _busy


def clear():
    _mass.clear()
    _track.clear()
    _redo.clear()
    _now.clear()


def changed():
    """The animation changed: the track is worked out again (the point itself is live; the
    old track stays until each frame is renewed, so nothing blinks)."""
    if _track:
        for name, tr in _track.items():
            _redo.setdefault(name, set()).update(tr)
        _schedule(0.05)


def armatures(scene):
    """The armatures to show the center of: the ones with edited bones, and the active one."""
    names = list(onion._edited(scene)[0])
    ob = bpy.context.view_layer.objects.active if bpy.context.view_layer else None
    if ob is not None and ob.type == 'ARMATURE' and ob.name not in names:
        names.append(ob.name)
    return [bpy.data.objects[n] for n in names if n in bpy.data.objects]


def _meshes(scene, arm):
    return [o for o in scene.objects if o.type == 'MESH' and o.visible_get() and
            any(m.type == 'ARMATURE' and m.object == arm and m.show_viewport for m in o.modifiers)]


def _seg_dist(p, a, b):
    """(P,) distance of points p (P, 3) from the segment a-b."""
    import numpy as np
    ab = b - a
    L = float(ab @ ab)
    if L < 1e-12:
        return np.linalg.norm(p - a, axis=1)
    f = np.clip((p - a) @ ab / L, 0.0, 1.0)
    return np.linalg.norm(p - (a + f[:, None] * ab), axis=1)


def _build_mass(scene, arm, st):
    """Bone masses (once per meshes / detail). Returns True when it had work to do."""
    import numpy as np
    meshes = _meshes(scene, arm)
    custom = tuple((pb.name, float(pb.get("com_mass", 1.0))) for pb in arm.pose.bones if "com_mass" in pb)
    sig = (tuple(o.name for o in meshes), tuple(onion._mesh_sig(o) for o in meshes), st.onion_max_tris, custom)
    old = _mass.get(arm.name)
    if old is not None and old["sig"] == sig:
        return False
    dg = bpy.context.evaluated_depsgraph_get()
    built = False
    for o in meshes:
        p = onion._proxies.get(o.name)
        if p is None or p.sig != (onion._mesh_sig(o), 'DEFORM', arm.name, st.onion_max_tris):
            onion._build(o, arm, 'DEFORM', st, dg)
            built = True
            break       # one mesh per tick
    if built:
        return True
    acc = {}
    zs = []
    for o in meshes:
        p = onion._proxies.get(o.name)
        if p is None or p.rest is None or not len(p.tri):
            continue
        r = p.rest[:, :3]
        zs.append((r[:, 2].min(), r[:, 2].max()))
        a, b, c = r[p.tri[:, 0]], r[p.tri[:, 1]], r[p.tri[:, 2]]
        area = 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)
        va = np.zeros(len(r))
        for i in range(3):
            np.add.at(va, p.tri[:, i], area / 3.0)
        for k in range(p.w.shape[1]):
            for bi, name in enumerate(p.bones):
                sel = (p.idx[:, k] == bi) & (p.w[:, k] > 0.0)
                if not sel.any():
                    continue
                bone = arm.data.bones.get(name)
                if bone is None:
                    continue
                d = _seg_dist(r[sel], np.array(bone.head_local), np.array(bone.tail_local))
                vol = p.w[sel, k] * va[sel] * np.maximum(d, 1e-4) * 0.5
                m, cx = acc.get(name, (0.0, np.zeros(3)))
                acc[name] = (m + float(vol.sum()), cx + (vol[:, None] * r[sel]).sum(axis=0))
    names = [n for n, (m, _c) in acc.items() if m > 0.0]
    scale = dict(custom)
    m = np.array([acc[n][0] * scale.get(n, 1.0) for n in names])
    c = np.array([np.append(acc[n][1] / acc[n][0], 1.0) for n in names]).reshape(-1, 4)
    height = (max(z[1] for z in zs) - min(z[0] for z in zs)) if zs else 1.0
    _mass[arm.name] = dict(sig=sig, names=names, rest_inv=onion.rest_inverses(arm, names), m=m, c=c,
                           height=max(height, 1e-3))
    _track.pop(arm.name, None)
    _now.pop(arm.name, None)
    return True


def center(arm):
    """The center of mass of the pose of now (world), or None."""
    import numpy as np
    d = _mass.get(arm.name)
    if d is None or not d["names"] or d["m"].sum() <= 0.0:
        return None
    Ma, Bs = onion.bone_mats(arm, d["names"], d["rest_inv"])
    pts = np.einsum('bij,bj->bi', Bs, d["c"])
    g = (d["m"][:, None] * pts).sum(axis=0) / d["m"].sum()
    return (Ma @ g)[:3]


def _lowest(scene, arm):
    """The lowest point of the character's meshes (their proxies) in the pose of now, and the
    proxies' points (world)."""
    import numpy as np
    pts = [onion._pose(onion._proxies[o.name]) for o in _meshes(scene, arm) if o.name in onion._proxies]
    pts = [p for p in pts if p is not None and len(p)]
    if not pts:
        return None, None
    allp = np.concatenate(pts)
    return float(allp[:, 2].min()), allp


# ---- the track: frame by frame in the background -------------------------------------------

def _range(scene):
    cs = state.editable_caches(scene)
    if cs:
        return min(c.fs for c in cs), max(c.fe for c in cs)
    return scene.frame_start, scene.frame_end


def _schedule(delay=0.05):
    global _timer_on
    if not _timer_on:
        _timer_on = True
        bpy.app.timers.register(_tick, first_interval=delay)


def _tick():
    global _timer_on, _busy, status
    try:
        scene = bpy.context.scene
        st = state.settings(scene)
        if st is None or not st.show_com:
            status = ""
            _timer_on = False
            return None
        screen = bpy.context.screen
        if (screen is not None and screen.is_animation_playing) or state.modal_running or onion.busy():
            return 0.2
        arms = armatures(scene)
        for arm in arms:
            _busy = True
            try:
                work = _build_mass(scene, arm, st)
            finally:
                _busy = False
            if work:
                status = "Preparing center of mass"
                state.tag_redraw()
                return 0.01
        status = ""
        f0, f1 = _range(scene)
        for arm in arms:
            tr = _track.setdefault(arm.name, {})
            redo = _redo.setdefault(arm.name, set())
            cur_f = scene.frame_current
            # the frames around the current one first (the arrow), then the rest
            want = sorted((f for f in range(f0 - 1, f1 + 2) if f not in tr or f in redo),
                          key=lambda f: abs(f - cur_f))
            if not want:
                continue
            cur, sub = scene.frame_current, scene.frame_subframe
            meshes = _meshes(scene, arm)
            t0 = time.time()
            _busy = True
            hidden = []
            try:
                for o in meshes:
                    if not o.hide_viewport:
                        o.hide_viewport = True      # only the bones are evaluated
                        hidden.append(o)
                for f in want:
                    scene.frame_set(f)
                    g = center(arm)
                    low, _p = _lowest(scene, arm)
                    tr[f] = (g, low) if g is not None else None
                    redo.discard(f)
                    if time.time() - t0 > 0.04:
                        break
            finally:
                for o in hidden:
                    o.hide_viewport = False
                scene.frame_set(cur, subframe=sub)
                _busy = False
            state.tag_redraw()
            return 0.01
        _timer_on = False
        return None
    except Exception as ex:   # never let the timer die with a traceback storm
        import traceback
        traceback.print_exc()
        print("Editable Motion Paths: center of mass failed:", ex)
        _busy = False
        _timer_on = False
        status = ""
        return None


# ---- support: what touches the floor -----------------------------------------------------

def _hull(pts):
    """Convex hull (counter-clockwise) of 2D points (monotone chain)."""
    pts = sorted(set(pts))
    if len(pts) < 3:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _inside(poly, x, y):
    if len(poly) < 3:
        return False
    for (ax, ay), (bx, by) in zip(poly, poly[1:] + poly[:1]):
        if (bx - ax) * (y - ay) - (by - ay) * (x - ax) < 0.0:
            return False
    return True


def state_now(scene, arm):
    """(center, floor z, support outline [(x, y)], balance 'IN' / 'OUT' / 'AIR', velocity) for the
    pose of now; worked out again only when the pose changes."""
    import numpy as np
    d = _mass.get(arm.name)
    if d is None:
        return None
    g = center(arm)
    if g is None:
        return None
    tr = _track.get(arm.name) or {}
    lows = [v[1] for v in tr.values() if v is not None and v[1] is not None]
    Ma, Bs = onion.bone_mats(arm, d["names"], d["rest_inv"])
    st_ = state.settings(scene)
    key = hash(Bs.tobytes()) ^ hash(Ma.tobytes()) ^ hash(len(lows)) ^ hash(
        (st_.com_floor, round(st_.com_floor_height, 5), round(scene.cursor.location.z, 5)) if st_ else 0)
    old = _now.get(arm.name)
    if old is not None and old[0] == key:
        _k, floor, poly, bal = old
    else:
        low, pts = _lowest(scene, arm)
        st = state.settings(scene)
        if st is not None and st.com_floor == 'HEIGHT':
            floor = st.com_floor_height
        elif st is not None and st.com_floor == 'CURSOR':
            floor = scene.cursor.location.z
        else:
            floor = min(lows + ([low] if low is not None else [])) if (lows or low is not None) else 0.0
        tol = 0.02 * d["height"]
        poly = []
        if pts is not None:
            touch = pts[pts[:, 2] <= floor + tol][:, :2]
            if len(touch):
                q = np.unique(np.round(touch / max(tol, 1e-4)), axis=0) * max(tol, 1e-4)
                poly = _hull([(float(x), float(y)) for x, y in q])
        if not poly:
            bal = 'AIR'
        else:
            bal = 'IN' if _inside(poly, float(g[0]), float(g[1])) else 'OUT'
        _now[arm.name] = (key, floor, poly, bal)
    f = scene.frame_current
    vel = None
    a, b = tr.get(f - 1), tr.get(f + 1)
    if a is not None and b is not None:
        vel = (b[0] - a[0]) * 0.5
    return g, floor, poly, bal, vel


# ---- drawing -----------------------------------------------------------------------------

def draw(scene, st, us):
    import math
    import numpy as np
    if not st.show_com or _busy or onion.busy():
        return
    arms = armatures(scene)
    if not arms:
        return
    if any(a.name not in _mass for a in arms) or status or \
            any(a.name not in _track or _redo.get(a.name) for a in arms):
        _schedule()
    sh = gpu.shader.from_builtin('UNIFORM_COLOR')
    try:
        psh = gpu.shader.from_builtin('POINT_UNIFORM_COLOR')
    except (ValueError, TypeError):
        psh = sh
    prev = gpu.state.depth_test_get()
    gpu.state.depth_test_set('NONE')        # always in front: it's a guide
    gpu.state.blend_set('ALPHA')
    for arm in arms:
        s = state_now(scene, arm)
        if s is None:
            continue
        g, floor, poly, bal, vel = s
        d = _mass[arm.name]
        col = {'IN': (0.3, 0.95, 0.4, 1.0), 'OUT': (1.0, 0.3, 0.25, 1.0), 'AIR': (0.6, 0.6, 0.6, 1.0)}[bal]
        foot = (float(g[0]), float(g[1]), float(floor))
        if st.com_trail:
            tr = _track.get(arm.name) or {}
            pts = [tuple(tr[f][0]) for f in sorted(tr) if tr[f] is not None]
            if len(pts) > 1:
                gpu.state.line_width_set(1.5 * us)
                sh.uniform_float("color", (1.0, 0.65, 0.2, 0.8))
                batch_for_shader(sh, 'LINE_STRIP', {"pos": pts}).draw(sh)
                gpu.state.point_size_set(3.0 * us)
                psh.uniform_float("color", (1.0, 0.75, 0.35, 0.9))
                batch_for_shader(psh, 'POINTS', {"pos": pts}).draw(psh)
        if st.com_support and poly:
            ring = [(x, y, floor) for x, y in poly]
            fill = (col[0], col[1], col[2], 0.12)
            sh.uniform_float("color", fill)
            batch_for_shader(sh, 'TRI_FAN', {"pos": [(sum(p[0] for p in ring) / len(ring),
                                                       sum(p[1] for p in ring) / len(ring), floor)] + ring +
                                              ring[:1]}).draw(sh)
            gpu.state.line_width_set(2.0 * us)
            sh.uniform_float("color", (col[0], col[1], col[2], 0.8))
            batch_for_shader(sh, 'LINE_LOOP', {"pos": ring}).draw(sh)
        # the plumb line and where it meets the floor
        gpu.state.line_width_set(1.5 * us)
        sh.uniform_float("color", (1.0, 1.0, 1.0, 0.55))
        batch_for_shader(sh, 'LINES', {"pos": [tuple(g), foot]}).draw(sh)
        r = 0.04 * d["height"]
        circ = [(foot[0] + math.cos(a * math.pi / 12) * r, foot[1] + math.sin(a * math.pi / 12) * r, floor)
                for a in range(24)]
        gpu.state.line_width_set(2.5 * us)
        sh.uniform_float("color", col)
        batch_for_shader(sh, 'LINE_LOOP', {"pos": circ}).draw(sh)
        if st.com_velocity and vel is not None and float(np.linalg.norm(vel)) > 1e-6:
            tip = g + vel * 8.0             # where it gets in about 8 frames
            v = tip - g
            n = float(np.linalg.norm(v))
            side = np.cross(v, (0.0, 0.0, 1.0))
            if float(np.linalg.norm(side)) < 1e-9:
                side = np.cross(v, (1.0, 0.0, 0.0))
            side = side / float(np.linalg.norm(side)) * n * 0.12
            head = [tuple(tip), tuple(tip - v * 0.2 + side), tuple(tip), tuple(tip - v * 0.2 - side)]
            gpu.state.line_width_set(3.0 * us)
            sh.uniform_float("color", (1.0, 0.85, 0.2, 0.95))
            batch_for_shader(sh, 'LINES', {"pos": [tuple(g), tuple(tip)] + head}).draw(sh)
    gpu.state.line_width_set(1.0)
    gpu.state.point_size_set(1.0)
    gpu.state.depth_test_set(prev)


def draw_2d(ctx, st, us):
    """The point itself: a round dot on the screen (dark ring, white middle), over everything."""
    import math
    from bpy_extras.view3d_utils import location_3d_to_region_2d
    from . import draw as _draw
    if not st.show_com or _busy or onion.busy():
        return
    for arm in armatures(ctx.scene):
        if arm.name not in _mass:
            continue
        g = center(arm)
        if g is None:
            continue
        s = location_3d_to_region_2d(ctx.region, ctx.region_data, g)
        if s is None:
            continue
        for r, col in ((9.0, (0.05, 0.05, 0.05, 0.9)), (6.0, (1.0, 1.0, 1.0, 1.0))):
            rr = r * us
            _draw._flat([(s.x, s.y)] + [(s.x + math.cos(a * math.pi / 12) * rr, s.y + math.sin(a * math.pi / 12) * rr)
                                        for a in range(25)], col, 'TRI_FAN')
