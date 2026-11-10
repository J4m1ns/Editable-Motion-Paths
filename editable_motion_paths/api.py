"""Scripting API, e.g. for an AI assistant driving Blender through an MCP connection.

    import sys
    emp = next(m for n, m in sys.modules.items() if n.endswith("editable_motion_paths.api"))

    print(emp.controls("Rig"))                      # the animator's bones, by bone collection
    emp.add_paths("Rig", ["CTRL_Foot_L"])           # show paths
    print(emp.analyze("Rig", "CTRL_Foot_L")["summary"])   # read the motion like an animator
    emp.preview_objects()                           # make the paths visible in screenshots
    emp.move_key("Rig", "CTRL_Foot_L", 25, offset=(0, 0, 0.05))
    emp.move_key("Rig", "CTRL_Hand_R", 12, target=(0.4, -0.3, 1.5))   # key it there, in world space

Every function takes object names / bone names (not RNA references) and works no matter
which tool, mode or selection is active. `mode` is 'LOCATION' or 'ROTATION' (default: the
current edit mode); 'SCALE' and 'VALUE' are the scale ribbon and the value track. A `mode`
given to one call is used for that call only (the edit mode shown in Blender stays).

An edit works on the one path named, at the keys of `frames` (frame numbers of its keys; None:
all of them): other paths and the selection in Blender aren't touched. A wrong name or a frame
without a key raises ValueError saying what there is.
"""

import contextlib
import difflib
import math
import os
import statistics

import bpy
from mathutils import Matrix, Vector

from . import compat, core, pick, state
from . import draw as emp_draw

PREVIEW_COLLECTION = "EMP Path Preview"
SPEED_BUCKETS = 5


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def _scene():
    return bpy.context.scene


MODES = ('LOCATION', 'ROTATION', 'SCALE', 'VALUE')


def _valid_mode(mode):
    if mode is not None and mode not in MODES:
        raise ValueError("mode %r: use one of %s" % (mode, ", ".join(MODES)))
    return mode or state.settings(_scene()).edit_mode


def _suggest(name, names):
    near = difflib.get_close_matches(name, list(names), n=3, cutoff=0.6)
    return (" Did you mean %s?" % " / ".join(repr(n) for n in near)) if near else ""


def _object(obj):
    """The object by name, or ValueError with the closest names."""
    if not isinstance(obj, str):
        return obj
    o = bpy.data.objects.get(obj)
    if o is None:
        raise ValueError("No object %r.%s" % (obj, _suggest(obj, bpy.data.objects.keys())))
    return o


def _check_bone(o, bone):
    if bone and (o.type != 'ARMATURE' or o.pose is None or o.pose.bones.get(bone) is None):
        names = o.pose.bones.keys() if o.type == 'ARMATURE' and o.pose is not None else []
        raise ValueError("No bone %r in %r.%s" % (bone, o.name, _suggest(bone, names)))


@contextlib.contextmanager
def _in_mode(mode):
    """The edit mode for one call, put back after it."""
    st = state.settings(_scene())
    before = st.edit_mode
    if mode and mode != before:
        st.edit_mode = mode
        state.sync_caches(_scene())
    try:
        yield
    finally:
        if st.edit_mode != before:
            st.edit_mode = before
            state.sync_caches(_scene())


def _cache(obj, bone=None, mode=None):
    """Path cache for obj/bone, adding and calculating the path if needed."""
    o = _object(obj)
    _check_bone(o, bone)
    mode = _valid_mode(mode)
    key = (o.name, bone or "", mode)
    c = state.caches.get(key)
    if c is None or not c.B:
        with _in_mode(mode):       # (a mode asked for this call only: the one shown stays)
            add_paths(o.name, [bone] if bone else None)
        c = state.caches.get(key)
    if c is None:
        raise ValueError("No path for %s %s" % (o.name, bone or ""))
    c.refresh(force=True)
    return c


def _v(p, nd=4):
    return [round(float(x), nd) for x in p]       # (plain floats: numpy ones don't go through JSON)


def _view3d():
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == 'VIEW_3D':
                region = next(r for r in area.regions if r.type == 'WINDOW')
                return win, area, region
    return None, None, None


# ----------------------------------------------------------------------------
# paths
# ----------------------------------------------------------------------------

def add_paths(obj, bones=None, mode=None):
    """Show editable paths for an object, or for bones of an armature (calculated now).
    Returns the path keys (object, bone, mode). mode: the kind of path to calculate for this
    call (like every other call: the mode shown stays - set_mode() switches it)."""
    o = _object(obj)
    for b in bones or ():
        _check_bone(o, b)
    st = state.settings(_scene())
    want = _valid_mode(mode)
    existing = {(e.object.name if e.object else None, e.bone_name) for e in st.paths}
    for b in (bones or [""]):
        if (o.name, b) not in existing:
            e = st.paths.add()
            e.object = o
            e.bone_name = b
    with _in_mode(want):
        update()
    return [k for k in state.caches if k[0] == o.name and (not bones or k[1] in bones) and k[2] == want]


def remove_paths(obj=None, bones=None):
    """Remove paths (all if obj is None)."""
    st = state.settings(_scene())
    for i in reversed(range(len(st.paths))):
        e = st.paths[i]
        name = e.object.name if e.object else None
        if obj is None or (name == obj and (bones is None or e.bone_name in bones)):
            st.paths.remove(i)
    state.sync_caches(_scene())
    state.tag_redraw()


def set_mode(mode):
    """The edit mode shown (and used by calls without mode=): 'LOCATION', 'ROTATION', 'SCALE' or
    'VALUE'."""
    if mode not in MODES:
        raise ValueError("mode %r: use one of %s" % (mode, ", ".join(MODES)))
    state.settings(_scene()).edit_mode = mode
    state.sync_caches(_scene())
    update()


def update():
    """Recalculate all paths (call after changing animation from a script)."""
    state.sync_caches(_scene())
    for c in state.caches.values():
        c.B = []
    state.bake_all(_scene())


def controls(obj):
    """The bones an animator works with on an armature: {bone collection: [bone names]} of the
    collections shown (hidden ones hold the rig's machinery: keying those does little or fights
    the rig), and 'keyed': the bones that have keys now. Start here on a rig you don't know."""
    o = _object(obj)
    if o.type != 'ARMATURE':
        raise ValueError("%r is a %s, not an armature" % (o.name, o.type.lower()))
    out = {}
    for coll in o.data.collections_all:
        if not coll.is_visible_effectively:
            continue
        names = [b.name for b in coll.bones if not b.hide and not b.use_deform]
        if names:
            out[coll.name] = names
    keyed = set()
    for fc in compat.get_fcurves(o) or []:
        m = state._BONE_PATH.match(fc.data_path)
        if m and len(fc.keyframe_points):
            keyed.add(m.group(1).replace('\\"', '"'))
    out["keyed"] = sorted(keyed)
    return out


def list_paths():
    """[{object, bone, mode, frames: [first, last], keys: count}] of the paths calculated."""
    return [{"object": k[0], "bone": k[1], "mode": k[2], "frames": [c.fs, c.fe], "keys": len(c.keys)}
            for k, c in state.caches.items() if c.B]


def get_path(obj, bone=None, mode=None):
    """Trajectory data: per-frame world points and speeds, and the keys with their handles."""
    c = _cache(obj, bone, mode)
    frames = [t for t in c.times if abs(t - round(t)) < 1e-6]
    pts = [p for t, p in zip(c.times, c.points) if abs(t - round(t)) < 1e-6]
    speeds = [(b - a).length for a, b in zip(pts, pts[1:])]
    keys = []
    for k in c.keys:
        keys.append({
            "frame": round(k.t, 3), "position": _v(k.co), "values": _v(k.vals),
            "selected": k.sel, "partial": k.partial,
            "handle_left": _v(k.hl) if k.hl else None, "handle_right": _v(k.hr) if k.hr else None,
            "handle_types": [k.type_l, k.type_r], "interpolation": k.interp,
        })
    return {"object": c.obj_name, "bone": c.bone_name, "mode": c.ptype,
            "what": {core.LOCATION: "bone head / object origin", core.ROTATION: "bone tip",
                     core.SCALE: "bone head / object origin (scale as ribbons)",
                     core.VALUE: "the value, drawn above the location path"}[c.ptype],
            "frames": [int(round(f)) for f in frames], "points": [_v(p) for p in pts],
            "key_frames": [round(k.t, 3) for k in c.keys],
            "speed_per_frame": [round(s, 5) for s in speeds], "keys": keys}


# ----------------------------------------------------------------------------
# reading the motion
# ----------------------------------------------------------------------------

def _hidden_note(c):
    """A word when the bone isn't one an animator sees (rig machinery)."""
    obj, owner = core.resolve(c.obj_name, c.bone_name)
    if not c.bone_name or owner is None:
        return ""
    b = owner.bone
    colls = list(b.collections)
    if b.hide or (colls and not any(col.is_visible_effectively for col in colls)):
        return ("note: %s is hidden from the animator (bone collection %s): rig machinery, whose keys the "
                "rig may override - see controls() for the bones to animate" %
                (c.bone_name, ", ".join(col.name for col in colls) or "-"))
    return ""


def analyze(obj, bone=None, mode=None):
    """Animator-style reading of a path. Returns a dict; dict['summary'] is plain text.

    Looks at: total distance, speed profile, holds (no motion), pops (sudden speed
    jumps), sharp turns (direction flips), and how each key is approached (ease in/out).
    """
    c = _cache(obj, bone, mode)
    frames = [t for t in c.times if abs(t - round(t)) < 1e-6]
    pts = [p for t, p in zip(c.times, c.points) if abs(t - round(t)) < 1e-6]
    if len(pts) < 3:
        return {"summary": "Path too short to analyze."}
    vel = [b - a for a, b in zip(pts, pts[1:])]
    spd = [v.length for v in vel]
    vmax = max(spd) or 1e-9
    arc = sum(spd)
    med_d = statistics.median([abs(b - a) for a, b in zip(spd, spd[1:])]) or 1e-9

    holds, start = [], None
    for i, s in enumerate(spd + [vmax]):
        if s < 0.02 * vmax:
            start = frames[i] if start is None else start
        elif start is not None:
            if frames[i] - start >= 2:
                holds.append([int(start), int(frames[i])])
            start = None

    pops = []
    for i in range(1, len(spd)):
        d = spd[i] - spd[i - 1]
        if abs(d) > max(4.0 * med_d, 0.3 * vmax):
            pops.append({"frame": int(frames[i]), "speed_change": round(d, 4),
                         "kind": "sudden speed-up" if d > 0 else "sudden stop"})

    turns = []
    for i in range(1, len(vel)):
        a, b = vel[i - 1], vel[i]
        if a.length > 0.1 * vmax and b.length > 0.1 * vmax:
            ang = math.degrees(a.angle(b))
            if ang > 50.0:
                turns.append({"frame": int(frames[i]), "angle_deg": round(ang, 1)})

    eases = []
    ks = [k for k in c.keys if c.fs <= k.t <= c.fe]
    for i, k in enumerate(ks):
        fi = int(round(k.t)) - int(frames[0])
        if fi <= 0 or fi >= len(spd):
            continue
        near = (spd[fi - 1] + spd[fi]) * 0.5
        nb = [ks[j].t for j in (i - 1, i + 1) if 0 <= j < len(ks)]
        seg = [s for f, s in zip(frames, spd) if min(nb + [k.t]) <= f < max(nb + [k.t])]
        avg = sum(seg) / len(seg) if seg else near
        ratio = near / avg if avg > 1e-9 else 1.0
        kind = "passes through" if ratio > 0.75 else ("eases (slow in/out)" if ratio > 0.25 else "stops")
        eases.append({"frame": round(k.t, 2), "speed_vs_average": round(ratio, 2), "key": kind})

    fast = int(frames[spd.index(vmax)])
    lines = [
        "%s %s, %s path (%s), frames %d-%d, %d keys" % (
            c.obj_name, c.bone_name, c.ptype.lower(),
            "bone tip" if c.ptype == core.ROTATION else "head/origin", frames[0], frames[-1], len(ks)),
        "distance %.3f, average speed %.4f/frame, fastest %.4f at frame %d" % (
            arc, arc / len(spd), vmax, fast),
        "holds (no motion): %s" % (", ".join("%d-%d" % tuple(h) for h in holds) or "none"),
        "pops (abrupt speed changes): %s" % (
            ", ".join("%d %s" % (p["frame"], p["kind"]) for p in pops) or "none"),
        "sharp turns (>50 deg in one frame): %s" % (
            ", ".join("%d (%.0f deg)" % (t["frame"], t["angle_deg"]) for t in turns) or "none"),
        "keys: " + ", ".join("%g %s" % (e["frame"], e["key"]) for e in eases),
    ]
    note = _hidden_note(c)
    if note:
        lines.append(note)
    return {"object": c.obj_name, "bone": c.bone_name, "mode": c.ptype, "distance": round(arc, 4),
            "speed_per_frame": [round(s, 5) for s in spd], "holds": holds, "pops": pops,
            "sharp_turns": turns, "keys": eases, "summary": "\n".join(lines)}


# ----------------------------------------------------------------------------
# editing (all undoable as one step each via bpy.ops.ed.undo_push)
# ----------------------------------------------------------------------------

def _select_owner_and_keys(c, frames):
    """The path's keys at `frames` selected - and nothing else: the keys of every other path are
    deselected, so an operator works on this path only. ValueError if no key is at `frames`."""
    obj, owner = core.resolve(c.obj_name, c.bone_name)
    if c.bone_name:
        if obj.mode != 'POSE':
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.mode_set(mode='POSE')
        compat.bone_select_set(owner, True)
    else:
        obj.select_set(True)
    c.refresh(force=True)
    pick.deselect_all([o for o in state.caches.values() if o.B])
    c.refresh(force=True)
    # (None or []: every key)
    want = {round(float(f), 3) for f in frames} if frames else None
    if want is not None and not any(round(k.t, 3) in want for k in c.keys):
        raise ValueError("No key of the %s path of %s %s at frames %s; its keys are at %s" % (
            c.ptype.lower(), c.obj_name, c.bone_name, sorted(want), [round(k.t, 3) for k in c.keys]))
    for k in c.keys:
        if want is None or round(k.t, 3) in want:
            pick.select_part(c, k, 'KEY', True)
    c.refresh(force=True)


@contextlib.contextmanager
def _only(c):
    """Only this path's bone / object selected while an operator runs (operators work on the paths
    of the selected bones / objects); the selection is put back after."""
    obj, owner = core.resolve(c.obj_name, c.bone_name)
    bones = []
    if c.bone_name and obj.pose is not None:
        bones = [(pb, compat.bone_selected(pb)) for pb in obj.pose.bones]
        for pb, _s in bones:
            if pb is not owner and _s:
                compat.bone_select_set(pb, False)
    # (None: an object removed a moment ago, the view layer's list not brought up to date yet)
    objs = [(o, o.select_get()) for o in bpy.context.view_layer.objects if o is not None]
    for o, sel in objs:
        if sel and o is not obj:
            o.select_set(False)
    try:
        yield
    finally:
        for pb, sel in bones:
            if compat.bone_selected(pb) != sel:
                compat.bone_select_set(pb, sel)
        for o, sel in objs:
            if o.select_get() != sel:
                o.select_set(sel)


def select_keys(obj, bone=None, frames=None, mode=None):
    """Select keys of a path (frames=None: all) and its bone/object, so the tools in Blender act
    on them (the keys of other paths get deselected)."""
    c = _cache(obj, bone, mode)
    _select_owner_and_keys(c, frames)
    state.tag_redraw()


def move_key(obj, bone=None, frame=None, target=None, offset=None, mode=None):
    """Move one path key in world space: to `target` (x, y, z) or by `offset`.

    Location paths move the bone head / object origin; rotation paths turn the bone so its
    tip points at the target. No key at `frame`: one is inserted there first (the motion doesn't
    change), likewise missing channel keys. Returns the new position.

    It is also the easy way to animate in world space: on a control with no keys yet, call it with
    target=(x, y, z) for each frame - the parents and constraints of the rig are worked out, the
    bone ends up exactly there (no bone-local axes to deal with).
    """
    with _in_mode(_valid_mode(mode)):
        return _move_key(obj, bone, frame, target, offset, mode)


def _move_key(obj, bone, frame, target, offset, mode):
    if frame is None or (target is None) == (offset is None):
        raise ValueError("move_key needs a frame and either target=(x, y, z) or offset=(x, y, z)")
    c = _cache(obj, bone, mode)
    tg = c.target()
    core.ensure_keys(tg, c.ptype, [float(frame)], None, state.settings(_scene()).pin_partial)
    core.cycle_skip = {round(float(frame), 3)}
    c.refresh(force=True)
    tg = c.target()
    k = next((k for k in c.keys if abs(k.t - frame) < 1e-3), None)
    if k is None:
        raise ValueError("No key at frame %s" % frame)
    W = Vector(target) if target is not None else k.co + Vector(offset)
    vals = c.solve(tg, k.t, W, k.vals)
    ch = tg.channels(c.ptype)
    touched = set()
    core.write_key(ch, k, vals, True, touched)
    core.update_fcurves(touched)
    c.refresh(force=True)
    k = next(k for k in c.keys if abs(k.t - frame) < 1e-3)
    _push("Move Path Key")
    return _v(k.co)


def set_key(obj, bone=None, frame=None, values=None, mode=None):
    """Set a key's channel values directly (no viewport, no world space): LOCATION (x, y, z) local
    location, ROTATION the rotation channels (Euler x, y, z in radians / quaternion w, x, y, z),
    SCALE (x, y, z), VALUE (v,) of the value channel picked with set_value_channel. No key at
    `frame`: one is inserted first (the motion doesn't change). Returns the values set."""
    with _in_mode(_valid_mode(mode)):
        c = _cache(obj, bone, mode)
        if frame is None or values is None:
            raise ValueError("set_key needs a frame and the values")
        tg = c.target()
        ch = tg.channels(c.ptype)
        vals = [float(v) for v in values]
        if len(vals) != ch.count:
            raise ValueError("the %s path of %s %s has %d channels (%s), not %d" % (
                c.ptype.lower(), c.obj_name, c.bone_name, ch.count, ch.path, len(vals)))
        core.ensure_keys(tg, c.ptype, [float(frame)], None, state.settings(_scene()).pin_partial)
        c.refresh(force=True)
        tg = c.target()
        k = next((k for k in c.keys if abs(k.t - frame) < 1e-3), None)
        if k is None:
            raise ValueError("No key could be made at frame %s" % frame)
        touched = set()
        core.write_key(tg.channels(c.ptype), k, vals, True, touched)
        core.update_fcurves(touched)
        c.refresh(force=True)
        _push("Set Path Key")
        return vals


def insert_key(obj, bone=None, frame=None, mode=None):
    """Insert a key on the path at `frame` without changing the motion."""
    from .ops_keys import insert_key as _insert
    with _in_mode(_valid_mode(mode)):
        c = _cache(obj, bone, mode)
        k = _insert(c, float(frame))
    _push("Insert Path Key")
    return k is not None


def _run_op(_op, _frames, _obj, _bone, _mode, **props):
    """Run a path operator on this path's keys at `frames` only. Returns the operator's result,
    e.g. {'FINISHED'} ({'CANCELLED'}: it had nothing to do)."""
    # (underscored names: operator properties like `frames` or `mode` go through **props)
    with _in_mode(_valid_mode(_mode)):
        c = _cache(_obj, _bone, _mode)
        with _only(c):
            _select_owner_and_keys(c, _frames)
            win, area, region = _view3d()
            if area is not None:
                with bpy.context.temp_override(window=win, area=area, region=region):
                    r = _op('EXEC_DEFAULT', **props)
            else:
                r = _op('EXEC_DEFAULT', **props)
        c = state.caches.get(c.key) or c
        c.refresh(force=True)
        if 'FINISHED' in r:
            # (operators run from a script push no undo step of their own: one per call here)
            _push(_op.get_rna_type().name)
        return r


def smooth(obj, bone=None, frames=None, factor=1.0, method='SHAPE', mode=None):
    """Smooth keys (frames=None: all). 'SHAPE' removes bumps/corners and keeps arcs and
    timing; 'TIME' evens the keys out in time. Negative factor sharpens."""
    return _run_op(bpy.ops.emp.smooth, frames, obj, bone, mode, factor=factor, method=method)


def ease(obj, bone=None, frames=None, factor=0.7, side='BOTH', mode=None):
    """Slow in / slow out at keys: factor 1 = the motion comes to rest at the key (frames
    bunch up there), negative = more constant speed through the key. side: BOTH, IN (arrive
    gently: the part before the key), OUT (leave gently: the part after it)."""
    return _run_op(bpy.ops.emp.ease, frames, obj, bone, mode, factor=factor, side=side)


def exaggerate(obj, bone=None, frames=None, factor=0.5, pivot='AUTO', mode=None):
    """Exaggerate (factor > 0) or tone down (factor < 0) the shape of the path."""
    return _run_op(bpy.ops.emp.exaggerate, frames, obj, bone, mode, factor=factor, pivot=pivot)


def slide(obj, bone=None, frames=None, offset=1.0, slide_mode='TIMING', continue_motion=False,
          whole_pose=True, mode=None):
    """Slide keys along the path by `offset` frames ('TIMING' keeps the pose and changes the
    frame, 'SPACING' keeps the frame and moves the pose along the trajectory)."""
    return _run_op(bpy.ops.emp.slide, frames, obj, bone, mode, offset=offset, slide_mode=slide_mode,
                   extrapolate=continue_motion, all_channels=whole_pose)


def fix_loop(obj, bone=None, frame=None, whole_pose=True, mode=None):
    """Match the other end of the loop to the key at `frame` (the path's first or last key;
    default: the first)."""
    c = _cache(obj, bone, mode)
    f = c.keys[0].t if frame is None else frame
    return _run_op(bpy.ops.emp.fix_loop, [f], obj, bone, mode, whole_pose=whole_pose)


def cleanup(obj, bone=None, frames=None, method='CLEAN', tolerance=1.0, mode=None):
    """Reduce keys while keeping the 3D shape: method 'CLEAN' removes existing keys,
    'ADAPTIVE' rebuilds with the fewest keys. tolerance in % of the path size. frames: the
    range to clean (2+ frames), None for the whole path."""
    return _run_op(bpy.ops.emp.cleanup, frames if frames is not None else [], obj, bone, mode,
                   method=method, tolerance=tolerance)


def set_handle_type(obj, bone=None, frames=None, handle_type='AUTO_CLAMPED', mode=None):
    """Handle type of the keys at `frames`: 'FREE', 'ALIGNED', 'VECTOR', 'AUTO', 'AUTO_CLAMPED'."""
    return _run_op(bpy.ops.emp.handle_type, frames, obj, bone, mode, type=handle_type)


def space(obj, bone=None, frames=None, profile='EVEN', factor=1.0, strength=2.0, mode=None):
    """Re-space the motion between the first and last of `frames` (None: whole path) while
    the path keeps its shape. profile: 'EVEN' (constant speed), 'SLOW_IN' (start slowly),
    'SLOW_OUT' (end slowly), 'SLOW_IN_OUT'."""
    return _run_op(bpy.ops.emp.space, frames if frames is not None else [], obj, bone, mode,
                   profile=profile, factor=factor, power=strength)


def delete_keys(obj, bone=None, frames=None, mode=None):
    """Delete the keys at `frames` (the motion between their neighbors changes; dissolve_keys keeps
    the shape instead)."""
    return _run_op(bpy.ops.emp.delete_keys, frames, obj, bone, mode)


def dissolve_keys(obj, bone=None, frames=None, mode=None):
    """Remove keys but keep the shape (refits the neighbors' handles)."""
    return _run_op(bpy.ops.emp.dissolve_keys, frames, obj, bone, mode)


def subdivide(obj, bone=None, frames=None, cuts=1, mode=None):
    """Insert `cuts` evenly spaced keys in every segment between selected `frames`."""
    return _run_op(bpy.ops.emp.subdivide, frames, obj, bone, mode, cuts=cuts)


def bevel(obj, bone=None, frames=None, width=3.0, segments=2, profile=0.5, clamp=False, mode=None):
    """Round the keys at `frames` into arcs `width` frames to each side (through the keys
    around them; clamp=True: stop at the next keys)."""
    return _run_op(bpy.ops.emp.bevel, frames, obj, bone, mode, width=width, segments=segments,
                   profile=profile, clamp=clamp)


def extrude(obj, bone=None, frame=None, frames_later=6, offset=(0.0, 0.0, 0.0), mode=None):
    """New key `frames_later` after the end key at `frame` (before it for the first key),
    continuing the motion, moved by `offset` (world)."""
    return _run_op(bpy.ops.emp.extrude, [frame], obj, bone, mode, frames=frames_later, offset=offset)


def loop_tool(obj, bone=None, frames=None, tool='CIRCLE', influence=1.0, mode=None):
    """tool: 'CIRCLE', 'ARC', 'FLATTEN' or 'STRAIGHTEN' on the keys at `frames`."""
    ops = {'CIRCLE': bpy.ops.emp.circle, 'ARC': bpy.ops.emp.arc, 'FLATTEN': bpy.ops.emp.flatten,
           'STRAIGHTEN': bpy.ops.emp.straighten}
    if tool not in ops:
        raise ValueError("tool %r: use one of %s.%s" % (tool, ", ".join(ops), _suggest(str(tool), ops)))
    op = ops[tool]
    return _run_op(op, frames, obj, bone, mode, influence=influence)


def path_to_curve(obj, bone=None, kind='KEYS', mode=None):
    """Make a Curve object of the path ('KEYS': Bezier per key, 'FRAMES': poly per frame)."""
    return _run_op(bpy.ops.emp.export_curve, [], obj, bone, mode, kind=kind)


def follow_curve(obj, bone=None, curve=None, frames=None, timing='KEEP', mode=None):
    """Make the path (between the first and last of `frames`) follow a curve object.
    timing: 'KEEP', 'EVEN' or 'POINTS'."""
    return _run_op(bpy.ops.emp.import_curve, frames if frames is not None else [], obj, bone, mode,
                   curve=curve, timing=timing)


def set_value_channel(obj, bone=None, data_path='', index=0):
    """Which animated value Value mode (4) shows, e.g. '["ik_fk"]' or
    'pose.bones["Hand"].constraints["IK"].influence'."""
    o = _object(obj)
    _check_bone(o, bone)
    owners = ([o.pose.bones[bone]] if bone else []) + [o]
    ok = False
    for owner in owners:
        try:
            owner.path_resolve(data_path)
            ok = True
            break
        except (ValueError, TypeError, AttributeError):
            pass
    if not ok:
        names = [p for p, _i, _label in core.value_candidates(o, bone or "")]
        prefix = 'pose.bones["%s"]' % bpy.utils.escape_identifier(bone) if bone else ""
        short = [p[len(prefix):] if prefix and p.startswith(prefix) else p for p in names]
        raise ValueError("%r is no value of %s %s; its animated values: %s.%s" % (
            data_path, o.name, bone or "", ", ".join(short[:12]) or "none", _suggest(data_path, short)))
    st = state.settings(_scene())
    found = False
    for e in st.paths:
        if e.object is not None and e.object.name == o.name and e.bone_name == (bone or ""):
            e.value_path, e.value_index = data_path, index
            found = True
    if not found:
        raise ValueError("No path for %s %s: add_paths() first" % (o.name, bone or ""))
    update()


# ----------------------------------------------------------------------------
# easing presets, timing tools, the speed (2.0)
# ----------------------------------------------------------------------------

EASINGS = ('LINEAR', 'SINE', 'QUAD', 'CUBIC', 'QUART', 'QUINT', 'EXPO', 'CIRC', 'BACK', 'BOUNCE', 'ELASTIC')


def _one_of(name, value, allowed):
    if value not in allowed:
        raise ValueError("%s %r: use one of %s.%s" % (name, value, ", ".join(allowed), _suggest(str(value), allowed)))


def apply_easing(obj, bone=None, frames=None, easing='SINE', direction='EASE_IN_OUT', keep_path=True,
                 strength=1.0, start=0.0, end=1.0, blend=0.0, invert=False, mode=None):
    """An easing curve on the part between the first and last of `frames` (the P gallery):
    easing 'LINEAR', 'SINE', 'QUAD', 'CUBIC', 'QUART', 'QUINT', 'EXPO', 'CIRC', 'BACK', 'BOUNCE',
    'ELASTIC'; direction 'EASE_IN' (start slow), 'EASE_OUT' (end slow), 'EASE_IN_OUT'.
    keep_path: retime along the path (its shape stays; Back / Elastic overshoot along the motion);
    False: Blender's easing interpolation on the channels. start / end (0..1 of the part): put the
    easing on there only (invert: outside), blend: how softly it joins (share of the part)."""
    _one_of("easing", easing, EASINGS)
    _one_of("direction", direction, ('EASE_IN', 'EASE_OUT', 'EASE_IN_OUT'))
    return _run_op(bpy.ops.emp.easing_preset, frames, obj, bone, mode, easing=easing, direction=direction,
                   mode='TIMING' if keep_path else 'CHANNELS', strength=strength, start=start, end=end,
                   blend=blend, invert=invert)


def breakdown(obj, bone=None, frames=None, favor=0.0, mode=None):
    """Blend the keys at `frames` between the poses of their neighbor keys (a breakdown):
    favor -1 = the previous pose, 0 = halfway, 1 = the next pose."""
    return _run_op(bpy.ops.emp.tween, frames, obj, bone, mode, amount=max(-1.0, min(1.0, favor)))


def scale_time(obj, bone=None, frames=None, factor=1.0, ripple=False, mode=None):
    """Stretch (factor > 1, slower) or squash (< 1, faster) the timing of the keys at `frames`
    around the first of them; ripple: the later keys move along."""
    if factor <= 0:
        raise ValueError("factor must be above 0 (1: as it is, 2: twice as long)")
    return _run_op(bpy.ops.emp.time_scale, frames, obj, bone, mode, amount=factor, ripple=ripple)


def add_frames(obj, bone=None, frame=None, count=1, mode=None):
    """Add `count` frames after the key at `frame` (negative: remove, stopping at the next key):
    everything after it moves (ripple)."""
    _key_frames(_cache(obj, bone, mode), [frame])
    return _run_op(bpy.ops.emp.add_frames, [frame], obj, bone, mode, amount=float(count))


def moving_hold(obj, bone=None, frame=None, frames=0, drift=0.15, mode=None):
    """A hold after the key at `frame` that still drifts a little in the direction of the motion
    (frames: how long, 0 = as long as it took to get there; drift 0..0.6). It needs room: the
    new key goes before the next one."""
    c = _cache(obj, bone, mode)
    t = _key_frames(c, [frame])[0]
    later = [k.t for k in c.keys if k.t > t + 1e-3]
    if later and frames and t + frames >= later[0] - 0.5:
        raise ValueError("no room for a %s-frame hold after frame %s: the next key is at %s (frames < %s)" % (
            frames, frame, later[0], round(later[0] - 0.5 - t, 2)))
    return _run_op(bpy.ops.emp.moving_hold, [frame], obj, bone, mode, frames=float(frames), drift=drift)


def snap_to_frames(obj, bone=None, frames=None, mode=None):
    """Put the keys at `frames` (None: all) on whole frames."""
    return _run_op(bpy.ops.emp.snap_frames, frames, obj, bone, mode)


def shift_phase(obj, bone=None, offset=1.0, loop=True, mode=None, affect='ALL', channels=None):
    """The whole animation of the bone / object `offset` frames later. loop: inside its loop (a Cycles
    curve keeps its frames and length, its shape and timing - only which pose comes when moves, e.g.
    a walk starting on the other foot); off, or no loop: all its keys simply move."""
    from .ops_timetools import shift_phase as _phase
    with _in_mode(_valid_mode(mode)):
        c = _cache(obj, bone, mode)
        _one_of("affect", affect, ('ALL', 'LOCATION', 'ROTATION', 'SCALE', 'VALUE'))
        _phase(c, float(offset), loop, affect, tuple(channels) if channels else (True,) * 4)
        c.refresh(force=True)
    _push("Shift Phase")
    return [round(k.t, 3) for k in c.keys]


def offset_cascade(obj, bones, frames=None, offset=2.0, mode=None, loop=True, affect='ALL'):
    """Overlap / follow-through: each bone in `bones` (in that order, e.g. a tail from its root)
    starts `offset` frames after the one before - its keys at `frames` (None: all) move later.
    The first bone stays. frames=None moves each bone's whole animation (loop: looping curves
    shift inside their loop)."""
    from .ops_timetools import move_keys_in_time
    if not bones or len(bones) < 2:
        raise ValueError("offset_cascade needs 2 or more bones, in the order they follow each other")
    with _in_mode(_valid_mode(mode)):
        caches = [_cache(obj, b, mode) for b in bones]
        for i, c in enumerate(caches):
            d = offset * i
            if abs(d) < 1e-9:
                continue
            if frames is None:
                from .ops_timetools import shift_phase as _phase
                _phase(c, d, loop, affect)
            else:
                ts = _key_frames(c, frames)
                move_keys_in_time(c, {t: t + d for t in ts}, [k.t for k in c.keys if k.sel], affect=affect)
            c.refresh(force=True)
    _push("Offset Cascade")
    return {b: [round(k.t, 3) for k in c.keys] for b, c in zip(bones, caches)}


def _key_frames(c, frames):
    """The keys of path c at `frames`, or ValueError listing its keys."""
    have = {round(k.t, 3): k.t for k in c.keys}
    missing = [f for f in frames if round(float(f), 3) not in have]
    if missing:
        raise ValueError("No key of %s at frames %s; its keys are at %s" % (
            c.label, missing, [round(k.t, 3) for k in c.keys]))
    return [have[round(float(f), 3)] for f in frames]


def change_speed(obj, bone=None, frame=None, amount=0.0, reach=1.0, mode=None):
    """The speed graph, dragged: faster (amount > 0, distance per frame) or slower (< 0) around
    `frame` - at a key, or anywhere between two keys. The path keeps its shape and the keys their
    frames (the rest makes up for it). reach: how far it spreads (1: about a third of the
    segment; more: into the segments around)."""
    c = _cache(obj, bone, mode)
    ks = [k.t for k in c.keys]
    f = float(frame)
    if any(abs(t - f) < 1e-3 for t in ks):
        props = dict(mode='KEYSPEED', frame=f)
    else:
        before = [t for t in ks if t < f]
        after = [t for t in ks if t > f]
        if not before or not after:
            raise ValueError("frame %s is outside the keys of %s (%s - %s)" % (frame, c.label, ks[0], ks[-1]))
        props = dict(mode='EASE', frame=before[-1], frame_b=after[0], grab=f)
    return _run_op(bpy.ops.emp.speed_drag, [], obj, bone, mode, obj_name=c.obj_name, bone_name=c.bone_name,
                   amount=amount, reach=reach, **props)


def smooth_speed(obj, bone=None, frames=None, radius=3.0, passes=3, mode=None, strength=0.5):
    """Even out the speed (bumps, dips, steps at keys) between the first and last of `frames`
    (None: the whole path) - the right-drag brush of the speed graph, run along it `passes`
    times, `strength` (0.1 - 1) of the way each time. The path keeps its shape and the keys their
    frames (their poses slide along the path)."""
    from .ops_timetools import SpeedSmoother
    with _in_mode(_valid_mode(mode)):
        c = _cache(obj, bone, mode)
        ks = [k.t for k in c.keys]
        a, b = (ks[0], ks[-1]) if not frames else (min(frames), max(frames))
        step = max(radius * 0.5, 0.5)
        sm = SpeedSmoother(c)
        for _p in range(max(1, int(passes))):
            t = a
            while t <= b + 1e-6:
                sm.dab(t, radius, min(max(strength, 0.1), 1.0))
                t += step
        c.refresh(force=True)
    _push("Smooth Speed")
    return [round(s, 5) for s in get_path(obj, bone, mode)["speed_per_frame"]]


def onion(show=True, before=None, after=None, step=None, display=None, obj=None, bones=None):
    """Onion skin: ghosts of the selected bones / objects on the frames around the current one
    (render_view shows them). obj (and bones): select those first. display: 'SILHOUETTE', 'WIRE'
    or 'POINTS'. The ghosts are captured in the background: "ready" says whether they all are -
    if not, render a moment later (a new tool call)."""
    from . import onion as _onion
    sc = _scene()
    st = state.settings(sc)
    if obj is not None:
        o = _object(obj)
        for b in bones or ():
            _check_bone(o, b)
        for other in sc.objects:
            if other != o and other.select_get():
                other.select_set(False)
        o.select_set(True)
        bpy.context.view_layer.objects.active = o
        if bones and o.type == 'ARMATURE':
            if bpy.context.mode != 'POSE':
                win, area, region = _view3d()
                with bpy.context.temp_override(window=win, area=area, region=region):
                    bpy.ops.object.mode_set(mode='POSE')
            for pb in o.pose.bones:
                compat.bone_select_set(pb, pb.name in bones)
            o.data.bones.active = o.data.bones[bones[0]]
    st.show_onion = bool(show)
    if before is not None:
        st.onion_before = int(before)
    if after is not None:
        st.onion_after = int(after)
    if step is not None:
        st.onion_step = int(step)
    if display is not None:
        _one_of("display", display, ('SILHOUETTE', 'WIRE', 'POINTS'))
        st.onion_mode = display
    out = {"show": st.show_onion, "before": st.onion_before, "after": st.onion_after, "step": st.onion_step,
           "display": st.onion_mode}
    if st.show_onion:
        _onion._schedule(0.01)
        done, shows = _onion.ready(sc, st)
        out.update(ghost_frames=shows, ready=bool(shows) and len(done) == len(shows))
        if not shows:
            out["note"] = "no ghosts: select the bones / objects to ghost (obj=, bones=) - their animation is shown"
        elif not out["ready"]:
            out["note"] = "the ghosts are being captured: call onion() or render_view again in a moment"
    return out


def center_of_mass(obj, frames=None):
    """Where the character's weight is (from its deform bones and meshes), frame by frame - to check
    a pose is balanced, a jump's arc a parabola. It is worked out in the background once it is
    shown; until then {"ready": False}: call again a moment later (a new tool call).
    Returns per frame the center (x, y, z) and the floor contact height, and for the current
    frame whether the center is over the feet ('IN'), outside them ('OUT') or in the air ('AIR')."""
    from . import com
    o = _object(obj)
    if o.type != 'ARMATURE':
        raise ValueError("%r is not an armature: the center of mass is worked out for characters" % o.name)
    sc = _scene()
    st = state.settings(sc)
    if not st.show_com:
        st.show_com = True
    tr = com._track.get(o.name) or {}
    f0, f1 = com._range(sc)
    want = [int(f) for f in (frames if frames is not None else range(f0, f1 + 1))]
    if o.name not in com._mass or any(f not in tr or tr[f] is None for f in want if f0 <= f <= f1):
        com._schedule()
        done = sum(1 for f in want if tr.get(f) is not None)
        return {"ready": False, "status": com.status or "working out the frames (%d of %d done)" % (done, len(want)),
                "note": "worked out in the background - call center_of_mass again in a moment"}
    now = com.state_now(sc, o)
    out = {"ready": True, "frames": {f: {"center": _v(tr[f][0]), "lowest": round(float(tr[f][1]), 4)}
                                     for f in want if tr.get(f) is not None}}
    if now is not None:
        g, floor, _poly, bal, vel = now
        out["now"] = {"frame": sc.frame_current, "center": _v(g), "floor": round(float(floor), 4), "balance": bal,
                      "velocity": _v(vel) if vel is not None else None}
    return out


def copy_pose(obj, bone=None, frame=None, whole_pose=True, mode=None):
    """Copy the pose of the key at `frame` (whole_pose: every channel of the bone / object, else
    those of the mode) - paste_pose puts it on another frame, or on another bone with the same
    channels (Ctrl+C / Ctrl+V)."""
    c = _cache(obj, bone, mode)
    _key_frames(c, [frame])
    return _run_op(bpy.ops.emp.copy_keys, [frame], obj, bone, mode, all_channels=whole_pose)


def paste_pose(obj, bone=None, frame=None, whole_pose=True, mode=None):
    """Key the copied pose on `frame` (a key is made there; whole_pose: every channel copied,
    else those of the mode)."""
    sc = _scene()
    now = sc.frame_current
    sc.frame_set(int(round(frame)))
    try:
        r = _run_op(bpy.ops.emp.paste_keys, [], obj, bone, mode, whole_pose=whole_pose)
    finally:
        sc.frame_set(now)
    return r


def mirror(obj, bone=None, frames=None, axis='X', orientation='GLOBAL', mode=None):
    """Mirror the keys at `frames` across `axis` through their pivot (Blender's pivot point
    setting: their middle by default) - e.g. make an arc bend the other way."""
    _one_of("axis", axis, ('X', 'Y', 'Z'))
    return _run_op(bpy.ops.emp.mirror, frames, obj, bone, mode, axis=axis, orient=orientation)


def jitter(obj, bone=None, mode=None):
    """Roughness per frame [(frame, value)]; values above ~0.4 look like hitches or noise."""
    from . import draw as d
    c = _cache(obj, bone, mode)
    return [(round(t, 2), round(v, 3)) for t, v in zip(c.times, d.jitter_values(c))
            if abs(t - round(t)) < 1e-6]


def _push(message):
    try:
        from . import onion
        onion.changed(pose=True)    # (a script may edit any bone: every ghost renews)
    except Exception:
        pass
    try:
        bpy.ops.ed.undo_push(message=message)
    except RuntimeError:
        pass


# ----------------------------------------------------------------------------
# seeing it
# ----------------------------------------------------------------------------

def _material(name, color):
    """Flat unlit color, readable in Solid, Material Preview and Rendered shading."""
    m = bpy.data.materials.get(name)
    if m is None:
        m = bpy.data.materials.new(name)
        m.use_nodes = True
        nt = m.node_tree
        nt.nodes.clear()
        em = nt.nodes.new('ShaderNodeEmission')
        out = nt.nodes.new('ShaderNodeOutputMaterial')
        nt.links.new(em.outputs[0], out.inputs['Surface'])
    m.diffuse_color = color
    em = next((n for n in m.node_tree.nodes if n.type == 'EMISSION'), None) if m.node_tree else None
    if em is not None:
        em.inputs['Color'].default_value = color
        em.inputs['Strength'].default_value = 1.0
    return m


def _octa_mesh(name, centers, r):
    verts, faces = [], []
    for c in centers:
        b = len(verts)
        verts += [c + Vector(d) * r for d in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))]
        for f in ((0, 2, 4), (2, 1, 4), (1, 3, 4), (3, 0, 4), (2, 0, 5), (1, 2, 5), (3, 1, 5), (0, 3, 5)):
            faces.append(tuple(b + i for i in f))
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    return me


def preview_objects(show=True):
    """Create (or remove with show=False) real objects that mirror the visible paths, so they
    show up in any viewport screenshot or render: a line per path, white octahedra on keys
    (orange when selected), small gray ones on every frame (spacing) and a green one on the
    current frame. They live in the "EMP Path Preview" collection and are not rendered."""
    coll = bpy.data.collections.get(PREVIEW_COLLECTION)
    if coll is not None:
        for o in list(coll.objects):
            data = o.data
            bpy.data.objects.remove(o, do_unlink=True)
            if data is not None and data.users == 0:
                (bpy.data.curves if isinstance(data, bpy.types.Curve) else bpy.data.meshes).remove(data)
    if not show:
        if coll is not None:
            bpy.data.collections.remove(coll)
        return []
    scene = _scene()
    if coll is None:
        coll = bpy.data.collections.new(PREVIEW_COLLECTION)
    if coll.name not in scene.collection.children:
        scene.collection.children.link(coll)
    made = []
    cur = scene.frame_current
    for c in state.visible_caches(scene):
        if not c.refresh():
            continue
        pts = c.points
        lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
        hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
        size = max((hi - lo).length, 0.05)
        r = size * 0.006
        label = c.bone_name or c.obj_name
        cu = bpy.data.curves.new("EMP " + label, 'CURVE')
        cu.dimensions = '3D'
        cu.bevel_depth = r
        cu.bevel_resolution = 0
        # one short spline per segment, colored by speed like the overlay (blue slow .. red fast)
        speeds = c.speeds
        vmax = sorted(speeds)[min(len(speeds) - 1, int(len(speeds) * 0.95))] if speeds else 1.0
        vmax = vmax or 1.0
        for i in range(SPEED_BUCKETS):
            cu.materials.append(_material("EMP Speed %d" % i,
                                          tuple(emp_draw._speed_color(i / (SPEED_BUCKETS - 1))) + (1.0,)))
        for i in range(len(pts) - 1):
            sp = cu.splines.new('POLY')
            sp.points.add(1)
            sp.points[0].co = (*pts[i], 1.0)
            sp.points[1].co = (*pts[i + 1], 1.0)
            sp.material_index = min(SPEED_BUCKETS - 1, int(speeds[i] / vmax * (SPEED_BUCKETS - 1) + 0.5))
        objs = [("EMP Path " + label, cu, None)]
        frames = [p for t, p in zip(c.times, c.points) if abs(t - round(t)) < 1e-6]
        objs.append(("EMP Frames " + label, _octa_mesh("EMP Frames " + label, frames, r * 1.4),
                     (0.75, 0.75, 0.75, 1.0)))
        keys = [k.co for k in c.keys if not k.sel]
        sel = [k.co for k in c.keys if k.sel]
        if keys:
            objs.append(("EMP Keys " + label, _octa_mesh("EMP Keys " + label, keys, r * 3.5), (1, 1, 1, 1)))
        if sel:
            objs.append(("EMP Selected " + label, _octa_mesh("EMP Selected " + label, sel, r * 4.0),
                         (1.0, 0.5, 0.1, 1.0)))
        if c.fs <= cur <= c.fe:
            p = c.point(c.target(), cur)
            objs.append(("EMP Current " + label, _octa_mesh("EMP Current " + label, [p], r * 3.5),
                         (0.3, 1.0, 0.4, 1.0)))
        for name, data, col in objs:
            o = bpy.data.objects.new(name, data)
            if col is not None:
                data.materials.append(_material("EMP " + name.split()[1], col))
                o.color = col
            o.show_in_front = True
            o.hide_render = True
            o.hide_select = True
            coll.objects.link(o)
            made.append(o.name)
    return made


def render_view(filepath, overlays=True, frame_numbers=False):
    """Save the largest 3D viewport, with the path overlays (as you see them in Blender), to a PNG.
    frame_numbers=True writes every key's frame next to it (to tell which key is which). Returns
    the path."""
    arr = _render(overlays, frame_numbers)
    _save_png(arr, filepath)
    return filepath


def render_speed_graph(filepath, obj, bone=None, frames=None, mode=None):
    """Save the speed graph of a path to a PNG, as the viewport draws it: the speed (height) over
    the frames, the keys as lines with their frames - to judge the timing by eye. frames: [first,
    last] key of the part shown (None: all the keys). The path's keys there get selected."""
    st = state.settings(_scene())
    shown = st.show_speed_graph
    rng = st.speed_range
    with _in_mode(_valid_mode(mode)):
        c = _cache(obj, bone, mode)
        ks = [k.t for k in c.keys]
        if frames:
            lo, hi = min(frames), max(frames)
            ks = [t for t in ks if lo - 1e-3 <= t <= hi + 1e-3]
        with _only(c):
            _select_owner_and_keys(c, ks or None)
            st.show_speed_graph = True
            st.speed_range = 'SEGMENT'
            try:
                arr = _render(True, False)
                sg = state.speed_graph
            finally:
                st.show_speed_graph = shown
                st.speed_range = rng
    if sg is None:
        raise ValueError("No speed graph for %s %s (too short?)" % (c.obj_name, c.bone_name))
    H, W = arr.shape[:2]
    m = 12
    x0, x1 = max(int(sg["x0"]) - 4 * m, 0), min(int(sg["x1"]) + 4 * m, W)
    y0, y1 = max(int(sg["y0"]) - m, 0), min(int(sg["y1"]) + 2 * m, H)
    _save_png(arr[y0:y1, x0:x1], filepath)
    return filepath


def _save_png(arr, filepath):
    import numpy as np
    H, W = arr.shape[:2]
    img = bpy.data.images.new("_emp_render_view", W, H, alpha=True)
    img.pixels.foreach_set(np.ascontiguousarray(arr).ravel())
    img.filepath_raw = os.path.abspath(bpy.path.abspath(filepath))    # (relative: to the working folder)
    img.file_format = 'PNG'
    img.save()
    bpy.data.images.remove(img)


def _render(overlays=True, frame_numbers=False):
    """The largest 3D viewport drawn offscreen with the add-on's overlays: (H, W, 4) floats."""
    import gpu
    import numpy as np
    win, area, region = _view3d()
    if area is None:
        raise RuntimeError("No 3D viewport")
    space = area.spaces.active
    rv3d = space.region_3d
    # the view matrices follow view changes (setup_view, frame_path) only at the next redraw:
    # bring them up to date, or the shot shows the view as it was before
    rv3d.update()
    W, H = region.width, region.height
    off = gpu.types.GPUOffScreen(W, H)
    ctx = bpy.context
    with ctx.temp_override(window=win, area=area, region=region):
        off.draw_view3d(ctx.scene, ctx.view_layer, space, region, rv3d.view_matrix,
                        rv3d.window_matrix, do_color_management=True)
        with off.bind():
            state.force_draw = True
            numbers = state.force_numbers
            state.force_numbers = numbers or frame_numbers
            if overlays:
                from . import onion
                onion.SRGB_TARGET[0] = 0.0      # (the offscreen buffer stores the colors as they are)
                try:
                    with gpu.matrix.push_pop():
                        gpu.matrix.load_matrix(rv3d.view_matrix)
                        gpu.matrix.load_projection_matrix(rv3d.window_matrix)
                        emp_draw._draw_view()
                finally:
                    onion.SRGB_TARGET[0] = 1.0
                with gpu.matrix.push_pop():
                    gpu.matrix.load_matrix(Matrix.Identity(4))
                    gpu.matrix.load_projection_matrix(
                        Matrix.Translation((-1, -1, 0)) @ Matrix.Diagonal((2 / W, 2 / H, 1, 1)))
                    emp_draw._draw_pixel()
            buf = gpu.state.active_framebuffer_get().read_color(0, 0, W, H, 4, 0, 'UBYTE')
            state.force_draw = False
            state.force_numbers = numbers
    off.free()
    return np.array(buf.to_list(), dtype=np.float32).reshape(H, W, 4) / 255.0


def setup_view(obj=None, bone=None, mode=None, solid=True, angle=None, show_bones=None):
    """Get the largest 3D viewport ready for screenshots: Solid shading (preview objects
    are drawn in front of the character only there), optional view angle (x, y, z euler in
    radians) and framed on a path. show_bones=False hides the rig's bones / control shapes
    (a big rig's controls cover the paths; the paths are drawn either way)."""
    win, area, region = _view3d()
    if area is None:
        return
    space = area.spaces.active
    if solid:
        space.shading.type = 'SOLID'
    if show_bones is not None:
        space.overlay.show_bones = show_bones
    if angle is not None:
        from mathutils import Euler
        space.region_3d.view_rotation = Euler(angle).to_quaternion()
        space.region_3d.view_perspective = 'PERSP'
    if obj is not None:
        frame_path(obj, bone, mode)


def frame_path(obj, bone=None, mode=None, margin=1.4):
    """Point the largest 3D viewport at a path (keeps the view angle)."""
    c = _cache(obj, bone, mode)
    win, area, region = _view3d()
    if area is None:
        return
    pts = c.points
    center = sum(pts, Vector()) / len(pts)
    radius = max((p - center).length for p in pts)
    r3 = area.spaces.active.region_3d
    r3.view_location = center
    r3.view_distance = max(radius * 2.0 * margin, 0.1)
    r3.update()         # (render_view right after this sees the new view)
    area.tag_redraw()
