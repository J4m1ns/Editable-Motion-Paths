"""Path evaluation core.

A path is evaluated as  world(f) = B(f) @ basis(f)  where
  * B(f)      is the "parent space" of the target at frame f (everything except the
              target's own loc/rot/scale), baked once by stepping through frames;
  * basis(f)  is rebuilt from the target's own F-Curves on every refresh.

Because only B is baked, the path follows F-Curve edits in real time, and edits made
in world space can be mapped back to channel values exactly (location) or by a
minimal swing rotation (rotation).
"""

import math
from array import array

import bpy
from mathutils import Euler, Matrix, Quaternion, Vector

from . import compat, state

LOCATION = 'LOCATION'
ROTATION = 'ROTATION'
SCALE = 'SCALE'
VALUE = 'VALUE'
VALUE_HEIGHT = 0.3      # value track: the value range spans this part of the path size
EPS = 1e-6
FRAME_ROUND = 3


# ----------------------------------------------------------------------------
# Rotation helpers
# ----------------------------------------------------------------------------

def rotation_prop(mode):
    if mode == 'QUATERNION':
        return "rotation_quaternion", 4
    if mode == 'AXIS_ANGLE':
        return "rotation_axis_angle", 4
    return "rotation_euler", 3


def vals_to_quat(vals, mode):
    if mode == 'QUATERNION':
        q = Quaternion(vals[:4])
        if q.magnitude < EPS:
            return Quaternion()
        return q.normalized()
    if mode == 'AXIS_ANGLE':
        axis = Vector(vals[1:4])
        if axis.length < EPS:
            return Quaternion()
        return Quaternion(axis.normalized(), vals[0])
    return Euler(vals[:3], mode).to_quaternion()


def quat_to_vals(q, mode, ref):
    """Convert quaternion to channel values, staying continuous with `ref` values."""
    if mode == 'QUATERNION':
        if Quaternion(ref[:4]).dot(q) < 0.0:
            q = -q
        return [q.w, q.x, q.y, q.z]
    if mode == 'AXIS_ANGLE':
        axis, angle = q.to_axis_angle()
        if Vector(ref[1:4]).dot(axis) < 0.0:
            axis = -axis
            angle = -angle
        while angle - ref[0] > math.pi:
            angle -= 2.0 * math.pi
        while angle - ref[0] < -math.pi:
            angle += 2.0 * math.pi
        return [angle, axis.x, axis.y, axis.z]
    return list(q.to_euler(mode, Euler(ref[:3], mode)))


# ----------------------------------------------------------------------------
# Live access to a target's channels (never stored across calls: undo frees RNA)
# ----------------------------------------------------------------------------

def resolve(obj_name, bone_name):
    obj = bpy.data.objects.get(obj_name)
    if obj is None:
        return None, None
    if bone_name:
        if obj.type != 'ARMATURE' or obj.pose is None:
            return obj, None
        return obj, obj.pose.bones.get(bone_name)
    return obj, obj


class Channels:
    """The F-Curves of one transform property (location / rotation / scale)."""

    __slots__ = ("prop", "path", "count", "fcurves", "live", "static")

    def __init__(self, coll, prefix, owner, prop, count):
        self.prop = prop
        self.path = prefix + prop
        self.count = count
        self.fcurves = [coll.find(self.path, index=i) if coll is not None else None
                        for i in range(count)]
        self.live = [fc is not None and not fc.mute and len(fc.keyframe_points) > 0
                     for fc in self.fcurves]
        self.static = list(getattr(owner, prop))

    def values(self, t):
        return [fc.evaluate(t) if live else s
                for fc, live, s in zip(self.fcurves, self.live, self.static)]

    def any(self):
        return any(fc is not None and len(fc.keyframe_points) for fc in self.fcurves)


class ValueChannel:
    """One animated property (custom property, constraint influence...) as a channel set."""

    __slots__ = ("prop", "path", "count", "fcurves", "live", "static", "index")

    def __init__(self, coll, data_path, index, frame):
        self.prop = data_path
        self.path = data_path
        self.index = index
        self.count = 1
        fc = coll.find(data_path, index=index) if coll is not None and data_path else None
        self.fcurves = [fc]
        self.live = [fc is not None and not fc.mute and len(fc.keyframe_points) > 0]
        self.static = [fc.evaluate(frame) if fc is not None else 0.0]

    def values(self, t):
        fc = self.fcurves[0]
        return [fc.evaluate(t)] if self.live[0] else list(self.static)

    def any(self):
        fc = self.fcurves[0]
        return fc is not None and len(fc.keyframe_points) > 0


_TRANSFORMS = {"location", "rotation_euler", "rotation_quaternion", "rotation_axis_angle", "scale",
               "delta_location", "delta_rotation_euler", "delta_rotation_quaternion", "delta_scale"}


def value_candidates(obj, bone):
    """[(data_path, index, label)] of the animated values that are not transforms, the bone's
    own first."""
    coll = compat.get_fcurves(obj) if obj is not None else None
    if coll is None:
        return []
    prefix = 'pose.bones["%s"]' % bpy.utils.escape_identifier(bone) if bone else ""
    own, other = [], []
    for fc in coll:
        dp = fc.data_path
        if dp.rsplit('.', 1)[-1] in _TRANSFORMS or not len(fc.keyframe_points):
            continue
        label = dp.replace('pose.bones[', '').replace('"]', '').replace('["', ' > ').replace('"', '')
        if fc.array_index or dp.endswith(('color', 'location')):
            label += " [%d]" % fc.array_index
        item = (dp, fc.array_index, label)
        (own if prefix and dp.startswith(prefix) else other).append(item)
    return own + other


class Target:
    """Resolved bone or object with its animation channels."""

    def __init__(self, obj, owner, is_bone, value=None):
        self.obj = obj
        self.owner = owner
        self.is_bone = is_bone
        if is_bone:
            self.prefix = 'pose.bones["%s"].' % bpy.utils.escape_identifier(owner.name)
            self.group_name = owner.name
        else:
            self.prefix = ""
            self.group_name = "Object Transforms"
        self.coll = compat.get_fcurves(obj)
        self.rot_mode = owner.rotation_mode
        rp, rn = rotation_prop(self.rot_mode)
        self.loc = Channels(self.coll, self.prefix, owner, "location", 3)
        self.rot = Channels(self.coll, self.prefix, owner, rp, rn)
        self.scale = Channels(self.coll, self.prefix, owner, "scale", 3)
        self.value = ValueChannel(self.coll, value[0], value[1], bpy.context.scene.frame_current) \
            if value else ValueChannel(None, "", 0, 0)
        if is_bone:
            self.length = owner.bone.length
            self.dloc = None
            self.drot = None
            self.dscale = None
        else:
            self.length = 1.0
            self.dloc = Vector(owner.delta_location)
            self.dscale = Vector(owner.delta_scale)
            if self.rot_mode == 'QUATERNION':
                self.drot = Quaternion(owner.delta_rotation_quaternion).normalized()
            elif self.rot_mode == 'AXIS_ANGLE':
                self.drot = Quaternion()
            else:
                self.drot = Euler(owner.delta_rotation_euler, self.rot_mode).to_quaternion()

    def channels(self, ptype):
        if ptype == LOCATION:
            return self.loc
        if ptype == ROTATION:
            return self.rot
        if ptype == SCALE:
            return self.scale
        return self.value

    def scale_locks(self):
        return tuple(self.owner.lock_scale)

    def locks(self, ptype):
        if ptype == LOCATION:
            return self.loc_locks()
        if ptype == ROTATION:
            return self.rot_locks()
        if ptype == SCALE:
            return self.scale_locks()
        return (False,)

    def basis(self, loc, rot, scale):
        q = vals_to_quat(rot, self.rot_mode)
        loc = Vector(loc)
        scale = Vector(scale)
        if self.dloc is not None:
            loc = loc + self.dloc
            q = self.drot @ q
            scale = Vector((scale[0] * self.dscale[0], scale[1] * self.dscale[1], scale[2] * self.dscale[2]))
        return Matrix.LocRotScale(loc, q, scale)

    def loc_locks(self):
        return tuple(self.owner.lock_location)

    def rot_locks(self):
        """Per-channel locks of the rotation channels (euler only, quaternion is free)."""
        if self.rot.count == 3:
            return tuple(self.owner.lock_rotation)
        return (False,) * self.rot.count


# ----------------------------------------------------------------------------
# Key data
# ----------------------------------------------------------------------------

class KeyData:
    __slots__ = ("t", "idx", "vals", "co", "hl", "hr", "dt_l", "dt_r", "s_l", "s_r",
                 "vp", "sel", "sel_l", "sel_r", "type_l", "type_r", "interp", "dir_l", "dir_r",
                 "partial", "cat_l", "cat_r", "offs", "hidden", "piece")

    def __init__(self, t):
        self.t = t
        self.idx = []
        self.vals = None
        self.co = None
        self.hl = None
        self.hr = None
        self.dt_l = 0.0
        self.dt_r = 0.0
        self.s_l = None
        self.s_r = None
        self.vp = Vector()
        self.sel = False
        self.sel_l = False
        self.sel_r = False
        self.type_l = 'AUTO_CLAMPED'
        self.type_r = 'AUTO_CLAMPED'
        self.interp = 'BEZIER'
        self.dir_l = Vector((-1.0, 0.0, 0.0))
        self.dir_r = Vector((1.0, 0.0, 0.0))
        self.partial = False
        self.cat_l = Vector()   # "natural" handles from the neighbors (Catmull-Rom tangent),
        self.cat_r = Vector()   # used when a flat (zero length) handle gets rotated or scaled
        self.offs = None        # per channel: evaluated value minus stored key value (cycle offset)
        self.hidden = False     # hidden with H: not drawn, not selectable
        self.piece = 0          # keys joined by visible segments share a piece (Ctrl+L, L)

    @property
    def any_sel(self):
        return self.sel or self.sel_l or self.sel_r


def cycle_info(fc):
    """(first, last) key frames if the F-Curve loops with a Cycles modifier, else None."""
    if fc is None or len(fc.keyframe_points) < 2:
        return None
    for m in fc.modifiers:
        if m.type == 'CYCLES' and not m.mute:
            kps = fc.keyframe_points
            first, last = kps[0].co[0], kps[-1].co[0]
            if last - first > EPS:
                return first, last
    return None


def wrap_time(t, cyc):
    """Frame inside the key range of a looping F-Curve that shows the same pose as t."""
    if cyc is None:
        return t
    first, last = cyc
    if first - 1e-3 <= t <= last + 1e-3:
        return t
    return first + ((t - first) % (last - first))


cycle_skip = set()      # key frames edited by the running operator (see _sync_cycle_partner)


def _sync_cycle_partner(fc, idx, d):
    """First and last key of a loop are the same pose: move the other one too."""
    if fc is None or abs(d) < 1e-12 or cycle_info(fc) is None:
        return
    kps = fc.keyframe_points
    n = len(kps)
    if idx == 0:
        j = n - 1
    elif idx == n - 1:
        j = 0
    else:
        return
    kp = kps[j]
    if round(kp.co[0], FRAME_ROUND) in cycle_skip:
        return
    x, y = kp.co
    hl, hr = tuple(kp.handle_left), tuple(kp.handle_right)
    kp.co = (x, y + d)
    kp.handle_left = (hl[0], hl[1] + d)
    kp.handle_right = (hr[0], hr[1] + d)


def _fc_signature(fc):
    if fc is None:
        return None
    kps = fc.keyframe_points
    n = len(kps)
    co = array('f', bytes(8 * n))
    hl = array('f', bytes(8 * n))
    hr = array('f', bytes(8 * n))
    kps.foreach_get("co", co)
    kps.foreach_get("handle_left", hl)
    kps.foreach_get("handle_right", hr)
    s0 = [False] * n
    s1 = [False] * n
    s2 = [False] * n
    kps.foreach_get("select_control_point", s0)
    kps.foreach_get("select_left_handle", s1)
    kps.foreach_get("select_right_handle", s2)
    return (fc.mute, n, len(fc.modifiers), co.tobytes(), hl.tobytes(), hr.tobytes(),
            tuple(s0), tuple(s1), tuple(s2))


# ----------------------------------------------------------------------------
# Path cache
# ----------------------------------------------------------------------------

def _lock_state(tg, ptype):
    if ptype == LOCATION:
        if tg.is_bone and tg.owner.bone.use_connect:
            return True, "Connected bone: its location can't move. Press 2 to edit Rotation"
        if all(tg.loc_locks()):
            return True, "Location is locked. Press 2 to edit Rotation"
    elif ptype == ROTATION and tg.rot.count == 3 and all(tg.rot_locks()):
        return True, "Rotation is locked. Press 1 to edit Location"
    elif ptype == SCALE and all(tg.scale_locks()):
        return True, "Scale is locked. Press 1 to edit Location"
    elif ptype == VALUE and not tg.value.any():
        return True, "Pick an animated value to edit (sidebar > Motion Paths > Value)"
    return False, ""


class PathCache:
    def __init__(self, obj_name, bone_name, ptype):
        self.obj_name = obj_name
        self.bone_name = bone_name
        self.ptype = ptype
        self.fs = 0
        self.B = []
        self.bake_id = 0
        self.sig = None
        self.dirty = True
        self.valid = False
        self.times = []
        self.points = []
        self.speeds = []
        self.keys = []
        self.rot_len = 1.0
        self.cyclic = False
        self.failed = False
        self.locked = False     # path can't be edited in this mode (e.g. connected bone location)
        self.lock_reason = ""
        self.value_ref = None   # (data_path, index) of the edited value (VALUE paths)
        self.vbase = 0.0        # value track: value drawn on the path itself
        self.vgain = 1.0        # value track: world height per unit of value
        self.vgain_id = None
        self.vdir = Vector((0.0, 0.0, 1.0))   # value track: direction "up" (follows the view)
        self.vview = Vector((0.0, 1.0, 0.0))  # value track: view direction (track beside the path)
        self.track_mode = 'UP'
        self.base_points = []   # scale / value paths: the location path they are drawn on
        self.sample_vals = []   # scale / value paths: channel values at every sample
        self.hidden = set()     # frames of hidden keys
        self.sample_axes = []   # scale paths: the target's X, Y, Z directions at every sample
        self.vis = []           # per sample: is this part of the path shown (not next to a hidden key)

    @property
    def key(self):
        return (self.obj_name, self.bone_name, self.ptype)

    @property
    def label(self):
        return self.bone_name or self.obj_name

    @property
    def fe(self):
        return self.fs + len(self.B) - 1

    # -- live access ---------------------------------------------------------

    def target(self):
        obj, owner = resolve(self.obj_name, self.bone_name)
        if owner is None:
            return None
        return Target(obj, owner, bool(self.bone_name), self.value_ref if self.ptype == VALUE else None)

    def owner_selected(self):
        obj, owner = resolve(self.obj_name, self.bone_name)
        if owner is None:
            return False
        if self.bone_name:
            return obj.mode == 'POSE' and compat.bone_selected(owner)
        try:
            return obj.select_get() and obj.mode == 'OBJECT'
        except RuntimeError:
            return False

    # -- parent space ----------------------------------------------------------

    def B_at(self, t):
        B = self.B
        x = t - self.fs
        if x <= 0.0:
            return B[0]
        n = len(B)
        if x >= n - 1:
            return B[-1]
        i = int(x)
        f = x - i
        if f < EPS:
            return B[i]
        return B[i] + (B[i + 1] - B[i]) * f

    def extend_to(self, t):
        """Make the path reach frame t (keys added past the ends) until the next bake; the
        parent space there is assumed to stay as at the end."""
        if not self.B:
            return
        if t > self.fe:
            n = int(math.ceil(t)) - self.fe
            self.B = self.B + [self.B[-1].copy() for _ in range(n)]
        if t < self.fs:
            n = self.fs - int(math.floor(t))
            self.B = [self.B[0].copy() for _ in range(n)] + self.B
            self.fs -= n
        self.dirty = True

    def head(self, tg, t):
        return self.B_at(t) @ Vector(tg.loc.values(t))

    def axes(self, tg, t, vals_rot=None):
        """World directions of the target's own X, Y, Z axes at frame t (unit length)."""
        rot = vals_rot if vals_rot is not None else tg.rot.values(t)
        m = (self.B_at(t) @ tg.basis(tg.loc.values(t), rot, (1.0, 1.0, 1.0))).to_3x3()
        out = []
        for i in range(3):
            v = m.col[i].copy()
            out.append(v.normalized() if v.length > 1e-12 else Vector([1.0 if j == i else 0.0 for j in range(3)]))
        return out

    def value_dir(self, tg, t):
        """Value track offset: up on the screen, or beside the path (left of the motion on the
        screen), so it follows the shape of the path."""
        if self.track_mode != 'PATH':
            return self.vdir
        tang = self.head(tg, t + 0.5) - self.head(tg, t - 0.5)
        n = tang.cross(self.vview)
        if n.length < 1e-9:
            return self.vdir
        return n.normalized()

    def point(self, tg, t, vals=None):
        """World position of the path at time t; `vals` overrides the edited channels."""
        B = self.B_at(t)
        if self.ptype == SCALE:
            return B @ Vector(tg.loc.values(t))
        if self.ptype == VALUE:
            v = vals[0] if vals is not None else tg.value.values(t)[0]
            return B @ Vector(tg.loc.values(t)) + self.value_dir(tg, t) * ((v - self.vbase) * self.vgain)
        if self.ptype == LOCATION:
            v = vals if vals is not None else tg.loc.values(t)
            return B @ Vector(v[:3])
        rot = vals if vals is not None else tg.rot.values(t)
        basis = tg.basis(tg.loc.values(t), rot, tg.scale.values(t))
        return B @ basis @ Vector((0.0, tg.length * self.rot_len, 0.0))

    def rot_frame(self, tg, t):
        """Matrix M and local vector u so that tail = M @ (R @ u) for rotation R."""
        loc = Vector(tg.loc.values(t))
        sc = tg.scale.values(t)
        sy = sc[1]
        if tg.dloc is not None:
            loc = loc + tg.dloc
            sy *= tg.dscale[1]
        M = self.B_at(t) @ Matrix.Translation(loc)
        if tg.drot is not None:
            M = M @ tg.drot.to_matrix().to_4x4()
        return M, Vector((0.0, tg.length * self.rot_len * sy, 0.0))

    def measure(self, tg, t, vals):
        """A point that moves with the channel values, to measure how much an edit changes the
        motion (scale paths: the scale values themselves, at the size of the path)."""
        if self.ptype == SCALE:
            return Vector(vals[:3]) * self.vgain
        return self.point(tg, t, vals)

    def velocity(self, tg, t, vals):
        """Path velocity at t caused by everything except the edited channels."""
        return (self.point(tg, t + 1.0, vals) - self.point(tg, t - 1.0, vals)) * 0.5

    # -- inverse mapping -------------------------------------------------------

    def solve(self, tg, t, W, ref_vals, dt=0.0, vp=None):
        """Channel values that put the path point at time t onto world position W.

        dt/vp describe a handle: W is then a handle end located dt frames away.
        """
        if vp is not None and dt:
            W = W - vp * dt
        if self.ptype == SCALE:
            return list(ref_vals)
        if self.ptype == VALUE:
            base = self.B_at(t) @ Vector(tg.loc.values(t))
            return [(W - base).dot(self.value_dir(tg, t)) / self.vgain + self.vbase]
        if self.ptype == LOCATION:
            v = self.B_at(t).inverted_safe() @ W
            locks = tg.loc_locks()
            return [ref_vals[i] if locks[i] else v[i] for i in range(3)]
        M, u = self.rot_frame(tg, t)
        d = M.inverted_safe() @ W
        q_ref = vals_to_quat(ref_vals, tg.rot_mode)
        c = q_ref @ u
        if d.length < EPS or c.length < EPS:
            return list(ref_vals)
        q = c.rotation_difference(d) @ q_ref
        return self.rot_vals(tg, q, ref_vals)

    def rot_vals(self, tg, q, ref_vals):
        vals = quat_to_vals(q, tg.rot_mode, ref_vals)
        locks = tg.rot_locks()
        return [ref_vals[i] if locks[i] else vals[i] for i in range(len(vals))]

    def twist(self, tg, ref_vals, angle):
        q = vals_to_quat(ref_vals, tg.rot_mode) @ Quaternion((0.0, 1.0, 0.0), angle)
        return self.rot_vals(tg, q, ref_vals)

    # -- refresh ---------------------------------------------------------------

    def refresh(self, force=False):
        """Rebuild points/keys from the F-Curves if anything changed. Returns validity."""
        if not self.B:
            self.valid = False
            return False
        tg = self.target()
        if tg is None:
            self.valid = False
            return False
        settings = state.settings()
        self.rot_len = settings.rot_length if settings else 1.0
        self.locked, self.lock_reason = _lock_state(tg, self.ptype)
        used = {LOCATION: (tg.loc,), SCALE: (tg.loc, tg.scale), VALUE: (tg.loc, tg.value)}.get(
            self.ptype, (tg.loc, tg.rot, tg.scale))
        self.track_mode = settings.value_track if settings else 'UP'
        sig = (self.bake_id, self.rot_len, tg.rot_mode, self.track_mode,
               tuple(tuple(_fc_signature(fc) for fc in ch.fcurves) for ch in used),
               tuple(tuple(ch.static) for ch in used))
        if not force and not self.dirty and sig == self.sig and self.valid:
            return True
        self.sig = sig
        self.dirty = False
        self._build(tg)
        self.valid = True
        return True

    def _build(self, tg):
        ch = tg.channels(self.ptype)
        fs, fe = self.fs, self.fe
        if self.ptype in (SCALE, VALUE) and self.vgain_id != self.bake_id:
            # the scale of the drawing is fixed per bake, so edits don't make it jump
            self.vgain_id = self.bake_id
            heads = [self.head(tg, float(t)) for t in range(fs, fe + 1)]
            lo = [min(p[i] for p in heads) for i in range(3)]
            hi = [max(p[i] for p in heads) for i in range(3)]
            size = max(sum((h - l) ** 2 for h, l in zip(hi, lo)) ** 0.5, tg.length, 0.1)
            if self.ptype == VALUE:
                vs = [ch.values(float(t))[0] for t in range(fs, fe + 1)]
                self.vbase = min(vs) if vs else 0.0
                self.vgain = size * VALUE_HEIGHT / max(max(vs) - min(vs), 1e-6) if vs else 1.0
            else:
                self.vgain = size * VALUE_HEIGHT
        self.cyclic = any(fc is not None and any(m.type == 'CYCLES' for m in fc.modifiers)
                          for fc in ch.fcurves)
        keys = self._collect_keys(ch)
        times = set(range(fs, fe + 1))
        for k in keys:
            if fs <= k.t <= fe:
                times.add(k.t)
        self.times = sorted(times)
        self.points = [self.point(tg, t) for t in self.times]
        if self.ptype in (SCALE, VALUE):
            self.base_points = [self.head(tg, t) for t in self.times]
            self.sample_vals = [ch.values(t) for t in self.times]
        if self.ptype == SCALE:
            self.sample_axes = [self.axes(tg, t) for t in self.times]
            if self.ptype == VALUE:
                # speed colors show how fast the value changes
                vs = [v[0] for v in self.sample_vals]
                self.speeds = [abs(b - a) * self.vgain / (tb - ta) if tb - ta > EPS else 0.0
                               for a, b, ta, tb in zip(vs, vs[1:], self.times, self.times[1:])]
        self.speeds = []
        for i in range(len(self.points) - 1):
            dt = self.times[i + 1] - self.times[i]
            self.speeds.append((self.points[i + 1] - self.points[i]).length / dt if dt > EPS else 0.0)
        self.keys = [k for k in keys if fs - EPS <= k.t <= fe + EPS]
        for i, k in enumerate(self.keys):
            self._fill_key(tg, ch, k, i)
        self._visibility()
        # fallback handle directions (used to display zero-length handles)
        n = len(self.keys)
        for i, k in enumerate(self.keys):
            a = self.keys[i - 1].co if i > 0 else k.co
            b = self.keys[i + 1].co if i < n - 1 else k.co
            ta = self.keys[i - 1].t if i > 0 else k.t
            tb = self.keys[i + 1].t if i < n - 1 else k.t
            chord = b - a
            vel = chord / (tb - ta) if tb - ta > EPS else Vector()
            k.cat_r = vel * k.dt_r
            k.cat_l = -vel * k.dt_l
            if chord.length < 1e-9:
                chord = self.point(tg, min(k.t + 1.0, fe)) - self.point(tg, max(k.t - 1.0, fs))
            if chord.length < 1e-9:
                chord = Vector((1.0, 0.0, 0.0))
            k.dir_r = chord.normalized()
            k.dir_l = -k.dir_r

    def _visibility(self):
        """Hidden keys take the path segments next to them with them; what stays visible falls
        apart into pieces."""
        ks = self.keys
        piece = 0
        for i, k in enumerate(ks):
            k.hidden = round(k.t, FRAME_ROUND) in self.hidden
            if k.hidden:
                k.sel = k.sel_l = k.sel_r = False     # (even if selected in the Graph Editor)
            if i > 0 and (k.hidden or ks[i - 1].hidden):
                piece += 1
            k.piece = piece
        if not self.hidden:
            self.vis = [True] * len(self.times)
            return
        kt = [k.t for k in ks]
        vis = []
        j = 0
        for t in self.times:
            while j < len(kt) and kt[j] < t - 1e-6:
                j += 1
            # keys around t: ks[j-1] (before) and ks[j] (at or after)
            at = j < len(kt) and abs(kt[j] - t) < 1e-6
            if at:
                vis.append(not ks[j].hidden)
                continue
            before = ks[j - 1] if j > 0 else None
            after = ks[j] if j < len(ks) else None
            vis.append(not ((before is not None and before.hidden) or (after is not None and after.hidden)))
        self.vis = vis

    def visible_keys(self):
        return [k for k in self.keys if not k.hidden]

    def _collect_keys(self, ch):
        by_t = {}
        for c, fc in enumerate(ch.fcurves):
            if fc is None:
                continue
            kps = fc.keyframe_points
            n = len(kps)
            co = array('f', bytes(8 * n))
            kps.foreach_get("co", co)
            for i in range(n):
                t = round(co[2 * i], FRAME_ROUND)
                k = by_t.get(t)
                if k is None:
                    k = by_t[t] = KeyData(co[2 * i])
                    k.idx = [-1] * ch.count
                k.idx[c] = i
        keys = [by_t[t] for t in sorted(by_t)]
        # looping channels: a key outside their range is the same pose as a key inside it
        for c, fc in enumerate(ch.fcurves):
            cyc = cycle_info(fc)
            if cyc is None:
                continue
            kps = fc.keyframe_points
            at = {round(kp.co[0], FRAME_ROUND): i for i, kp in enumerate(kps)}
            for k in keys:
                if k.idx[c] >= 0:
                    continue
                tw = wrap_time(k.t, cyc)
                j = at.get(round(tw, FRAME_ROUND))
                if j is not None and abs(tw - k.t) > 1e-3:
                    k.idx[c] = j
                    if k.offs is None:
                        k.offs = [0.0] * ch.count
                    k.offs[c] = fc.evaluate(k.t) - kps[j].co[1]
        return keys

    def _fill_key(self, tg, ch, k, i):
        t = k.t
        k.vals = ch.values(t)
        k.co = self.point(tg, t, k.vals)
        k.vp = self.velocity(tg, t, k.vals)
        s_l = [0.0] * ch.count
        s_r = [0.0] * ch.count
        dts_l = []
        dts_r = []
        first = None
        sel = sel_l = sel_r = False
        for c, fc in enumerate(ch.fcurves):
            idx = k.idx[c]
            if fc is None:
                continue
            if idx < 0:
                if ch.live[c]:
                    e = 0.25
                    v0 = fc.evaluate(t)
                    s_l[c] = (v0 - fc.evaluate(t - e)) / e
                    s_r[c] = (fc.evaluate(t + e) - v0) / e
                continue
            kp = fc.keyframe_points[idx]
            if first is None:
                first = kp
            x, y = kp.co
            hlx, hly = kp.handle_left
            hrx, hry = kp.handle_right
            if x - hlx > EPS:
                s_l[c] = (y - hly) / (x - hlx)
                dts_l.append(x - hlx)
            if hrx - x > EPS:
                s_r[c] = (hry - y) / (hrx - x)
                dts_r.append(hrx - x)
            sel |= kp.select_control_point
            sel_l |= kp.select_left_handle
            sel_r |= kp.select_right_handle
        k.sel, k.sel_l, k.sel_r = sel, sel_l, sel_r
        k.partial = any(live and k.idx[c] < 0 for c, live in enumerate(ch.live))
        k.s_l, k.s_r = s_l, s_r
        if first is not None:
            k.type_l = first.handle_left_type
            k.type_r = first.handle_right_type
            k.interp = first.interpolation
        k.dt_l = sum(dts_l) / len(dts_l) if dts_l else 0.0
        k.dt_r = sum(dts_r) / len(dts_r) if dts_r else 0.0

        keys = self.keys
        prev_k = keys[i - 1] if i > 0 else None
        has_left = k.dt_l > EPS and (prev_k is not None or self.cyclic)
        if prev_k is not None and prev_k.interp != 'BEZIER':
            has_left = False
        has_right = k.dt_r > EPS and (i < len(keys) - 1 or self.cyclic) and k.interp == 'BEZIER'
        if self.ptype == SCALE:
            has_left = has_right = False    # scale is edited by size, not by handles in space
        k.hl = self.handle_point(tg, k, 'L') if has_left else None
        k.hr = self.handle_point(tg, k, 'R') if has_right else None

    def handle_vals(self, k, side, slopes=None, dt=None):
        if side == 'R':
            s = slopes if slopes is not None else k.s_r
            d = dt if dt is not None else k.dt_r
            return [v + si * d for v, si in zip(k.vals, s)]
        s = slopes if slopes is not None else k.s_l
        d = dt if dt is not None else k.dt_l
        return [v - si * d for v, si in zip(k.vals, s)]

    def handle_point(self, tg, k, side, slopes=None, dt=None):
        d = dt if dt is not None else (k.dt_r if side == 'R' else k.dt_l)
        p = self.point(tg, k.t, self.handle_vals(k, side, slopes, dt))
        return p + k.vp * d if side == 'R' else p - k.vp * d

    def slopes_from_point(self, tg, k, side, W, key_vals, dt):
        """Per-channel slopes so that the handle on `side` ends at world W."""
        if dt < EPS:
            return [0.0] * len(key_vals)
        sign = 1.0 if side == 'R' else -1.0
        ref = self.handle_vals(k, side, None, dt)
        vals = self.solve(tg, k.t, W, ref, dt * sign, k.vp)
        return [(hv - v) * sign / dt for hv, v in zip(vals, key_vals)]


# ----------------------------------------------------------------------------
# Writing to F-Curves
# ----------------------------------------------------------------------------

def write_key(ch, k, vals, move_handles=True, touched=None):
    for c, fc in enumerate(ch.fcurves):
        idx = k.idx[c] if c < len(k.idx) else -1
        if fc is None or idx < 0:
            continue
        kp = fc.keyframe_points[idx]
        x, y = kp.co
        target = vals[c] - (k.offs[c] if k.offs else 0.0)
        d = target - y
        if abs(d) < 1e-12:
            continue
        hl = tuple(kp.handle_left)
        hr = tuple(kp.handle_right)
        kp.co = (x, target)
        if move_handles:
            kp.handle_left = (hl[0], hl[1] + d)
            kp.handle_right = (hr[0], hr[1] + d)
        _sync_cycle_partner(fc, idx, d)
        if touched is not None:
            touched.add(fc)


def write_handle(ch, k, side, slopes, dt, key_vals, htype=None, touched=None):
    """Set one handle side on all channels of key k to the given slopes and length."""
    for c, fc in enumerate(ch.fcurves):
        idx = k.idx[c] if c < len(k.idx) else -1
        if fc is None or idx < 0:
            continue
        kp = fc.keyframe_points[idx]
        x = kp.co[0]
        y = key_vals[c] - (k.offs[c] if k.offs else 0.0)
        if htype is not None:
            if side == 'R':
                kp.handle_right_type = htype
            else:
                kp.handle_left_type = htype
        if side == 'R':
            kp.handle_right = (x + dt, y + slopes[c] * dt)
        else:
            kp.handle_left = (x - dt, y - slopes[c] * dt)
        if touched is not None:
            touched.add(fc)


def set_handle_types(ch, k, left=None, right=None, touched=None):
    for c, fc in enumerate(ch.fcurves):
        idx = k.idx[c] if c < len(k.idx) else -1
        if fc is None or idx < 0:
            continue
        kp = fc.keyframe_points[idx]
        if left is not None and kp.handle_left_type != left:
            kp.handle_left_type = left
        if right is not None and kp.handle_right_type != right:
            kp.handle_right_type = right
        if touched is not None:
            touched.add(fc)


def update_fcurves(fcs):
    for fc in fcs:
        try:
            fc.update()
        except ReferenceError:
            pass


def key_times(ch):
    ts = {}
    for fc in ch.fcurves:
        if fc is None:
            continue
        for kp in fc.keyframe_points:
            ts[round(kp.co[0], FRAME_ROUND)] = kp.co[0]
    return [ts[t] for t in sorted(ts)]


def _key_selection(ch):
    """Selection flags per key time, merged over channels."""
    sel = {}
    for fc in ch.fcurves:
        if fc is None:
            continue
        for kp in fc.keyframe_points:
            t = round(kp.co[0], FRAME_ROUND)
            a, b, c = sel.get(t, (False, False, False))
            sel[t] = (a or kp.select_control_point, b or kp.select_left_handle,
                      c or kp.select_right_handle)
    return sel


def _insert(fc, t, value, sel):
    kp = fc.keyframe_points.insert(t, value, options={'FAST'})
    a, b, c = sel.get(round(t, FRAME_ROUND), (False, False, False))
    kp.select_control_point = a
    kp.select_left_handle = b
    kp.select_right_handle = c
    return kp


def ensure_keys(tg, ptype, frames, created=None, pin=False):
    """Make sure every unlocked channel of the edited property has a key on `frames`.

    Missing channels are created and keyed on all existing key times with their
    current static value, so the motion doesn't change.  Inserted keys inherit the
    selection of the other channels at the same time.  Returns True if anything
    was added.
    """
    ch = tg.channels(ptype)
    locks = tg.locks(ptype)
    union = key_times(ch)
    sel = _key_selection(ch)
    changed = False
    for c in range(ch.count):
        if locks[c]:
            continue
        fc = ch.fcurves[c]
        if fc is None:
            fc = compat.ensure_fcurve(tg.obj, ch.path, c, tg.group_name)
            if created is not None:
                created.append((ch.path, c))
            for t in sorted(set(union) | set(frames)):
                _insert(fc, t, ch.static[c], sel)
            fc.update()
            changed = True
            continue
        cyc = cycle_info(fc)
        xs = sorted(kp.co[0] for kp in fc.keyframe_points)
        have = {round(x, FRAME_ROUND) for x in xs}
        want = {}
        for t in frames:
            # never key outside a loop's range (that would change its period): use the same
            # pose inside the loop instead
            tt = wrap_time(t, cyc)
            if round(tt, FRAME_ROUND) not in have:
                want[round(tt, FRAME_ROUND)] = tt
            if pin:
                # path keys keyed on other channels only interpolate this channel through the
                # edited frame; key them here too so they stay where they are
                # the edit also re-shapes the auto handles of the neighbor keys, which bends
                # the curve one more segment out: pin up to the second key on each side
                before = [x for x in xs if x < tt - 1e-3]
                after = [x for x in xs if x > tt + 1e-3]
                prev = before[-2] if len(before) >= 2 else None
                nxt = after[1] if len(after) >= 2 else None
                for u in union:
                    b = wrap_time(u, cyc)
                    if abs(b - tt) < 1e-3 or round(b, FRAME_ROUND) in have:
                        continue
                    if (prev is None or b > prev + 1e-3) and (nxt is None or b < nxt - 1e-3):
                        want[round(b, FRAME_ROUND)] = b
        added = False
        for t in sorted(want.values()):
            _insert(fc, t, fc.evaluate(t), sel)
            added = True
        if added:
            fc.update()
            changed = True
    return changed


class FCurveSnapshot:
    """Restorable copy of a set of F-Curves' keyframes."""

    def __init__(self, tg, ch=None, items=None):
        """Snapshot the channels of `ch`, or `items`: a list of (data_path, index, fcurve)."""
        self.obj_name = tg.obj.name
        self.items = []
        if items is None:
            items = [(ch.path, fc.array_index if fc is not None else c, fc) for c, fc in enumerate(ch.fcurves)]
        for path, c, fc in items:
            if fc is None:
                self.items.append((path, c, None))
                continue
            kps = fc.keyframe_points
            data = [(tuple(kp.co), tuple(kp.handle_left), tuple(kp.handle_right),
                     kp.handle_left_type, kp.handle_right_type, kp.interpolation,
                     kp.select_control_point, kp.select_left_handle, kp.select_right_handle,
                     kp.type, kp.easing)
                    for kp in kps]
            self.items.append((path, c, data))

    def restore(self):
        obj = bpy.data.objects.get(self.obj_name)
        if obj is None:
            return
        coll = compat.get_fcurves(obj)
        if coll is None:
            return
        for path, c, data in self.items:
            fc = coll.find(path, index=c)
            if data is None:
                if fc is not None:
                    coll.remove(fc)
                continue
            if fc is None:
                continue
            kps = fc.keyframe_points
            if len(kps) != len(data):
                kps.clear()
                kps.add(len(data))
            for kp, d in zip(kps, data):
                kp.interpolation = d[5]
                kp.handle_left_type = d[3]
                kp.handle_right_type = d[4]
                kp.co = d[0]
                kp.handle_left = d[1]
                kp.handle_right = d[2]
                kp.select_control_point = d[6]
                kp.select_left_handle = d[7]
                kp.select_right_handle = d[8]
                kp.type = d[9]
                kp.easing = d[10]
            fc.update()


# ----------------------------------------------------------------------------
# Baking
# ----------------------------------------------------------------------------

def _pose_state(objs):
    saved = []
    for obj in objs:
        tf = (tuple(obj.location), tuple(obj.rotation_quaternion), tuple(obj.rotation_euler),
              tuple(obj.rotation_axis_angle), tuple(obj.scale))
        bones = None
        if obj.type == 'ARMATURE' and obj.pose is not None:
            bones = [(pb.name, tuple(pb.location), tuple(pb.rotation_quaternion),
                      tuple(pb.rotation_euler), tuple(pb.rotation_axis_angle), tuple(pb.scale))
                     for pb in obj.pose.bones]
        saved.append((obj.name, tf, bones))
    return saved


def _restore_pose_state(saved):
    for name, tf, bones in saved:
        obj = bpy.data.objects.get(name)
        if obj is None:
            continue
        for prop, v in zip(("location", "rotation_quaternion", "rotation_euler",
                            "rotation_axis_angle", "scale"), tf):
            if tuple(getattr(obj, prop)) != v:
                setattr(obj, prop, v)
        if bones:
            pbs = obj.pose.bones
            for bname, *vals in bones:
                pb = pbs.get(bname)
                if pb is None:
                    continue
                for prop, v in zip(("location", "rotation_quaternion", "rotation_euler",
                                    "rotation_axis_angle", "scale"), vals):
                    if tuple(getattr(pb, prop)) != v:
                        setattr(pb, prop, v)


def bake_range(scene, settings, caches):
    mode = settings.range_mode
    if mode == 'MANUAL':
        fs, fe = settings.frame_start, settings.frame_end
    elif mode == 'ACTION':
        fs, fe = None, None
        for c in caches:
            obj = bpy.data.objects.get(c.obj_name)
            ad = obj.animation_data if obj else None
            if ad and ad.action:
                a, b = ad.action.frame_range
                fs = int(math.floor(a)) if fs is None else min(fs, int(math.floor(a)))
                fe = int(math.ceil(b)) if fe is None else max(fe, int(math.ceil(b)))
        if fs is None:
            fs, fe = scene.frame_start, scene.frame_end
    else:
        if scene.use_preview_range:
            fs, fe = scene.frame_preview_start, scene.frame_preview_end
        else:
            fs, fe = scene.frame_start, scene.frame_end
    if mode != 'MANUAL':
        # always include every key of the path targets (keys slid past the end stay visible)
        for c in caches:
            obj, owner = resolve(c.obj_name, c.bone_name)
            coll = compat.get_fcurves(obj) if obj is not None else None
            if coll is None:
                continue
            prefix = ('pose.bones["%s"].' % bpy.utils.escape_identifier(c.bone_name)) if c.bone_name else ""
            for fc in coll:
                kps = fc.keyframe_points
                if len(kps) and fc.data_path.startswith(prefix):
                    fs = min(fs, int(math.floor(kps[0].co[0])))
                    fe = max(fe, int(math.ceil(kps[-1].co[0])))
    if fe < fs:
        fs, fe = fe, fs
    if fe - fs > 20000:
        fe = fs + 20000
    return fs, fe


def bake(scene, caches):
    """Bake parent spaces of the given caches by stepping through the frame range."""
    settings = state.settings(scene)
    if not caches or settings is None:
        return
    fs, fe = bake_range(scene, settings, caches)
    resolved = []
    objs = {}
    for c in caches:
        obj, owner = resolve(c.obj_name, c.bone_name)
        if owner is None:
            c.B = []
            c.valid = False
            c.failed = True
            continue
        resolved.append((c, obj, owner, []))
        o = obj
        while o is not None:
            objs[o.name] = o
            o = o.parent
    if not resolved:
        return
    saved = _pose_state(objs.values())
    cur = scene.frame_current
    sub = scene.frame_subframe
    state.baking = True
    try:
        for f in range(fs, fe + 1):
            scene.frame_set(f)
            for c, obj, owner, mats in resolved:
                if c.bone_name:
                    W = obj.matrix_world @ owner.matrix
                else:
                    W = owner.matrix_world.copy()
                mats.append(W @ owner.matrix_basis.inverted_safe())
    finally:
        scene.frame_set(cur, subframe=sub)
        _restore_pose_state(saved)
        state.baking = False
    for c, obj, owner, mats in resolved:
        c.fs = fs
        c.B = mats
        c.failed = False
        c.bake_id += 1
        c.dirty = True
        c.refresh(force=True)
    state.remember_action_hashes()
