"""Match Motion: the selected bones / objects follow a target over a range of frames, like a Copy constraint baked
into their own keys (only as many as the motion needs). Before the range they blend in from their own motion,
after it back out; an influence for all of it. No path is needed. With nothing to follow they are pinned: they stay
where they were on the reference frame. Run from the sidebar panel, which then keeps it adjustable.

As Copy Transforms: the target's matrix T is its world matrix (World) or its own channels (Local: the transform
against its rest pose and parent, matrix_basis), the follower's own O the same in its space, and what comes out
(world or own channels) is T mixed with O as Blender's Mix does it (formulas.mix_transforms): Replace O(f) = T(f),
Before Original T(f) @ O(f), After Original O(f) @ T(f) (Full; Aligned / Split Channels as Blender has them).
Keep Offset (or pinned): on the reference frame it stays where it is - the target counts from there: Replace
O(f) = T(f) @ T(ref)^-1 @ O(ref) (as if parented there), Before T(f) @ T(ref)^-1 mixed with O(f) (Child Of after
Set Inverse), After O(f) mixed with T(ref)^-1 @ T(f) (Split Channels: the channels' own changes).
In a loop (the follower's curves repeated by a Cycles modifier) all of it stays inside the loop: each frame of the
loop takes the leader on the frame of the turn that falls in the range (a blend before the range goes on from the
loop's end, one after it into its start; Repeat with Offset: its step taken off; Mirror: the turns played back),
only the spans of the loop that change are written - the other keys stay - and the loop stays closed.
A world matrix is turned into its own channels through its parents on each frame (_basis), blended with its own
channels where it blends in / out (location and scale mixed, rotation turned the shortest way) and fitted with the
Clean Up fitter (fit.py): its keys there are replaced, the curves split at the ends first without changing their
shape (also past its keys: on the extrapolation), so the frames outside stay exactly as they were."""

import hashlib
import json
import math
import uuid

import bpy
from mathutils import Matrix, Vector

from . import compat, core, fit, formulas, pick, state

MATCH_SCENE = ".Motion Paths Match"     # (the scene the frames are stepped through in - see core.aux_scene)
_TRANSFORMS = {"location", "rotation_euler", "rotation_quaternion", "rotation_axis_angle", "scale"}
MIXES = ('REPLACE', 'BEFORE_FULL', 'BEFORE', 'BEFORE_SPLIT', 'AFTER_FULL', 'AFTER', 'AFTER_SPLIT')


def _since(T, T_ref, mix):
    """Keep Offset: the target's change since the reference frame, as the mix puts it on (identity on that frame) -
    Before: in front (T @ T_ref^-1), After: behind (T_ref^-1 @ T), Split Channels: each channel's own change."""
    if mix in {'BEFORE_SPLIT', 'AFTER_SPLIT'}:
        l1, r1, s1 = formulas.loc_rot_size(T)
        l0, r0, s0 = formulas.loc_rot_size(T_ref)
        rot = r1 @ r0.inverted_safe() if mix == 'BEFORE_SPLIT' else r0.inverted_safe() @ r1
        size = Vector([a / b if abs(b) > 1e-12 else a for a, b in zip(s1, s0)])
        return formulas.loc_rot_size_matrix(l1 - l0, rot, size)
    if mix.startswith('BEFORE'):
        return T @ T_ref.inverted_safe()
    return T_ref.inverted_safe() @ T


def _world(obj, bone):
    if bone:
        pb = obj.pose.bones.get(bone) if obj.pose is not None else None
        return None if pb is None else obj.matrix_world @ pb.matrix
    return obj.matrix_world.copy()


def _parent_world(obj, bone):
    """What a bone / object is placed against: its parent bone's world matrix (a root bone: its armature's); for an
    object what comes before its own channels - its parent (a bone, a vertex) with the parent inverse, worked out
    from where it is (world @ basis^-1) unless constraints of its own are in that too (then from its parent);
    without a parent the world (identity)."""
    if bone:
        pb = obj.pose.bones.get(bone) if obj.pose is not None else None
        if pb is None:
            return None
        return obj.matrix_world @ pb.parent.matrix if pb.parent is not None else obj.matrix_world.copy()
    if obj.parent is None:
        return Matrix.Identity(4)
    if not any(not c.mute and getattr(c, "enabled", True) and c.influence > 0.0 for c in obj.constraints):
        return obj.matrix_world @ obj.matrix_basis.inverted_safe()
    return core._object_parent_matrix(obj)


def _channels(obj, bone):
    """Its own channels as a matrix (location, rotation, scale - with an object's deltas): a constraint's Local
    Space."""
    if bone:
        pb = obj.pose.bones.get(bone) if obj.pose is not None else None
        return None if pb is None else pb.matrix_basis.copy()
    return obj.matrix_basis.copy()


def _basis(obj, bone, W):
    """The basis (channels before constraints, with an object's deltas) that puts this bone / object at world
    matrix W, its parents as they are now. (Not convert_space's Local for an object: without a parent Blender
    takes the object's own rotation off there, and with a bone or a vertex for a parent it leaves those out.)"""
    if bone:
        return obj.convert_space(pose_bone=obj.pose.bones[bone], matrix=W, from_space='WORLD', to_space='LOCAL')
    return _parent_world(obj, "").inverted_safe() @ W


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


# ----------------------------------------------------------------------------
# Loops (a Cycles modifier on the follower's curves)
# ----------------------------------------------------------------------------

def _kind(mode):
    return 'REPEAT' if mode == 'REPEAT_OFFSET' else mode


def _flat(fc):
    """A curve that holds one value: every key and handle on it (a leftover key or two of a constant channel)."""
    kps = fc.keyframe_points
    v = kps[0].co[1]
    return all(abs(k.co[1] - v) < 1e-9 and abs(k.handle_left[1] - v) < 1e-9 and abs(k.handle_right[1] - v) < 1e-9
               for k in kps)


def _free(tg, ptype):
    """Has this channel set nothing of its own to loop (no curves, a key, one value)?"""
    return all(fc is None or len(fc.keyframe_points) < 2 or _flat(fc) for fc in tg.channels(ptype).fcurves)


def _cycle(tg, sets):
    """The follower's loop, when a Cycles modifier repeats the curves of these channel sets: {"c0", "c1": its first
    / last key, "modes": (before, count, after, count) with Repeat with Offset as Repeat, "raw": one curve's
    cycles_of, "mirror": (before, after) played back and forth, "tie": its last key is the next turn's first,
    "steps": {(path, index): (what a turn adds, on the turns before, on the turns after)}}. None: no loop. A
    string: why it can't be matched in it (its curves loop in different ways, or only some of them). Curves that
    hold one value don't count (they get the loop's keys and modifier - see _loop_keys)."""
    sig = span = raw = None
    steps = {}
    plain = False
    for p in sets:
        for fc in tg.channels(p).fcurves:
            if fc is None or len(fc.keyframe_points) < 2 or _flat(fc):
                continue        # (no key, one, one value: it gets the loop's keys - see _loop_keys)
            m = core.cycles_of(fc)
            if m is None and any(md.type == 'CYCLES' for md in fc.modifiers):
                return "A Cycles modifier together with other modifiers on one curve: its loop can't be kept"
            if not m or (m[0] == 'NONE' and m[2] == 'NONE'):
                plain = True
                continue
            kps = fc.keyframe_points
            s = (_kind(m[0]), m[1] if m[0] != 'NONE' else 0, _kind(m[2]), m[3] if m[2] != 'NONE' else 0)
            a, b = kps[0].co[0], kps[-1].co[0]
            if sig is None:
                sig, span, raw = s, (a, b), m
            elif s != sig or abs(a - span[0]) > 1e-3 or abs(b - span[1]) > 1e-3:
                return "Its curves loop in different ways (their Cycles modifiers or their first / last keys differ)"
            steps[(fc.data_path, fc.array_index)] = (kps[-1].co[1] - kps[0].co[1], m[0] == 'REPEAT_OFFSET',
                                                     m[2] == 'REPEAT_OFFSET')
    if sig is None:
        return None
    if plain:
        return "Some of its curves loop (a Cycles modifier) and some don't: loop all of them or none"
    c0, c1 = span
    if abs(c0 - round(c0)) > 1e-3 or abs(c1 - round(c1)) > 1e-3 or c1 - c0 < 2.0:
        return "Its loop's first and last keys must be on whole frames, two or more apart"
    mirror = (sig[0] == 'MIRROR', sig[2] == 'MIRROR')
    return {"c0": int(round(c0)), "c1": int(round(c1)), "modes": sig, "raw": raw, "mirror": mirror,
            "tie": not any(mirror), "steps": steps}


def _turns(cyc, lo, hi):
    """The loop's turns k (0: its keys, k > 0 after them, k < 0 before) frames lo - hi can fall in - only the ones
    its Cycles modifier plays (a count: that many)."""
    c0, c1 = cyc["c0"], cyc["c1"]
    P = c1 - c0
    mb, cb, ma, ca = cyc["modes"]
    out = []
    for k in range(int(math.floor((lo - c1) / P)) - 1, int(math.ceil((hi - c0) / P)) + 2):
        if k < 0 and (mb == 'NONE' or (cb > 0 and -k > cb)):
            continue
        if k > 0 and (ma == 'NONE' or (ca > 0 and k > ca)):
            continue
        out.append(k)
    return out


def _windows(changed, c0, c1):
    """The spans of the loop written: each run of changed frames with a frame of its own motion on either side
    (the curve split there, the keys outside kept), inside the loop's keys; spans that meet made one."""
    out = []
    for t in changed:
        a, b = max(c0, t - 1), min(c1, t + 1)
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _loop_keys(fobj, owner, fbone, ptype, cyc):
    """Every curve of the set in the loop like the others: keys on its first and last frame (a curve without keys:
    its value as it is; one that holds one value: that value, its keys outside the loop taken off) and the same
    Cycles modifier."""
    _ensure_curves(owner, fbone, ptype, cyc["c0"])
    tg = core.Target(fobj, owner, bool(fbone))
    c0, c1 = cyc["c0"], cyc["c1"]
    for fc in tg.channels(ptype).fcurves:
        if fc is None:
            continue
        kps = fc.keyframe_points
        flat = len(kps) < 2 or _flat(fc)
        if flat:
            v = kps[0].co[1]
            for x in (c0, c1):
                if not any(abs(k.co[0] - x) < 1e-3 for k in kps):
                    k = kps.insert(x, v, options={'FAST'})
                    k.co = (x, v)
            for i in reversed(range(len(kps))):
                if not c0 - 1e-3 <= kps[i].co[0] <= c1 + 1e-3:
                    kps.remove(kps[i], fast=True)
            for k in kps:
                k.handle_left_type = k.handle_right_type = 'FREE'
                k.handle_left = (k.handle_left[0], v)
                k.handle_right = (k.handle_right[0], v)
        m = next((m for m in fc.modifiers if m.type == 'CYCLES'), None)
        if m is None or flat:
            if m is None:
                m = fc.modifiers.new('CYCLES')
            m.mode_before, m.cycles_before, m.mode_after, m.cycles_after = cyc["raw"]
        fc.update()


def _from_nla(fobj):
    """Why its keys can't be matched: it plays its animation from NLA strips only (no action of its own to key) -
    else ''."""
    ad = fobj.animation_data
    if ad is None or ad.action is not None or getattr(ad, "use_tweak_mode", False):
        return ""
    if any(not t.mute and any(not s.mute for s in t.strips) for t in ad.nla_tracks):
        return ("%s plays its animation from NLA strips: edit the strip's action (Tab: Tweak Mode) to match it" %
                fobj.name)
    return ""


def _overridden(fobj, owner, fbone):
    """Do its own constraints or drivers decide where it ends up, whatever its keys say?"""
    for c in getattr(owner, "constraints", ()):
        if not c.mute and getattr(c, "enabled", True) and c.influence > 0.0:
            return True
    ad = fobj.animation_data
    if ad is not None:
        prefix = 'pose.bones["%s"].' % bpy.utils.escape_identifier(fbone) if fbone else ""
        for d in ad.drivers:
            dp = d.data_path
            if dp.startswith(prefix) and dp[len(prefix):] in _TRANSFORMS and not d.mute:
                return True
    return False


def _verify(scene, fobj, fbone, lobj, D, frames):
    """(how far off, how much turned off) it is on these frames from where it should be (D: world matrices)."""
    worst = [0.0, 0.0]

    def at(f, dg):
        fo = fobj.evaluated_get(dg) if dg is not None else fobj
        W = _world(fo, fbone)
        if W is None:
            return
        d = D[f]
        worst[0] = max(worst[0], (W.translation - d.translation).length)
        worst[1] = max(worst[1], W.to_quaternion().rotation_difference(d.to_quaternion()).angle)
    step_frames(scene, [fobj, lobj], frames, at)
    return worst


# ----------------------------------------------------------------------------
# Match
# ----------------------------------------------------------------------------

def _plan(cyc, start, end, blend_in, blend_out, influence, notes):
    """Which frames of a channel set are written: {"cyc": its loop (or None), "when": {frame of its keys: (frame the
    leader is taken on, how much of it, the loop's turn it is seen in)}, "windows": the spans written} - or a
    string: why nothing can be done. What doesn't come out exactly goes into notes."""
    if cyc is None:
        # (no blend: it goes over within a frame - its own motion on the frame before and after the range)
        r0, r1 = int(start) - max(blend_in, 1), int(end) + max(blend_out, 1)
        return {"cyc": None, "windows": [(r0, r1)],
                "when": {f: (f, influence * _ramp(f, start, end, blend_in, blend_out), 0) for f in range(r0, r1 + 1)}}
    c0, c1 = cyc["c0"], cyc["c1"]
    P = c1 - c0
    ks = _turns(cyc, start - blend_in, end + blend_out)
    when = {}
    for t in range(c0, (c1 - 1 if cyc["tie"] else c1) + 1):
        best = (0.0, t, 0)
        for k in ks:
            s = c0 + c1 - t + k * P if k % 2 and cyc["mirror"][k > 0] else t + k * P
            w = _ramp(s, start, end, blend_in, blend_out)
            if w > best[0] + 1e-12:
                best = (w, s, k)
        when[t] = (best[1], influence * best[0], best[2])
    if cyc["tie"]:
        when[c1] = when[c0]     # (its last key is the first one of the next turn)
    changed = [t for t in sorted(when) if when[t][1] > 0.0]
    if not changed:
        return "The range is outside the turns of its loop (keys %d - %d)" % (c0, c1)
    said = []
    if (end + blend_out) - (start - blend_in) > P:
        said.append("the range is longer than its loop (%d frames): it follows one turn of it" % P)
    past = [s for s in range(int(start), int(end) + 1) if not any(c0 + k * P <= s <= c1 + k * P for k in ks)]
    if past:
        said.append("frames %d - %d are past its loop's last turn: they don't follow" % (past[0], past[-1]))
    notes += [n for n in said if n not in notes]
    return {"cyc": cyc, "when": when, "windows": _windows(changed, c0, c1)}


def match(scene, follower, leader, start, end, reference, blend_in=0, blend_out=0, influence=1.0,
          location=True, rotation=True, scale=True, tolerance=0.5, space='GLOBAL', leader_space='GLOBAL',
          use_offset=False, mix='REPLACE'):
    """follower / leader: (object, bone name or ''); leader (None, ''): pinned - it stays where it was. Like Copy
    Transforms: leader_space (the target's) / space (the owner's) 'GLOBAL' = world, 'LOCAL' = its own channels (the
    transform against its rest pose and parent, a constraint's Local Space); mix: how the target's transform and its
    own come together (MIXES, Blender's Mix); use_offset (Keep Offset): on the reference frame it stays where it is
    and the target counts from there (Replace: as if parented there). Returns {channel set: keys written, "notes":
    [what didn't come out exactly, in words]}, or a string saying why nothing could be done."""
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
    if influence <= 0.0:
        return "Influence 0: nothing would change"
    local_f, local_l = space == 'LOCAL', leader_space == 'LOCAL'
    if mix not in MIXES:
        return "Unknown mix %r" % (mix,)
    keep = use_offset or lobj is None
    if lobj is None:
        mix = 'REPLACE'
    if end < start:
        start, end = end, start
    blend_in, blend_out = max(0, int(blend_in)), max(0, int(blend_out))
    sets = [p for p, on in ((core.LOCATION, location), (core.ROTATION, rotation), (core.SCALE, scale)) if on]
    if not sets:
        return "Pick Location, Rotation or Scale"
    tg = core.Target(fobj, owner, bool(fbone))
    locked, why = core._anim_lock(tg)
    if locked:
        return why
    why = _from_nla(fobj)
    if why:
        return why
    notes = []
    # ---- which frames, set by set (each may loop on its own): on each, the frame the leader is taken on, how much
    # of it, the loop's turn it is seen in
    loops = {}
    for p in sets:
        loops[p] = _cycle(tg, [p])
        if isinstance(loops[p], str):
            return loops[p]
    shared = next((c for c in loops.values() if c is not None), None)
    plans = {}
    for p in sets:
        cyc = loops[p]
        if cyc is None and shared is not None and _free(tg, p):
            cyc = shared        # (nothing of its own to loop - no curves, one value: it loops with the others)
        plans[p] = _plan(cyc, start, end, blend_in, blend_out, influence, notes)
        if isinstance(plans[p], str):
            return plans[p]
    # ---- the target's matrix in its space, the owner's in its own, on those frames
    got, D, off = {}, {}, []
    ident = Matrix.Identity(4)

    def leader_now(dg):
        """The target's matrix on this frame - in the world, or its own channels (Local); nothing: identity."""
        if lobj is None:
            return ident
        lo = lobj.evaluated_get(dg) if dg is not None else lobj
        return _channels(lo, lbone) if local_l else _world(lo, lbone)

    def own_now(fo):
        """Its own matrix on this frame - in the world, or its own channels (Local)."""
        return _channels(fo, fbone) if local_f else _world(fo, fbone)

    def at_ref(_f, dg):
        fo = fobj.evaluated_get(dg) if dg is not None else fobj
        T, O = leader_now(dg), own_now(fo)
        if T is not None and O is not None:
            off.append((T, O))

    def each(f, dg):
        fo = fobj.evaluated_get(dg) if dg is not None else fobj
        T = leader_now(dg)
        O = own_now(fo) if mix != 'REPLACE' else ident
        if T is None or O is None:
            got[f] = None
            return
        if mix == 'REPLACE':
            R = T @ off[0][0].inverted_safe() @ off[0][1] if keep else T
        else:
            R = formulas.mix_transforms(_since(T, off[0][0], mix) if keep else T, O, mix)
        if local_f:
            got[f] = R          # (its own channels: written as they are)
        else:
            D[f] = R
            got[f] = _basis(fo, fbone, R)
    if keep:
        step_frames(scene, [fobj, lobj], [int(reference)], at_ref)
    else:
        off.append((ident, ident))      # (the target's transform taken as it is: no offset kept)
    if not off:
        return "The bone is gone"
    need = sorted({w[0] for pl in plans.values() for w in pl["when"].values() if w[1] > 0.0})
    step_frames(scene, [fobj, lobj], need, each)
    if any(got.get(s) is None for s in need):
        return "The bone is gone"
    # ---- its own channels blended with what it takes from the leader, on every frame written - set by set
    dl = tg.dloc
    dq = tg.drot.inverted() if tg.drot is not None else None
    ds = tg.dscale
    quat = tg.rot_mode in {'QUATERNION', 'AXIS_ANGLE'}
    written = {}
    for p in sets:
        pl = plans[p]
        cyc, when, windows = pl["cyc"], pl["when"], pl["windows"]
        steps = cyc["steps"] if cyc is not None else {}
        ch = tg.channels(p)

        def turned(vals, k, sign, ch=ch, steps=steps):
            """Values of the loop's own keys as seen k turns away (sign 1) and back (-1): a Repeat with Offset adds
            its step every turn."""
            out = list(vals)
            if not k or not steps:
                return out
            for i, fc in enumerate(ch.fcurves):
                st = steps.get((fc.data_path, fc.array_index)) if fc is not None else None
                if st is not None and (st[2] if k > 0 else st[1]):
                    out[i] += sign * k * st[0]
            return out
        want = {}
        for x0, x1 in windows:
            prev = list(tg.rot.values(x0 - 1))
            for t in range(x0, x1 + 1):
                s, w, k = when.get(t, (t, 0.0, 0))
                own = ch.values(t)
                if w <= 0.0:
                    want[t] = prev = list(own)
                    continue
                loc, q, sc = got[s].decompose()
                if dl is not None:
                    loc = loc - dl
                    q = dq @ q
                    sc = Vector([x / d if abs(d) > 1e-12 else x for x, d in zip(sc, ds)])
                if p == core.LOCATION:
                    want[t] = list(Vector(own).lerp(Vector(turned(loc, k, -1)), w))
                elif p == core.SCALE:
                    want[t] = list(Vector(own).lerp(Vector(turned(sc, k, -1)), w))
                else:
                    if k and steps:
                        rv = core.quat_to_vals(q, tg.rot_mode, turned(own, k, 1))
                        q = core.vals_to_quat(turned(rv, k, -1), tg.rot_mode)
                    o_q = core.vals_to_quat(own, tg.rot_mode)
                    if o_q.dot(q) < 0.0:
                        q = -q
                    want[t] = prev = core.quat_to_vals(o_q.slerp(q, w), tg.rot_mode, prev)
        tie = cyc is not None and cyc["tie"] and when[cyc["c0"]][1] > 0.0
        if tie:
            # (the loop closed again: its last key as far from its first as it was - the same, a Repeat with
            # Offset's step, a turn of the rotation)
            c0, c1 = cyc["c0"], cyc["c1"]
            a0, a1 = ch.values(c0), ch.values(c1)
            if p == core.ROTATION and quat:
                d = core.vals_to_quat(a0, tg.rot_mode).inverted() @ core.vals_to_quat(a1, tg.rot_mode)
                q1 = core.vals_to_quat(want[c0], tg.rot_mode) @ d
                want[c1] = core.quat_to_vals(q1, tg.rot_mode, want.get(c1 - 1, a1))
            else:
                want[c1] = [v + (b - a) for v, a, b in zip(want[c0], a0, a1)]
        # ---- fitted into keys, span by span
        if cyc is not None:
            _loop_keys(fobj, owner, fbone, p, cyc)
        n = 0
        for x0, x1 in windows:
            frames = list(range(x0, x1 + 1))
            n += _write(fobj, owner, fbone, p, x0, x1, frames, [want[t] for t in frames], tolerance)
        written[p] = n
        if tie and when[cyc["c0"] + 1][1] > 0.0 and when[cyc["c1"] - 1][1] > 0.0:
            _close_seam(fobj, owner, fbone, p, (cyc["c0"], cyc["c1"]))
    # ---- what its own constraints / drivers make of it (they come after its keys)
    if _overridden(fobj, owner, fbone) and influence >= 1.0 - 1e-9 and not notes:
        full = [f for f in range(int(start), int(end) + 1) if f in D]
        if full:
            if len(full) > 12:
                full = sorted({full[int(round(i * (len(full) - 1) / 11.0))] for i in range(12)})
            far, turn = _verify(scene, fobj, fbone, lobj, D, full)
            pts = [D[f].translation for f in full]
            size = max((a - b).length for a in pts for b in pts) if len(pts) > 1 else 0.0
            if far > max(1e-3, 0.01 * size) or turn > math.radians(0.5):
                notes.append("its constraints / drivers keep it up to %.3f off (%.1f deg)" % (far, math.degrees(turn)))
    written["notes"] = notes
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


def _ensure_curves(owner, fbone, ptype, frame):
    """Every channel of the set has a curve with a key (a channel without one gets a key now: its value as it
    is)."""
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


def _extrapolated(fc, after):
    """(the end key's index, the slope Blender carries on past it) - its extrapolation as Blender works it out:
    level for Constant (or a Constant end key), a Linear end key: on to its neighbor's line, else along its
    handle."""
    kps = fc.keyframe_points
    i = len(kps) - 1 if after else 0
    e = kps[i]
    if e.interpolation == 'CONSTANT' or fc.extrapolation == 'CONSTANT':
        return i, 0.0
    if e.interpolation == 'LINEAR':
        if len(kps) < 2:
            return i, 0.0
        n = kps[i - 1] if after else kps[1]
        dx = n.co[0] - e.co[0]
        return i, ((n.co[1] - e.co[1]) / dx if abs(dx) > 1e-9 else 0.0)
    h = e.handle_right if after else e.handle_left
    dx = e.co[0] - h[0]
    return i, ((e.co[1] - h[1]) / dx if abs(dx) > 1e-9 else 0.0)


def _extend_to(fc, x):
    """A key at frame x past the curve's keys, on its extrapolation, and the piece from the end key to it that same
    level / line: the frames between stay as they were (and past it: it carries the same slope on)."""
    kps = fc.keyframe_points
    after = x > kps[-1].co[0]
    i, m = _extrapolated(fc, after)
    e = kps[i]
    ex, ey = e.co[0], e.co[1]
    y = ey + m * (x - ex)
    d = abs(x - ex) / 3.0
    others = [(k.co[0], tuple(k.handle_left), tuple(k.handle_right)) for j, k in enumerate(kps) if j != i]
    e_inner = tuple(e.handle_left) if after else tuple(e.handle_right)
    kps.insert(x, y, options={'FAST'})
    ek = min(kps, key=lambda k: abs(k.co[0] - ex))
    nk = min(kps, key=lambda k: abs(k.co[0] - x))
    for k in (ek, nk):
        k.handle_left_type = k.handle_right_type = 'FREE'
    nk.co = (x, y)
    nk.handle_left, nk.handle_right = (x - d, y - m * d), (x + d, y + m * d)
    if after:
        # (the piece after the old end key is its own: Bezier along the line; what comes before it as it was)
        ek.interpolation = 'BEZIER'
        ek.handle_left, ek.handle_right = e_inner, (ex + d, ey + m * d)
    else:
        ek.handle_left, ek.handle_right = (ex - d, ey - m * d), e_inner
    nk.interpolation = 'BEZIER'
    fc.update()
    _keep_handles(fc, others)
    return True


def split_at(fc, x):
    """A key put on the curve at frame x without changing its shape: a Bezier piece split in two (de Casteljau),
    a linear / constant one keyed where it is, past the keys: on the extrapolation (_extend_to). False where it
    can't be (an easing piece: Sine, Back...)."""
    kps = fc.keyframe_points
    if not len(kps) or any(abs(k.co[0] - x) < 1e-4 for k in kps):
        return True
    if not kps[0].co[0] < x < kps[-1].co[0]:
        return _extend_to(fc, x)
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


def _icon(ob):
    """The object's icon as the path list draws it (its type)."""
    if ob is None:
        return 'OBJECT_DATA'
    from .ui import _object_icon
    return _object_icon(ob)


def frames_of(scene, m):
    """(start, end) of the settings: the scene's range (the preview range when it is on) or Start - End."""
    if m.frames == 'SCENE':
        if scene.use_preview_range:
            return scene.frame_preview_start, scene.frame_preview_end
        return scene.frame_start, scene.frame_end
    return tuple(sorted((m.start, m.end)))


def target_of(scene):
    """(object, bone name) of the panel's Target - (None, ""): nothing to follow (pinned)."""
    st = state.settings(scene)
    ob = st.match_leader if st is not None else None
    if ob is None:
        return None, ""
    bone = st.match_leader_bone if ob.type == 'ARMATURE' else ""
    if bone and (ob.pose is None or ob.pose.bones.get(bone) is None):
        bone = ""
    return ob, bone


def followers_of(context, lobj, lbone):
    """Who follows: everything selected - the bones (in Pose Mode, and objects selected along), else the objects -
    parents first; the target itself left out, and its armature when it is a bone of it (an armature can't follow a
    bone of its own)."""
    if context.mode == 'POSE':
        sel = [(pb.id_data.name, pb.name) for pb in (context.selected_pose_bones or ())]
        sel += [(o.name, "") for o in (context.selected_objects or ()) if o.mode != 'POSE']
    else:
        sel = [(o.name, "") for o in (context.selected_objects or ())]
    if lobj is not None:
        sel = [n for n in sel if n != (lobj.name, lbone) and not (lbone and n == (lobj.name, ""))]
    return sorted(sel, key=_depth)


def _run(scene, followers, lobj, lbone, m):
    """match() for each follower with these settings: (the ones done, keys written, notes)."""
    start, end = frames_of(scene, m)
    done, keys, notes = [], 0, []
    for name, bone in followers:
        try:
            res = match(scene, (bpy.data.objects.get(name), bone), (lobj, lbone), start, end, m.reference,
                        m.blend_in, m.blend_out, m.influence, m.use_location, m.use_rotation, m.use_scale,
                        m.tolerance, m.space, m.leader_space, m.use_offset, m.mix)
        except RuntimeError as e:       # (keys Blender won't take: linked data...)
            res = str(e).strip().splitlines()[-1] if str(e).strip() else "Blender refused the keys"
        label = bone or name
        if isinstance(res, str):
            notes.append("%s: %s" % (label, res))
            continue
        done.append(label)
        keys += sum(v for k, v in res.items() if k != "notes")
        notes += ["%s: %s" % (label, t) for t in res.get("notes", ())]
    return done, keys, notes


def _summary(scene, done, keys, lobj, lbone, m):
    who = ", ".join(done) if len(done) <= 2 else "%d bones / objects" % len(done)
    what = ("follow%s %s" % ("s" if len(done) == 1 else "", lbone or lobj.name)) if lobj is not None else "pinned"
    start, end = frames_of(scene, m)
    return "%s %s on frames %d - %d (%d keys)" % (who, what, start, end, keys)


def _refresh(objs=()):
    """After their keys changed: Blender evaluates them again (the pose on this frame), paths and editors redrawn."""
    for ob in objs:
        if ob is not None:
            ob.update_tag(refresh={'OBJECT', 'DATA', 'TIME'})
    for c in state.all_caches():
        c.dirty = True
    state.schedule_rebake(0.2)
    pick.tag_anim_editors()
    state.tag_redraw()


# ----------------------------------------------------------------------------
# The one just run, kept adjustable (the sidebar panel stays while scrubbing / playing)
# ----------------------------------------------------------------------------

_SESSIONS = {}          # session id -> {follower key: its own curves and values before it ran} (memory only)
_applying = [False]     # (settings written by the code itself: no run again)
_PROPS = ("location", "rotation_quaternion", "rotation_euler", "rotation_axis_angle", "scale")


def _key(node):
    return "\x1f".join(node)


def _unkey(s):
    a = s.split("\x1f")
    return a[0], (a[1] if len(a) > 1 else "")


def _followers_of(m):
    return [_unkey(s) for s in m.followers.split("\x1e") if s]


def _owner(obj, bone):
    return obj.pose.bones.get(bone) if bone else obj


def _own_curves(obj, bone):
    """[(data path, index, F-Curve or None)] of its location / rotation / scale channels."""
    owner = _owner(obj, bone)
    if owner is None:
        return []
    tg = core.Target(obj, owner, bool(bone))
    return [(ch.path, i, fc) for ch in (tg.loc, tg.rot, tg.scale) for i, fc in enumerate(ch.fcurves)]


def _snapshot(obj, bone):
    """Its own curves as they are - keys, handles, extrapolation, a Cycles modifier - and its channels' values (what
    an unkeyed channel goes back to), to put back."""
    ad = obj.animation_data
    owner = _owner(obj, bone)
    curves = []
    for path, i, fc in _own_curves(obj, bone):
        if fc is None:
            curves.append((path, i, None))
            continue
        cyc = next((m for m in fc.modifiers if m.type == 'CYCLES'), None)
        curves.append((path, i, {
            "keys": [(tuple(k.co), tuple(k.handle_left), tuple(k.handle_right), k.handle_left_type,
                      k.handle_right_type, k.interpolation, k.easing, k.back, k.amplitude, k.period, k.type)
                     for k in fc.keyframe_points],
            "ext": fc.extrapolation,
            "cycles": None if cyc is None else (cyc.mode_before, cyc.cycles_before, cyc.mode_after,
                                                cyc.cycles_after)}))
    values = {a: tuple(getattr(owner, a)) for a in _PROPS} if owner is not None else {}
    return {"action": ad is not None and ad.action is not None, "curves": curves, "values": values}


def _restore(obj, bone, snap):
    """Its own curves back as the snapshot has them (curves Match Motion made removed) and the channels without
    keys back to their values - Blender leaves the last evaluated value in a channel whose curve goes."""
    coll = compat.get_fcurves(obj)
    if coll is not None:
        for path, i, d in snap["curves"]:
            fc = coll.find(path, index=i)
            if d is None:
                if fc is not None:
                    coll.remove(fc)
                continue
            if fc is None:
                continue
            kps = fc.keyframe_points
            kps.clear()
            kps.add(len(d["keys"]))
            for kp, k in zip(kps, d["keys"]):
                kp.interpolation = k[5]
                kp.handle_left_type, kp.handle_right_type = k[3], k[4]
                kp.co, kp.handle_left, kp.handle_right = k[0], k[1], k[2]
                kp.easing = k[6]
                kp.back, kp.amplitude, kp.period = k[7], k[8], k[9]
                kp.type = k[10]
            fc.extrapolation = d["ext"]
            for m in [m for m in fc.modifiers if m.type == 'CYCLES']:
                if d["cycles"] is None:
                    fc.modifiers.remove(m)
            if d["cycles"] is not None:
                m = next((m for m in fc.modifiers if m.type == 'CYCLES'), None) or fc.modifiers.new('CYCLES')
                if m is not None:
                    m.mode_before, m.cycles_before, m.mode_after, m.cycles_after = d["cycles"]
            fc.update()
    owner = _owner(obj, bone)
    if owner is not None:
        keyed = {(p.rsplit(".", 1)[-1], i) for p, i, d in snap["curves"] if d is not None}
        for attr, vals in snap.get("values", {}).items():
            cur = getattr(owner, attr)
            for i, v in enumerate(vals):
                if (attr, i) not in keyed:
                    cur[i] = v


def _sig(obj, bone):
    """A fingerprint of its own curves (keys and handles): were they changed since Match Motion wrote them?"""
    out = []
    for _path, _i, fc in _own_curves(obj, bone):
        if fc is None:
            out.append("-")
            continue
        n = len(fc.keyframe_points)
        vals = []
        for attr in ("co", "handle_left", "handle_right"):
            a = [0.0] * (2 * n)
            fc.keyframe_points.foreach_get(attr, a)
            vals += a
        out.append("%d:%s" % (n, ",".join("%.5f" % v for v in vals)))
    return hashlib.md5("|".join(out).encode()).hexdigest()


def _written(followers):
    return json.dumps({_key(n): _sig(bpy.data.objects[n[0]], n[1]) for n in followers
                       if bpy.data.objects.get(n[0]) is not None})


def _changed_since(m, followers):
    """Were their keys changed since Match Motion last wrote them (by hand, another tool)?"""
    before = json.loads(m.written or "{}")
    return any(bpy.data.objects.get(n[0]) is None or before.get(_key(n)) != _sig(bpy.data.objects[n[0]], n[1])
               for n in followers)


def _put_back(followers, snaps):
    """Their keys and values back as they were before Match Motion."""
    objs = []
    for n in followers:
        ob = bpy.data.objects.get(n[0])
        snap = snaps.get(_key(n))
        if ob is None or snap is None:
            continue
        _restore(ob, n[1], snap)
        objs.append(ob)
    return objs


def session_changed(context):
    """A setting changed while Match Motion is being adjusted: their keys back as they were before it, and it runs
    again."""
    if _applying[0]:
        return
    st = state.settings(context.scene)
    m = st.match if st is not None else None
    if m is None or not m.active:
        return
    snaps = _SESSIONS.get(m.sid)
    if snaps is None:
        state.flash_message("Match Motion can't be adjusted any more (the file was opened again)", 4.0)
        return
    followers = _followers_of(m)
    if _changed_since(m, followers):
        m.active = False
        state.flash_message("Their keys were changed since: Match Motion is done, the keys stay as they are", 5.0)
        return
    lobj, lbone = target_of(context.scene)
    _applying[0] = True
    try:
        objs = _put_back(followers, snaps)
        done, keys, notes = _run(context.scene, followers, lobj, lbone, m)
        m.written = _written(followers)
    finally:
        _applying[0] = False
    _refresh(objs)
    msg = _summary(context.scene, done, keys, lobj, lbone, m) if done else "Nothing follows"
    state.flash_message(msg + (". " + "; ".join(notes) if notes else ""), 4.0 if notes else 2.0)


def draw_panel(layout, context):
    """The sidebar's Match Motion panel: the target and the settings, like a Copy constraint's; Match for the
    selected ones - then, while it is being adjusted, a change runs it again; Cancel / Done."""
    sc = context.scene
    st = state.settings(sc)
    m = st.match
    layout.use_property_split = True
    layout.use_property_decorate = False     # (no keyframe dots: these settings aren't animation)
    if m.active:
        followers = _followers_of(m)
        if m.sid not in _SESSIONS:
            layout.label(text="Opened again: it can't be adjusted any more", icon='INFO')
            layout.operator("emp.match_finish", text="Done", icon='CHECKMARK')
            return
        names = ", ".join(b or o for o, b in followers[:2]) + (" and %d more" % (len(followers) - 2)
                                                               if len(followers) > 2 else "")
        box = layout.box()
        box.label(text="Adjusting: %s" % names, icon='CON_CHILDOF')
    ob = st.match_leader
    layout.prop(st, "match_leader", icon=_icon(ob))
    if ob is not None and ob.type == 'ARMATURE' and ob.pose is not None:
        layout.prop_search(st, "match_leader_bone", ob.pose, "bones", icon='BONE_DATA')
    if ob is not None:
        layout.prop(m, "mix")
        layout.prop(m, "leader_space", expand=True)
    layout.prop(m, "space", expand=True)
    if ob is None:
        layout.label(text="No target: they stay where they are on the reference frame", icon='PINNED')
    else:
        layout.prop(m, "use_offset")
    if ob is None or m.use_offset:
        row = layout.row(align=True)
        row.prop(m, "reference")
        row.operator("emp.match_reference", text="", icon='TIME')
    row = layout.row(align=True)
    row.prop(m, "frames", expand=True)
    row.operator("emp.match_keys_range", text="", icon='KEYFRAME_HLT')
    if m.frames == 'CUSTOM':
        col = layout.column(align=True)
        col.prop(m, "start")
        col.prop(m, "end")
    col = layout.column(align=True)
    col.prop(m, "blend_in")
    col.prop(m, "blend_out")
    layout.prop(m, "influence")
    row = layout.row(align=True, heading="Channels")
    row.prop(m, "use_location", toggle=True)
    row.prop(m, "use_rotation", toggle=True)
    row.prop(m, "use_scale", toggle=True)
    layout.prop(m, "tolerance")
    layout.separator()
    if m.active:
        row = layout.row(align=True)
        row.scale_y = 1.3
        row.operator("emp.match_cancel", text="Cancel", icon='X')
        row.operator("emp.match_finish", text="Done", icon='CHECKMARK')
        return
    n = len(followers_of(context, *target_of(sc))) if context.mode in {'POSE', 'OBJECT'} else 0
    col = layout.column()
    col.scale_y = 1.3
    col.enabled = n > 0
    col.operator("emp.match_motion", text=("Match %d Selected" % n) if n else "Select What Should Follow",
                 icon='CON_CHILDOF')


class EMP_OT_match_motion(bpy.types.Operator):
    """The selected bones / objects follow the target over the frames, like Copy Transforms (its transform mixed with
    their own as Mix says; Keep Offset: from where they are on the reference frame), baked into their own keys,
    blended in before and out after. No target: they stay where they are (pinned). Then the panel keeps it
    adjustable while you scrub and play"""
    bl_idname = "emp.match_motion"
    bl_label = "Match Motion"
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode in {'POSE', 'OBJECT'}

    def execute(self, context):
        sc = context.scene
        m = state.settings(sc).match
        lobj, lbone = target_of(sc)
        followers = followers_of(context, lobj, lbone)
        if not followers:
            self.report({'WARNING'}, "Select what should follow (the target is picked in the panel)")
            return {'CANCELLED'}
        # (their own curves and values as they are: a change in the panel runs it again from them, Cancel puts
        # them back)
        sid = uuid.uuid4().hex
        _SESSIONS[sid] = {_key(n): _snapshot(bpy.data.objects[n[0]], n[1]) for n in followers
                          if bpy.data.objects.get(n[0]) is not None}
        while len(_SESSIONS) > 8:
            _SESSIONS.pop(next(iter(_SESSIONS)))
        done, keys, notes = _run(sc, followers, lobj, lbone, m)
        if not done:
            _SESSIONS.pop(sid, None)
            self.report({'WARNING'}, "; ".join(notes) or "Nothing could follow")
            return {'CANCELLED'}
        _applying[0] = True
        try:
            m.sid = sid
            m.followers = "\x1e".join(_key(n) for n in followers)
            m.written = _written(followers)
            m.active = True
        finally:
            _applying[0] = False
        _refresh([bpy.data.objects.get(n[0]) for n in followers])
        msg = _summary(sc, done, keys, lobj, lbone, m)
        if notes:
            self.report({'WARNING'}, msg + ". " + "; ".join(notes))
        else:
            self.report({'INFO'}, msg)
        return {'FINISHED'}


class EMP_OT_match_finish(bpy.types.Operator):
    """Done: the keys stay as they are"""
    bl_idname = "emp.match_finish"
    bl_label = "Done"
    bl_options = {'UNDO'}

    def execute(self, context):
        state.settings(context.scene).match.active = False
        state.tag_redraw()
        return {'FINISHED'}


class EMP_OT_match_cancel(bpy.types.Operator):
    """Their own keys and values back exactly as they were before Match Motion"""
    bl_idname = "emp.match_cancel"
    bl_label = "Cancel Match Motion"
    bl_options = {'UNDO'}

    def execute(self, context):
        m = state.settings(context.scene).match
        snaps = _SESSIONS.get(m.sid)
        followers = _followers_of(m)
        m.active = False
        if snaps is None:
            self.report({'WARNING'}, "It can't be put back from here any more (the file was opened again): Ctrl+Z")
            return {'FINISHED'}
        if _changed_since(m, followers):
            self.report({'WARNING'}, "Their keys were changed since: they stay as they are (Ctrl+Z steps back)")
            return {'FINISHED'}
        objs = _put_back(followers, snaps)
        for n in followers:
            ob = bpy.data.objects.get(n[0])
            snap = snaps.get(_key(n))
            ad = ob.animation_data if ob is not None else None
            if snap is not None and not snap["action"] and ad is not None and ad.action is not None and \
                    not len(list(compat.get_fcurves(ob) or ())):
                ad.action = None        # (it had no animation: the action Match Motion made goes)
        _refresh(objs)
        return {'FINISHED'}


class EMP_OT_match_reference(bpy.types.Operator):
    """The reference frame: the current one"""
    bl_idname = "emp.match_reference"
    bl_label = "Reference: This Frame"
    bl_options = {'UNDO', 'INTERNAL'}

    def execute(self, context):
        state.settings(context.scene).match.reference = context.scene.frame_current
        return {'FINISHED'}


def selected_key_frames(context):
    """Frames of the selected keys of the ones that follow (as the Dope Sheet / Timeline / Graph Editor select them:
    all their curves - a bone's own), else of the keys selected on the paths."""
    sc = context.scene
    m = state.settings(sc).match
    nodes = _followers_of(m) if m.active else followers_of(context, *target_of(sc))
    out = set()
    for name, bone in nodes:
        ob = bpy.data.objects.get(name)
        if ob is None:
            continue
        ad = ob.animation_data
        tweak = ad is not None and getattr(ad, "use_tweak_mode", False)
        pre = 'pose.bones["%s"]' % bpy.utils.escape_identifier(bone) if bone else None
        for fc in compat.get_fcurves(ob) or ():
            if pre is not None and not fc.data_path.startswith(pre):
                continue
            for kp in fc.keyframe_points:
                if kp.select_control_point:
                    x = kp.co.x
                    if tweak:       # (an action edited in the NLA: its time put in the scene's)
                        x = ad.nla_tweak_strip_time_to_scene(x)
                    out.add(int(round(x)))
    if len(out) < 2:
        for c in state.editable_caches(sc):
            out |= {int(round(k.t)) for k in c.keys if k.sel}
    return sorted(out)


class EMP_OT_match_keys_range(bpy.types.Operator):
    """Frames: from the first to the last selected key of the ones that follow - in the Dope Sheet, Timeline or Graph
    Editor, or on a path (Custom)"""
    bl_idname = "emp.match_keys_range"
    bl_label = "Frames of the Selected Keys"
    bl_options = {'UNDO', 'INTERNAL'}

    def execute(self, context):
        sel = selected_key_frames(context)
        if len(sel) < 2:
            self.report({'WARNING'}, "Select keys on two or more frames (Dope Sheet, Timeline, Graph Editor or a "
                                     "path) of the ones that follow")
            return {'CANCELLED'}
        m = state.settings(context.scene).match
        _applying[0] = True
        try:
            m.frames = 'CUSTOM'
            m.start, m.end = int(round(min(sel))), int(round(max(sel)))
        finally:
            _applying[0] = False
        session_changed(context)
        return {'FINISHED'}


classes = (EMP_OT_match_motion, EMP_OT_match_finish, EMP_OT_match_cancel, EMP_OT_match_reference,
           EMP_OT_match_keys_range)
