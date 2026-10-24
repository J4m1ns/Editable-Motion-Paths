"""Scripting API, e.g. for an AI assistant driving Blender through an MCP connection.

    import sys
    emp = next(m for n, m in sys.modules.items() if n.endswith("editable_motion_paths.api"))

    emp.add_paths("Rig", ["CTRL_Foot_L"])           # show paths
    print(emp.analyze("Rig", "CTRL_Foot_L")["summary"])   # read the motion like an animator
    emp.preview_objects()                           # make the paths visible in screenshots
    emp.move_key("Rig", "CTRL_Foot_L", 25, offset=(0, 0, 0.05))

Every function takes object names / bone names (not RNA references) and works no matter
which tool, mode or selection is active. `mode` is 'LOCATION' or 'ROTATION' (default: the
current edit mode); 'SCALE' and 'VALUE' are the scale ribbon and the value track.
"""

import math
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


def _mode(mode):
    return mode or state.settings(_scene()).edit_mode


def _cache(obj, bone=None, mode=None):
    """Path cache for obj/bone, adding and calculating the path if needed."""
    name = obj if isinstance(obj, str) else obj.name
    mode = _mode(mode)
    key = (name, bone or "", mode)
    c = state.caches.get(key)
    if c is None or not c.B:
        add_paths(name, [bone] if bone else None, mode=mode)
        c = state.caches.get(key)
    if c is None:
        raise ValueError("No path for %s %s" % (name, bone or ""))
    c.refresh(force=True)
    return c


def _v(p, nd=4):
    return [round(x, nd) for x in p]


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
    """Show editable paths for an object, or for bones of an armature. Returns path keys."""
    o = bpy.data.objects[obj] if isinstance(obj, str) else obj
    st = state.settings(_scene())
    if mode:
        st.edit_mode = mode
    existing = {(e.object.name if e.object else None, e.bone_name) for e in st.paths}
    for b in (bones or [""]):
        if (o.name, b) not in existing:
            e = st.paths.add()
            e.object = o
            e.bone_name = b
    update()
    return [k for k in state.caches if k[0] == o.name and (not bones or k[1] in bones)]


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
    """'LOCATION', 'ROTATION', 'SCALE' or 'VALUE'."""
    state.settings(_scene()).edit_mode = mode
    state.sync_caches(_scene())
    update()


def update():
    """Recalculate all paths (call after changing animation from a script)."""
    state.sync_caches(_scene())
    for c in state.caches.values():
        c.B = []
    state.bake_all(_scene())


def list_paths():
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
            "what": "bone head / object origin" if c.ptype == core.LOCATION else "bone tip",
            "frames": [int(round(f)) for f in frames], "points": [_v(p) for p in pts],
            "speed_per_frame": [round(s, 5) for s in speeds], "keys": keys}


# ----------------------------------------------------------------------------
# reading the motion
# ----------------------------------------------------------------------------

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
            "head/origin" if c.ptype == core.LOCATION else "bone tip", frames[0], frames[-1], len(ks)),
        "distance %.3f, average speed %.4f/frame, fastest %.4f at frame %d" % (
            arc, arc / len(spd), vmax, fast),
        "holds (no motion): %s" % (", ".join("%d-%d" % tuple(h) for h in holds) or "none"),
        "pops (abrupt speed changes): %s" % (
            ", ".join("%d %s" % (p["frame"], p["kind"]) for p in pops) or "none"),
        "sharp turns (>50 deg in one frame): %s" % (
            ", ".join("%d (%.0f deg)" % (t["frame"], t["angle_deg"]) for t in turns) or "none"),
        "keys: " + ", ".join("%g %s" % (e["frame"], e["key"]) for e in eases),
    ]
    return {"object": c.obj_name, "bone": c.bone_name, "mode": c.ptype, "distance": round(arc, 4),
            "speed_per_frame": [round(s, 5) for s in spd], "holds": holds, "pops": pops,
            "sharp_turns": turns, "keys": eases, "summary": "\n".join(lines)}


# ----------------------------------------------------------------------------
# editing (all undoable as one step each via bpy.ops.ed.undo_push)
# ----------------------------------------------------------------------------

def _select_owner_and_keys(c, frames):
    obj, owner = core.resolve(c.obj_name, c.bone_name)
    if c.bone_name:
        if obj.mode != 'POSE':
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.mode_set(mode='POSE')
        compat.bone_select_set(owner, True)
    else:
        obj.select_set(True)
    c.refresh(force=True)
    pick.deselect_all([c])
    c.refresh(force=True)
    want = None if frames is None else {round(float(f), 3) for f in frames}
    for k in c.keys:
        if want is None or round(k.t, 3) in want:
            pick.select_part(c, k, 'KEY', True)
    c.refresh(force=True)


def select_keys(obj, bone=None, frames=None, mode=None):
    """Select keys of a path (frames=None: all) and its bone/object, so operators act on them."""
    c = _cache(obj, bone, mode)
    _select_owner_and_keys(c, frames)
    state.tag_redraw()


def move_key(obj, bone=None, frame=None, target=None, offset=None, mode=None):
    """Move one path key in world space: to `target` (x, y, z) or by `offset`.

    Location paths move the bone head / object origin; rotation paths turn the bone so its
    tip points at the target. Missing channel keys are added. Returns the new position.
    """
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


def insert_key(obj, bone=None, frame=None, mode=None):
    """Insert a key on the path at `frame` without changing the motion."""
    from .ops_keys import insert_key as _insert
    c = _cache(obj, bone, mode)
    k = _insert(c, float(frame))
    _push("Insert Path Key")
    return k is not None


def _run_op(_op, _frames, _obj, _bone, _mode, **props):
    # (underscored names: operator properties like `frames` or `mode` go through **props)
    c = _cache(_obj, _bone, _mode)
    _select_owner_and_keys(c, _frames)
    win, area, region = _view3d()
    if area is not None:
        with bpy.context.temp_override(window=win, area=area, region=region):
            r = _op('EXEC_DEFAULT', **props)
    else:
        r = _op('EXEC_DEFAULT', **props)
    c.refresh(force=True)
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
    return _run_op(bpy.ops.emp.handle_type, frames, obj, bone, mode, type=handle_type)


def space(obj, bone=None, frames=None, profile='EVEN', factor=1.0, strength=2.0, mode=None):
    """Re-space the motion between the first and last of `frames` (None: whole path) while
    the path keeps its shape. profile: 'EVEN' (constant speed), 'SLOW_IN' (start slowly),
    'SLOW_OUT' (end slowly), 'SLOW_IN_OUT'."""
    return _run_op(bpy.ops.emp.space, frames if frames is not None else [], obj, bone, mode,
                   profile=profile, factor=factor, power=strength)


def delete_keys(obj, bone=None, frames=None, mode=None):
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
    op = {'CIRCLE': bpy.ops.emp.circle, 'ARC': bpy.ops.emp.arc, 'FLATTEN': bpy.ops.emp.flatten,
          'STRAIGHTEN': bpy.ops.emp.straighten}[tool]
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
    st = state.settings(_scene())
    for e in st.paths:
        if e.object is not None and e.object.name == (obj if isinstance(obj, str) else obj.name) \
                and e.bone_name == (bone or ""):
            e.value_path, e.value_index = data_path, index
    update()


def jitter(obj, bone=None, mode=None):
    """Roughness per frame [(frame, value)]; values above ~0.4 look like hitches or noise."""
    from . import draw as d
    c = _cache(obj, bone, mode)
    return [(round(t, 2), round(v, 3)) for t, v in zip(c.times, d.jitter_values(c))
            if abs(t - round(t)) < 1e-6]


def _push(message):
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


def render_view(filepath, overlays=True):
    """Save the largest 3D viewport, with the path overlays, to a PNG. Returns the path."""
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
            if overlays:
                with gpu.matrix.push_pop():
                    gpu.matrix.load_matrix(rv3d.view_matrix)
                    gpu.matrix.load_projection_matrix(rv3d.window_matrix)
                    emp_draw._draw_view()
                with gpu.matrix.push_pop():
                    gpu.matrix.load_matrix(Matrix.Identity(4))
                    gpu.matrix.load_projection_matrix(
                        Matrix.Translation((-1, -1, 0)) @ Matrix.Diagonal((2 / W, 2 / H, 1, 1)))
                    emp_draw._draw_pixel()
            buf = gpu.state.active_framebuffer_get().read_color(0, 0, W, H, 4, 0, 'UBYTE')
            state.force_draw = False
    off.free()
    arr = np.array(buf.to_list(), dtype=np.float32).reshape(H, W, 4) / 255.0
    img = bpy.data.images.new("_emp_render_view", W, H, alpha=True)
    img.pixels.foreach_set(arr.ravel())
    img.filepath_raw = filepath
    img.file_format = 'PNG'
    img.save()
    bpy.data.images.remove(img)
    return filepath


def setup_view(obj=None, bone=None, mode=None, solid=True, angle=None):
    """Get the largest 3D viewport ready for screenshots: Solid shading (preview objects
    are drawn in front of the character only there), optional view angle (x, y, z euler in
    radians) and framed on a path."""
    win, area, region = _view3d()
    if area is None:
        return
    space = area.spaces.active
    if solid:
        space.shading.type = 'SOLID'
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
