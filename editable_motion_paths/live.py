"""The paths of children follow an edit of a parent at once: the end of a child while its parent turns, also when
the child hangs on it through a Child Of, a copy of another bone or a vertex parent.

A path is its parent space B(f) - baked frame by frame - times its own channels. When a bone / object above it is
edited (with the add-on, in the Graph Editor, with Blender's G / R / S), everything it carries rigidly moves with
it: the world change of that parent on each frame, D(f) = B(f) @ basis_now(f) @ basis_baked(f)^-1 @ B(f)^-1, put
in front of a child's baked parent space gives the child's new one exactly - no frames stepped through, a fraction
of a millisecond for a long range. Parents edited one under the other (a parent, then its child) are applied from
the root down.

"Rigidly" is followed link by link up from each path's owner (walk()): a bone to its parent (rotation and scale
inherited in full), an object to its parent / parent bone, a bone or object that is a copy of another (Copy
Transforms in full, Copy Location + Copy Rotation (+ Copy Scale) of one target, a full Child Of, an Armature
constraint on one bone) to that one, an object hung on one vertex to that mesh (only the vertex's move reaches it -
when the mesh is the same on every frame). Limits in a bone's own Local space change nothing above it.
The bones / objects on the way that are animated get hidden baked caches of their own (helper_keys) when they have
no path: an edit of any of them is followed too.

Where a link isn't rigid (IK, Stretch To, a constraint mixing in another bone, an influence that changes over time)
nothing is guessed: the child's path waits for the re-bake as before. After each full bake (and an undo that brings
an older bake back) what the bake was made from is noted."""

import bpy
from mathutils import Matrix

from . import compat, core, state

_anc = {}       # owner (object, bone) -> {"c", "B", "fs", "basis", "sig", "limits"}: an edited parent, as baked
_kids = {}      # cache key -> (baked B list, fs): every path's parent space as baked
_chain = {}     # owner -> [(ancestor owner, Q, Q^-1, index in its walk)] that carry it rigidly, root first (Q:
                # None or a constant matrix turning the ancestor's world change into this one's - a copy between spaces)
_how = {}       # owner -> ('RIGID' | 'VERTEX' | 'VERTEX3', its parent inverse inverted): how it hangs on the first
_strict = {}    # owner -> the lowest link (index in its walk) that passes only a change without scale on
_live = {}      # cache key -> the B list put on the cache here (a live one)
_depth = {}
_objs = {}      # object name -> its world matrix as baked: an object without animation that carries paths
_carry = {}     # owner -> the nearest such object that carries it rigidly (constraints aiming inside it)
_moved_before = set()    # ancestors edited since the bake (an edit undone back to the bake: worked out once more)

_OBJ_TF = {"location", "rotation_euler", "rotation_quaternion", "rotation_axis_angle", "scale", "delta_location",
           "delta_rotation_euler", "delta_rotation_quaternion", "delta_scale"}
_BONE_TF = ("location", "rotation_euler", "rotation_quaternion", "rotation_axis_angle", "scale")
_LIMITS = {'LIMIT_ROTATION', 'LIMIT_LOCATION', 'LIMIT_SCALE'}
_EULER = {'XYZ', 'XZY', 'YXZ', 'YZX', 'ZXY', 'ZYX'}
# modifiers whose result changes with the time (a mesh hung on by a vertex: that vertex would move by itself)
_TIMED_MODS = {'CLOTH', 'SOFT_BODY', 'DYNAMIC_PAINT', 'FLUID', 'OCEAN', 'PARTICLE_SYSTEM', 'PARTICLE_INSTANCE',
               'EXPLODE', 'MESH_CACHE', 'MESH_SEQUENCE_CACHE', 'WAVE', 'BUILD', 'COLLISION'}
_TIMED_NODES = {'GeometryNodeInputSceneTime', 'GeometryNodeObjectInfo', 'GeometryNodeCollectionInfo',
                'GeometryNodeSelfObject', 'GeometryNodeInputActiveCamera', 'GeometryNodeSimulationInput',
                'GeometryNodeSimulationOutput', 'GeometryNodeBake', 'GeometryNodeInputObject',
                'GeometryNodeInputCollection', 'GeometryNodeDeformCurvesOnSurface'}


def _active(cons):
    return [c for c in cons if not c.mute and getattr(c, "enabled", True) and c.influence > 0.0]


def _all_curves(act):
    """Every F-Curve of an action (all its slots: a strip's action is read whole)."""
    if getattr(act, "layers", None) is not None and len(act.layers):
        for la in act.layers:
            for s in la.strips:
                for cb in getattr(s, "channelbags", ()):
                    yield from cb.fcurves
    elif hasattr(act, "fcurves"):
        yield from act.fcurves


def _animated_paths(idb, memo):
    """What animates this ID: (the data paths of its curves - its action's and its NLA strips' -, the driven ones)."""
    key = ("paths", type(idb).__name__, idb.name)
    got = memo.get(key)
    if got is None:
        curves, driven = set(), set()
        ad = getattr(idb, "animation_data", None)
        if ad is not None:
            curves.update(fc.data_path for fc in compat.get_fcurves(idb) or ())
            for t in ad.nla_tracks:
                if not t.mute:
                    for s in t.strips:
                        if not s.mute and s.action is not None:
                            curves.update(fc.data_path for fc in _all_curves(s.action))
            driven.update(d.data_path for d in ad.drivers)
        got = memo[key] = (curves, driven)
    return got


def _varies(idb, path, memo):
    """Can this value change from frame to frame (a curve on it, a driver reading something animated)?"""
    curves, driven = _animated_paths(idb, memo)
    if path in curves:
        return True
    if path not in driven:
        return False
    d = idb.animation_data.drivers.find(path)
    if d is None:
        return True
    for v in d.driver.variables:
        if v.type != 'SINGLE_PROP':
            return True         # (a bone's transform, a distance...: it moves with the pose)
        for t in v.targets:
            if t.id is None:
                continue
            c2, d2 = _animated_paths(t.id, memo)
            if t.data_path in c2 or t.data_path in d2:
                return True
    return False


def _local_limit(c):
    return c.type in _LIMITS and c.owner_space == 'LOCAL'


def _con_path(prefix, c):
    return prefix + 'constraints["%s"].influence' % bpy.utils.escape_identifier(c.name)


def _working(owner, idb, prefix, memo):
    """Its constraints that work - None when one of them may work on some frames only (its influence animated)."""
    out = []
    for c in owner.constraints:
        if c.mute or not getattr(c, "enabled", True):
            continue
        if _varies(idb, _con_path(prefix, c), memo):
            return None
        if c.influence > 0.0:
            out.append(c)
    return out


def _still(ob, memo):
    """Its world matrix is the same on every frame: no constraints, no animated transform - nor above it."""
    for _i in range(100):
        if ob is None:
            return True
        if _active(ob.constraints):
            return False
        curves, driven = _animated_paths(ob, memo)
        if any(p in _OBJ_TF for p in curves) or any(p in _OBJ_TF for p in driven):
            return False
        if ob.parent is not None and ob.parent_type not in {'OBJECT', 'ARMATURE'}:
            return False
        ob = ob.parent
    return False


def _identity(m, eps=1e-9):
    return all(abs(m[i][j] - (1.0 if i == j else 0.0)) < eps for i in range(4) for j in range(4))


def _copy_of(c, owner_arm, memo):
    """A Copy Transforms (in full, replacing) or a full Child Of: the bone / object it copies - (node, M), where its
    world change is M @ that one's @ M^-1 (M None: the same) - or None."""
    t = getattr(c, "target", None)
    if t is None or c.influence < 1.0 - 1e-6:
        return None
    if c.type == 'COPY_TRANSFORMS':
        if c.mix_mode != 'REPLACE' or c.owner_space not in {'WORLD', 'POSE'} or \
                c.target_space not in {'WORLD', 'POSE'}:
            return None
        if getattr(c, "use_bbone_shape", False) and getattr(c, "head_tail", 0.0) > 0.0:
            return None         # (a point on a curved bone: it bends with its neighbours)
    elif not core._full_child_of(c, owner_arm):
        return None
    sub = c.subtarget if t.type == 'ARMATURE' and getattr(c, "subtarget", "") else ""
    if sub and (t.pose is None or t.pose.bones.get(sub) is None):
        return None
    # (as Blender works them out - measured: a Copy Transforms in Pose space copies the target seen from the
    # armature; a Child Of puts its target in front whatever the owner's space - only the target's space counts)
    Ao = owner_arm if owner_arm is not None and c.owner_space == 'POSE' and c.type == 'COPY_TRANSFORMS' else None
    At = t if sub and c.target_space == 'POSE' else None
    if Ao is At:
        return (t.name, sub), None
    M = (Ao.matrix_world if Ao is not None else Matrix.Identity(4)) @ \
        (At.matrix_world.inverted_safe() if At is not None else Matrix.Identity(4))
    if _identity(M, 1e-6):
        return (t.name, sub), None
    if any(a is not None and not _still(a, memo) for a in (Ao, At)):
        return None             # (an armature that moves by itself in between: the copy isn't rigid)
    return (t.name, sub), M.copy()


_COPIES = {'COPY_LOCATION', 'COPY_ROTATION', 'COPY_SCALE'}


def _all_axes(c):
    return all(getattr(c, "use_" + a, True) for a in "xyz") and \
        not any(getattr(c, "invert_" + a, False) for a in "xyz")


def _plain_copy(c):
    """A Copy Location / Rotation / Scale that puts the target's channel in place of the owner's: every axis,
    full influence, nothing added or mixed."""
    if c.influence < 1.0 - 1e-6 or getattr(c, "target", None) is None or not _all_axes(c):
        return False
    if c.type == 'COPY_LOCATION':
        return not c.use_offset and not (getattr(c, "use_bbone_shape", False) and c.head_tail > 0.0)
    if c.type == 'COPY_ROTATION':
        return c.mix_mode == 'REPLACE'
    if c.type == 'COPY_SCALE':
        return not c.use_offset and abs(c.power - 1.0) < 1e-9 and not getattr(c, "use_make_uniform", False)
    return False


def _stack_of(cons, owner_arm, memo):
    """Copy Location + Copy Rotation (+ Copy Scale) of one target, last on the stack: a copy of that target like
    Copy Transforms (measured: the same spaces rule) - (node, M, True) or None. Only a world change without scale
    passes on rigidly (the scale is the owner's own, or copied without its shear)."""
    run = []
    for c in reversed(cons):
        if c.type not in _COPIES:
            break
        run.append(c)
    kinds = {c.type for c in run}
    if not {'COPY_LOCATION', 'COPY_ROTATION'} <= kinds or not all(_plain_copy(c) for c in run):
        return None
    c0 = run[0]
    t, sub = c0.target, getattr(c0, "subtarget", "")
    if any(c.target != t or getattr(c, "subtarget", "") != sub or c.owner_space != c0.owner_space or
           c.target_space != c0.target_space for c in run):
        return None
    if c0.owner_space not in {'WORLD', 'POSE'} or c0.target_space not in {'WORLD', 'POSE'}:
        return None
    own_scale = 'COPY_SCALE' not in kinds
    if own_scale and not all(_local_limit(c) for c in cons[:len(cons) - len(run)]):
        return None             # (the scale it keeps would come from those)
    sub = sub if t.type == 'ARMATURE' else ""
    if sub and (t.pose is None or t.pose.bones.get(sub) is None):
        return None
    Ao = owner_arm if owner_arm is not None and c0.owner_space == 'POSE' else None
    At = t if sub and c0.target_space == 'POSE' else None
    if Ao is At:
        return (t.name, sub), None, True
    M = (Ao.matrix_world if Ao is not None else Matrix.Identity(4)) @ \
        (At.matrix_world.inverted_safe() if At is not None else Matrix.Identity(4))
    if _identity(M, 1e-6):
        return (t.name, sub), None, True
    if any(a is not None and not _still(a, memo) for a in (Ao, At)):
        return None
    return (t.name, sub), M.copy(), True


def _armature_of(c):
    """An Armature constraint on one bone in full (weight 1, no envelopes): its owner moves as that bone does
    (measured: the bone's world change, whatever the armatures) - (the bone's node, strict) or None; strict:
    Preserve Volume - only a world change without scale is sure to pass on as it is."""
    if c.type != 'ARMATURE' or c.influence < 1.0 - 1e-6 or getattr(c, "use_bone_envelopes", False):
        return None
    on = [t for t in c.targets if t.weight > 1e-6]
    if len(on) != 1 or abs(on[0].weight - 1.0) > 1e-6:
        return None
    t = on[0]
    arm = t.target
    if arm is None or arm.type != 'ARMATURE' or arm.pose is None or arm.pose.bones.get(t.subtarget) is None:
        return None
    return (arm.name, t.subtarget), bool(getattr(c, "use_deform_preserve_volume", False))


def _quiet_nodes(tree, seen):
    if tree is None or tree.name in seen:
        return True
    seen.add(tree.name)
    for n in tree.nodes:
        if n.bl_idname in _TIMED_NODES:
            return False
        sub = getattr(n, "node_tree", None)
        if sub is not None and not _quiet_nodes(sub, seen):
            return False
    return True


def _fixed_shape(ob, memo):
    """Its mesh is the same on every frame whatever is edited: no shape keys, nothing animated on it or its
    modifiers, no modifier reading another object or the time."""
    if ob.type != 'MESH' or ob.data is None or ob.data.shape_keys is not None:
        return False
    ad = getattr(ob.data, "animation_data", None)
    if ad is not None and (ad.action is not None or len(ad.drivers)):
        return False
    curves, driven = _animated_paths(ob, memo)
    if any(p.startswith("modifiers[") for p in curves | driven):
        return False
    for m in ob.modifiers:
        if not m.show_viewport:
            continue
        if m.type in _TIMED_MODS:
            return False
        for p in m.bl_rna.properties:
            if p.type == 'POINTER' and getattr(p, "fixed_type", None) is not None and \
                    p.fixed_type.identifier in {"Object", "Collection"} and getattr(m, p.identifier, None) is not None:
                return False
        if m.type == 'NODES':
            if not _quiet_nodes(m.node_group, set()):
                return False
            # (an object / collection it is given: it reads that one)
            tree = m.node_group
            items = getattr(getattr(tree, "interface", None), "items_tree", ()) if tree is not None else ()
            if any(getattr(it, "in_out", "") == 'INPUT' and
                   getattr(it, "socket_type", "") in {'NodeSocketObject', 'NodeSocketCollection'} for it in items):
                return False
    return True


def _constrained_up(cons, owner_arm, has_parent, memo):
    """What a bone's / object's constraints make it a copy of: (node, M, strict) or None. A Copy Transforms (or
    Copy Location + Rotation (+ Scale) of one target) replaces what came before; a Child Of and an Armature
    constraint work on the owner's own matrix: only limits before them and no parent of its own - else the
    parent's edits reach it another way too. strict: only a world change without scale passes on rigidly."""
    last = cons[-1]
    if last.type in {'CHILD_OF', 'ARMATURE'} and (has_parent or not all(_local_limit(c) for c in cons[:-1])):
        return None
    if last.type == 'ARMATURE':
        got = _armature_of(last)
        return (got[0], None, got[1]) if got is not None else None
    if last.type in _COPIES:
        return _stack_of(cons, owner_arm, memo)
    got = _copy_of(last, owner_arm, memo)
    return (got[0], got[1], False) if got is not None else None


def _up(node, memo, first=False):
    """What this bone / object hangs on rigidly: (node above, M, how, strict) - M as in _copy_of, how 'RIGID' (its
    world change is that one's), 'VERTEX' (hung on one vertex: only that point's move reaches it), 'VERTEX3'
    (three: rigid while the change is), strict: the link passes only a change without scale on - or None."""
    o = bpy.data.objects.get(node[0])
    if o is None:
        return None
    if node[1]:
        if o.type != 'ARMATURE' or o.pose is None:
            return None
        pb = o.pose.bones.get(node[1])
        if pb is None:
            return None
        cons = _working(pb, o, 'pose.bones["%s"].' % bpy.utils.escape_identifier(node[1]), memo)
        if cons is None:
            return None
        if cons and not all(_local_limit(c) for c in cons):
            got = _constrained_up(cons, o, pb.parent is not None, memo)
            return (got[0], got[1], 'RIGID', got[2]) if got is not None else None
        if not pb.bone.use_inherit_rotation or pb.bone.inherit_scale != 'FULL':
            return None
        return ((o.name, pb.parent.name) if pb.parent is not None else (o.name, "")), None, 'RIGID', False
    cons = _working(o, o, "", memo)
    if cons is None:
        return None
    if cons:
        if len(cons) == 1 and o.parent is None and cons[0].type in {'COPY_TRANSFORMS', 'CHILD_OF'}:
            got = _copy_of(cons[0], None, memo)
            return (got[0], got[1], 'RIGID', False) if got is not None else None
        if o.parent is not None or all(_local_limit(c) for c in cons) or \
                cons[-1].type not in _COPIES | {'ARMATURE'}:
            return None
        got = _constrained_up(cons, None, False, memo)
        return (got[0], got[1], 'RIGID', got[2]) if got is not None else None
    p = o.parent
    if p is None:
        return None
    t = o.parent_type
    if t in {'OBJECT', 'ARMATURE'}:
        return (p.name, ""), None, 'RIGID', False
    if t == 'BONE':
        if p.type != 'ARMATURE' or p.pose is None or p.pose.bones.get(o.parent_bone) is None:
            return None
        return (p.name, o.parent_bone), None, 'RIGID', False
    if t in {'VERTEX', 'VERTEX_3'} and first and _fixed_shape(p, memo):
        return (p.name, ""), None, ('VERTEX' if t == 'VERTEX' else 'VERTEX3'), False
    return None


def walk(obj_name, bone, memo):
    """What carries this bone / object rigidly, nearest first: ([(node, Q)], how it hangs on the first) - Q turns
    a world change of that node into this one's (None: the same)."""
    key = ("walk", obj_name, bone)
    got = memo.get(key)
    if got is not None:
        return got
    out, Q, how = [], None, 'RIGID'
    node = (obj_name, bone)
    seen = {node}
    first = True
    strict = []         # (the links passing only a change without scale on: (index in out of the node above))
    for _i in range(2000):
        got = _up(node, memo, first)
        if got is None:
            break
        nxt, M, h, s = got
        if first:
            how = h
        first = False
        if nxt in seen:
            break
        seen.add(nxt)
        if M is not None:
            Q = M if Q is None else Q @ M
        if s:
            strict.append(len(out))
        out.append((nxt, Q))
        node = nxt
    memo[key] = (out, how)
    memo[("strict",) + key[1:]] = strict
    return memo[key]


def strict_links(obj_name, bone, memo):
    """After walk(): the indices (in its chain) of the nodes whose change reaches this one only without scale."""
    walk(obj_name, bone, memo)
    return memo.get(("strict", obj_name, bone), [])


def rigidly_carried(a_obj, a_bone, c_obj, c_bone):
    """Does C hang from A rigidly (see walk): an edit of A moves C with it."""
    if a_obj is None or c_obj is None or (a_obj == c_obj and a_bone == c_bone):
        return False
    chain, _how = walk(c_obj.name, c_bone, {})
    return any(n == (a_obj.name, a_bone) for n, _Q in chain)


def _limits(node, memo):
    """An edit of this bone's / object's curves turned into its world change: its constraints may only be full
    Child Ofs (in its parent space as baked) and, before them, limits in its Local space (checked against the
    edit: inside them they change nothing) - the limits, or None when it can't."""
    o = bpy.data.objects.get(node[0])
    if o is None:
        return None
    if node[1]:
        owner = o.pose.bones.get(node[1]) if o.pose is not None else None
        prefix, arm = 'pose.bones["%s"].' % bpy.utils.escape_identifier(node[1]), o
    else:
        owner, prefix, arm = o, "", None
    if owner is None:
        return None
    cons = _working(owner, o, prefix, memo)
    if cons is None:
        return None
    lims, child_of = [], False
    for c in cons:
        if core._full_child_of(c, arm):
            child_of = True
        elif _local_limit(c) and not child_of:
            lims.append(c)
        else:
            return None
    return lims


def _animated_node(node, memo):
    o = bpy.data.objects.get(node[0])
    if o is None:
        return False
    curves, _driven = _animated_paths(o, memo)
    if node[1]:
        pre = 'pose.bones["%s"].' % bpy.utils.escape_identifier(node[1])
        return any(pre + p in curves for p in _BONE_TF)
    return any(p in curves for p in _OBJ_TF)


def helper_keys(st):
    """The animated bones / objects that carry the paths' owners rigidly: hidden caches are baked for them (their
    parent spaces) when they have no path - an edit of any of them moves the paths below at once."""
    out = set()
    if st is None:
        return out
    memo = {}
    try:
        for e in st.paths:
            if e.object is None:
                continue
            chain, _how = walk(e.object.name, e.bone_name, memo)
            for node, _Q in chain:
                if node not in out and _animated_node(node, memo) and _limits(node, memo) is not None:
                    out.add(node)
    except Exception:      # (no hidden caches then: nothing followed, the re-bake as before)
        return set()
    return out


def _moves_by_itself(ob):
    """Its own transform is animated (curves, drivers, NLA) or set by constraints, or it hangs on a bone / on
    vertices (the pose, the mesh move it): moved by hand, it isn't moved the same on every frame - and an edit of
    that bone isn't a move by hand (else a child's path would shift as a whole)."""
    if _active(ob.constraints):
        return True
    if ob.parent is not None and ob.parent_type not in {'OBJECT', 'ARMATURE'}:
        return True
    ad = ob.animation_data
    if ad is None:
        return False
    if len(ad.nla_tracks):
        return True
    for fc in compat.get_fcurves(ob) or ():
        if fc.data_path in _OBJ_TF:
            return True
    return any(d.data_path in _OBJ_TF for d in ad.drivers)


def _inside(t, x):
    """The object t is x or hangs from it."""
    while t is not None:
        if t == x:
            return True
        t = t.parent
    return False


def _static_carrier(obj, owner, bone):
    """The nearest object above this bone / object (or itself) that nothing moves but a hand - nothing animates
    it or anything above it - and that carries it rigidly: every constraint on the way aims inside it. Moved by
    hand (the whole character's armature moved, no keys), it moves the path the same on every frame."""
    targets = []
    if bone:
        pb = owner
        while pb is not None:
            for c in _active(pb.constraints):
                targets += [getattr(c, "target", None), getattr(c, "pole_target", None)]
            pb = pb.parent
    chain = []
    o = obj
    while o is not None:
        chain.append(o)
        o = o.parent
    for i, x in enumerate(chain):
        if any(_moves_by_itself(a) for a in chain[i:]):
            continue
        below = chain[:i]
        tg = targets + [t for b in below for c in _active(b.constraints)
                        for t in (getattr(c, "target", None), getattr(c, "pole_target", None))]
        if all(t is None or _inside(t, x) for t in tg):
            return x
    return None


def _basis_list(tg, fs, n):
    out = []
    for i in range(n):
        t = float(fs + i)
        out.append(tg.basis(tg.loc.values(t), tg.rot.values(t), tg.scale.values(t)))
    return out


def _full_space(c):
    """This path's B is the whole parent space (world = B @ basis) - not a location path's own map."""
    return c.ptype != core.LOCATION or not state._own_loc_space(c.obj_name, c.bone_name)


def _cache(key):
    return state.caches.get(key) or state.helper_caches.get(key)


def clear():
    _anc.clear()
    _kids.clear()
    _chain.clear()
    _how.clear()
    _strict.clear()
    _live.clear()
    _depth.clear()
    _objs.clear()
    _carry.clear()
    _moved_before.clear()


def snapshot():
    """After a full bake (or an undo that brought one back): what it was made from."""
    clear()
    try:
        owners = {}
        for c in state.all_caches():        # (the tracked vertices' bones and the hidden parents follow too)
            if c.B:
                owners.setdefault((c.obj_name, c.bone_name), []).append(c)
        if not owners:
            return
        memo = {}
        full = {}
        for k, cs in owners.items():
            a = next((c for c in cs if _full_space(c)), None)
            if a is not None:
                full[k] = a
        for k in owners:
            obj, owner = core.resolve(*k)
            if owner is None:
                continue
            x = _static_carrier(obj, owner, k[1])
            if x is not None:
                _carry[k] = x.name
                _objs.setdefault(x.name, x.matrix_world.copy())
            chain, how = walk(k[0], k[1], memo)
            _depth[k] = len(chain)
            strict = strict_links(k[0], k[1], memo)
            if strict:
                _strict[k] = min(strict)
            ancs = []
            for idx, (n, Q) in enumerate(chain):
                if n not in full:
                    continue
                if n not in _anc:
                    lims = _limits(n, memo) if _animated_node(n, memo) else None
                    if lims is None:
                        continue
                    a = full[n]
                    tg = a.target()
                    if tg is None:
                        continue
                    a.refresh()
                    _anc[n] = {"c": a, "B": a.B, "fs": a.fs, "basis": _basis_list(tg, a.fs, len(a.B)), "sig": a.sig,
                               "limits": lims}
                ancs.append((n, Q, Q.inverted_safe() if Q is not None else None, idx))
            if ancs:
                _chain[k] = ancs[::-1]          # (root first)
                _how[k] = (how, obj.matrix_parent_inverse.inverted_safe() if how == 'VERTEX' else None)
        for n in _anc:
            _depth[n] = len(walk(n[0], n[1], memo)[0])
        for kb, cs in owners.items():
            if kb in _chain or kb in _carry:
                for c in cs:
                    _kids[c.key] = (c.B, c.fs)
    except Exception:      # (never half of it: nothing followed then, the re-bake as before)
        clear()


def _apply(D, fsD, B, fsB):
    """D (per frame, from fsD) in front of B (per frame, from fsB)."""
    n = len(D)
    out = []
    for i, b in enumerate(B):
        j = min(max(fsB + i - fsD, 0), n - 1)
        out.append(D[j] @ b if D[j] is not None else b)
    return out


def update():
    """The edited parents' curves now against the bake: their children's paths moved with them. True if any."""
    if _pv["tmp"] and not state.blender_edit_running():
        end_preview()
    if not _anc and not _objs:
        return False
    try:
        return _update()
    except (ReferenceError, AttributeError, IndexError):
        clear()
        return False


# ----------------------------------------------------------------------------
# Blender's own G / R / S with auto keying: the paths as they will be once the drag is keyed
# ----------------------------------------------------------------------------

_pv = {"tmp": {}, "caches": set()}      # object name -> (copy of its action, its curves); caches drawn from them


def baked(c):
    """The parent spaces this cache got from the last bake (a live one put on it here: the one it replaced)."""
    got = _kids.get(c.key)
    if got is not None and c.B is not got[0] and c.B is _live.get(c.key):
        return got[0]
    return c.B


def previewing(key):
    return key in _pv["caches"]


def _copy_curves(ob):
    """A copy of the object's action (never assigned: the real curves and the pose being dragged stay as they are)
    and the curves of its slot in it."""
    ad = ob.animation_data
    tmp = ad.action.copy()
    coll = None
    slot = getattr(ad, "action_slot", None)
    if compat._HAS_SLOTS and slot is not None:
        from bpy_extras import anim_utils
        s = tmp.slots.get(slot.identifier) if hasattr(tmp, "slots") else None
        cb = anim_utils.action_get_channelbag_for_slot(tmp, s) if s is not None else None
        coll = cb.fcurves if cb is not None else None
    elif hasattr(tmp, "fcurves"):
        coll = tmp.fcurves
    return tmp, coll


def _put(coll, fc, v, f):
    c = coll.find(fc.data_path, index=fc.array_index)
    if c is None:
        return
    kp = next((k for k in c.keyframe_points if abs(k.co[0] - f) < 1e-3), None)
    if kp is None:
        c.keyframe_points.insert(f, v, options={'FAST'})
    else:
        dv = v - kp.co[1]
        if abs(dv) < 1e-12:
            return
        kp.co[1] = v
        kp.handle_left[1] += dv
        kp.handle_right[1] += dv
    c.update()


def preview(scene):
    """During Blender's own G / R / S with auto keying: the dragged bones' / objects' own paths and those of what
    they carry drawn as they will be once the drag is keyed (a key on this frame; the frames around it change with
    it) - not shifted as a whole and set right only after the drag."""
    if not scene.tool_settings.use_keyframe_insert_auto or not state.blender_edit_running():
        end_preview()
        return update()         # (an object without animation moved by hand: its paths with it)
    from . import onion
    try:
        pend = onion._pending_keys(scene)
    except Exception:
        pend = []
    f = scene.frame_current
    owners = {}
    for ob, fc, v in pend:
        m = state._BONE_PATH.match(fc.data_path)
        bone = m.group(1).replace('\\"', '"').replace('\\\\', '\\') if m else ""
        owners.setdefault((ob.name, bone), []).append((ob, fc, v))
    if not owners:
        end_preview()
        return update()
    try:
        for (oname, _b), items in owners.items():
            ob = items[0][0]
            if oname not in _pv["tmp"]:
                if ob.animation_data is None or ob.animation_data.action is None:
                    continue
                _pv["tmp"][oname] = _copy_curves(ob)
            tmp, coll = _pv["tmp"][oname]
            if coll is None:
                continue
            for _ob, fc, v in items:
                _put(coll, fc, v, f)
        shown = set()
        for c in state.all_caches():
            got = _pv["tmp"].get(c.obj_name)
            if got is not None and (c.obj_name, c.bone_name) in owners and got[1] is not None:
                if c.preview_coll is not got[1]:
                    c.preview_coll = got[1]
                c.dirty = True
                shown.add(c.key)
        for key in _pv["caches"] - shown:
            c = state.caches.get(key) or state.helper_caches.get(key)
            if c is not None:
                c.preview_coll = None
                c.dirty = True
        _pv["caches"] = shown
        if _anc or _objs:
            for key in _update(dragged=set(owners)):
                _pv["caches"].add(key)
        state.tag_redraw()
        return True
    except (ReferenceError, AttributeError, IndexError, RuntimeError):
        end_preview()
        return False


def end_preview():
    """The drag is over (keyed, or cancelled): the paths from the real curves again, the copies gone."""
    if not _pv["tmp"] and not _pv["caches"]:
        return False
    for key in _pv["caches"]:
        c = state.caches.get(key) or state.helper_caches.get(key)
        if c is not None:
            c.preview_coll = None
            c.dirty = True
    _pv["caches"] = set()
    for tmp, _coll in _pv["tmp"].values():
        try:
            bpy.data.actions.remove(tmp)
        except Exception:
            pass
    _pv["tmp"] = {}
    try:
        _update()
    except (ReferenceError, AttributeError, IndexError):
        clear()
    state.tag_redraw()
    return True


def _object_moves():
    """The objects without animation moved by hand since the bake: their world change (the same on every frame)."""
    out = {}
    for name, m0 in _objs.items():
        ob = bpy.data.objects.get(name)
        if ob is None:
            continue
        d = ob.matrix_world @ m0.inverted_safe()
        if not _identity(d):
            out[name] = d
    return out


def _rigid(d, eps=1e-5):
    """A turn and a move only (no scale): what three vertices of a mesh do together."""
    r = d.to_3x3()
    return all(abs((r @ r.transposed())[i][j] - (1.0 if i == j else 0.0)) < eps for i in range(3) for j in range(3))


def _inside_limits(lims, rot_mode, mats):
    """Every one of these local matrices inside the limits (there they change nothing)."""
    eps = 1e-4
    for c in lims:
        for m in mats:
            loc, q, s = m.decompose()
            if c.type == 'LIMIT_ROTATION':
                order = c.euler_order if c.euler_order != 'AUTO' else (rot_mode if rot_mode in _EULER else 'XYZ')
                e = q.to_euler(order)
                for i, ax in enumerate("xyz"):
                    if getattr(c, "use_limit_" + ax) and not \
                            getattr(c, "min_" + ax) - eps <= e[i] <= getattr(c, "max_" + ax) + eps:
                        return False
            else:
                v = loc if c.type == 'LIMIT_LOCATION' else s
                for i, ax in enumerate("xyz"):
                    if getattr(c, "use_min_" + ax) and v[i] < getattr(c, "min_" + ax) - eps:
                        return False
                    if getattr(c, "use_max_" + ax) and v[i] > getattr(c, "max_" + ax) + eps:
                        return False
    return True


def _moved(k, B0, fs0, deltas, moves):
    """Its baked parent space with the world changes of what carries it put in front - None when they can't
    be (a mesh hung on by three vertices scaled)."""
    M = B0
    if _carry.get(k) in moves:
        Dm = moves[_carry[k]]
        M = [Dm @ b for b in M]
    chain = [x for x in _chain.get(k, ()) if x[0] in deltas]
    if not chain:
        return M
    how, pinv = _how.get(k, ('RIGID', None))
    thr = _strict.get(k)
    out = []
    for i, b in enumerate(M):
        tot = None          # (the whole world change of what it hangs on, on this frame)
        hi = None           # (the part of it passing a link that takes only a change without scale)
        for ka, Q, Qi, idx in chain:
            fsD, D = deltas[ka]
            d = D[min(max(fs0 + i - fsD, 0), len(D) - 1)]
            if d is None:
                continue
            if Q is not None:
                d = Q @ d @ Qi
            tot = d if tot is None else d @ tot
            if thr is not None and idx >= thr:
                hi = tot
        if hi is not None and not _rigid(hi):
            return None         # (a copy keeping its own scale, its target scaled: not followed - the re-bake)
        if tot is None:
            out.append(b)
        elif how == 'VERTEX':
            p = (b @ pinv).translation          # (where the vertex was: only its move reaches it)
            out.append(Matrix.Translation(tot @ p - p) @ b)
        elif how == 'VERTEX3' and not _rigid(tot):
            return None
        else:
            out.append(tot @ b)
    return out


def _update(dragged=()):
    moves = _object_moves()
    deltas = {}         # ancestor owner -> (fs, [D per frame or None])
    blocked = set()     # ancestors edited in a way not followed here: what they carry waits for the re-bake
    for ka in sorted(_anc, key=lambda k: _depth.get(k, 0)):
        info = _anc[ka]
        a = info["c"]
        if _cache(a.key) is not a or (a.B is not info["B"] and a.B is not _live.get(a.key)):
            continue        # (baked again some other way meanwhile)
        a.refresh()
        if a.sig == info["sig"] and ka not in _moved_before and ka not in dragged:
            continue        # (its curves as baked)
        tg = a.target()
        if tg is None:
            continue
        basis = _basis_list(tg, info["fs"], len(info["basis"]))
        _moved_before.add(ka)
        # (a key moved in the Graph Editor has the handles of every curve worked out again: a change in the last
        # digits isn't an edit - it mustn't count, e.g. against a limit the bake was already past)
        changed = [i for i, (n, o) in enumerate(zip(basis, info["basis"]))
                   if not _identity(n @ o.inverted_safe(), 1e-7)]
        if not changed:
            continue
        if info["limits"] and not _inside_limits(info["limits"], tg.rot_mode,
                                                 [basis[i] for i in changed] + [info["basis"][i] for i in changed]):
            blocked.add(ka)
            continue
        # its own parent space as the parents above it moved it (and an object carrying it moved by hand)
        B = _moved(ka, info["B"], info["fs"], deltas, moves)
        if B is None or any(x[0] in blocked for x in _chain.get(ka, ())):
            blocked.add(ka)
            continue
        D = [None] * len(B)
        for i in changed:
            b = B[i]
            d = b @ basis[i] @ info["basis"][i].inverted_safe() @ b.inverted_safe()
            D[i] = None if _identity(d) else d
        if any(d is not None for d in D):
            deltas[ka] = (info["fs"], D)
    moved = False
    touched = []
    for key, (B0, fs0) in list(_kids.items()):
        c = _cache(key)
        if c is None or (c.B is not B0 and c.B is not _live.get(key)) or c.fs != fs0:
            continue
        M = None
        if not any(x[0] in blocked for x in _chain.get(key[:2], ())):
            M = _moved(key[:2], B0, fs0, deltas, moves)
        if M is None or M is B0:
            if c.B is not B0:            # (the parents back as baked - an undo of the edit; or not followed)
                c.B = B0
                _live.pop(key, None)
                c.dirty = True
                moved = True
            continue
        c.B = M
        _live[key] = M
        c.dirty = True
        moved = True
        if dragged:
            touched.append(key)
    return touched if dragged else moved
