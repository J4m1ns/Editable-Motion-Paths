"""Match Motion: the selected bones / objects follow the active one over a range of frames, keeping where they were
against it on a reference frame (as if parented to it there) - baked into their own keys, only as many as the motion
needs. Before the range they blend in from their own motion, after it back out; an influence for all of it. No path
is needed. With nothing to follow they are pinned: they stay where they were on the reference frame.

The leader's motion is taken in the world (Global: L its world matrix) or against its parent (Local: L =
Lp^-1 @ its world matrix), and put on the follower in the world (Global: P = I) or against its parent (Local: P its
parent's world matrix):
    W(f) = P(f) @ L(f) @ L(ref)^-1 @ P(ref)^-1 @ W(ref).
In a loop (the follower's curves repeated by a Cycles modifier) all of it stays inside the loop: a blend before
the range goes on from the loop's end, one after it into its start, and the loop is closed again.
Turned into its own channels through its parents on each frame (Blender's convert_space), blended with its own
channels where it blends in / out (location and scale mixed, rotation turned the shortest way) and fitted with the
Clean Up fitter (fit.py): its keys there are replaced, the keys on either side keep their outer handles."""

import math

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from mathutils import Matrix, Vector

from . import core, fit, pick, state

MATCH_SCENE = ".Motion Paths Match"     # (the scene the frames are stepped through in - see core.aux_scene)


def _world(obj, bone):
    if bone:
        pb = obj.pose.bones.get(bone) if obj.pose is not None else None
        return None if pb is None else obj.matrix_world @ pb.matrix
    return obj.matrix_world.copy()


def _parent_world(obj, bone):
    """What a bone / object is placed against: its parent bone's world matrix (a root bone: its armature's), an
    object's parent matrix (the world: none)."""
    if bone:
        pb = obj.pose.bones.get(bone) if obj.pose is not None else None
        if pb is None:
            return None
        return obj.matrix_world @ pb.parent.matrix if pb.parent is not None else obj.matrix_world.copy()
    return core._object_parent_matrix(obj)


def _basis(obj, bone, W):
    """The basis (channels before constraints, with an object's deltas) that puts this bone / object at world
    matrix W, its parents as they are now."""
    if bone:
        return obj.convert_space(pose_bone=obj.pose.bones[bone], matrix=W, from_space='WORLD', to_space='LOCAL')
    return obj.convert_space(matrix=W, from_space='WORLD', to_space='LOCAL')


def step_frames(scene, objs, frames, fn):
    """fn(frame, depsgraph or None) on each frame - in the bake scene when these objects can be baked there (the
    user's scene, its frame and poses left alone), else in the user's scene, its frame and poses put back."""
    objs = [o for o in objs if o is not None]
    aux = core.aux_scene(scene, objs, name=MATCH_SCENE) if core._aux_fits(objs) else None
    state.baking = True
    try:
        if aux is not None:
            vl = aux.view_layers[0]
            for f in frames:
                dg = vl.depsgraph
                if dg is None:
                    aux.frame_set(f)
                    dg = vl.depsgraph
                else:
                    aux.frame_current = f
                    dg.update()
                fn(f, dg)
            return
        saved = state.keep_scene_pose(scene, objs)
        cur, sub = scene.frame_current, scene.frame_subframe
        try:
            for f in frames:
                scene.frame_set(f)
                fn(f, None)
        finally:
            scene.frame_set(cur, subframe=sub)
            if state.put_pose_back(saved):
                bpy.context.view_layer.update()
    finally:
        state.baking = False
        if aux is not None:
            core.drop_aux(MATCH_SCENE)


def _ramp(f, start, end, blend_in, blend_out):
    """How much of the leader's motion a frame takes (before the influence): 1 over the range, easing in / out over
    the blends, 0 outside."""
    if start <= f <= end:
        return 1.0
    if blend_in > 0 and start - blend_in < f < start:
        u = (f - (start - blend_in)) / blend_in
    elif blend_out > 0 and end < f < end + blend_out:
        u = ((end + blend_out) - f) / blend_out
    else:
        return 0.0
    return u * u * (3.0 - 2.0 * u)


def _catmull(vals, frames, t):
    """The value at t from values on whole frames (a Catmull-Rom spline through them)."""
    i = max(0, min(len(frames) - 2, int(math.floor(t - frames[0]))))
    u = t - frames[i]
    p1, p2 = vals[i], vals[i + 1]
    p0 = vals[i - 1] if i > 0 else p1
    p3 = vals[i + 2] if i + 2 < len(vals) else p2
    u2, u3 = u * u, u * u * u
    return [0.5 * (2 * b + (-a + c) * u + (2 * a - 5 * b + 4 * c - d) * u2 + (-a + 3 * b - 3 * c + d) * u3)
            for a, b, c, d in zip(p0, p1, p2, p3)]


class _Measure:
    """What the fitter needs of a path: which channels it fits, and a point that shows two values apart (location
    / scale: themselves; rotation: a point turned with it, off every axis)."""

    def __init__(self, ptype, tg):
        self.ptype = ptype
        self.length = tg.length if tg.is_bone else 1.0

    def measure(self, tg, t, v):
        if self.ptype == core.ROTATION:
            return core.vals_to_quat(v, tg.rot_mode) @ Vector((0.35, 1.0, 0.2)) * self.length
        return Vector(v[:3])


def _cycle(tg):
    """(first, last) key frame of the follower's own curves when a Cycles modifier repeats them - its keys then
    stay inside that loop and the loop stays closed - else None."""
    lo = hi = None
    looped = False
    for ch in (tg.loc, tg.rot, tg.scale):
        for fc in ch.fcurves:
            if fc is None or not len(fc.keyframe_points):
                continue
            cyc = core.cycles_of(fc)
            if cyc and 'NONE' not in (cyc[0], cyc[2]):
                looped = True
            a, b = fc.keyframe_points[0].co[0], fc.keyframe_points[-1].co[0]
            lo = a if lo is None else min(lo, a)
            hi = b if hi is None else max(hi, b)
    if not looped or lo is None or hi - lo < 2.0:
        return None
    return int(round(lo)), int(round(hi))


def match(scene, follower, leader, start, end, reference, blend_in=0, blend_out=0, influence=1.0,
          location=True, rotation=True, scale=True, tolerance=0.5, space='GLOBAL', leader_space='GLOBAL'):
    """follower / leader: (object, bone name or ''); leader (None, ''): pinned - it stays where it was. space: where
    the motion is put on the follower, leader_space: what of the leader's motion is taken - 'GLOBAL' (the world) /
    'LOCAL' (against its parent; see the module's notes). Returns the number of keys written per channel set, or a
    string saying why nothing could be done."""
    fobj, fbone = follower
    lobj, lbone = leader
    if fobj is None:
        return "Select the bones / objects that should follow"
    if lobj is not None and fobj == lobj and fbone == lbone:
        return "A bone / object can't follow itself"
    owner = fobj.pose.bones.get(fbone) if fbone else fobj
    if owner is None or (lobj is not None and lbone and (lobj.pose is None or lobj.pose.bones.get(lbone) is None)):
        return "The bone is gone"
    if lobj is not None and core.follows(fobj, fbone, lobj, lbone):
        return "%s moves with %s: it can't follow it" % (lbone or lobj.name, fbone or fobj.name)
    local_f, local_l = space == 'LOCAL', leader_space == 'LOCAL'
    if end < start:
        start, end = end, start
    blend_in, blend_out = max(0, int(blend_in)), max(0, int(blend_out))
    sets = [p for p, on in ((core.LOCATION, location), (core.ROTATION, rotation), (core.SCALE, scale)) if on]
    if not sets:
        return "Pick Location, Rotation or Scale"
    tg = core.Target(fobj, owner, bool(fbone))
    cyc = _cycle(tg)
    # ---- which frames, and on each: the frame the leader is taken on and how much of it
    seam = None
    if cyc is not None:
        # (a loop: everything inside it - a blend before the range goes on from the loop's end, one after it from
        # its start; the frame of the leader taken is the one in the loop's turn the range is in)
        c0, c1 = cyc
        P = c1 - c0
        when = {}
        for t in range(c0, c1 + 1):
            best = (0.0, t)
            for k in range(-4, 5):
                w = _ramp(t + k * P, start, end, blend_in, blend_out)
                if w > best[0] + 1e-12:
                    best = (w, t + k * P)
            when[t] = (best[1], influence * best[0])
        touched = [t for t in when if when[t][1] > 0.0]
        if not touched:
            return "The range is outside the loop of its keys (%d - %d)" % (c0, c1)
        if min(touched) > c0 and max(touched) < c1:
            r0, r1 = min(touched) - 1, max(touched) + 1
        else:
            r0, r1, seam = c0, c1, (c0, c1)      # (across the loop's end: all of it, the loop kept closed)
    else:
        # (no blend: it goes over within a frame - its own motion on the frame before and after the range)
        r0, r1 = int(start) - max(blend_in, 1), int(end) + max(blend_out, 1)
        when = {f: (f, influence * _ramp(f, start, end, blend_in, blend_out)) for f in range(r0, r1 + 1)}
    frames = list(range(r0, r1 + 1))
    # ---- the leader's motion, the follower's own parents, on those frames
    got = {}
    off = []
    ident = Matrix.Identity(4)

    def leader_now(dg):
        """The leader's matrix on this frame - the world's, or against its parent (Local); nothing: the world."""
        if lobj is None:
            return ident
        lo = lobj.evaluated_get(dg) if dg is not None else lobj
        L = _world(lo, lbone)
        if L is None or not local_l:
            return L
        Pl = _parent_world(lo, lbone)
        return None if Pl is None else Pl.inverted_safe() @ L

    def follower_parent(fo):
        return _parent_world(fo, fbone) if local_f else ident

    def at_ref(_f, dg):
        fo = fobj.evaluated_get(dg) if dg is not None else fobj
        L, W, Pf = leader_now(dg), _world(fo, fbone), follower_parent(fo)
        if L is not None and W is not None and Pf is not None:
            off.append(L.inverted_safe() @ Pf.inverted_safe() @ W)

    def each(f, dg):
        fo = fobj.evaluated_get(dg) if dg is not None else fobj
        L, Pf = leader_now(dg), follower_parent(fo)
        got[f] = _basis(fo, fbone, Pf @ L @ off[0]) if L is not None and Pf is not None else None
    step_frames(scene, [fobj, lobj], [int(reference)], at_ref)
    if not off:
        return "The bone is gone"
    step_frames(scene, [fobj, lobj], sorted({when[t][0] for t in frames if when[t][1] > 0.0}), each)
    if any(got.get(when[t][0]) is None for t in frames if when[t][1] > 0.0):
        return "The bone is gone"
    # ---- its own channels blended with what it takes from the leader, on every frame
    dl = tg.dloc
    dq = tg.drot.inverted() if tg.drot is not None else None
    ds = tg.dscale
    want = {p: [] for p in sets}
    prev = list(tg.rot.values(r0 - 1))
    for t in frames:
        s, w = when[t]
        o_loc = Vector(tg.loc.values(t))
        o_rot = tg.rot.values(t)
        o_q = core.vals_to_quat(o_rot, tg.rot_mode)
        o_s = Vector(tg.scale.values(t))
        if w > 0.0:
            loc, q, sc = got[s].decompose()
            if dl is not None:
                loc = loc - dl
                q = dq @ q
                sc = Vector([x / d if abs(d) > 1e-12 else x for x, d in zip(sc, ds)])
        else:
            loc, q, sc = o_loc, o_q, o_s
        if core.LOCATION in want:
            want[core.LOCATION].append(list(o_loc.lerp(loc, w)))
        if core.ROTATION in want:
            if o_q.dot(q) < 0.0:
                q = -q
            prev = core.quat_to_vals(o_q.slerp(q, w), tg.rot_mode, prev)
            want[core.ROTATION].append(prev)
        if core.SCALE in want:
            want[core.SCALE].append(list(o_s.lerp(sc, w)))
    if seam is not None:
        # (the loop closed again: its end as far from its start as it was - the same, or a Repeat with Offset's step)
        for p in sets:
            ch = tg.channels(p)
            step = [a - b for a, b in zip(ch.values(seam[1]), ch.values(seam[0]))]
            want[p][-1] = [a + d for a, d in zip(want[p][0], step)]
    # ---- fitted into keys, set by set
    written = {}
    for ptype in sets:
        written[ptype] = _write(fobj, owner, fbone, ptype, r0, r1, frames, want[ptype], tolerance, cyc)
        if seam is not None:
            _close_seam(fobj, owner, fbone, ptype, seam)
    return written


def _close_seam(fobj, owner, fbone, ptype, seam):
    """A loop's last key leaves as its first key comes in: the slope there one (the mean of the two)."""
    tg = core.Target(fobj, owner, bool(fbone))
    c0, c1 = seam
    for fc in tg.channels(ptype).fcurves:
        if fc is None or len(fc.keyframe_points) < 2:
            continue
        kps = fc.keyframe_points
        a = min(kps, key=lambda k: abs(k.co[0] - c0))
        b = min(kps, key=lambda k: abs(k.co[0] - c1))
        if abs(a.co[0] - c0) > 1e-3 or abs(b.co[0] - c1) > 1e-3 or a.interpolation != 'BEZIER':
            continue
        hr = (a.handle_right[0] - a.co[0], a.handle_right[1] - a.co[1])
        hl = (b.co[0] - b.handle_left[0], b.co[1] - b.handle_left[1])
        if hr[0] <= 1e-6 or hl[0] <= 1e-6:
            continue
        m = 0.5 * (hr[1] / hr[0] + hl[1] / hl[0])
        a.handle_left_type = a.handle_right_type = 'FREE'
        b.handle_left_type = b.handle_right_type = 'FREE'
        a.handle_right = (a.co[0] + hr[0], a.co[1] + m * hr[0])
        b.handle_left = (b.co[0] - hl[0], b.co[1] - m * hl[0])
        fc.update()


def _ensure_curves(owner, fbone, ptype, frame, cyc=None):
    """Every channel of the set has a curve with a key (a channel without one gets a key now: its value as it is -
    in a loop: on its first and last frame, looped like the others)."""
    tg = core.Target(owner.id_data if fbone else owner, owner, bool(fbone))
    ch = tg.channels(ptype)
    made = False
    for i, fc in enumerate(ch.fcurves):
        if fc is None or not len(fc.keyframe_points):
            for f in ((frame,) if cyc is None else cyc):
                if fbone:
                    owner.keyframe_insert(ch.prop, index=i, frame=f, group=fbone)
                else:
                    owner.keyframe_insert(ch.prop, index=i, frame=f)
            made = True
    if made and cyc is not None:
        for fc in core.Target(owner.id_data if fbone else owner, owner, bool(fbone)).channels(ptype).fcurves:
            if fc is not None and not len(fc.modifiers):
                fc.modifiers.new('CYCLES')
    return made


def _bezier_split(p0, p1, p2, p3, u):
    """de Casteljau: the two halves of a cubic at u - (left handle of the new point, the point, its right handle,
    the first point's new right handle, the last point's new left handle)."""
    def lerp(a, b):
        return (a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u)
    p01, p12, p23 = lerp(p0, p1), lerp(p1, p2), lerp(p2, p3)
    p012, p123 = lerp(p01, p12), lerp(p12, p23)
    return p012, lerp(p012, p123), p123, p01, p23


def split_at(fc, x):
    """A key put on the curve at frame x without changing its shape: a Bezier piece split in two (de Casteljau),
    a linear / constant one keyed where it is. False where it can't be (an easing piece: Sine, Back...)."""
    kps = fc.keyframe_points
    if not len(kps) or any(abs(k.co[0] - x) < 1e-4 for k in kps) or not kps[0].co[0] < x < kps[-1].co[0]:
        return True     # (a key there already, or outside the keys: the extrapolation goes on from the key)
    i = max(j for j, k in enumerate(kps) if k.co[0] < x)
    k0, k1 = kps[i], kps[i + 1]
    ip = k0.interpolation
    if ip not in {'BEZIER', 'LINEAR', 'CONSTANT'}:
        return False
    # (the other keys' handles as they are: Blender works automatic ones out again - Continuous Acceleration
    # across the keys around, a loop's ends from each other - once the keys next to them change)
    others = [(k.co[0], tuple(k.handle_left), tuple(k.handle_right)) for j, k in enumerate(kps) if j not in (i, i + 1)]
    if ip == 'BEZIER':
        p0, p1, p2, p3 = tuple(k0.co), tuple(k0.handle_right), tuple(k1.handle_left), tuple(k1.co)
        lo, hi = 0.0, 1.0       # (the parameter where the piece is at frame x: its frames only go forward)
        for _i in range(60):
            u = 0.5 * (lo + hi)
            x_u = (1 - u) ** 3 * p0[0] + 3 * (1 - u) ** 2 * u * p1[0] + 3 * (1 - u) * u * u * p2[0] + u ** 3 * p3[0]
            lo, hi = (u, hi) if x_u < x else (lo, u)
        hl, pt, hr, r0h, l1h = _bezier_split(p0, p1, p2, p3, 0.5 * (lo + hi))
        x0, x1 = p0[0], p3[0]
        k0l, k1r = tuple(k0.handle_left), tuple(k1.handle_right)
        # (Blender's insert reshapes the neighbors' handles itself: every handle is set once all three keys are
        # there - from the piece as it was; the keys looked up again, an insert moves them in memory)
        kps.insert(x, pt[1], options={'FAST'})
        for kx, want in ((x0, (k0l, r0h)), (x, (hl, hr)), (x1, (l1h, k1r))):
            k = min(kps, key=lambda kk, kx=kx: abs(kk.co[0] - kx))
            k.handle_left_type = k.handle_right_type = 'FREE'
            if kx == x:
                k.co = pt
            k.handle_left, k.handle_right = want
        kp = min(kps, key=lambda kk: abs(kk.co[0] - x))
    else:
        v = k0.co[1] if ip == 'CONSTANT' else k0.co[1] + (k1.co[1] - k0.co[1]) * (x - k0.co[0]) / (k1.co[0] - k0.co[0])
        kp = kps.insert(x, v, options={'FAST'})
        kp.co = (x, v)
    kp.interpolation = ip
    fc.update()
    _keep_handles(fc, others)
    return True


def _keep_handles(fc, was):
    """These keys' handles back where they were (made Free where Blender moved them); [(frame, left, right)]."""
    for _i in range(3):
        moved = False
        for x, hl, hr in was:
            k = next((k for k in fc.keyframe_points if abs(k.co[0] - x) < 1e-3), None)
            if k is None:
                continue
            if max(abs(a - b) for a, b in zip(tuple(k.handle_left) + tuple(k.handle_right), hl + hr)) > 1e-6:
                k.handle_left_type = k.handle_right_type = 'FREE'
                k.handle_left, k.handle_right = hl, hr
                moved = True
        if not moved:
            return
        fc.update()


def _write(fobj, owner, fbone, ptype, r0, r1, frames, vals, tolerance, cyc=None):
    """Replace the keys of this channel set over [r0, r1] by as few as follow `vals` (on whole frames) within the
    tolerance. The curves are split at r0 and r1 first without changing their shape (split_at): the frames outside
    stay exactly as they were - else the fit goes on to the keys on either side (their outer handles stay)."""
    _ensure_curves(owner, fbone, ptype, r0, cyc)
    tg = core.Target(fobj, owner, bool(fbone))
    ch = tg.channels(ptype)
    cut = all(split_at(fc, x) for fc in ch.fcurves if fc is not None for x in (float(r0), float(r1)))
    tg = core.Target(fobj, owner, bool(fbone))
    ch = tg.channels(ptype)
    kept = _outside(ch, r0, r1)     # (as split: the frames outside are what they must stay)
    times = sorted({round(kp.co[0], 4) for fc in ch.fcurves if fc is not None for kp in fc.keyframe_points})
    a = max([t for t in times if t <= r0 + 1e-6], default=float(r0)) if not cut else float(r0)
    b = min([t for t in times if t >= r1 - 1e-6], default=float(r1)) if not cut else float(r1)
    a, b = min(a, float(r0)), max(b, float(r1))

    def target(t):
        if t < r0 or t > r1:
            return ch.values(t)     # (its own motion: the keys on either side and the curve to them stay)
        return _catmull(vals, frames, t)
    fitter = fit.Fitter(_Measure(ptype, tg), tg, float(a), float(b), target)
    knots = sorted({float(a), float(b), float(r0), float(r1)})
    tol = max(fitter.size * tolerance / 100.0, 1e-5)
    fixed = {k for k in (a, b) if k not in (float(r0), float(r1))}
    knots, slopes = fitter.refine(knots, fixed, tol, max_add=max(60, len(frames)))
    fit.write(fitter, knots, slopes, select=False)
    _restore_outside(core.Target(fobj, owner, bool(fbone)).channels(ptype), kept)
    return len(knots)


def _outside(ch, r0, r1):
    """The keys outside [r0, r1] as they are: {(curve's path, index): [(frame, left handle, right handle)]}."""
    out = {}
    for fc in ch.fcurves:
        if fc is None:
            continue
        out[(fc.data_path, fc.array_index)] = [(k.co[0], tuple(k.handle_left), tuple(k.handle_right))
                                               for k in fc.keyframe_points if not r0 - 1e-3 <= k.co[0] <= r1 + 1e-3]
    return out


def _restore_outside(ch, kept):
    """Their handles back where they were - Blender works the automatic ones out again (with Continuous
    Acceleration across the keys around, in a loop across its ends) once the keys next to them changed: one that
    moved is put back and made Free (the frames outside stay exactly as they were)."""
    for fc in ch.fcurves:
        if fc is not None:
            _keep_handles(fc, kept.get((fc.data_path, fc.array_index), ()))


def selection(context):
    """(the selected bones / objects that follow - parents first, the one they follow or None), like parenting:
    the active one is followed. In Pose Mode the bones - and objects selected along (selected before going into it):
    several bones and one object: the bones follow the object; objects and one bone: the objects follow the bone.
    Only one selected: it is the one that follows (pinned)."""
    if context.mode == 'POSE':
        act = context.active_pose_bone
        active = (act.id_data.name, act.name) if act is not None else None
        bones = [(pb.id_data.name, pb.name) for pb in (context.selected_pose_bones or ())]
        objs = [(o.name, "") for o in (context.selected_objects or ()) if o.mode != 'POSE']
        others = [n for n in bones if n != active]
        if objs and not others:
            followers, active = objs, active            # (objects follow the active bone)
        elif len(objs) == 1:
            followers, active = bones, objs[0]          # (bones follow the object)
        else:
            followers = others + objs
    else:
        ob = context.active_object
        active = (ob.name, "") if ob is not None else None
        followers = [(o.name, "") for o in (context.selected_objects or ()) if (o.name, "") != active]
    if not followers:
        return ([active] if active is not None else []), None
    return sorted(followers, key=_depth), active


def _depth(node):
    """How deep it hangs (its parents first: their new motion is under the children's)."""
    o = bpy.data.objects.get(node[0])
    d = 0
    p = o.parent if o is not None else None
    while p is not None and d < 1000000:
        d += 1000
        p = p.parent
    if node[1] and o is not None and o.pose is not None:
        pb = o.pose.bones.get(node[1])
        d += len(pb.parent_recursive) if pb is not None else 0
    return d


class EMP_OT_match_motion(bpy.types.Operator):
    """The selected bones / objects follow the active one over a range of frames, keeping where they are against it
    on a reference frame - baked into their own keys, blended in before the range and out after it. Nothing to
    follow: they stay where they are (pinned)"""
    bl_idname = "emp.match_motion"
    bl_label = "Match Motion"
    bl_options = {'REGISTER', 'UNDO'}

    leader: StringProperty(name="Follow", description="The object (an armature: with a bone) to follow - the active "
                                                       "one. Empty: the selected ones stay where they are (pinned)")
    leader_bone: StringProperty(name="Bone", description="The bone of that armature to follow")
    from_dialog: BoolProperty(options={'HIDDEN', 'SKIP_SAVE'})
    leader_space: EnumProperty(name="Followed Space", default='GLOBAL', items=[
        ('GLOBAL', "Global", "Its motion in the world"),
        ('LOCAL', "Local", "Its motion against its parent (what its parents do left out)"),
    ])
    space: EnumProperty(name="Follower Space", default='GLOBAL', items=[
        ('GLOBAL', "Global", "Put on them in the world: where it goes, they go"),
        ('LOCAL', "Local", "Put on them against their parent: they move against it as the followed one moves"),
    ])
    start: IntProperty(name="Start", description="The first frame they follow on")
    end: IntProperty(name="End", description="The last frame they follow on")
    reference: IntProperty(name="Reference Frame", description="Where they are against the followed one on this "
                                                               "frame is kept over the range")
    blend_in: IntProperty(name="Blend In", min=0, soft_max=200,
                          description="Frames before the range over which they go over from their own motion (in a "
                                      "loop: from its end)")
    blend_out: IntProperty(name="Blend Out", min=0, soft_max=200,
                           description="Frames after the range over which they go back to their own motion (in a "
                                       "loop: into its start)")
    influence: FloatProperty(name="Influence", min=0.0, max=1.0, default=1.0, subtype='FACTOR',
                             description="How much of the followed motion they take")
    use_location: BoolProperty(name="Location", default=True)
    use_rotation: BoolProperty(name="Rotation", default=True)
    use_scale: BoolProperty(name="Scale", default=True)
    tolerance: FloatProperty(name="Tolerance", subtype='PERCENTAGE', default=0.5, min=0.01, soft_max=5.0,
                             description="How closely the keys follow (more keys where it needs them)")

    @classmethod
    def poll(cls, context):
        return context.mode in {'POSE', 'OBJECT'} and context.active_object is not None

    def _lead(self, context):
        """The one followed: (object, bone name) - None: pinned. From the dialog's fields (an eyedropper) when it was
        opened, else from the operator's own."""
        if self.from_dialog:
            st = state.settings(context.scene)
            ob = st.match_leader if st is not None else None
            if ob is None:
                return None
            return ob, (st.match_leader_bone if ob.type == 'ARMATURE' else "")
        ob = bpy.data.objects.get(self.leader) if self.leader else None
        if ob is None:
            return None
        return ob, (self.leader_bone if ob.type == 'ARMATURE' else "")

    def _followers(self, context):
        """The selected ones that follow (the one they follow left out)."""
        followers, active = selection(context)
        got = self._lead(context)
        lead = (got[0].name, got[1]) if got is not None else None
        if active is not None and active != lead and active not in followers:
            followers = sorted(followers + [active], key=_depth)
        return [n for n in followers if n != lead]

    def invoke(self, context, event):
        followers, active = selection(context)
        if not followers:
            self.report({'WARNING'}, "Select what should follow, the one to follow last (active)")
            return {'CANCELLED'}
        st = state.settings(context.scene)
        ob = bpy.data.objects.get(active[0]) if active is not None else None
        st.match_leader = ob
        st.match_leader_bone = active[1] if ob is not None else ""
        self.from_dialog = True
        sc = context.scene
        sel = []
        for c in state.editable_caches(sc):
            sel += [k.t for k in c.keys if k.sel]
        if len(sel) >= 2:
            self.start, self.end = int(round(min(sel))), int(round(max(sel)))
        else:
            self.start, self.end = (sc.frame_preview_start, sc.frame_preview_end) if sc.use_preview_range else \
                (sc.frame_start, sc.frame_end)
        self.reference = sc.frame_current if self.start <= sc.frame_current <= self.end else self.start
        return context.window_manager.invoke_props_dialog(self, width=360)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        n = len(self._followers(context))
        layout.label(text="%d selected %s" % (n, "follows" if n == 1 else "follow"), icon='CON_CHILDOF')
        st = state.settings(context.scene)
        ob = None
        if self.from_dialog and st is not None:
            layout.prop(st, "match_leader")
            ob = st.match_leader
            if ob is not None and ob.type == 'ARMATURE' and ob.pose is not None:
                layout.prop_search(st, "match_leader_bone", ob.pose, "bones")
        else:
            layout.prop_search(self, "leader", context.scene, "objects")
            ob = bpy.data.objects.get(self.leader)
            if ob is not None and ob.type == 'ARMATURE' and ob.pose is not None:
                layout.prop_search(self, "leader_bone", ob.pose, "bones")
        if ob is None:
            layout.label(text="Nothing to follow: they stay where they are", icon='PINNED')
        else:
            layout.prop(self, "leader_space", expand=True)
        layout.prop(self, "space", expand=True)
        col = layout.column(align=True)
        col.prop(self, "start")
        col.prop(self, "end")
        layout.prop(self, "reference")
        col = layout.column(align=True)
        col.prop(self, "blend_in")
        col.prop(self, "blend_out")
        layout.prop(self, "influence")
        row = layout.row(align=True, heading="Channels")
        row.prop(self, "use_location", toggle=True)
        row.prop(self, "use_rotation", toggle=True)
        row.prop(self, "use_scale", toggle=True)
        layout.prop(self, "tolerance")

    def execute(self, context):
        if not self.from_dialog and self.leader and bpy.data.objects.get(self.leader) is None:
            self.report({'WARNING'}, "No object %r to follow" % self.leader)
            return {'CANCELLED'}
        got = self._lead(context)
        lobj, lbone = got if got is not None else (None, "")
        if lobj is not None:
            self.leader, self.leader_bone = lobj.name, lbone
        followers = self._followers(context)
        if not followers:
            self.report({'WARNING'}, "Select what should follow, the one to follow last (active)")
            return {'CANCELLED'}
        done = 0
        for name, bone in followers:
            res = match(context.scene, (bpy.data.objects.get(name), bone), (lobj, lbone), self.start, self.end,
                        self.reference, self.blend_in, self.blend_out, self.influence, self.use_location,
                        self.use_rotation, self.use_scale, self.tolerance, self.space, self.leader_space)
            if isinstance(res, str):
                self.report({'WARNING'}, res)
                continue
            done += 1
        if not done:
            return {'CANCELLED'}
        for c in state.all_caches():
            c.dirty = True
        state.schedule_rebake(0.2)
        pick.tag_anim_editors()
        return {'FINISHED'}


def menu_entry(self, context):
    """In Object / Pose > Animation and the right-click menus."""
    if context.mode in {'POSE', 'OBJECT'}:
        self.layout.separator()
        self.layout.operator_context = 'INVOKE_DEFAULT'
        self.layout.operator("emp.match_motion", text="Match Motion...", icon='CON_CHILDOF')


classes = (EMP_OT_match_motion,)
