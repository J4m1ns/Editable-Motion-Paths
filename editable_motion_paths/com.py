"""Center of mass: where the character's weight is, from its deform bones, to check that a
pose is believable (a visual aid only; nothing is changed).

* The mass of every deform bone is worked out once from the meshes it moves: the onion proxies
  (the meshes melted down, with their bone weights), each vertex counting its area times its
  distance from the bone (about the volume around the bone), so a torso weighs more than a
  hand. A pose bone's custom property "com_mass" scales its mass by hand. The weights set in
  the panel are stored on the bones: "com_zone" (the bone with what hangs on it) and "com_own"
  (the bone alone), shares of the body that the masses are solved to (see _apply_limbs). A
  linked rig (a library override in a shot) keeps them in the scene instead - Blender doesn't
  save new properties on an override's bones; those set in the rig's own file still count.
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


def meshes_changed():
    """The character's meshes were edited: the masses (from their proxies) are worked out again."""
    if _mass:
        clear()
        _schedule(0.05)


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
        limbs = _limbs(arm)
        if old["limbs"] != limbs:
            # only a limb's weight changed: no need to measure the meshes again
            _apply_limbs(arm, old, limbs)
            changed_all(arm)
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
                           height=max(height, 1e-3), m_base=m, limbs=None)
    _apply_limbs(arm, _mass[arm.name], _limbs(arm))
    _track.pop(arm.name, None)
    _now.pop(arm.name, None)
    return True


WEIGHT_PROPS = {'zone': "com_zone", 'own': "com_own"}   # pose bone property of each kind of weight
SCENE_WEIGHTS = "emp_com_weights"   # scene property: {rig: {"zone:bone": share}} for linked rigs
WORKED_OUT = -1.0                   # (stored there: the rig file's weight is left out, worked out instead)


def _linked(arm):
    """A rig from another file (a library override, or linked as it is): what's set on its bones
    here isn't saved, so the weights go to the scene."""
    return arm.override_library is not None or arm.library is not None


def _scene_weights(arm, create=False):
    """This rig's weights kept in the scene (the current one first), or None."""
    scenes = [bpy.context.scene] + [s for s in bpy.data.scenes if s != bpy.context.scene]
    for sc in scenes:
        if sc is None:
            continue
        got = sc.get(SCENE_WEIGHTS)
        if got is not None and arm.name in got:
            return got[arm.name]
    if not create or bpy.context.scene is None:
        return None
    sc = bpy.context.scene
    if sc.get(SCENE_WEIGHTS) is None:
        sc[SCENE_WEIGHTS] = {}
    sc[SCENE_WEIGHTS][arm.name] = {}
    return sc[SCENE_WEIGHTS][arm.name]


def weight_of(arm, bone, part):
    """The weight set for the bone (share of the body), or None (worked out from the meshes)."""
    if _linked(arm):
        got = _scene_weights(arm)
        if got is not None and "%s:%s" % (part, bone) in got:
            v = float(got["%s:%s" % (part, bone)])
            return None if v < 0.0 else v
    pb = arm.pose.bones.get(bone)
    v = pb.get(WEIGHT_PROPS[part]) if pb is not None else None
    return float(v) if v is not None else None


def _store(arm, bone, part, share):
    """Keep the weight (None: no weight - worked out again)."""
    pb = arm.pose.bones.get(bone)
    prop = WEIGHT_PROPS[part]
    if not _linked(arm):
        if share is None:
            if pb is not None and prop in pb:
                del pb[prop]
        else:
            pb[prop] = share
        return
    got = _scene_weights(arm, create=share is not None)
    key = "%s:%s" % (part, bone)
    if share is not None:
        got[key] = share
    elif pb is not None and prop in pb:
        got = _scene_weights(arm, create=True)
        got[key] = WORKED_OUT           # (the rig file has one: left out)
    elif got is not None and key in got:
        del got[key]


def _limbs(arm):
    """The weights set on bones: ((bone, 'zone' / 'own', share of the body), ...)."""
    out = {(pb.name, part): float(pb[prop]) for pb in arm.pose.bones
           for part, prop in WEIGHT_PROPS.items() if prop in pb}
    if _linked(arm):
        for key, v in (_scene_weights(arm) or {}).items():
            part, _sep, bone = key.partition(":")
            if part in WEIGHT_PROPS:
                out[(bone, part)] = float(v)
    return tuple(sorted((b, p, v) for (b, p), v in out.items() if v >= 0.0))


def own_bones(arm, bone, zone=None):
    """The bones the bone moves itself: its zone without what its child bones carry (a hand
    without the fingers, a foot without the toes)."""
    zone = onion.affected_bones(arm, {bone}) if zone is None else zone
    pb = arm.pose.bones.get(bone)
    out = set(zone)
    for child in (pb.children if pb is not None else ()):
        out -= onion.affected_bones(arm, {child.name})
    out.add(bone)
    return out


def _sets(arm, d, bone):
    """(zone, own): the indices of the bones with mass the bone carries / moves itself."""
    import numpy as np
    cache = d.setdefault("sets", {})
    got = cache.get(bone)
    if got is None:
        idx = {n: i for i, n in enumerate(d["names"])}
        zone = onion.affected_bones(arm, {bone})
        own = own_bones(arm, bone, zone)
        got = cache[bone] = (np.array(sorted(idx[n] for n in zone if n in idx), dtype=np.int64),
                             np.array(sorted(idx[n] for n in own if n in idx), dtype=np.int64))
    return got


def _apply_limbs(arm, d, limbs):
    """The masses with the weights set: every weight holds its part of the body exactly; the bones
    of a part that have no weight of their own share what is left of it (as they compare); the
    bones in no part share the rest of the body. Smaller parts are worked out first, so a hand
    set inside an arm keeps the arm's weight (the rest of the arm makes up for it)."""
    import numpy as np
    base = d["m_base"]
    total = float(base.sum()) or 1.0
    m = base / total
    b = m.copy()
    fixed = np.zeros(len(m), dtype=bool)
    parts = []
    for name, part, share in limbs:
        sel = _sets(arm, d, name)[0 if part == 'zone' else 1]
        if len(sel):
            parts.append((len(sel), sel, min(max(share, 0.0005), 0.99)))
    for _n, sel, share in sorted(parts, key=lambda p: p[0]):
        held = float(m[sel][fixed[sel]].sum())
        free = sel[~fixed[sel]]
        if len(free):
            room = float(b[free].sum())
            if room > 0.0:
                m[free] = b[free] * max(share - held, 0.0005 * len(free)) / room
        fixed[sel] = True
    rest = ~fixed
    if rest.any() and float(b[rest].sum()) > 0.0:
        m[rest] = b[rest] * max(1.0 - float(m[fixed].sum()), 0.001) / float(b[rest].sum())
    d["m"] = m * total
    d["limbs"] = limbs


def changed_all(arm):
    """The masses changed: the whole track is worked out again, from scratch (old and new
    frames mixed would bend the trajectory and throw the velocity arrow off)."""
    _track.pop(arm.name, None)
    _redo.pop(arm.name, None)
    _now.pop(arm.name, None)
    _schedule(0.05)


def limb_share(arm, bone, part='zone'):
    """How much of the body's mass the bone carries ('zone': it and what hangs on it; 'own': it
    alone), 0..1, or None (not worked out yet, or no bone with weight)."""
    d = _mass.get(arm.name)
    if d is None or not d["names"]:
        return None
    sel = _sets(arm, d, bone)[0 if part == 'zone' else 1]
    total = float(d["m"].sum())
    if not len(sel) or total <= 0.0:
        return None
    return float(d["m"][sel].sum()) / total


def set_limb_share(arm, bone, share, part='zone'):
    """Make the bone carry this much of the body ('zone': with what hangs on it; 'own': alone); it is
    stored on the bone ("com_zone" / "com_own"), the other bones make up for it."""
    d = _mass.get(arm.name)
    pb = arm.pose.bones.get(bone)
    now = limb_share(arm, bone, part)
    if d is None or pb is None or not now:
        return False
    if part == 'zone' and limb_share(arm, bone, 'zone') > WHOLE:
        return False            # it moves the whole body: its weight can't move the center
    share = min(max(share, 0.001), 0.95)
    # the weight worked out without this one: set to that (a drag cancelled with a right click puts
    # the value it began with back), nothing is stored - the reset arrow stays off
    others = tuple(item for item in _limbs(arm) if not (item[0] == bone and item[1] == part))
    probe = dict(d)
    _apply_limbs(arm, probe, others)
    sel = _sets(arm, d, bone)[0 if part == 'zone' else 1]
    natural = float(probe["m"][sel].sum()) / max(float(probe["m"].sum()), 1e-12)
    _store(arm, bone, part, None if abs(share - natural) < 5e-4 else share)
    _apply_limbs(arm, d, _limbs(arm))
    changed_all(arm)
    flash_limb(arm, bone, part)
    return True


def reset_all(arm):
    """Every weight set on this rig goes: all of them worked out from the meshes again. Returns how
    many there were."""
    items = _limbs(arm)
    for bone, part, _share in items:
        _store(arm, bone, part, None)
    d = _mass.get(arm.name)             # (a linked rig: its file's weights are left out in the scene)
    if d is not None:
        _apply_limbs(arm, d, _limbs(arm))
        changed_all(arm)
    return len(items)


def reset_limb(arm, bone, part=None):
    """The weight(s) set on the bone go: worked out from the meshes again."""
    pb = arm.pose.bones.get(bone)
    if pb is None:
        return False
    gone = False
    for p in ([part] if part else list(WEIGHT_PROPS)):
        if weight_of(arm, bone, p) is not None:
            _store(arm, bone, p, None)
            gone = True
    d = _mass.get(arm.name)
    if gone and d is not None:
        _apply_limbs(arm, d, _limbs(arm))
        changed_all(arm)
    flash_limb(arm, bone, part or 'zone')
    return gone


WHOLE = 0.97       # a controller moving more of the body than this moves all of it


_flash = [None, 0.0, False]     # (armature, controller) lit up, when, its fade timer runs


def flash_limb(arm, bone, part='zone'):
    """Light up the limb (or the bone alone) for a moment (while its weight is being set)."""
    _flash[:2] = [(arm.name, bone, part), time.time()]

    def fade():
        state.tag_redraw()
        if time.time() - _flash[1] < FLASH:
            return 0.05
        _flash[2] = False
        return None
    if not _flash[2]:
        _flash[2] = True
        bpy.app.timers.register(fade, first_interval=0.05)
    state.tag_redraw()


FLASH = 1.5


def _draw_flash(scene, us):
    """The limb lit up: the part of the meshes its bones move (like By Weight of the onion)."""
    import numpy as np
    if _flash[0] is None:
        return
    age = time.time() - _flash[1]
    if age > FLASH:
        _flash[0] = None
        return
    arm = bpy.data.objects.get(_flash[0][0])
    if arm is None or arm.type != 'ARMATURE':
        return
    zone = onion.affected_bones(arm, {_flash[0][1]})
    own = own_bones(arm, _flash[0][1], zone)
    if len(_flash[0]) > 2 and _flash[0][2] == 'own':
        zone = own
    # the bone itself in its own color, the rest of the limb in the limb's (the preferences)
    limb_tris, bone_tris = [], []
    for o in _meshes(scene, arm):
        p = onion._proxies.get(o.name)
        if p is None or p.rest is None or not len(p.tri):
            continue
        keep = onion._shown_by_weight(p, zone)
        if keep is None or not keep.any():
            continue
        tri = p.tri[keep[p.tri].all(axis=1)]
        if not len(tri):
            continue
        mine = onion._shown_by_weight(p, own)
        in_bone = mine[tri].all(axis=1) if mine is not None else np.zeros(len(tri), dtype=bool)
        posed = onion._pose(p)
        if in_bone.any():
            bone_tris.append(posed[tri[in_bone]].reshape(-1, 3))
        if (~in_bone).any():
            limb_tris.append(posed[tri[~in_bone]].reshape(-1, 3))
    if not limb_tris and not bone_tris:
        return
    from .props import prefs
    pr = prefs()
    alpha = 0.35 * (1.0 - age / FLASH) + 0.05
    sh = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.face_culling_set('BACK')
    for tris, col in ((limb_tris, pr.com_limb_color if pr else (1.0, 0.8, 0.25)),
                      (bone_tris, pr.com_bone_color if pr else (0.35, 0.75, 1.0))):
        if tris:
            sh.uniform_float("color", tuple(col) + (alpha,))
            batch_for_shader(sh, 'TRIS', {"pos": np.concatenate(tris)}).draw(sh)
    gpu.state.face_culling_set('NONE')


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
    """The frames the track is worked out for (the Trajectory Range, like the paths' Range)."""
    st = state.settings(scene)
    mode = st.com_range_mode if st is not None else 'SCENE'
    if mode == 'MANUAL':
        return st.com_frame_start, max(st.com_frame_end, st.com_frame_start + 1)
    if mode == 'PATHS':
        cs = state.editable_caches(scene)
        if cs:
            return min(c.fs for c in cs), max(c.fe for c in cs)
    if mode == 'ACTION':
        rs = [ad.action.frame_range for ad in (a.animation_data for a in armatures(scene))
              if ad is not None and ad.action is not None]
        if rs:
            return int(min(r[0] for r in rs)), int(max(r[1] for r in rs) + 0.999)
    if scene.use_preview_range:
        return scene.frame_preview_start, scene.frame_preview_end
    return scene.frame_start, scene.frame_end


def trail_frames(scene, st):
    """(first, last) frame of the trajectory drawn: the range, around the current frame if set."""
    f0, f1 = _range(scene)
    if st.com_use_window:
        cur = scene.frame_current
        f0, f1 = max(f0, cur - st.com_window_before), min(f1, cur + st.com_window_after)
    return f0, f1


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
        if (screen is not None and screen.is_animation_playing) or state.modal_running or onion.busy() or \
                state.blender_edit_running() or state.render_running():
            return 0.2
        state.flush_changes()       # (see there: what changed a moment ago counts as a change)
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
            held = state.keep_scene_pose(scene, [arm])
            try:
                for o in meshes:
                    # only the bones are evaluated; a tick does ~40 ms of frames (not all of them),
                    # so a mesh is left out only if showing it again costs about a frame or less
                    # (seen the first time it is hidden)
                    if not o.hide_viewport and state.can_hide(o, 5):
                        o.hide_viewport = True
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
                t_back = time.time()
                scene.frame_set(cur, subframe=sub)
                state.unhide_cost(hidden, time.time() - t_back)
                if state.put_pose_back(held):     # (what isn't keyed stays as it was)
                    bpy.context.view_layer.update()
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
        (st_.com_floor, round(st_.com_floor_height, 5), round(scene.cursor.location.z, 5),
         round(st_.com_floor_detail, 3)) if st_ else 0)
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
                # (on a grid, so a dense mesh doesn't slow it down; Floor Detail makes it finer)
                cell = max(tol / max(st.com_floor_detail if st is not None else 1.0, 1.0), 1e-4)
                q = np.unique(np.round(touch / cell), axis=0) * cell
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
    from . import draw as _draw
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
    _draw_flash(scene, us)
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
            f0, f1 = trail_frames(scene, st)
            pts = [tuple(tr[f][0]) for f in sorted(tr) if tr[f] is not None and f0 <= f <= f1]
            if len(pts) > 1:
                _draw._flat(pts, (1.0, 0.65, 0.2, 0.8), 'LINE_STRIP', 1.5 * us)
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
            _draw._flat(ring, (col[0], col[1], col[2], 0.8), 'LINE_LOOP', 2.0 * us)
        # the plumb line and where it meets the floor
        _draw._flat([tuple(g), foot], (1.0, 1.0, 1.0, 0.55), 'LINES', 1.5 * us)
        r = 0.04 * d["height"]
        circ = [(foot[0] + math.cos(a * math.pi / 12) * r, foot[1] + math.sin(a * math.pi / 12) * r, floor)
                for a in range(24)]
        _draw._flat(circ, col, 'LINE_LOOP', 2.5 * us)
        if st.com_velocity and vel is not None and float(np.linalg.norm(vel)) > 1e-6:
            unit = vel / float(np.linalg.norm(vel))
            if st.com_velocity_mode == 'FIXED':
                tip = g + unit * st.com_velocity_length
            elif st.com_velocity_mode == 'BOTH':
                tip = g + vel * 8.0 + unit * st.com_velocity_length
            else:
                tip = g + vel * 8.0             # where it gets in about 8 frames
            v = tip - g
            n = float(np.linalg.norm(v))
            side = np.cross(v, (0.0, 0.0, 1.0))
            if float(np.linalg.norm(side)) < 1e-9:
                side = np.cross(v, (1.0, 0.0, 0.0))
            side = side / float(np.linalg.norm(side)) * n * 0.12
            head = [tuple(tip), tuple(tip - v * 0.2 + side), tuple(tip), tuple(tip - v * 0.2 - side)]
            _draw._flat([tuple(g), tuple(tip)] + head, (1.0, 0.85, 0.2, 0.95), 'LINES', 3.0 * us)
    gpu.state.line_width_set(1.0)
    gpu.state.point_size_set(1.0)
    gpu.state.depth_test_set(prev)


def draw_2d(ctx, st, us):
    """The point itself: a round dot on the screen (dark ring, white middle), over everything."""
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
        discs = _draw.Discs()           # (round dots with soft edges, like the keys)
        discs.add((s.x, s.y), 9.0 * us, (0.05, 0.05, 0.05, 0.9))
        discs.add((s.x, s.y), 6.0 * us, (1.0, 1.0, 1.0, 1.0))
        gpu.state.blend_set('ALPHA')
        discs.draw()
