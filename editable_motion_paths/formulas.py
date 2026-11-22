"""Formulas: what Blender works out for a bone / object whose place isn't a rigid copy of another one's, redone in
Python - so the paths below it follow an edit at once (live.py), not only after a re-bake.

A recipe says how a bone / object is placed: its parent (a bone through Blender's own inherit rules -
Bone.convert_local_to_pose -, an object through its parent matrix), its own channels, then its constraints in their
spaces with their influence (static, keyed, or driven: as baked), as Blender's constraint solver does them: Copy
Location / Rotation / Scale / Transforms (any axes, mix, offset), Child Of, Armature (linear, or with Preserve Volume
as dual quaternions), Damped Track, Track To, Stretch To, Limit Location / Rotation / Scale / Distance - and an object
hung on a vertex of a mesh an armature bends. A sheared matrix (a non-uniform scale above a turned bone) is
straightened as Blender does it (orthogonalize_m4_stable). (An IK chain is Blender's iterative solver: not redone
here - the bake during the drag follows it.)

evaluate() takes the world matrices of what the recipe reads (the new ones where an edit changed them, else as
baked) and gives the new world matrix. Nothing is trusted blindly: after each bake every recipe is checked against
what Blender worked out on a few frames (check()); one that doesn't come out the same is not used."""

import math

import bpy
from mathutils import Euler, Matrix, Quaternion, Vector

_EULER = {'XYZ', 'XZY', 'YXZ', 'YZX', 'ZXY', 'ZYX'}
_DIRS = (Vector((1, 0, 0)), Vector((0, 1, 0)), Vector((0, 0, 1)), Vector((-1, 0, 0)), Vector((0, -1, 0)),
         Vector((0, 0, -1)))
_TRACK = {'TRACK_X': 0, 'TRACK_Y': 1, 'TRACK_Z': 2, 'TRACK_NEGATIVE_X': 3, 'TRACK_NEGATIVE_Y': 4,
          'TRACK_NEGATIVE_Z': 5}
_UP = {'UP_X': 0, 'UP_Y': 1, 'UP_Z': 2}
BONE_SPACES = {'WORLD', 'POSE', 'LOCAL_WITH_PARENT', 'LOCAL', 'CUSTOM'}
OBJECT_SPACES = {'WORLD', 'LOCAL', 'CUSTOM'}
CHECK_EPS = 1e-4        # (a recipe's result against Blender's: relative to the size of what is placed)
CHECK_FRAMES = 12


class Unsupported(Exception):
    """A setting the recipes don't do: that bone / object waits for the re-bake as before."""


def _order(owner):
    m = getattr(owner, "rotation_mode", 'XYZ')
    return m if m in _EULER else 'XYZ'


def _cols(M):
    return [Vector(M.col[i][:3]) for i in range(3)]


def _set_cols(M, cols):
    for i in range(3):
        for r in range(3):
            M[r][i] = cols[i][r]


def _size(M):
    return Vector([c.length for c in _cols(M)])


def _normalized(M):
    """The 3x3 part's axes made unit (Blender's normalize_m4_ex): (matrix, their lengths)."""
    N = M.copy()
    cols = _cols(M)
    size = Vector([c.length for c in cols])
    _set_cols(N, [c / s if s > 1e-30 else c for c, s in zip(cols, size)])
    return N, size


_SHEAR_MIN = 1e-4          # (Blender corrects the angle between X and Z when its cosine is above this...)
_SHEAR_MAX = 1.0 - 1.1920929e-07    # (...and below this: X and Z not parallel)
_SHEAR_EDGE = 2e-6         # (that close to either bound Blender's float math may decide the other way: not done)


def _ortho(M, normalize=True):
    """Blender's orthogonalize_m4_stable(M, Y, normalize): the X / Z axes made perpendicular to Y (projected on its
    plane), then to each other - a sheared matrix (a non-uniform scale above a turned bone) corrected as Blender
    does it: X and Z each turned by half the missing angle, their lengths kept in proportion and the area kept
    (normalize: unit axes)."""
    x, y, z = _cols(M)
    ly = y.length_squared
    if ly > 0.0:
        x = x - y * (x.dot(y) / ly)
        z = z - y * (z.dot(y) / ly)
        if normalize:
            y = y / math.sqrt(ly)
    lx, lz = x.length, z.length
    nx = x / lx if lx > 0.0 else x
    nz = z / lz if lz > 0.0 else z
    c = nx.dot(nz)
    a = abs(c)
    if abs(a - _SHEAR_MIN) < _SHEAR_EDGE or a > _SHEAR_MAX - _SHEAR_EDGE:
        # (on the edge of Blender's own test - or X and Z all but parallel: which way it went can't be told)
        raise Unsupported("shear on the edge")
    if a > _SHEAR_MIN:
        angle = math.acos(c)
        target = angle + (math.pi / 2 - angle) / 2
        nx = nx - nz * c
        nx = nx * (math.sin(target) / nx.length)
        nx = nx + nz * math.cos(target)
        nz = nx.cross(nz).cross(nx)
        nz.normalize()
        if not normalize:
            f = math.sqrt(math.sin(angle))
            x, z = nx * (lx * f), nz * (lz * f)
    if normalize:
        x, z = nx, nz
    N = M.copy()
    _set_cols(N, [x, y, z])
    return N


def _wrap(a):
    b = a * (0.5 / math.pi) + 0.5
    return ((b - math.floor(b)) - 0.5) * (2.0 * math.pi)


def _clamp_angle(a, lo, hi):
    """Blender's Limit Rotation: the nearest angle inside [lo, hi] going round the circle."""
    if hi - lo >= 2.0 * math.pi:
        return a
    if hi <= lo:
        return lo
    lw, hw = _wrap(lo - a), _wrap(hi - a)
    if lw < hw:
        return a + min(max(0.0, lw), hw)
    if hw >= 0.0 or lw <= 0.0:
        return a
    return a + (hw if abs(hw) < abs(lw) else lw)


# ----------------------------------------------------------------------------
# what a recipe is made of
# ----------------------------------------------------------------------------

class Con:
    """One constraint of a recipe: its type, spaces, targets [(node, head_tail, target's rotation order)] and
    settings; its influence: a number, a curve's data path (keyed), or a value baked on every frame (driven)."""
    __slots__ = ("type", "name", "ob", "own", "tar", "targets", "s", "infl", "infl_path", "infl_baked", "space",
                 "drv")

    def __init__(self, c, idb, prefix):
        self.type = c.type
        self.name = c.name
        self.ob = idb.name
        self.own = getattr(c, "owner_space", 'WORLD')
        self.tar = getattr(c, "target_space", 'WORLD')
        self.targets = []
        self.s = {}
        self.infl = c.influence
        self.infl_path = None
        self.infl_baked = None
        self.drv = ([], [])         # (a driven influence: the bones / objects and the animated values it reads)
        self.space = None
        if 'CUSTOM' in (self.own, self.tar):
            so = getattr(c, "space_object", None)
            if so is None:
                raise Unsupported("custom space without an object")
            sub = getattr(c, "space_subtarget", "") if so.type == 'ARMATURE' else ""
            if sub and (so.pose is None or so.pose.bones.get(sub) is None):
                raise Unsupported("custom space bone")
            self.space = (so.name, sub)
        dp = prefix + 'constraints["%s"].influence' % bpy.utils.escape_identifier(c.name)
        ad = idb.animation_data
        if ad is not None:
            drv = next((d for d in ad.drivers if d.data_path == dp), None)
            if drv is not None:
                self.infl_baked = (idb.name, dp)
                self.drv = _driver_reads(drv)
            else:
                from . import compat
                coll = compat.get_fcurves(idb)
                if coll is not None and coll.find(dp) is not None:
                    self.infl_path = dp


def _driver_reads(d):
    """What a driver reads: ([bone / object nodes whose transforms it reads], [(object name, data path) of values it
    reads]) - Unsupported for a Python expression reading more than its variables."""
    if d.driver.type == 'SCRIPTED' and "bpy" in d.driver.expression:
        raise Unsupported("python driver")
    nodes, props = [], []
    for v in d.driver.variables:
        for t in v.targets:
            if t.id is None:
                continue
            if v.type == 'SINGLE_PROP':
                if isinstance(t.id, bpy.types.Object):
                    props.append((t.id.name, t.data_path))
                else:
                    raise Unsupported("driver reads another kind of data")
            elif isinstance(t.id, bpy.types.Object):
                nodes.append((t.id.name, t.bone_target if t.id.type == 'ARMATURE' else ""))
    return nodes, props


def _target(c, ob, sub, head_tail=0.0, bbone=False):
    """(node, head_tail, rotation order) of a target, or Unsupported."""
    if ob is None:
        raise Unsupported("no target")
    if sub:
        if ob.type != 'ARMATURE' or ob.pose is None:
            raise Unsupported("bone of a non-armature")
        pb = ob.pose.bones.get(sub)
        if pb is None:
            raise Unsupported("no bone")
        if bbone and pb.bone.bbone_segments > 1:
            raise Unsupported("curved bone")
        return (ob.name, sub), head_tail, _order(pb)
    return (ob.name, ""), 0.0, _order(ob)


def _con(c, idb, prefix):
    """The constraint as a recipe step, or Unsupported."""
    k = Con(c, idb, prefix)
    t = c.type
    s = k.s
    ht = getattr(c, "head_tail", 0.0)
    bb = getattr(c, "use_bbone_shape", False)
    if t in {'COPY_LOCATION', 'COPY_ROTATION', 'COPY_SCALE', 'COPY_TRANSFORMS', 'CHILD_OF', 'DAMPED_TRACK',
             'TRACK_TO', 'STRETCH_TO', 'LIMIT_DISTANCE', 'TRANSFORM'}:
        k.targets.append(_target(c, c.target, c.subtarget if c.target is not None and
                                 c.target.type == 'ARMATURE' else "", ht, bb))
        if bb and ht > 0.0:
            n = k.targets[0][0]
            if n[1] and bpy.data.objects[n[0]].pose.bones[n[1]].bone.bbone_segments > 1:
                raise Unsupported("curved bone")
    if t == 'COPY_LOCATION':
        s.update(axes=[c.use_x, c.use_y, c.use_z], inv=[c.invert_x, c.invert_y, c.invert_z], offset=c.use_offset)
    elif t == 'COPY_ROTATION':
        s.update(axes=[c.use_x, c.use_y, c.use_z], inv=[c.invert_x, c.invert_y, c.invert_z], mix=c.mix_mode,
                 order=c.euler_order)
    elif t == 'COPY_SCALE':
        s.update(axes=[c.use_x, c.use_y, c.use_z], offset=c.use_offset, add=c.use_add, power=c.power,
                 uniform=c.use_make_uniform)
    elif t == 'COPY_TRANSFORMS':
        s.update(mix=c.mix_mode, shear=c.remove_target_shear)
    elif t == 'CHILD_OF':
        s.update(loc=[c.use_location_x, c.use_location_y, c.use_location_z],
                 rot=[c.use_rotation_x, c.use_rotation_y, c.use_rotation_z],
                 scale=[c.use_scale_x, c.use_scale_y, c.use_scale_z], inv=c.inverse_matrix.copy())
        if not (all(s["loc"]) and all(s["rot"]) and all(s["scale"])):
            raise Unsupported("Child Of on some channels")
        if getattr(c, "set_inverse_pending", False):
            raise Unsupported("inverse pending")
    elif t == 'DAMPED_TRACK':
        s.update(axis=_TRACK[c.track_axis])
    elif t == 'TRACK_TO':
        s.update(axis=_TRACK[c.track_axis], up=_UP[c.up_axis], target_z=c.use_target_z)
    elif t == 'STRETCH_TO':
        if c.rest_length <= 0.0:
            raise Unsupported("rest length")
        s.update(rest=c.rest_length, bulge=c.bulge, vol=c.volume, plane=c.keep_axis,
                 use_max=c.use_bulge_max, use_min=c.use_bulge_min, bmax=c.bulge_max, bmin=c.bulge_min,
                 smooth=c.bulge_smooth)
    elif t == 'LIMIT_DISTANCE':
        if c.distance <= 0.0:
            raise Unsupported("distance")
        s.update(dist=c.distance, mode=c.limit_mode)
    elif t == 'LIMIT_LOCATION':
        s.update(lims=[(getattr(c, "use_min_" + a), getattr(c, "min_" + a), getattr(c, "use_max_" + a),
                        getattr(c, "max_" + a)) for a in "xyz"])
    elif t == 'LIMIT_SCALE':
        s.update(lims=[(getattr(c, "use_min_" + a), getattr(c, "min_" + a), getattr(c, "use_max_" + a),
                        getattr(c, "max_" + a)) for a in "xyz"])
    elif t == 'LIMIT_ROTATION':
        s.update(lims=[(getattr(c, "use_limit_" + a), getattr(c, "min_" + a), getattr(c, "max_" + a))
                       for a in "xyz"], order=c.euler_order, legacy=getattr(c, "use_legacy_behavior", False))
    elif t == 'TRANSFORM':
        sf = {'LOCATION': "", 'ROTATION': "_rot", 'SCALE': "_scale"}
        fr, to = c.map_from, c.map_to
        if fr == 'ROTATION' and c.from_rotation_mode not in _EULER | {'AUTO'}:
            raise Unsupported("rotation mode")

        def trip(name, suffix):
            return [getattr(c, "%s_%s%s" % (name, a, suffix)) for a in "xyz"]
        s.update(fr=fr, to=to, fmin=trip("from_min", sf[fr]), fmax=trip("from_max", sf[fr]),
                 tmin=trip("to_min", sf[to]), tmax=trip("to_max", sf[to]),
                 map=["XYZ".index(getattr(c, "map_to_%s_from" % a)) for a in "xyz"], expo=c.use_motion_extrapolate,
                 mix_loc=c.mix_mode, mix_rot=c.mix_mode_rot, mix_scale=c.mix_mode_scale, order=c.to_euler_order,
                 from_order=c.from_rotation_mode)
    elif t == 'ARMATURE':
        if c.use_bone_envelopes:
            raise Unsupported("envelopes")
        s.update(cur=c.use_current_location, vol=c.use_deform_preserve_volume)
        for tt in c.targets:
            if tt.weight <= 0.0:
                continue
            node, _ht, _o = _target(c, tt.target, tt.subtarget)
            if not node[1]:
                raise Unsupported("armature target without a bone")
            if _bone(node).use_envelope_multiply:
                raise Unsupported("envelope multiply")      # (its weight changes with where the owner is)
            k.targets.append((node, tt.weight, None))
        if not k.targets:
            raise Unsupported("no bones")
    else:
        raise Unsupported(t)
    return k


class Recipe:
    """How a bone / object is placed (see the module's notes): kind 'BONE' / 'OBJECT', what it reads (inputs)."""

    def __init__(self, node):
        self.node = node
        self.kind = 'BONE' if node[1] else 'OBJECT'
        self.cons = []
        self.inputs = []
        self.parent = None          # (a bone's parent bone, an object's parent node)
        self.order = 'XYZ'
        self.connected = False
        self.par_kind = None        # (an object: 'OBJECT', 'BONE', 'BONE_REL')
        self.vert = None            # (an object hung on a vertex: see _vertex_parent)

    def need(self, node):
        if node is not None and node not in self.inputs and node != self.node:
            self.inputs.append(node)


def _vertex_parent(r, o, p):
    """An object hung on one vertex of a mesh an armature bends (its only modifier, linear, by vertex groups, no
    shape keys): the vertex's rest place and the deform bones' weights on it."""
    if p.type != 'MESH' or p.data is None or p.data.shape_keys is not None:
        raise Unsupported("vertex parent")
    mods = [m for m in p.modifiers if m.show_viewport]
    if len(mods) != 1 or mods[0].type != 'ARMATURE':
        raise Unsupported("vertex parent modifiers")
    m = mods[0]
    a = m.object
    if a is None or a.type != 'ARMATURE' or a.pose is None or not m.use_vertex_groups or m.use_bone_envelopes or \
            m.use_deform_preserve_volume or m.use_multi_modifier or m.vertex_group:
        raise Unsupported("armature modifier settings")
    if getattr(p.data, "animation_data", None) is not None:
        raise Unsupported("animated mesh")
    vi = o.parent_vertices[0]
    if vi >= len(p.data.vertices):
        raise Unsupported("vertex")
    v = p.data.vertices[vi]
    names = {g.index: g.name for g in p.vertex_groups}
    weights = []
    for g in v.groups:
        b = a.data.bones.get(names.get(g.group, ""))
        if b is not None and b.use_deform and g.weight > 0.0:
            weights.append((b.name, g.weight))
    r.par_kind = 'VERT'
    r.parent = (p.name, "")
    r.vert = (a.name, v.co.copy(), weights)
    r.need((a.name, ""))
    for b, _w in weights:
        r.need((a.name, b))


def recipe(node):
    """The recipe of a bone / object (Unsupported: one it can't be done for)."""
    o = bpy.data.objects.get(node[0])
    if o is None:
        raise Unsupported("gone")
    r = Recipe(node)
    if node[1]:
        if o.type != 'ARMATURE' or o.pose is None:
            raise Unsupported("not a bone")
        pb = o.pose.bones.get(node[1])
        if pb is None:
            raise Unsupported("gone")
        owner, prefix = pb, 'pose.bones["%s"].' % bpy.utils.escape_identifier(node[1])
        r.order = _order(pb)
        r.connected = pb.bone.use_connect
        r.need((o.name, ""))
        if pb.parent is not None:
            r.parent = (o.name, pb.parent.name)
            r.need(r.parent)
    else:
        owner, prefix = o, ""
        r.order = _order(o)
        p = o.parent
        if p is not None:
            if o.parent_type in {'OBJECT', 'ARMATURE'}:
                r.par_kind, r.parent = 'OBJECT', (p.name, "")
            elif o.parent_type == 'BONE' and p.type == 'ARMATURE' and p.pose is not None and \
                    p.pose.bones.get(o.parent_bone) is not None:
                r.par_kind = 'BONE_REL' if p.pose.bones[o.parent_bone].bone.use_relative_parent else 'BONE'
                r.parent = (p.name, o.parent_bone)
                r.need((p.name, ""))
            elif o.parent_type == 'VERTEX':
                _vertex_parent(r, o, p)
            else:
                raise Unsupported("parent type")
            r.need(r.parent)
    spaces = BONE_SPACES if node[1] else OBJECT_SPACES
    for c in owner.constraints:
        if c.mute or not getattr(c, "enabled", True) or not c.is_valid:
            continue
        if c.type == 'IK':
            raise Unsupported("IK")
        k = _con(c, o, prefix)
        k.s["own_order"] = r.order
        if k.infl <= 0.0 and k.infl_path is None and k.infl_baked is None:
            continue
        if k.own not in spaces:
            raise Unsupported("owner space")
        if k.own == 'LOCAL' and not node[1] and r.parent is None:
            raise Unsupported("object local space without a parent")
        for dn in k.drv[0]:
            r.need(dn)
            if dn[1]:
                r.need((dn[0], ""))
        if k.space is not None:
            r.need(k.space)
            if k.space[1]:
                r.need((k.space[0], ""))
        for tn, _h, _o in k.targets:
            r.need(tn)
            if tn[1]:
                r.need((tn[0], ""))
            if k.type != 'ARMATURE':
                tob = bpy.data.objects[tn[0]]
                tsp = BONE_SPACES if tn[1] else OBJECT_SPACES
                if k.tar not in tsp:
                    raise Unsupported("target space")
                if k.tar == 'LOCAL':
                    if tn[1]:
                        tp = tob.pose.bones[tn[1]].parent
                        if tp is not None:
                            r.need((tn[0], tp.name))
                    elif tob.parent is None or tob.parent_type not in {'OBJECT', 'ARMATURE'}:
                        raise Unsupported("target local space")
                    else:
                        r.need((tob.parent.name, ""))
        r.cons.append(k)
    return r


# ----------------------------------------------------------------------------
# spaces (Blender's BKE_constraint_mat_convertspace)
# ----------------------------------------------------------------------------

def _bone(node):
    return bpy.data.objects[node[0]].data.bones[node[1]]


def _parent_pose(node, world):
    """A bone's parent: (its pose matrix, its rest matrix) - (None, None) without one."""
    b = _bone(node)
    if b.parent is None:
        return None, None
    arm = world((node[0], ""))
    return arm.inverted_safe() @ world((node[0], b.parent.name)), b.parent.matrix_local


def to_space(M, node, space, world, basis=None, k=None):
    """A world matrix of this bone / object into its constraint space."""
    if space == 'WORLD':
        return M
    if space == 'CUSTOM':
        return world(k.space).inverted_safe() @ M
    if node[1]:
        P = world((node[0], "")).inverted_safe() @ M
        if space == 'POSE':
            return P
        b = _bone(node)
        if space == 'LOCAL_WITH_PARENT':
            return b.matrix_local.inverted_safe() @ P
        pp, pr = _parent_pose(node, world)
        return b.convert_local_to_pose(P, b.matrix_local, parent_matrix=pp, parent_matrix_local=pr, invert=True) \
            if pp is not None else b.convert_local_to_pose(P, b.matrix_local, invert=True)
    return _object_local(node, world, basis).inverted_safe() @ M


def from_space(M, node, space, world, basis=None, k=None):
    if space == 'WORLD':
        return M
    if space == 'CUSTOM':
        return world(k.space) @ M
    if node[1]:
        if space == 'POSE':
            P = M
        elif space == 'LOCAL_WITH_PARENT':
            P = _bone(node).matrix_local @ M
        else:
            b = _bone(node)
            pp, pr = _parent_pose(node, world)
            P = b.convert_local_to_pose(M, b.matrix_local, parent_matrix=pp, parent_matrix_local=pr) \
                if pp is not None else b.convert_local_to_pose(M, b.matrix_local)
        return world((node[0], "")) @ P
    return _object_local(node, world, basis) @ M


def _object_local(node, world, basis):
    """What an object's Local constraint space is against the world: its parent's world matrix times the parent
    inverse (the armature's, not a parent bone's - as Blender does it); without a parent its own turn."""
    o = bpy.data.objects[node[0]]
    if o.parent is not None:
        return world((o.parent.name, "")) @ o.matrix_parent_inverse
    if basis is None:
        raise Unsupported("object local space")
    D = _normalized(basis)[0]
    D.translation = Vector()
    return D


def target_matrix(node, head_tail, space, world, k=None):
    """A constraint target's matrix in the target space (a bone: at head_tail along it)."""
    M = world(node)
    if node[1] and head_tail > 1e-6:
        arm = world((node[0], ""))
        P = arm.inverted_safe() @ M
        b = _bone(node)
        head = P.translation
        tail = head + Vector(P.col[1][:3]) * b.length
        P = P.copy()
        P.translation = head.lerp(tail, head_tail)
        M = arm @ P
    if space == 'WORLD':
        return M
    if space == 'CUSTOM':
        return world(k.space).inverted_safe() @ M
    if node[1]:
        P = world((node[0], "")).inverted_safe() @ M
        if space == 'POSE':
            return P
        b = _bone(node)
        if space == 'LOCAL_WITH_PARENT':
            return b.matrix_local.inverted_safe() @ P
        pp, pr = _parent_pose(node, world)
        return b.convert_local_to_pose(P, b.matrix_local, parent_matrix=pp, parent_matrix_local=pr, invert=True) \
            if pp is not None else b.convert_local_to_pose(P, b.matrix_local, invert=True)
    return _object_local(node, world, None).inverted_safe() @ M


# ----------------------------------------------------------------------------
# the constraints (Blender's *_evaluate, on the owner's matrix in its space)
# ----------------------------------------------------------------------------

def _copy_location(M, T, s, _r):
    M = M.copy()
    off = M.translation.copy() if s["offset"] else Vector()
    for i in range(3):
        if s["axes"][i]:
            v = T[i][3]
            if s["inv"][i]:
                v = -v
            M[i][3] = v + off[i]
    return M


def _copy_rotation(M, T, s, r):
    loc, _q, size = M.decompose()
    old = M.to_3x3().normalized()
    order = r.order if s["order"] == 'AUTO' else s["order"]
    obeul = M.to_3x3().to_euler(order)
    Tm = _ortho(T, True)
    eul = Tm.to_3x3().to_euler(order, obeul)
    mix = s["mix"]
    legacy = mix == 'OFFSET'
    defeul = obeul.copy() if mix in {'OFFSET', 'REPLACE'} else Euler((0, 0, 0), order)
    for i, ax in enumerate("XYZ"):
        if not s["axes"][i]:
            eul[i] = defeul[i]
        else:
            if legacy:
                eul.rotate_axis(ax, obeul[i])
            if s["inv"][i]:
                eul[i] = -eul[i]
    if mix == 'ADD':
        eul = Euler([a + b for a, b in zip(eul, obeul)], order)
    if legacy:
        eul.make_compatible(obeul)
    eul.make_compatible(obeul)
    new = eul.to_matrix()
    if mix == 'BEFORE':
        new = new @ old
    elif mix == 'AFTER':
        new = old @ new
    return Matrix.LocRotScale(loc, new, size)


def _copy_scale(M, T, s, _r):
    obsize = _size(M)
    size = _size(T)
    uni = s["uniform"]
    if uni:
        # (one factor for all three axes: the volume of the target's scale - or of the axes picked)
        if all(s["axes"]):
            total = abs(T.to_3x3().determinant())
        else:
            total = 1.0
            for i in range(3):
                if s["axes"][i]:
                    total *= size[i]
        avg = math.copysign(abs(total) ** (1.0 / 3.0), total)
        size = Vector((avg, avg, avg))
    size = Vector([math.pow(v, s["power"]) for v in size])
    if s["offset"]:
        if s["add"]:
            size = size + obsize - Vector((1, 1, 1))
        else:
            size = Vector([a * b for a, b in zip(size, obsize)])
    M = M.copy()
    cols = _cols(M)
    for i in range(3):
        if (s["axes"][i] or uni) and obsize[i] != 0.0:
            cols[i] = cols[i] * (size[i] / obsize[i])
    _set_cols(M, cols)
    return M


def _copy_transforms(M, T, s, _r):
    if s["shear"]:
        T = _ortho(T, False)
    return mix_transforms(T, M, s["mix"])


def loc_rot_size(M):
    """Blender's mat4_to_loc_rot_size: (location, the 3x3 axes made unit - turned over with a negative determinant -,
    their lengths)."""
    cols = _cols(M)
    size = Vector([c.length for c in cols])
    R = Matrix.Identity(3)
    _set_cols(R, [c / s if s > 1e-35 else Vector() for c, s in zip(cols, size)])
    if R.determinant() < 0.0:
        R = R * -1.0
        size = -size
    return M.translation.copy(), R, size


def loc_rot_size_matrix(loc, R, size):
    """Blender's loc_rot_size_to_mat4."""
    M = R.copy()
    _set_cols(M, [Vector(M.col[i]) * size[i] for i in range(3)])
    M = M.to_4x4()
    M.translation = loc
    return M


def mix_transforms(T, M, mix):
    """Copy Transforms: the target's matrix T mixed with the owner's M as its Mix says (Blender's
    translike_evaluate): Replace; Before / After Original - Full (one matrix times the other), Aligned (location as
    Full, rotation and scale each on their own), Split Channels (location added too)."""
    if mix == 'REPLACE':
        return T.copy()
    if mix == 'BEFORE_FULL':
        return T @ M
    if mix == 'AFTER_FULL':
        return M @ T
    A, B = (T, M) if mix in {'BEFORE', 'BEFORE_SPLIT'} else (M, T)
    la, ra, sa = loc_rot_size(A)
    lb, rb, sb = loc_rot_size(B)
    loc = A @ lb if mix in {'BEFORE', 'AFTER'} else la + lb
    return loc_rot_size_matrix(loc, ra @ rb, Vector([x * y for x, y in zip(sa, sb)]))

def _child_of(M, T, s, k):
    inv = s["inv"]
    if all(s["loc"]) and all(s["rot"]) and all(s["scale"]):
        P = T
    else:
        # (the channels left out: taken out of the target's matrix and of the inverse alike)
        order, oorder = k.targets[0][2], s["own_order"]
        loc, eul, size = T.translation.copy(), T.to_3x3().to_euler(order), _size(T)
        loco, eulo, sizeo = inv.translation.copy(), inv.to_3x3().to_euler(oorder), _size(inv)
        for i in range(3):
            if not s["loc"][i]:
                loc[i] = loco[i] = 0.0
            if not s["rot"][i]:
                eul[i] = eulo[i] = 0.0
            if not s["scale"][i]:
                size[i] = sizeo[i] = 1.0
        P = Matrix.LocRotScale(loc, eul, size)
        inv = Matrix.LocRotScale(loco, eulo, sizeo)
    return P @ inv @ M


def _damped_track(M, T, s, _r):
    tarvec = T.translation - M.translation
    if tarvec.length == 0.0:
        return M
    return _damp(M, tarvec.normalized(), s["axis"])


def _damp(M, tarvec, axis):
    obvec = M.to_3x3() @ _DIRS[axis]
    if obvec.length == 0.0:
        obvec = _DIRS[axis].copy()
    obvec.normalize()
    obloc = M.translation.copy()
    raxis = obvec.cross(tarvec)
    rangle = math.acos(max(-1.0, min(1.0, obvec.dot(tarvec))))
    norm = raxis.length
    if norm < 1.2e-7:
        if abs(rangle) < math.pi - 0.01:
            return M
        rangle = math.pi
        tmp = M.to_3x3() @ _DIRS[(axis + 1) % 6]
        raxis = obvec.cross(tmp)
        if raxis.length == 0.0:
            return M
        raxis.normalize()
    else:
        if norm < 0.1:
            rangle = math.pi - math.asin(norm) if rangle > math.pi / 2 else math.asin(norm)
        raxis = raxis / norm
    R = Matrix.Rotation(rangle, 3, raxis)
    out = (R @ M.to_3x3()).to_4x4()
    out.translation = obloc
    return out


def _basis_cross(n, m):
    d = n - m
    if d in (1, -2):
        return 1
    if d in (-1, 2):
        return -1
    return 0


def _vectomat(vec, target_up, axis, upflag, target_z):
    n = vec.normalized() if vec.length > 0.0 else Vector((0, 0, 1))
    if axis > 2:
        axis -= 3
    else:
        n = -n
    u = target_up.copy() if target_z else Vector((0, 0, 1))
    proj = u - n * (u.dot(n) / n.dot(n))
    if proj.length == 0.0:
        proj = Vector((0, 1, 0))
    else:
        proj.normalize()
    right = proj.cross(n)
    if right.length > 0.0:
        right.normalize()
    m = Matrix.Identity(3)
    if axis != upflag:
        ri = 3 - axis - upflag
        neg = float(_basis_cross(axis, upflag))
        cols = [None, None, None]
        cols[ri] = right * neg
        cols[upflag] = proj
        cols[axis] = n
        for i in range(3):
            for j in range(3):
                m[j][i] = cols[i][j]
    return m


def _track_to(M, T, s, _r):
    size = _size(M)
    D = Matrix.Diagonal(size).to_4x4()
    D.translation = M.translation
    vec = M.translation - T.translation
    tot = _vectomat(vec, Vector(T.col[2][:3]), s["axis"], s["up"], s["target_z"])
    out = tot.to_4x4() @ D
    out.translation = M.translation
    return out


def _stretch_to(M, T, s, _r):
    if s["plane"] == 'SWING_Y':
        M = _ortho(M, False)
    N, size = _normalized(M)
    xx, zz = Vector(N.col[0][:3]), Vector(N.col[2][:3])
    vec = T.translation - N.translation
    dist = vec.length
    if dist > 0.0:
        vec = vec / dist
    dist /= size[1]
    rest = s["rest"]
    scale = [1.0, dist / rest, 1.0]
    bulge = math.pow(rest / dist, s["bulge"]) if dist > 0.0 else 1.0
    if bulge > 1.0 and s["use_max"]:
        bmax = max(s["bmax"], 1.0)
        hard = min(bulge, bmax)
        rng = bmax - 1.0
        f = 1.0 / rng if rng > 0.0 else 0.0
        soft = 1.0 + rng * math.atan((bulge - 1.0) * f) / (math.pi / 2)
        bulge = s["smooth"] * soft + (1.0 - s["smooth"]) * hard
    if bulge < 1.0 and s["use_min"]:
        bmin = min(max(s["bmin"], 0.0), 1.0)
        hard = max(bulge, bmin)
        rng = 1.0 - bmin
        f = 1.0 / rng if rng > 0.0 else 0.0
        soft = 1.0 - rng * math.atan((1.0 - bulge) * f) / (math.pi / 2)
        bulge = s["smooth"] * soft + (1.0 - s["smooth"]) * hard
    vol = s["vol"]
    if vol == 'VOLUME_XZX':
        scale[0] = scale[2] = math.sqrt(bulge)
    elif vol == 'VOLUME_X':
        scale[0] = bulge
    elif vol == 'VOLUME_Z':
        scale[2] = bulge
    size = Vector([a * b for a, b in zip(size, scale)])
    plane = s["plane"]
    if plane == 'SWING_Y':
        y = Vector(N.col[1][:3])
        R = y.rotation_difference(vec).to_matrix()
        x, y2, z = R @ xx, vec, R @ zz
    elif plane == 'PLANE_X':
        orth = xx.cross(vec).normalized()
        z = orth
        x = vec.cross(orth).normalized()
        y2 = vec
    else:
        orth = zz.cross(vec).normalized()
        x = -orth
        z = x.cross(vec).normalized()
        y2 = vec
    out = N.copy()
    _set_cols(out, [x * size[0], y2 * size[1], z * size[2]])
    return out


def _limit_distance(M, T, s, _r):
    p, t = M.translation, T.translation
    dist = (p - t).length
    d = s["dist"]
    sfac = 1.0
    clamp = False
    if s["mode"] == 'LIMITDIST_OUTSIDE':
        if dist <= d:
            clamp = True
            if dist != 0.0:
                sfac = d / dist
    elif s["mode"] == 'LIMITDIST_INSIDE':
        if dist >= d:
            clamp = True
            if dist != 0.0:
                sfac = d / dist
    else:
        if abs(dist - d) > 1e-6:
            clamp = True
            if dist != 0.0:
                sfac = d / dist
    if not clamp:
        return M
    M = M.copy()
    M.translation = t.lerp(p, sfac)
    return M


def _limit_location(M, _T, s, _r):
    M = M.copy()
    for i, (umin, vmin, umax, vmax) in enumerate(s["lims"]):
        if umin and M[i][3] < vmin:
            M[i][3] = vmin
        if umax and M[i][3] > vmax:
            M[i][3] = vmax
    return M


def _limit_scale(M, _T, s, _r):
    obsize = _size(M)
    size = obsize.copy()
    for i, (umin, vmin, umax, vmax) in enumerate(s["lims"]):
        if umin and size[i] < vmin:
            size[i] = vmin
        if umax and size[i] > vmax:
            size[i] = vmax
    M = M.copy()
    cols = _cols(M)
    _set_cols(M, [c * (size[i] / obsize[i]) if obsize[i] != 0.0 else c for i, c in enumerate(cols)])
    return M


def _limit_rotation(M, _T, s, r):
    # (as Blender: the shear taken out without making the axes unit - the size is read from that, the turn from
    # it made unit)
    Mo = _ortho(M, False)
    loc = M.translation.copy()
    size = _size(Mo)
    order = r.order if s["order"] == 'AUTO' else s["order"]
    eul = Mo.to_3x3().normalized().to_euler(order)
    for i, (use, lo, hi) in enumerate(s["lims"]):
        if use:
            eul[i] = min(max(eul[i], lo), hi) if s["legacy"] else _clamp_angle(eul[i], lo, hi)
    return Matrix.LocRotScale(loc, eul, size)


def _transformation(M, T, s, r):
    """Blender's Transformation constraint: a channel of the target mapped from one range to another onto a
    channel of the owner."""
    fr = s["fr"]
    if fr == 'SCALE':
        dvec = _size(T)
        if T.to_3x3().determinant() < 0.0:
            dvec = -dvec
    elif fr == 'ROTATION':
        dvec = Vector(T.to_3x3().to_euler(r.order if s["from_order"] == 'AUTO' else s["from_order"]))
    else:
        dvec = T.translation.copy()
    sval = []
    for i in range(3):
        d, lo, hi = dvec[i], s["fmin"][i], s["fmax"][i]
        if not s["expo"]:
            d = lo if d < lo else (hi if d > hi else d)
        sval.append((d - lo) / (hi - lo) if hi - lo else 0.0)
    loc, _q, size = M.decompose()
    rot = M.to_3x3().normalized()
    new = [s["tmin"][i] + sval[s["map"][i]] * (s["tmax"][i] - s["tmin"][i]) for i in range(3)]
    to = s["to"]
    if to == 'SCALE':
        size = Vector(new) if s["mix_scale"] == 'REPLACE' else Vector([a * b for a, b in zip(size, new)])
    elif to == 'ROTATION':
        order = r.order if s["order"] == 'AUTO' else s["order"]
        mix = s["mix_rot"]
        nr = Euler(new, order).to_matrix()
        if mix == 'REPLACE':
            rot = nr
        elif mix == 'BEFORE':
            rot = nr @ rot
        elif mix == 'AFTER':
            rot = rot @ nr
        else:
            old = rot.to_euler(order)
            rot = Euler([a + b for a, b in zip(new, old)], order).to_matrix()
    else:
        loc = Vector(new) if s["mix_loc"] == 'REPLACE' else loc + Vector(new)
    return Matrix.LocRotScale(loc, rot, size)


_EVAL = {'TRANSFORM': _transformation, 'COPY_LOCATION': _copy_location, 'COPY_ROTATION': _copy_rotation, 'COPY_SCALE': _copy_scale,
         'COPY_TRANSFORMS': _copy_transforms, 'CHILD_OF': _child_of, 'DAMPED_TRACK': _damped_track,
         'TRACK_TO': _track_to, 'STRETCH_TO': _stretch_to, 'LIMIT_DISTANCE': _limit_distance,
         'LIMIT_LOCATION': _limit_location, 'LIMIT_SCALE': _limit_scale, 'LIMIT_ROTATION': _limit_rotation}


def _armature(M, k, world, r):
    """Blender's Armature constraint: the target bones' deform matrices (pose @ rest^-1, in the world) mixed by
    their weights - linearly, or with Preserve Volume as dual quaternions (armdef_evaluate) - in front of the
    owner."""
    if k.s["vol"]:
        return _armature_dq(M, k, world, r)
    S = None
    total = 0.0
    for node, w, _o in k.targets:
        aw = world((node[0], ""))
        P = aw.inverted_safe() @ world(node)
        D = aw @ P @ _bone(node).matrix_local.inverted_safe() @ aw.inverted_safe()
        S = D * w if S is None else S + D * w
        total += w
    if S is None or total <= 1e-4:
        return M
    S = S * (1.0 / total)
    return S @ M


# Dual quaternions as Blender's math_rotation does them (DualQuat): [quat (w, x, y, z), trans (4), scale matrix,
# scale weight]

def _orthonormal(m3):
    """Blender's is_orthonormal_m3 (its axes: unit and perpendicular, to 1e-5) - (the answer, how near a bound)."""
    cols = [Vector(m3.col[i]) for i in range(3)]
    near = 1.0
    for i in range(3):
        for j in range(i):
            d = abs(cols[i].dot(cols[j]))
            near = min(near, abs(d - 1e-5))
            if d > 1e-5:
                return False, near
    for i in range(3):
        d = abs(cols[i].dot(cols[i]) - 1.0)
        near = min(near, abs(d - 1e-5))
        if d > 1e-5:
            return False, near
    return True, near


def _ortho_y(M):
    """Blender's orthogonalize_m4(M, 1): Y made unit, X / Z made perpendicular to it by cross products, then all
    three given their lengths back."""
    size = _size(M)
    x, y, z = _cols(M)
    y = y.normalized()
    if y.dot(x) < 1.0:
        z = x.cross(y).normalized()
        x = y.cross(z)
    elif x.dot(z) < 1.0:
        x = y.cross(z).normalized()
        z = x.cross(y)
    else:
        x = y.cross(Vector((y[1], y[2], y[0]))).normalized()
        z = x.cross(y)
    N = M.copy()
    _set_cols(N, [x * size[0], y * size[1], z * size[2]])
    return N


def _mat_to_dq(base, mat):
    """Blender's mat4_to_dquat: the deform matrix `mat` of a bone at rest `base` (orthonormal, in the world) as a
    dual quaternion - its scale (in the bone's rest axes) kept apart as a matrix."""
    baseRS = mat @ base
    dscale = _size(baseRS) - Vector((1.0, 1.0, 1.0))
    ortho, near = _orthonormal(mat.to_3x3())
    ds = dscale.length_squared
    if near < 2e-6 or (ortho and abs(ds - 1e-8) < 4e-10):
        raise Unsupported("dual quaternion on the edge")     # (Blender's float math may take the other branch)
    if mat.determinant() < 0.0:
        raise Unsupported("negative scale")
    if not ortho or ds > 1e-8:
        tmp = _ortho_y(baseRS)
        baseR = tmp.to_quaternion().to_matrix().to_4x4()
        baseR.translation = baseRS.translation
        baseinv = base.inverted_safe()
        R = baseR @ baseinv
        S = baseR.inverted_safe() @ baseRS
        scale = base @ S @ baseinv
        sw = 1.0
    else:
        R = mat
        scale = Matrix.Identity(4)
        sw = 0.0
    q = R.to_quaternion()
    t = R.translation
    trans = [-0.5 * (t[0] * q[1] + t[1] * q[2] + t[2] * q[3]),
             0.5 * (t[0] * q[0] + t[1] * q[3] - t[2] * q[2]),
             0.5 * (-t[0] * q[3] + t[1] * q[0] + t[2] * q[1]),
             0.5 * (t[0] * q[2] - t[1] * q[1] + t[2] * q[0])]
    return [list(q), trans, scale, sw]


def _dq_add(acc, dq, pivot, w):
    """Blender's add_weighted_dq_dq_pivot (its scale matrix taken about the pivot, the rest of it carried by the
    dual quaternion) and add_weighted_dq_dq: a weighted dual quaternion added to the sum (the shorter way round)."""
    q, trans, scale, sw = dq
    if sw:
        d = scale @ pivot - pivot
        trans = [trans[0] - 0.5 * (q[1] * d[0] + q[2] * d[1] + q[3] * d[2]),
                 trans[1] + 0.5 * (q[0] * d[0] + q[2] * d[2] - q[3] * d[1]),
                 trans[2] + 0.5 * (q[0] * d[1] + q[3] * d[0] - q[1] * d[2]),
                 trans[3] + 0.5 * (q[0] * d[2] + q[1] * d[1] - q[2] * d[0])]
        scale = scale.copy()
        scale.translation = scale.translation - d
    flipped = sum(a * b for a, b in zip(q, acc[0])) < 0.0
    ws = -w if flipped else w
    acc[0] = [a + ws * b for a, b in zip(acc[0], q)]
    acc[1] = [a + ws * b for a, b in zip(acc[1], trans)]
    if sw:
        acc[2] = acc[2] + scale * w
        acc[3] += w


def _dq_to_mat(acc, total):
    """Blender's normalize_dq and dquat_to_mat4: the sum divided by the weights, back to a matrix (the scale
    matrix of the ones without one counted as identity)."""
    f = 1.0 / total
    q = [a * f for a in acc[0]]
    t = [a * f for a in acc[1]]
    scale, sw = acc[2], acc[3]
    if sw:
        add = total - sw
        if add:
            scale = scale + Matrix.Identity(4) * add
        scale = scale * f
    n = math.sqrt(sum(a * a for a in q))
    if n != 0.0:
        n = 1.0 / n
    q0 = [a * n for a in q]
    R = Quaternion(q0).to_matrix().to_4x4()
    R[0][3] = 2.0 * (-t[0] * q0[1] + t[1] * q0[0] - t[2] * q0[3] + t[3] * q0[2]) * n
    R[1][3] = 2.0 * (-t[0] * q0[2] + t[1] * q0[3] + t[2] * q0[0] - t[3] * q0[1]) * n
    R[2][3] = 2.0 * (-t[0] * q0[3] - t[1] * q0[2] + t[2] * q0[1] + t[3] * q0[0]) * n
    return R @ scale if sw else R


def _armature_dq(M, k, world, r):
    """armdef_evaluate with Preserve Volume: about the owner's rest head (a bone; Use Current Location: where it
    is), each bone's deform matrix against its rest in the world (made orthonormal) as a dual quaternion."""
    if r.kind == 'BONE' and not k.s["cur"]:
        pivot = world((r.node[0], "")) @ _bone(r.node).head_local
    else:
        pivot = M.translation.copy()
    acc = [[0.0] * 4, [0.0] * 4, Matrix(((0.0,) * 4,) * 4), 0.0]
    total = 0.0
    for node, w, _o in k.targets:
        aw = world((node[0], ""))
        mat = world(node) @ _bone(node).matrix_local.inverted_safe() @ aw.inverted_safe()
        base = _ortho(aw @ _bone(node).matrix_local, True)
        _dq_add(acc, _mat_to_dq(base, mat), pivot, w)
        total += w
    if total <= 1e-4:
        raise Unsupported("weights")        # (all but none: where Blender draws the line isn't followed here)
    return _dq_to_mat(acc, total) @ M


# ----------------------------------------------------------------------------
# evaluating a recipe
# ----------------------------------------------------------------------------

def influence(k, f, values):
    """A constraint's influence on frame f: keyed (its curve), driven (as baked: values), else its setting."""
    if k.infl_baked is not None:
        v = values.get(k.infl_baked)
        if v is None:
            raise Unsupported("driven influence not baked")
        return v(f)
    if k.infl_path is not None:
        from . import compat
        coll = compat.get_fcurves(bpy.data.objects[k.ob])
        fc = coll.find(k.infl_path) if coll is not None else None
        return min(1.0, max(0.0, fc.evaluate(f))) if fc is not None else k.infl
    return k.infl


def pre_matrix(r, world, basis):
    """Where a bone / object is before its constraints, in the world."""
    if r.kind == 'BONE':
        arm = world((r.node[0], ""))
        b = _bone(r.node)
        pp, pr = _parent_pose(r.node, world)
        P = b.convert_local_to_pose(basis, b.matrix_local, parent_matrix=pp, parent_matrix_local=pr) \
            if pp is not None else b.convert_local_to_pose(basis, b.matrix_local)
        return arm @ P
    o = bpy.data.objects[r.node[0]]
    if r.parent is None:
        return basis.copy()
    if r.par_kind == 'VERT':
        # (the vertex where the armature bends it: its weighted deform matrices, linear - Blender's Armature
        # modifier; the object's parent matrix is only that point)
        arm_name, co, weights = r.vert
        A = world((arm_name, ""))
        Ai = A.inverted_safe()
        co_a = Ai @ world(r.parent) @ co
        acc, tot = Vector(), 0.0
        for b, w in weights:
            D = Ai @ world((arm_name, b)) @ bpy.data.objects[arm_name].data.bones[b].matrix_local.inverted_safe()
            acc += (D @ co_a) * w
            tot += w
        v = A @ (acc / tot if tot > 1e-4 else co_a)
        return Matrix.Translation(v) @ o.matrix_parent_inverse @ basis
    if r.par_kind == 'OBJECT':
        par = world(r.parent)
    else:
        arm = world((r.parent[0], ""))
        pose = arm.inverted_safe() @ world(r.parent)
        if r.par_kind == 'BONE_REL':
            raise Unsupported("relative parenting")
        pose = pose.copy()
        pose.translation = pose.translation + Vector(pose.col[1][:3]) * _bone(r.parent).length
        par = arm @ pose
    return par @ o.matrix_parent_inverse @ basis


def evaluate(r, f, world, basis, values):
    """The world matrix of the bone / object of recipe r on frame f: world(node) the matrices it reads, basis its
    own channels' matrix, values the driven influences baked."""
    M = pre_matrix(r, world, basis)
    head = M.translation.copy()
    for k in r.cons:
        w = influence(k, f, values)
        if w <= 0.0:
            continue
        old = M
        S = to_space(M, r.node, k.own, world, basis, k)
        if k.type == 'ARMATURE':
            S = _armature(S, k, world, r)
        else:
            T = target_matrix(k.targets[0][0], k.targets[0][1], k.tar, world, k) if k.targets else None
            S = _EVAL[k.type](S, T, k.s, k if k.type == 'CHILD_OF' else r)
        # (Child Of's result is taken as it is, not put back from the owner's space - measured, every pair of
        # spaces: Blender evaluates it "space once")
        M = S if k.type == 'CHILD_OF' else from_space(S, r.node, k.own, world, basis, k)
        if w < 1.0:
            M = old.lerp(M, w)
    if r.kind == 'BONE' and r.connected and r.cons:
        M = M.copy()
        M.translation = head
    return M


def close(A, B, size):
    """Two world matrices alike to CHECK_EPS (relative to the size of what they place)."""
    tol = CHECK_EPS * max(1.0, size)
    return max(abs(A[i][j] - B[i][j]) for i in range(4) for j in range(4)) <= tol
