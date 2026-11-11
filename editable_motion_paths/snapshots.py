"""Snapshots: poses kept to compare with. Static: the pose as it was when taken (a photo, unkeyed
changes too). Live: the pose on a frame as the animation has it now - it follows every edit, like
an onion ghost of that one frame. Of the whole character or only the part some bones move (picked
by the bones' weights, like the onion skin); drawn as a see-through silhouette or a wire, moved
aside by an offset (side by side). Apply puts a snapshot's values back on the bones and keys them.

A snapshot that is off (or all of them, Snapshots off) costs nothing: nothing is drawn, worked out
or kept up to date. A Static one keeps its shape and its values in a mesh of its own that no object
uses (saved with the file, never evaluated); a Live one is worked out in a scene of its own (only
the character in it), when its frame or the animation changes."""

import time

import bpy
import gpu
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty
from gpu_extras.batch import batch_for_shader

from . import compat, state

AUX_NAME = ".Motion Paths Snapshot"     # (Live snapshots are worked out in this scene)
_TF = (("location", 3), ("rotation_quaternion", 4), ("rotation_euler", 3), ("rotation_axis_angle", 4),
       ("scale", 3))
NV = sum(k for _p, k in _TF)            # values kept per bone
LIMIT = 40000                           # triangles a snapshot keeps at most (melted down beyond, like the ghosts)
PALETTE = ((0.25, 0.65, 1.0), (1.0, 0.55, 0.2), (0.45, 0.9, 0.4), (0.95, 0.4, 0.75), (0.95, 0.85, 0.3),
           (0.6, 0.5, 1.0))

_geo = {}           # uid -> the shape {"key", "co", "tri", "ed", "dom", "names"} (None: couldn't be made)
_live = {}          # uid -> (sig, cluster of every vertex, cluster sizes) of a Live one: the same melt each update
_poses = {}         # uid -> (bone names, values, rotation modes) of a Live one on its frame
_batches = {}       # uid -> (key, triangles batch, lines batch)
_dirty = set()      # Live ones to work out again
_timer = [False]
_counter = [0]


# ----------------------------------------------------------------------------
# what a snapshot is of
# ----------------------------------------------------------------------------

def previewing(snap):
    """A Static snapshot whose Frame was changed but not taken yet (the check mark): it shows the pose
    of that frame, its own pose is kept."""
    return snap.mode == 'STATIC' and snap.frame != snap.taken


def worked(snap):
    """Its shape comes from the animation now (Live, or a Static one previewing another frame)."""
    return snap.mode == 'LIVE' or previewing(snap)


def _ready(snap):
    g = _geo.get(snap.uid, False)
    return g is None or (g is not False and g["key"][0] == "live")


def find(st, uid):
    for i, s in enumerate(st.snapshots):
        if s.uid == uid:
            return i, s
    return -1, None


def controls(arm):
    """The bones an animator works with: the ones shown (not hidden, in a shown collection)."""
    out = []
    for pb in arm.pose.bones:
        b = pb.bone
        if b.hide:
            continue
        cols = getattr(b, "collections", ())
        if len(cols) and not any(c.is_visible for c in cols):
            continue
        out.append(pb)
    return out


def selected_bones(arm):
    return [pb.name for pb in arm.pose.bones if compat.bone_selected(pb)]


def parts(arm, scene=None):
    """The meshes the snapshot shows: those the armature bends, and those on it (on a bone, or
    parented to it)."""
    out = []
    vl = bpy.context.view_layer
    in_layer = set(vl.objects.keys()) if vl is not None else None
    for o in (scene or bpy.context.scene).objects:
        if o.type != 'MESH':
            continue
        # (not visible_get(): in a 3D view in Local View it leaves the character's meshes out)
        try:
            if o.hide_viewport or o.hide_get() or (in_layer is not None and o.name not in in_layer):
                continue
        except RuntimeError:
            continue
        if o.parent == arm or any(m.type == 'ARMATURE' and m.object == arm and m.show_viewport for m in o.modifiers):
            out.append(o)
    return out


def _dominant(o, me, arm, deform, names, index):
    """(N,) the bone every vertex mostly follows (index into names; -1: none - it goes with the
    object): a bent mesh's strongest deforming bone; a mesh on a bone: that bone."""
    import numpy as np
    n = len(me.vertices)
    dom = np.full(n, -1, dtype=np.int32)

    def at(name):
        i = index.get(name)
        if i is None:
            i = index[name] = len(names)
            names.append(name)
        return i
    if o.parent == arm and o.parent_type == 'BONE' and o.parent_bone:
        dom[:] = at(o.parent_bone)
        return dom
    if not any(m.type == 'ARMATURE' and m.object == arm for m in o.modifiers):
        return dom
    gmap = {i: at(g.name) for i, g in enumerate(o.vertex_groups) if g.name in deform}
    if not gmap:
        return dom
    for v in me.vertices:
        best, bw = -1, 0.0
        for g in v.groups:
            k = gmap.get(g.group)
            if k is not None and g.weight > bw:
                best, bw = k, g.weight
        dom[v.index] = best
    return dom


def _shape(arm, objs, dg, want_dom=True):
    """The meshes as the depsgraph has them now: world positions, triangles, edges, every vertex's
    bone (or None) and the bones' names."""
    import numpy as np
    deform = {b.name for b in arm.data.bones if b.use_deform}
    cos, tris, eds, doms, names, index = [], [], [], [], [], {}
    base = 0
    for o in objs:
        ev = o.evaluated_get(dg)
        try:
            me = ev.to_mesh()
        except RuntimeError:
            me = None
        if me is None:
            continue
        try:
            n = len(me.vertices)
            if not n:
                continue
            co = np.empty(n * 3, dtype=np.float32)
            me.vertices.foreach_get("co", co)
            M = np.array(ev.matrix_world, dtype=np.float64)
            co = (co.reshape(-1, 3) @ M[:3, :3].T + M[:3, 3]).astype(np.float32)
            me.calc_loop_triangles()
            tri = np.empty(len(me.loop_triangles) * 3, dtype=np.int64)
            me.loop_triangles.foreach_get("vertices", tri)
            ed = np.empty(len(me.edges) * 2, dtype=np.int64)
            me.edges.foreach_get("vertices", ed)
            cos.append(co)
            tris.append(tri.reshape(-1, 3) + base)
            eds.append(ed.reshape(-1, 2) + base)
            if want_dom:
                doms.append(_dominant(o, me, arm, deform, names, index))
            base += n
        finally:
            ev.to_mesh_clear()
    if not cos:
        z = np.zeros((0, 3), dtype=np.float32)
        return z, np.zeros((0, 3), dtype=np.int64), np.zeros((0, 2), dtype=np.int64), \
            np.zeros(0, dtype=np.int32), []
    return np.concatenate(cos), np.concatenate(tris), np.concatenate(eds), \
        (np.concatenate(doms) if want_dom else None), names


def _melt(co, tri, ed, dom):
    """At most LIMIT triangles (melted like the onion's ghosts): (co, tri, ed, dom, cluster of every
    vertex or None)."""
    import numpy as np
    if len(tri) <= LIMIT:
        return co, tri, ed, dom, None
    from .onion import _decimate
    co2, tri2, ed2, inv = _decimate(co, tri, LIMIT, ed)
    dom2 = np.full(len(co2), -1, dtype=np.int32)
    dom2[inv] = dom
    return co2, tri2, ed2, dom2, inv


def _pose_values(pbs, names):
    """(names kept, (n, NV) values, rotation modes) of these pose bones (original or evaluated)."""
    import numpy as np
    vals, keep, modes = [], [], []
    for n in names:
        pb = pbs.get(n)
        if pb is None:
            continue
        row = []
        for prop, _k in _TF:
            row.extend(getattr(pb, prop))
        vals.append(row)
        keep.append(n)
        modes.append(pb.rotation_mode)
    return keep, np.array(vals, dtype=np.float64).reshape(-1, NV), modes


# ----------------------------------------------------------------------------
# Static: kept in a mesh of its own (no object uses it: never evaluated, saved with the file)
# ----------------------------------------------------------------------------

def _store(co, tri, dom, names, pose):
    import numpy as np
    me = bpy.data.meshes.new(".Motion Paths Snapshot")
    nt = len(tri)
    me.vertices.add(len(co))
    me.vertices.foreach_set("co", co.astype(np.float32).ravel())
    me.loops.add(nt * 3)
    me.loops.foreach_set("vertex_index", tri.astype(np.int32).ravel())
    me.polygons.add(nt)
    me.polygons.foreach_set("loop_start", np.arange(0, nt * 3, 3, dtype=np.int32))
    me.update(calc_edges=True)
    a = me.attributes.new("emp_bone", 'INT', 'POINT')
    a.data.foreach_set("value", dom.astype(np.int32))
    me["emp_bone_names"] = "\n".join(names)
    pnames, vals, modes = pose
    me["emp_pose_names"] = "\n".join(pnames)
    me["emp_pose_modes"] = "\n".join(modes)
    me["emp_pose"] = vals.ravel().tolist() if len(vals) else [0.0]
    return me


def _stored_shape(me):
    import numpy as np
    n = len(me.vertices)
    co = np.empty(n * 3, dtype=np.float32)
    me.vertices.foreach_get("co", co)
    lv = np.empty(len(me.loops), dtype=np.int64)
    me.loops.foreach_get("vertex_index", lv)
    ed = np.empty(len(me.edges) * 2, dtype=np.int64)
    me.edges.foreach_get("vertices", ed)
    dom = np.full(n, -1, dtype=np.int32)
    a = me.attributes.get("emp_bone")
    if a is not None and n:
        a.data.foreach_get("value", dom)
    names = me.get("emp_bone_names", "").split("\n") if me.get("emp_bone_names") else []
    return co.reshape(-1, 3), lv.reshape(-1, 3), ed.reshape(-1, 2), dom, names


def stored_pose(me):
    """(bone names, (n, NV) values, rotation modes) kept in a Static snapshot."""
    import numpy as np
    names = me.get("emp_pose_names", "")
    names = names.split("\n") if names else []
    modes = me.get("emp_pose_modes", "")
    modes = modes.split("\n") if modes else []
    vals = np.array(list(me.get("emp_pose", [])), dtype=np.float64)
    if not names or len(vals) != len(names) * NV:
        return [], np.zeros((0, NV)), []
    return names, vals.reshape(-1, NV), modes


def take(context, kind, name=None):
    """A new Static snapshot of the active armature's pose as it is now. Returns it or a message."""
    scene = context.scene
    st = state.settings(scene)
    arm = context.active_object
    if arm is None or arm.type != 'ARMATURE':
        return "Select an armature (Pose Mode) to take a snapshot of"
    bones = selected_bones(arm) if kind == 'BONES' else []
    if kind == 'BONES' and not bones:
        return "Select the bones whose part to take (Pose Mode)"
    me = _capture(context, arm)
    snap = st.snapshots.add()
    snap.uid = max([s.uid for s in st.snapshots] + [0]) + 1
    snap.name = name or ("Pose %d" % scene.frame_current if kind == 'POSE' else
                         "%s %d" % (bones[-1] if len(bones) == 1 else "Bones", scene.frame_current))
    snap.armature = arm
    snap.kind = kind
    snap.bones = "\n".join(bones)
    snap.mode = 'STATIC'
    snap.taken = snap.frame = scene.frame_current
    snap.color = PALETTE[(snap.uid - 1) % len(PALETTE)]
    snap.data = me
    st.snapshot_index = len(st.snapshots) - 1
    return snap


def _capture(context, arm):
    """The armature's character as it is now, and its controls' values: in a new mesh."""
    dg = context.evaluated_depsgraph_get()
    co, tri, ed, dom, names = _shape(arm, parts(arm, context.scene), dg)
    co, tri, ed, dom, _inv = _melt(co, tri, ed, dom)
    pose = _pose_values(arm.pose.bones, [pb.name for pb in controls(arm)])
    return _store(co, tri, dom, names, pose)


def retake(context, snap):
    """A Static snapshot taken again: the pose as it is now, on this frame (its other settings
    stay). Returns True, or a message."""
    arm = snap.armature
    if arm is None or arm.type != 'ARMATURE':
        return "Its armature is gone"
    old = snap.data
    snap.data = _capture(context, arm)
    if old is not None and old.users == 0:
        bpy.data.meshes.remove(old)
    snap.taken = snap.frame = context.scene.frame_current
    _geo.pop(snap.uid, None)
    _batches.pop(snap.uid, None)
    state.tag_redraw()
    return True


def freeze(context, snap):
    """A Live snapshot made Static (or a Static one's preview taken): its shape and values on its
    frame now are kept."""
    if snap.uid in _dirty or not _ready(snap) or _geo.get(snap.uid) is None:
        _work_out(context.scene, snap)
    g = _geo.get(snap.uid)
    pose = _poses.get(snap.uid)
    if not g or pose is None:
        return False
    if snap.data is not None:
        _drop_mesh(snap)
    snap.data = _store(g["co"], g["tri"], g["dom"], g["names"], pose)
    snap.taken = snap.frame
    return True


def _drop_mesh(snap):
    me = snap.data
    snap.data = None
    if me is not None and me.users == 0:
        bpy.data.meshes.remove(me)


# ----------------------------------------------------------------------------
# Live: the pose on its frame, worked out in a scene of its own (the user's scene is left as it is)
# ----------------------------------------------------------------------------

def _work_out(scene, snap):
    """A Live snapshot's shape and values on its frame, from the animation as it is now."""
    import numpy as np
    from . import core
    arm = snap.armature
    uid = snap.uid
    _dirty.discard(uid)
    if arm is None or arm.type != 'ARMATURE':
        _geo[uid] = None
        return
    objs = parts(arm, scene)
    # (the scene is kept while Live ones are updated - linking objects has Blender rebuild its own
    # scene's depsgraph: not on every update of a drag)
    aux = core.aux_scene(scene, [arm] + objs, name=AUX_NAME)
    if aux is None:
        _geo[uid] = None
        return
    was = state.baking
    state.baking = True         # (our own handlers sit this out)
    try:
        vl = aux.view_layers[0]
        dg = vl.depsgraph
        if dg is None:
            aux.frame_set(snap.frame)       # (its depsgraph made; inactive: the rig's own values aren't touched)
            dg = vl.depsgraph
        else:
            # (not frame_set: every window redrawn, every frame handler called; not the view layer's
            # update(): it makes this depsgraph the active one - the animation would go into the rig)
            if aux.frame_current != snap.frame:
                aux.frame_current = snap.frame
            dg.update()
        sig = tuple((o.name, len(o.data.vertices)) for o in objs)
        kept = _live.get(uid)
        old = _geo.get(uid)
        n = _counter[0] = _counter[0] + 1      # (a new key: drawn anew)
        done = False
        if kept is not None and kept[0] == sig and old is not None:
            # (the same meshes: only where the vertices are now - melted the same way as before, so the
            # snapshot doesn't shimmer as the animation changes)
            co = _shape(arm, objs, dg, want_dom=False)[0]
            inv, counts = kept[1], kept[2]
            if inv is not None and len(co) == len(inv):
                co = np.stack([np.bincount(inv, weights=co[:, k], minlength=len(counts)) / counts
                               for k in range(3)], axis=1).astype(np.float32)
                done = True
            elif inv is None and len(co) == len(old["co"]):
                done = True
            if done:
                _geo[uid] = dict(old, co=co, key=("live", uid, n))
        if not done:
            co, tri, ed, dom, names = _shape(arm, objs, dg)
            co, tri, ed, dom, inv = _melt(co, tri, ed, dom)
            counts = np.maximum(np.bincount(inv, minlength=len(co)), 1).astype(np.float64) \
                if inv is not None else None
            _live[uid] = (sig, inv, counts)
            _geo[uid] = {"key": ("live", uid, n), "co": co, "tri": tri, "ed": ed, "dom": dom, "names": names}
        ae = arm.evaluated_get(dg)
        _poses[uid] = _pose_values(ae.pose.bones, [pb.name for pb in controls(arm)])
    finally:
        state.baking = was
        _last_work[0] = time.time()
        if not bpy.app.timers.is_registered(_drop_idle):
            bpy.app.timers.register(_drop_idle, first_interval=IDLE_DROP)


IDLE_DROP = 2.0         # the Live snapshots' scene goes this long after their last update
_last_work = [0.0]
_spent = [0.02]         # how long the last update took (the next one waits as long: Blender keeps up)


def _drop_idle():
    """The Live snapshots' scene removed once they haven't been updated for a while (not kept in the
    file or in undo steps for nothing)."""
    from . import core
    if _pending() or time.time() - _last_work[0] < IDLE_DROP:
        return 0.5
    try:
        core.drop_aux(AUX_NAME)
    except Exception:
        pass
    return None


def anim_changed():
    """The animation changed: the Live snapshots on show are worked out again - at once, also while
    keys are dragged (like the onion's ghosts)."""
    st = state.settings()
    if st is None or not st.show_snapshots:
        return
    any_live = False
    for s in st.snapshots:
        if s.show and worked(s):
            _dirty.add(s.uid)
            any_live = True
    if any_live:
        _schedule(max(0.02, _spent[0]))


def _schedule(delay=0.02):
    if not _timer[0]:
        _timer[0] = True
        bpy.app.timers.register(_tick, first_interval=delay)


def _tick():
    st = state.settings()
    if st is None or not st.show_snapshots:
        _timer[0] = False
        return None
    screen = getattr(bpy.context, "screen", None)
    if state.baking or (screen is not None and screen.is_animation_playing) or state.render_running():
        return 0.3
    todo = [s for s in st.snapshots if s.show and worked(s) and (s.uid in _dirty or not _ready(s))]
    if not todo:
        _timer[0] = False
        return None
    t = time.time()
    try:
        _work_out(bpy.context.scene, todo[0])
    except Exception as ex:      # (never a timer dying with a traceback storm)
        print("Editable Motion Paths: snapshot failed:", ex)
        _geo[todo[0].uid] = None
        _dirty.discard(todo[0].uid)
    _spent[0] = time.time() - t
    state.tag_redraw()
    if _pending(st):
        return max(0.02, _spent[0])     # (as long again for Blender: a drag stays smooth)
    _timer[0] = False
    return None


def _pending(st=None):
    """The snapshots on show still to be worked out."""
    st = st or state.settings()
    return [s for s in st.snapshots if s.show and worked(s) and (s.uid in _dirty or not _ready(s))] \
        if st is not None and st.show_snapshots else []


def changed(snap):
    """A snapshot's settings changed (its frame, part, mode...): drawn anew, a Live one worked out."""
    _batches.pop(snap.uid, None)
    if worked(snap):
        _dirty.add(snap.uid)
        _schedule()
    else:
        _dirty.discard(snap.uid)
    state.tag_redraw()


def frame_changed(scene):
    """The scene went to another frame (scrubbed, played): a Static snapshot previewing a frame
    without its check mark goes back to its own - its pose isn't lost by accident."""
    st = state.settings(scene)
    if st is None or not len(st.snapshots):
        return
    for snap in st.snapshots:
        if previewing(snap):
            snap.frame = snap.taken


def after_undo():
    """After an undo / redo: what is drawn stays until its new shape is ready - a setting dragged in
    the Adjust Last Operation panel undoes and redoes on every change: dropping the shapes made the
    snapshots blink. The Live ones and the previews are worked out again (the animation may be
    another one now); a Static one's shape is taken again only if its mesh is another one."""
    st = state.settings()
    if st is None:
        return
    for snap in st.snapshots:
        if worked(snap):
            _dirty.add(snap.uid)
    if _pending(st):
        _schedule()


def clear_runtime():
    """After an undo / a file load: what was kept is made again from the file's snapshots."""
    _geo.clear()
    _live.clear()
    _poses.clear()
    _batches.clear()
    _dirty.clear()


# ----------------------------------------------------------------------------
# drawing
# ----------------------------------------------------------------------------

def _shape_of(snap):
    if worked(snap):
        g = _geo.get(snap.uid)
        return (g["key"], g["co"], g["tri"], g["ed"], g["dom"], g["names"]) if g else None
    me = snap.data
    if me is None:
        return None
    key = ("static", me.session_uid, len(me.vertices))
    g = _geo.get(snap.uid)
    if g is None or g["key"] != key:
        co, tri, ed, dom, names = _stored_shape(me)
        g = _geo[snap.uid] = {"key": key, "co": co, "tri": tri, "ed": ed, "dom": dom, "names": names}
    return g["key"], g["co"], g["tri"], g["ed"], g["dom"], g["names"]


def _on_segment(p, a, b):
    """(how far point p is from the segment a-b, where along it: 0 at a, 1 at b)."""
    ab = b - a
    t = 0.0 if ab.length_squared < 1e-12 else max(0.0, min(1.0, (p - a).dot(ab) / ab.length_squared))
    return (p - (a + ab * t)).length, t


def part_bones(arm, bones, scope):
    """The bones whose part of the mesh a Selected Bones snapshot shows. Limb: what the selected bones
    move (their children, IK chains - like the onion's By Weight). Bone: only their own part - every
    deforming bone of the limb goes with the control nearest to it in the rest pose (the controls
    the limb has: the selected ones, and the ones the animator poses further down it); a selected
    one's are its part: a hand with its palm, not the fingers; an upper arm, not the forearm. Found
    by where the bones are, not by their names; nothing there: the limb."""
    sel = set(bones)
    if arm is None or arm.type != 'ARMATURE':
        return sel
    from .onion import affected_bones
    limb = affected_bones(arm, sel)
    if scope != 'BONE':
        return limb
    data = arm.data.bones
    shown = {pb.name for pb in controls(arm)}
    ctl = [data[n] for n in (sel | (limb & shown)) if data.get(n) is not None]
    out = set()
    for d in data:
        if not d.use_deform or d.name not in limb:
            continue
        if d.name in sel:
            out.add(d.name)
            continue
        mid = (d.head_local + d.tail_local) * 0.5
        dist = [(_on_segment(mid, c.head_local, c.tail_local)[0], c.name) for c in ctl]
        if not dist:
            continue
        best = min(x for x, _n in dist)
        if any(n in sel for x, n in dist if x <= best + 1e-5):
            out.add(d.name)
    return out or limb


def _batch(snap, sh):
    import numpy as np
    got = _shape_of(snap)
    if got is None:
        return None
    key, co, tri, ed, dom, names = got
    bones = snap.bones.split("\n") if snap.kind == 'BONES' and snap.bones else []
    bkey = (key, snap.kind, snap.bones, snap.scope)
    kept = _batches.get(snap.uid)
    if kept is not None and kept[0] == bkey:
        return kept
    if not len(co):
        return None
    if snap.kind == 'BONES':
        aff = part_bones(snap.armature, bones, snap.scope)
        ids = np.array([i for i, n in enumerate(names) if n in aff], dtype=np.int32)
        keep = np.isin(dom, ids) if len(ids) else np.zeros(len(co), dtype=bool)
        tri = tri[keep[tri].all(axis=1)] if len(tri) else tri
        ed = ed[keep[ed].all(axis=1)] if len(ed) else ed
    bt = batch_for_shader(sh, 'TRIS', {"pos": co}, indices=tri.astype(np.int32)) if len(tri) else None
    bl = batch_for_shader(sh, 'LINES', {"pos": co}, indices=ed.astype(np.int32)) if len(ed) else None
    kept = _batches[snap.uid] = (bkey, bt, bl)
    return kept


def draw(scene, st, us):
    """The snapshots that are on (called from the 3D view's draw handler; nothing to do when off)."""
    if not st.show_snapshots or not len(st.snapshots):
        return
    sh = gpu.shader.from_builtin('UNIFORM_COLOR')
    prev = gpu.state.depth_test_get()
    gpu.state.blend_set('ALPHA')
    for snap in st.snapshots:
        if not snap.show:
            continue
        if worked(snap) and (snap.uid in _dirty or not _ready(snap)):
            _schedule()             # (the last shape stays drawn until the new one is ready)
        try:
            got = _batch(snap, sh)
        except Exception as ex:
            print("Editable Motion Paths: snapshot couldn't be drawn:", ex)
            got = None
        if got is None:
            continue
        _k, bt, bl = got
        col = tuple(snap.color[:3])
        a = snap.opacity
        test = 'ALWAYS' if snap.in_front else 'LESS_EQUAL'
        gpu.matrix.push()
        gpu.matrix.translate(tuple(snap.offset))
        if snap.display == 'SILHOUETTE' and bt is not None:
            # one flat layer: depth first, then the color only where it is in front
            gpu.state.face_culling_set('BACK')
            gpu.state.depth_test_set(test)
            gpu.state.depth_mask_set(True)
            gpu.state.color_mask_set(False, False, False, False)
            sh.uniform_float("color", (0.0, 0.0, 0.0, 0.0))
            bt.draw(sh)
            gpu.state.color_mask_set(True, True, True, True)
            gpu.state.depth_test_set('EQUAL')
            sh.uniform_float("color", col + (a,))
            bt.draw(sh)
            gpu.state.face_culling_set('NONE')
            gpu.state.depth_mask_set(False)
        elif bl is not None:
            gpu.state.depth_test_set(test if not snap.in_front else 'NONE')
            gpu.state.line_width_set(max(1.0, snap.line_width * us))
            sh.uniform_float("color", col + (min(1.0, a * 1.6),))
            bl.draw(sh)
            gpu.state.line_width_set(1.0)
        gpu.matrix.pop()
    gpu.state.depth_mask_set(False)
    gpu.state.depth_test_set(prev)


# ----------------------------------------------------------------------------
# Apply
# ----------------------------------------------------------------------------

def pose_of(context, snap):
    """(names, values, rotation modes) a snapshot keeps: a Static one's from when it was taken (also
    while it previews another frame: a preview is only to look at), a Live one's on its frame now."""
    if snap.mode == 'LIVE':
        if snap.uid in _dirty or snap.uid not in _poses or not _ready(snap):
            _work_out(context.scene, snap)
        return _poses.get(snap.uid)
    return stored_pose(snap.data) if snap.data is not None else None


def _blend(prop, now, want, factor):
    """now -> want by factor (0: now, 1: want); a quaternion turns the short way."""
    if factor >= 1.0:
        return tuple(want)
    if prop == "rotation_quaternion":
        from mathutils import Quaternion
        a, b = Quaternion(now).normalized(), Quaternion(want).normalized()
        return tuple(a.slerp(b, factor))
    return tuple(x + (y - x) * factor for x, y in zip(now, want))


def _values_on(pb, f, curves):
    """The bone's transform values on frame f as its curves have them (no curve: as it is now)."""
    out = []
    for prop, k in _TF:
        v = list(getattr(pb, prop))
        path = pb.path_from_id(prop)
        for i in range(k):
            fc = curves.get((path, i))
            if fc is not None:
                v[i] = fc.evaluate(f)
        out.append(tuple(v))
    return out


def apply(context, snap, part, current_frame, factor=1.0):
    """Put the snapshot's values on the bones (all it keeps / the selected ones) and key them - on
    the current frame, or on the snapshot's own; factor: how far from the pose there toward the
    snapshot's (0: as it is, 1: the snapshot's). Returns how many bones."""
    arm = snap.armature
    if arm is None or arm.type != 'ARMATURE':
        return 0
    got = pose_of(context, snap)
    if not got:
        return 0
    names, vals, _modes = got
    scene = context.scene
    # (a Static one: the frame its kept pose is of, whatever frame it previews)
    f = scene.frame_current if current_frame else (snap.frame if snap.mode == 'LIVE' else snap.taken)
    sel = set(selected_bones(arm)) if part == 'SELECTED' else None
    shown = {pb.name for pb in controls(arm)}
    pbs = arm.pose.bones
    curves = {(fc.data_path, fc.array_index): fc for fc in (compat.get_fcurves(arm) or ())}         if f != scene.frame_current else {}
    done = 0
    for name, row in zip(names, vals):
        pb = pbs.get(name)
        if pb is None or name not in shown or (sel is not None and name not in sel):
            continue
        before = [tuple(getattr(pb, prop)) for prop, _k in _TF]
        # (the pose there: as it is on this frame - unkeyed changes too -, else as the curves have it)
        there = before if f == scene.frame_current else _values_on(pb, f, curves)
        i = 0
        for (prop, k), now in zip(_TF, there):
            setattr(pb, prop, _blend(prop, now, row[i:i + k], factor))
            i += k
        rot = {'QUATERNION': "rotation_quaternion", 'AXIS_ANGLE': "rotation_axis_angle"}.get(
            pb.rotation_mode, "rotation_euler")
        for prop in ("location", rot, "scale"):
            try:
                pb.keyframe_insert(prop, frame=f, group=pb.name)
            except (RuntimeError, TypeError):
                pass
        if f != scene.frame_current:
            # (keyed on the snapshot's frame: the pose on this frame stays the animation's)
            for (prop, _k), v in zip(_TF, before):
                setattr(pb, prop, v)
        done += 1
    return done


# ----------------------------------------------------------------------------
# operators
# ----------------------------------------------------------------------------

class _SnapOp:
    bl_options = {'REGISTER', 'UNDO'}
    uid: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def _snap(self, context):
        st = state.settings(context.scene)
        return (st,) + find(st, self.uid)


class EMP_OT_snapshot_add(bpy.types.Operator):
    """Keep the pose as it is now, to compare with (Static: a photo of it). The whole character, or
    only the part the selected bones move"""
    bl_idname = "emp.snapshot_add"
    bl_label = "Take Snapshot"
    bl_options = {'REGISTER', 'UNDO'}

    kind: EnumProperty(name="Part", items=[('POSE', "Whole Pose", "The whole character"),
                                           ('BONES', "Selected Bones", "Only the part the selected bones move")])

    def execute(self, context):
        got = take(context, self.kind)
        if isinstance(got, str):
            self.report({'WARNING'}, got)
            return {'CANCELLED'}
        state.settings(context.scene).show_snapshots = True
        state.tag_redraw()
        return {'FINISHED'}


class EMP_OT_snapshot_remove(_SnapOp, bpy.types.Operator):
    """Remove this snapshot"""
    bl_idname = "emp.snapshot_remove"
    bl_label = "Remove Snapshot"

    def execute(self, context):
        st, i, snap = self._snap(context)
        if snap is None:
            return {'CANCELLED'}
        me = snap.data
        st.snapshots.remove(i)
        if me is not None and me.users == 0:
            bpy.data.meshes.remove(me)
        for d in (_geo, _live, _poses, _batches):
            d.pop(self.uid, None)
        st.snapshot_index = min(st.snapshot_index, len(st.snapshots) - 1)
        state.tag_redraw()
        return {'FINISHED'}


class EMP_OT_snapshot_duplicate(_SnapOp, bpy.types.Operator):
    """Duplicate this snapshot (its own copy: change its frame, offset, part...)"""
    bl_idname = "emp.snapshot_duplicate"
    bl_label = "Duplicate Snapshot"

    def execute(self, context):
        st, i, snap = self._snap(context)
        if snap is None:
            return {'CANCELLED'}
        new = st.snapshots.add()
        for p in snap.bl_rna.properties:
            if not p.is_readonly and p.identifier not in {"rna_type", "uid", "data"}:
                setattr(new, p.identifier, getattr(snap, p.identifier))
        new.uid = max(s.uid for s in st.snapshots) + 1
        new.name = snap.name + " copy"
        new.color = PALETTE[(new.uid - 1) % len(PALETTE)]
        if snap.data is not None:
            new.data = snap.data.copy()
        st.snapshots.move(len(st.snapshots) - 1, i + 1)
        st.snapshot_index = i + 1
        changed(st.snapshots[i + 1])
        return {'FINISHED'}


class EMP_OT_snapshot_convert(_SnapOp, bpy.types.Operator):
    """Show the whole character, or only the part the selected bones move"""
    bl_idname = "emp.snapshot_convert"
    bl_label = "Convert Snapshot"

    kind: EnumProperty(name="Part", items=[('POSE', "Whole Pose", "Show the whole character"),
                                           ('BONES', "Selected Bones", "Show only the part the selected bones "
                                                                       "move now")])

    def execute(self, context):
        st, i, snap = self._snap(context)
        if snap is None:
            return {'CANCELLED'}
        if self.kind == 'BONES':
            arm = snap.armature
            bones = selected_bones(arm) if arm is not None and arm.type == 'ARMATURE' else []
            if not bones:
                self.report({'WARNING'}, "Select the bones whose part to show (Pose Mode)")
                return {'CANCELLED'}
            snap.bones = "\n".join(bones)
        snap.kind = self.kind
        changed(snap)
        return {'FINISHED'}


class EMP_OT_snapshot_mode(_SnapOp, bpy.types.Operator):
    """Static: the pose as it was taken. Live: the pose on its frame as the animation has it now
    (follows every edit). Made Static, a Live snapshot keeps its pose of now"""
    bl_idname = "emp.snapshot_mode"
    bl_label = "Static / Live"

    mode: EnumProperty(name="Mode", items=[('STATIC', "Static", ""), ('LIVE', "Live", "")])

    def execute(self, context):
        st, i, snap = self._snap(context)
        if snap is None or snap.mode == self.mode:
            return {'CANCELLED'}
        if self.mode == 'STATIC':
            if not freeze(context, snap):
                self.report({'WARNING'}, "This snapshot couldn't be worked out")
                return {'CANCELLED'}
        snap.mode = self.mode
        changed(snap)
        return {'FINISHED'}


class EMP_OT_snapshot_update(_SnapOp, bpy.types.Operator):
    """Static: take it again - the pose as it is now, on this frame (its name, color, offset and part
    stay). Live: work it out again"""
    bl_idname = "emp.snapshot_update"
    bl_label = "Update Snapshot"

    def execute(self, context):
        st, i, snap = self._snap(context)
        if snap is None:
            return {'CANCELLED'}
        if snap.mode == 'LIVE':
            changed(snap)
            return {'FINISHED'}
        got = retake(context, snap)
        if got is not True:
            self.report({'WARNING'}, got)
            return {'CANCELLED'}
        return {'FINISHED'}


class EMP_OT_snapshot_take_frame(_SnapOp, bpy.types.Operator):
    """Keep this frame's pose in the snapshot (its pose of before is replaced)"""
    bl_idname = "emp.snapshot_take_frame"
    bl_label = "Take This Frame's Pose"

    def execute(self, context):
        st, i, snap = self._snap(context)
        if snap is None or not previewing(snap):
            return {'CANCELLED'}
        if not freeze(context, snap):
            self.report({'WARNING'}, "This frame's pose couldn't be worked out")
            return {'CANCELLED'}
        changed(snap)
        return {'FINISHED'}


class EMP_OT_snapshot_move(_SnapOp, bpy.types.Operator):
    """Move this snapshot up / down the list"""
    bl_idname = "emp.snapshot_move"
    bl_label = "Move Snapshot"

    direction: EnumProperty(name="Direction", items=[('UP', "Up", ""), ('DOWN', "Down", "")])

    def execute(self, context):
        st, i, snap = self._snap(context)
        if snap is None:
            return {'CANCELLED'}
        j = i - 1 if self.direction == 'UP' else i + 1
        if not 0 <= j < len(st.snapshots):
            return {'CANCELLED'}
        st.snapshots.move(i, j)
        st.snapshot_index = j
        return {'FINISHED'}


class EMP_OT_snapshot_apply(_SnapOp, bpy.types.Operator):
    """Put the snapshot's pose back on the bones and key it (the snapshot stays)"""
    bl_idname = "emp.snapshot_apply"
    bl_label = "Apply Snapshot"

    part: EnumProperty(name="Part", items=[('ALL', "Whole Pose", "Every bone the snapshot keeps"),
                                           ('SELECTED', "Selected Bones", "Only the selected bones")])
    current_frame: BoolProperty(name="Current Frame", default=True,
                                description="Key the pose on the current frame (off: on the snapshot's own "
                                            "frame)")
    factor: FloatProperty(name="Influence", default=1.0, min=0.0, max=1.0, subtype='FACTOR',
                          description="How far toward the snapshot's pose: 0 the pose as it is, 1 the snapshot's")

    def invoke(self, context, event):
        # (a window with an Apply button - Blender's popup that applies on every change doesn't apply
        # at all until something is changed; afterwards the Adjust Last Operation panel has the same
        # settings and applies them again as they change)
        return context.window_manager.invoke_props_dialog(self, confirm_text="Apply")

    def draw(self, context):
        layout = self.layout
        layout.row().prop(self, "part", expand=True)
        layout.prop(self, "factor", slider=True)
        layout.prop(self, "current_frame")

    def execute(self, context):
        st, i, snap = self._snap(context)
        if snap is None:
            return {'CANCELLED'}
        n = apply(context, snap, self.part, self.current_frame, self.factor)
        if not n:
            self.report({'WARNING'}, "No bone to apply it to (select some, or Whole Pose)")
            return {'CANCELLED'}
        self.report({'INFO'}, "Applied to %d bone%s" % (n, "" if n == 1 else "s"))
        state.tag_redraw()
        return {'FINISHED'}


class EMP_OT_snapshot_tools(bpy.types.Operator):
    """Snapshots: all of them at once"""
    bl_idname = "emp.snapshot_tools"
    bl_label = "Snapshot Tools"
    bl_options = {'REGISTER', 'UNDO'}

    action: EnumProperty(name="Action", items=[('REMOVE_ALL', "Remove All", "Remove every snapshot", 'X', 0),
                                ('TOGGLE_SHOW', "Toggle Visibility", "Show / hide them all", 'HIDE_OFF', 1),
                                ('TOGGLE_EXPAND', "Expand / Collapse", "Open / close them all", 'DOWNARROW_HLT', 2)])

    def execute(self, context):
        st = state.settings(context.scene)
        if self.action == 'REMOVE_ALL':
            meshes = [s.data for s in st.snapshots if s.data is not None]
            st.snapshots.clear()
            for me in meshes:
                if me.users == 0:
                    bpy.data.meshes.remove(me)
            clear_runtime()
        elif self.action == 'TOGGLE_SHOW':
            on = not any(s.show for s in st.snapshots)
            for s in st.snapshots:
                s.show = on
        else:
            on = not any(s.expanded for s in st.snapshots)
            for s in st.snapshots:
                s.expanded = on
        state.tag_redraw()
        return {'FINISHED'}


class EMP_MT_snapshot_tools(bpy.types.Menu):
    bl_label = "Snapshot Tools"
    bl_idname = "EMP_MT_snapshot_tools"

    def draw(self, context):
        self.layout.operator_enum("emp.snapshot_tools", "action")


class EMP_MT_snapshot_entry(bpy.types.Menu):
    """The drop-down of one snapshot (like a modifier's)"""
    bl_label = "Snapshot"
    bl_idname = "EMP_MT_snapshot_entry"

    def draw(self, context):
        layout = self.layout
        snap = getattr(context, "emp_snapshot", None)
        if snap is None:
            return
        uid = snap.uid
        layout.operator("emp.snapshot_update", text="Update", icon='FILE_REFRESH').uid = uid
        layout.operator("emp.snapshot_duplicate", text="Duplicate", icon='DUPLICATE').uid = uid
        layout.separator()
        for kind, text, icon in (('POSE', "Convert to Whole Pose", 'ARMATURE_DATA'),
                                 ('BONES', "Convert to Selected Bones", 'BONE_DATA')):
            op = layout.operator("emp.snapshot_convert", text=text, icon=icon)
            op.uid, op.kind = uid, kind
        other = 'LIVE' if snap.mode == 'STATIC' else 'STATIC'
        op = layout.operator("emp.snapshot_mode", text="Make %s" % other.title(),
                             icon='RECORD_ON' if other == 'LIVE' else 'IMAGE_DATA')
        op.uid, op.mode = uid, other
        layout.separator()
        for d, text, icon in (('UP', "Move Up", 'TRIA_UP'), ('DOWN', "Move Down", 'TRIA_DOWN')):
            op = layout.operator("emp.snapshot_move", text=text, icon=icon)
            op.uid, op.direction = uid, d


def draw_list(layout, context, toggle=True):
    st = state.settings(context.scene)
    row = layout.row(align=True)
    row.operator("emp.snapshot_add", text="Whole Pose", icon='ADD').kind = 'POSE'
    row.operator("emp.snapshot_add", text="Selected Bones", icon='ADD').kind = 'BONES'
    row.menu("EMP_MT_snapshot_tools", text="", icon='DOWNARROW_HLT')
    if toggle:      # (the sidebar's panel has it in its header)
        layout.row().prop(st, "show_snapshots", text="Show Snapshots")
    if not len(st.snapshots):
        layout.label(text="No snapshots: take one of the pose (Pose Mode)", icon='INFO')
        return
    for snap in st.snapshots:
        box = layout.box()
        row = box.row(align=True)
        row.prop(snap, "expanded", text="", emboss=False, icon='DOWNARROW_HLT' if snap.expanded else 'RIGHTARROW')
        row.prop(snap, "show", text="", emboss=False, icon='HIDE_OFF' if snap.show else 'HIDE_ON')
        # (Static / Live: told by the icon - switched in the menu, not by a stray click)
        row.label(text="", icon='RECORD_ON' if snap.mode == 'LIVE' else 'IMAGE_DATA')
        row.prop(snap, "name", text="")
        if not snap.expanded:
            row.operator("emp.snapshot_apply", text="", icon='CHECKMARK', emboss=False).uid = snap.uid
        row.context_pointer_set("emp_snapshot", snap)
        row.menu("EMP_MT_snapshot_entry", text="", icon='DOWNARROW_HLT')
        row.operator("emp.snapshot_remove", text="", icon='X', emboss=False).uid = snap.uid
        if not snap.expanded:
            continue
        # (every field named in itself: an undo step is called by it - "Offset X: 1.2 m", not "1.2 m")
        col = box.column()
        r = col.row(align=True)
        if snap.kind == 'POSE':
            r.label(text="Whole Pose", icon='ARMATURE_DATA')
        else:
            n = len([b for b in snap.bones.split(chr(10)) if b])
            r.label(text="%d Bone%s" % (n, "" if n == 1 else "s"), icon='BONE_DATA')
            r.prop(snap, "scope", expand=True)
        r = col.row(align=True)
        if previewing(snap):
            r.alert = True          # (another frame shown - not kept until the check mark)
        r.prop(snap, "frame", text="%s Frame" % ("Live" if snap.mode == 'LIVE' else "Static"))
        if previewing(snap):
            r.alert = False
            r.operator("emp.snapshot_take_frame", text="", icon='CHECKMARK').uid = snap.uid
        sub = col.column(align=True)
        for i, a in enumerate("XYZ"):
            sub.prop(snap, "offset", index=i, text="Offset " + a)
        r = col.row(align=True)
        r.prop(snap, "color", text="")
        r.prop(snap, "opacity", text="Opacity", slider=True)
        r = col.row(align=True)
        r.prop(snap, "display", expand=True)
        r = col.row(align=True)
        r.prop(snap, "in_front")
        if snap.display == 'WIRE':
            r.prop(snap, "line_width", text="Width")
        box.operator("emp.snapshot_apply", text="Apply...", icon='CHECKMARK').uid = snap.uid


class EMP_PT_snapshots(bpy.types.Panel):
    """The sidebar's Motion Paths tab: Snapshots, under Modifiers."""
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Motion Paths"
    bl_label = "Snapshots"
    bl_parent_id = "EMP_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        st = state.settings(context.scene)
        if st is not None:
            self.layout.prop(st, "show_snapshots", text="")

    def draw(self, context):
        draw_list(self.layout, context, toggle=False)


class EMP_PT_snapshots_popover(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'HEADER'
    bl_label = "Snapshots"
    bl_idname = "EMP_PT_snapshots_popover"
    bl_ui_units_x = 17

    def draw(self, context):
        draw_list(self.layout, context)


classes = (EMP_OT_snapshot_add, EMP_OT_snapshot_remove, EMP_OT_snapshot_duplicate, EMP_OT_snapshot_convert,
           EMP_OT_snapshot_mode, EMP_OT_snapshot_update, EMP_OT_snapshot_take_frame, EMP_OT_snapshot_move, EMP_OT_snapshot_apply, EMP_OT_snapshot_tools,
           EMP_MT_snapshot_tools, EMP_MT_snapshot_entry, EMP_PT_snapshots_popover)
