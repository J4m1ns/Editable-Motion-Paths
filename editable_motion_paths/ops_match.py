"""Match Motion: a bone / object follows another one over a range of frames, keeping where it was against it on a
reference frame (as if parented to it there) - baked into its own keys, only as many as the motion needs. Before
the range it blends in from its own motion, after it back out; an influence for all of it.

Over the range its world matrix is the leader's times the offset on the reference frame,
    W(f) = L(f) @ L(ref)^-1 @ W_own(ref),
turned into its own channels through its parents on each frame (Blender's convert_space), blended with its own
channels where it blends in / out (location and scale mixed, rotation turned the shortest way) and fitted with the
Clean Up fitter (fit.py): its keys there are replaced, the keys on either side keep their outer handles."""

import math

import bpy
from bpy.props import BoolProperty, FloatProperty, IntProperty, StringProperty
from mathutils import Vector

from . import core, fit, pick, state

MATCH_SCENE = ".Motion Paths Match"     # (the scene the frames are stepped through in - see core.aux_scene)


def _world(obj, bone):
    if bone:
        pb = obj.pose.bones.get(bone) if obj.pose is not None else None
        return None if pb is None else obj.matrix_world @ pb.matrix
    return obj.matrix_world.copy()


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


def match(scene, follower, leader, start, end, reference, blend_in=0, blend_out=0, influence=1.0,
          location=True, rotation=True, scale=True, tolerance=0.5):
    """follower / leader: (object, bone name or ''). Returns the number of keys written per channel set, or a
    string saying why nothing could be done."""
    fobj, fbone = follower
    lobj, lbone = leader
    if fobj is None or lobj is None:
        return "Pick the bone / object to follow"
    if fobj == lobj and fbone == lbone:
        return "A bone / object can't follow itself"
    owner = fobj.pose.bones.get(fbone) if fbone else fobj
    if owner is None or (lbone and (lobj.pose is None or lobj.pose.bones.get(lbone) is None)):
        return "The bone is gone"
    if core.follows(fobj, fbone, lobj, lbone):
        return "%s moves with %s: it can't follow it" % (lbone or lobj.name, fbone or fobj.name)
    if end < start:
        start, end = end, start
    blend_in, blend_out = max(0, int(blend_in)), max(0, int(blend_out))
    r0, r1 = int(start) - blend_in, int(end) + blend_out
    sets = [p for p, on in ((core.LOCATION, location), (core.ROTATION, rotation), (core.SCALE, scale)) if on]
    if not sets:
        return "Pick Location, Rotation or Scale"
    frames = list(range(r0, r1 + 1))
    # ---- the leader's motion over the range, the follower's own parents on each frame
    got = {}
    off = []

    def at_ref(_f, dg):
        lo = lobj.evaluated_get(dg) if dg is not None else lobj
        fo = fobj.evaluated_get(dg) if dg is not None else fobj
        L, W = _world(lo, lbone), _world(fo, fbone)
        if L is not None and W is not None:
            off.append(L.inverted_safe() @ W)

    def each(f, dg):
        lo = lobj.evaluated_get(dg) if dg is not None else lobj
        fo = fobj.evaluated_get(dg) if dg is not None else fobj
        L = _world(lo, lbone)
        got[f] = _basis(fo, fbone, L @ off[0]) if L is not None else None
    step_frames(scene, [fobj, lobj], [int(reference)], at_ref)
    if not off:
        return "The bone is gone"
    step_frames(scene, [fobj, lobj], frames, each)
    if any(m is None for m in got.values()):
        return "The bone is gone"
    # ---- its own channels blended with what it takes from the leader, on every frame
    tg = core.Target(fobj, owner, bool(fbone))
    dl = tg.dloc
    dq = tg.drot.inverted() if tg.drot is not None else None
    ds = tg.dscale
    want = {p: [] for p in sets}
    prev = list(tg.rot.values(r0 - 1))
    for f in frames:
        w = influence * _ramp(f, start, end, blend_in, blend_out)
        loc, q, s = got[f].decompose()
        if dl is not None:
            loc = loc - dl
            q = dq @ q
            s = Vector([x / d if abs(d) > 1e-12 else x for x, d in zip(s, ds)])
        o_loc = Vector(tg.loc.values(f))
        o_rot = tg.rot.values(f)
        o_q = core.vals_to_quat(o_rot, tg.rot_mode)
        o_s = Vector(tg.scale.values(f))
        if core.LOCATION in want:
            want[core.LOCATION].append(list(o_loc.lerp(loc, w)))
        if core.ROTATION in want:
            if o_q.dot(q) < 0.0:
                q = -q
            prev = core.quat_to_vals(o_q.slerp(q, w), tg.rot_mode, prev)
            want[core.ROTATION].append(prev)
        if core.SCALE in want:
            want[core.SCALE].append(list(o_s.lerp(s, w)))
    # ---- fitted into keys, set by set
    written = {}
    for ptype in sets:
        n = _write(fobj, owner, fbone, ptype, r0, r1, frames, want[ptype], tolerance)
        written[ptype] = n
    return written


def _ensure_curves(owner, fbone, ptype, frame):
    """Every channel of the set has a curve with a key (a channel without one gets a key now: its value as it is)."""
    tg = core.Target(owner.id_data if fbone else owner, owner, bool(fbone))
    ch = tg.channels(ptype)
    made = False
    for i, fc in enumerate(ch.fcurves):
        if fc is None or not len(fc.keyframe_points):
            if fbone:
                owner.keyframe_insert(ch.prop, index=i, frame=frame, group=fbone)
            else:
                owner.keyframe_insert(ch.prop, index=i, frame=frame)
            made = True
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
    return True


def _write(fobj, owner, fbone, ptype, r0, r1, frames, vals, tolerance):
    """Replace the keys of this channel set over [r0, r1] by as few as follow `vals` (on whole frames) within the
    tolerance. The curves are split at r0 and r1 first without changing their shape (split_at): the frames outside
    stay exactly as they were - else the fit goes on to the keys on either side (their outer handles stay)."""
    _ensure_curves(owner, fbone, ptype, r0)
    tg = core.Target(fobj, owner, bool(fbone))
    ch = tg.channels(ptype)
    cut = all(split_at(fc, x) for fc in ch.fcurves if fc is not None for x in (float(r0), float(r1)))
    tg = core.Target(fobj, owner, bool(fbone))
    ch = tg.channels(ptype)
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
    return len(knots)


def _leader_default(context, followers):
    """The active bone / object when something else is selected too (selected ones follow the active one, like
    parenting), else none."""
    ob = context.active_object
    if ob is None:
        return "", ""
    if context.mode == 'POSE' and ob.type == 'ARMATURE':
        pb = context.active_pose_bone
        if pb is not None and (ob.name, pb.name) not in followers:
            return ob.name, pb.name
        return "", ""
    if (ob.name, "") not in followers:
        return ob.name, ""
    return "", ""


class EMP_OT_match_motion(bpy.types.Operator):
    """The selected paths' bones / objects follow another bone / object over a range of frames, keeping where they
    are against it on a reference frame - baked into their own keys, blended in before the range and out after it"""
    bl_idname = "emp.match_motion"
    bl_label = "Match Motion"
    bl_options = {'REGISTER', 'UNDO'}

    leader: StringProperty(name="Follow", description="The object (an armature: with a bone) to follow")
    leader_bone: StringProperty(name="Bone", description="The bone of that armature to follow")
    start: IntProperty(name="Start", description="The first frame it follows on")
    end: IntProperty(name="End", description="The last frame it follows on")
    reference: IntProperty(name="Reference Frame", description="Where it is against the one it follows on this "
                                                               "frame is kept over the range")
    blend_in: IntProperty(name="Blend In", min=0, soft_max=200,
                          description="Frames before the range over which it goes over from its own motion")
    blend_out: IntProperty(name="Blend Out", min=0, soft_max=200,
                           description="Frames after the range over which it goes back to its own motion")
    influence: FloatProperty(name="Influence", min=0.0, max=1.0, default=1.0, subtype='FACTOR',
                             description="How much of the followed motion it takes")
    use_location: BoolProperty(name="Location", default=True)
    use_rotation: BoolProperty(name="Rotation", default=True)
    use_scale: BoolProperty(name="Scale", default=True)
    tolerance: FloatProperty(name="Tolerance", subtype='PERCENTAGE', default=0.5, min=0.01, soft_max=5.0,
                             description="How closely the keys follow (more keys where it needs them)")

    @classmethod
    def poll(cls, context):
        return context.mode in {'POSE', 'OBJECT'}

    def _followers(self, context):
        out = []
        for c in state.editable_caches(context.scene):
            k = (c.obj_name, c.bone_name)
            if k not in out:
                out.append(k)
        return out

    def invoke(self, context, event):
        followers = self._followers(context)
        if not followers:
            self.report({'WARNING'}, "Select a bone / object that has a path")
            return {'CANCELLED'}
        if not self.leader:
            self.leader, self.leader_bone = _leader_default(context, followers)
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
        return context.window_manager.invoke_props_dialog(self, width=340)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop_search(self, "leader", context.scene, "objects")
        ob = bpy.data.objects.get(self.leader)
        if ob is not None and ob.type == 'ARMATURE' and ob.pose is not None:
            layout.prop_search(self, "leader_bone", ob.pose, "bones")
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
        lobj = bpy.data.objects.get(self.leader)
        if lobj is None:
            self.report({'WARNING'}, "Pick the bone / object to follow")
            return {'CANCELLED'}
        lbone = self.leader_bone if lobj.type == 'ARMATURE' else ""
        done = 0
        for name, bone in self._followers(context):
            fobj = bpy.data.objects.get(name)
            got = match(context.scene, (fobj, bone), (lobj, lbone), self.start, self.end, self.reference,
                        self.blend_in, self.blend_out, self.influence, self.use_location, self.use_rotation,
                        self.use_scale, self.tolerance)
            if isinstance(got, str):
                self.report({'WARNING'}, got)
                continue
            done += 1
        if not done:
            return {'CANCELLED'}
        for c in state.all_caches():
            c.dirty = True
        state.schedule_rebake(0.2)
        pick.tag_anim_editors()
        return {'FINISHED'}


classes = (EMP_OT_match_motion,)
