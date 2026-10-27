"""Path evaluation core.

A path is evaluated as  world(f) = B(f) @ basis(f)  where
  * B(f)      is the "parent space" of the target at frame f (everything except the
              target's own loc/rot/scale), baked once by stepping through frames;
  * basis(f)  is rebuilt from the target's own F-Curves on every refresh.

Because only B is baked, the path follows F-Curve edits in real time, and edits made
in world space can be mapped back to channel values exactly (location) or by a
minimal swing rotation (rotation).
"""

import bisect
import math
import time
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
                 "partial", "cat_l", "cat_r", "offs", "hidden", "piece", "dts_l", "dts_r", "types_l", "types_r")

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
        # per channel: the handles' own time lengths and types (dt_l / dt_r, type_l / type_r are
        # what the path shows: an average, the first channel's) - a handle edit keeps each
        # channel's own, or the first move would make them all alike (the curve jumps)
        self.dts_l = None
        self.dts_r = None
        self.types_l = None
        self.types_r = None

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
    """First and last key of a loop are the same pose: move the other one too (not for a
    mirrored loop: it goes back and forth, its ends needn't meet)."""
    if fc is None or abs(d) < 1e-12 or cycle_info(fc) is None:
        return
    if any(m.type == 'CYCLES' and not m.mute and 'MIRROR' in (m.mode_before, m.mode_after) for m in fc.modifiers):
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


AUTO_HANDLES = {'AUTO', 'AUTO_CLAMPED'}


def _cycle_partner(fc, idx):
    """The other end's key of a loop (first <-> last; not a mirrored loop) and its index, or None."""
    if fc is None or cycle_info(fc) is None:
        return None
    if any(m.type == 'CYCLES' and not m.mute and 'MIRROR' in (m.mode_before, m.mode_after) for m in fc.modifiers):
        return None
    n = len(fc.keyframe_points)
    j = n - 1 if idx == 0 else 0 if idx == n - 1 else None
    if j is None or j == idx:
        return None
    kp = fc.keyframe_points[j]
    if round(kp.co[0], FRAME_ROUND) in cycle_skip:
        return None
    return kp


def _freeze_partner(fc, idx, sides):
    """A loop end's handle stops being auto: the other end's auto handles keep where they are
    (Blender would work them out again from the changed end, and the curve would jump)."""
    kp = _cycle_partner(fc, idx)
    if kp is None:
        return
    hl, hr = tuple(kp.handle_left), tuple(kp.handle_right)
    for side in sides:
        if side == 'L' and kp.handle_left_type in AUTO_HANDLES:
            kp.handle_left_type = 'ALIGNED'
        if side == 'R' and kp.handle_right_type in AUTO_HANDLES:
            kp.handle_right_type = 'ALIGNED'
    kp.handle_left, kp.handle_right = hl, hr


def _sync_cycle_handle(fc, idx, side, slope, htype, old_slope=None):
    """A handle of a loop's first / last key: the other end's handle on the same side follows it
    (the same slope and type: the loop stays smooth where it joins) - if it did so far; if the
    join had a corner already, the other end only stays as it is (no jump)."""
    kp = _cycle_partner(fc, idx)
    if kp is None:
        return
    x, y = kp.co
    d = kp.handle_right[0] - x if side == 'R' else x - kp.handle_left[0]
    if d > EPS and old_slope is not None:
        cur = (kp.handle_right[1] - y) / d if side == 'R' else (y - kp.handle_left[1]) / d
        if abs(cur - old_slope) > 1e-4 * max(1.0, abs(old_slope)):
            _freeze_partner(fc, idx, (side,))
            return
    if side == 'R':
        if htype is not None:
            kp.handle_right_type = htype
        if d > EPS:
            kp.handle_right = (x + d, y + slope * d)
    else:
        if htype is not None:
            kp.handle_left_type = htype
        if d > EPS:
            kp.handle_left = (x - d, y - slope * d)


def _fmod_signature(m):
    """Every setting of an F-Curve modifier (editing one changes the curve's shape)."""
    out = [m.type]
    for p in m.bl_rna.properties:
        if p.identifier in {'rna_type', 'show_expanded', 'active', 'name'}:
            continue
        try:
            v = getattr(m, p.identifier)
        except AttributeError:
            continue
        if p.type in {'BOOLEAN', 'INT', 'FLOAT', 'ENUM', 'STRING'}:
            out.append(tuple(v) if getattr(p, "is_array", False) or isinstance(v, set) else v)
    for cp in getattr(m, "control_points", ()):
        out.append((cp.frame, cp.min, cp.max))
    return tuple(out)


def fc_shape(fc):
    """Everything about the keys that shapes the curve, as bytes: where the keys and handles
    are, the interpolation and easing (Constant / Back / Elastic... with their settings), and the
    extrapolation. (A change of interpolation alone moves no key or handle.)"""
    kps = fc.keyframe_points
    n = len(kps)
    out = []
    for name in ("co", "handle_left", "handle_right"):
        a = array('f', bytes(8 * n))
        kps.foreach_get(name, a)
        out.append(a.tobytes())
    for name in ("interpolation", "easing"):
        a = array('i', bytes(4 * n))
        kps.foreach_get(name, a)
        out.append(a.tobytes())
    for name in ("back", "amplitude", "period"):
        a = array('f', bytes(4 * n))
        kps.foreach_get(name, a)
        out.append(a.tobytes())
    out.append(fc.extrapolation.encode())
    return b"|".join(out)


def _fc_signature(fc):
    if fc is None:
        return None
    kps = fc.keyframe_points
    n = len(kps)
    s0 = [False] * n
    s1 = [False] * n
    s2 = [False] * n
    kps.foreach_get("select_control_point", s0)
    kps.foreach_get("select_left_handle", s1)
    kps.foreach_get("select_right_handle", s2)
    return (fc.mute, n, tuple(_fmod_signature(m) for m in fc.modifiers), fc_shape(fc),
            tuple(s0), tuple(s1), tuple(s2))


# ----------------------------------------------------------------------------
# Path cache
# ----------------------------------------------------------------------------

def _anim_lock(tg):
    """(locked, reason) for animation that can't be edited from here at all."""
    ad = getattr(tg.obj, "animation_data", None)
    act = ad.action if ad is not None else None
    if act is None:
        return False, ""
    if act.library is not None or not getattr(act, "is_editable", True):
        return True, ("The animation comes linked from a library (read-only): an edit would be lost "
                      "on save. Make the action local to edit it")
    if getattr(ad, "use_tweak_mode", False):
        strip = next((s for t in ad.nla_tracks for s in t.strips if s.active and s.action == act), None)
        if strip is None:
            strip = next((s for t in ad.nla_tracks for s in t.strips if s.action == act), None)
        if strip is not None and (abs(strip.scale - 1.0) > 1e-6 or abs(strip.repeat - 1.0) > 1e-6 or
                                  abs(strip.frame_start - strip.action_frame_start) > 1e-6):
            return True, ("NLA tweak mode with a moved / scaled strip: its keys aren't on the scene's "
                          "frames. Edit the action outside the strip (or with the strip unmoved)")
    return False, ""


def _lock_state(tg, ptype):
    locked, why = _anim_lock(tg)
    if locked:
        return True, why
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
        return True, "Pick an animated value to edit (Sidebar > Motion Paths > Value)"
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
        self.size_ref = 1.0     # path size the value / scale drawing was set up for
        self.vdir = Vector((0.0, 0.0, 1.0))   # value track: up on the screen (follows the view)
        self.vview = Vector((0.0, 1.0, 0.0))  # value track: view direction
        self.vplane_n = Vector((0.0, 0.0, 1.0))  # value track beside the path: the path's plane normal
        self.value_side = 'OUTSIDE'
        self.circle = None      # scale / value paths drawn around a circle (see _circle_layout)
        self.local = None       # Local space: the frame whose parent space every frame is drawn in
        self.track_mode = 'UP'
        self.base_points = []   # scale / value paths: the location path they are drawn on
        self.sample_vals = []   # scale / value paths: channel values at every sample
        self.hidden = set()     # frames of hidden keys
        self.sample_axes = []   # scale / rotation paths: the target's X, Y, Z directions at every sample
        self.vis = []           # per sample: is this part of the path shown (not next to a hidden key)
        self.rep = []           # per sample: a repeat of a loop outside its keys (drawn faint)
        self.loop_range = None  # (first, last) key frame of looping motion

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
        if self.local is not None:
            # Local: the parent stays as it is on the current frame, so only the bone's / object's
            # own motion shows (and is edited)
            t = self.local
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

    def world_head(self, tg, t):
        return self.B_at(t) @ Vector(tg.loc.values(t))

    def head(self, tg, t):
        """Where scale / value paths are drawn: the location path, or the circle."""
        if self.circle is not None:
            center, e1, e2, n, r, f0, f1 = self.circle
            a = 2.0 * math.pi * (t - f0) / max(f1 - f0 + 1.0, 1.0)
            return center + (e1 * math.sin(a) + e2 * math.cos(a)) * r
        return self.B_at(t) @ Vector(tg.loc.values(t))

    def _circle_axes(self, t):
        center, e1, e2, n, r, f0, f1 = self.circle
        a = 2.0 * math.pi * (t - f0) / max(f1 - f0 + 1.0, 1.0)
        radial = e1 * math.sin(a) + e2 * math.cos(a)
        tangent = e1 * math.cos(a) - e2 * math.sin(a)
        return [radial, tangent, n]

    def circle_range(self):
        return (self.circle[5], self.circle[6]) if self.circle is not None else None

    def axes(self, tg, t, vals_rot=None):
        """World directions of the target's own X, Y, Z axes at frame t (unit length). On the
        circle: outward, along the circle and across it."""
        if self.circle is not None:
            return self._circle_axes(t)
        rot = vals_rot if vals_rot is not None else tg.rot.values(t)
        m = (self.B_at(t) @ tg.basis(tg.loc.values(t), rot, (1.0, 1.0, 1.0))).to_3x3()
        out = []
        for i in range(3):
            v = m.col[i].copy()
            out.append(v.normalized() if v.length > 1e-12 else Vector([1.0 if j == i else 0.0 for j in range(3)]))
        return out

    def value_dir(self, tg, t):
        """Value track offset: up on the screen, or beside the path in the plane of the motion
        (inside or outside, see _track_plane), so it follows the shape of the path and stays put
        when the view turns."""
        if self.circle is not None:
            return self._circle_axes(t)[0]     # on the circle a bigger value is farther out
        if self.track_mode != 'PATH':
            return self.vdir
        tang = self.head(tg, t + 0.5) - self.head(tg, t - 0.5)
        n = self.vplane_n.cross(tang)
        if n.length < 1e-9:
            n = Vector((0.0, 0.0, 1.0)).cross(tang)
            if n.length < 1e-9:
                return self.vdir
        return n.normalized()

    def _track_plane(self, tg):
        """Value track beside the path: the normal of the plane the motion lies in, turned so
        that (normal x direction of motion) points to the chosen side (inside: toward the middle
        of the motion) on the whole."""
        from .geom import best_plane
        heads = [self.world_head(tg, float(t)) for t in range(self.fs, self.fe + 1)]
        if len(heads) < 3:
            return
        center, n = best_plane(heads)
        if n.length < 1e-9:
            n = Vector((0.0, 0.0, 1.0))
        score = 0.0
        for a, b in zip(heads, heads[1:]):
            score += n.cross(b - a).dot(center - (a + b) * 0.5)
        if (score < 0.0) == (self.value_side == 'INSIDE'):
            n = -n
        self.vplane_n = n

    def point(self, tg, t, vals=None):
        """World position of the path at time t; `vals` overrides the edited channels."""
        B = self.B_at(t)
        if self.ptype == SCALE:
            return self.head(tg, t)
        if self.ptype == VALUE:
            v = vals[0] if vals is not None else tg.value.values(t)[0]
            return self.head(tg, t) + self.value_dir(tg, t) * ((v - self.vbase) * self.vgain)
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
            base = self.head(tg, t)
            v = (W - base).dot(self.value_dir(tg, t)) / self.vgain + self.vbase
            return [v] if math.isfinite(v) and abs(v) < 1e12 else list(ref_vals)
        if self.ptype == LOCATION:
            B = self.B_at(t)
            if abs(B.to_3x3().determinant()) <= SINGULAR:
                return list(ref_vals)       # (no parent space to solve in)
            v = B.inverted_safe() @ W
            locks = tg.loc_locks()
            return [ref_vals[i] if locks[i] or not (math.isfinite(v[i]) and abs(v[i]) < 1e9) else v[i]
                    for i in range(3)]
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
        self.value_side = settings.value_side if settings else 'OUTSIDE'
        self.local = None
        for e in (settings.paths if settings else ()):
            if e.object is not None and e.object.name == self.obj_name and e.bone_name == self.bone_name:
                if e.local:
                    self.local = float(bpy.context.scene.frame_current)
                break
        lay = (settings.layout, settings.circle_radius, settings.circle_use_range, settings.circle_start,
               settings.circle_end) if settings and self.ptype in (SCALE, VALUE) else None
        sig = (self.bake_id, self.rot_len, tg.rot_mode, self.track_mode, self.value_side, lay, self.local,
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
        self.circle = self._circle_layout(tg) if self.ptype in (SCALE, VALUE) else None
        if self.ptype == VALUE and self.circle is None and self.track_mode == 'PATH':
            self._track_plane(tg)
        if self.ptype in (SCALE, VALUE):
            # the scale of the drawing is set once (per value), so edits and recalculations
            # never make the track jump; only values far outside it set it again
            vs = [ch.values(float(t))[0] for t in range(fs, fe + 1)] if self.ptype == VALUE else []
            renew = self.vgain_id is None
            # never while a key is being dragged: a new scale in the middle of a drag makes the
            # same mouse move mean a bigger value, which makes a new scale... (the value runs off
            # to huge numbers). It's set again once the edit is over.
            if vs and not renew and not state.modal_running:
                span = self.size_ref * VALUE_HEIGHT / self.vgain
                renew = min(vs) < self.vbase - span or max(vs) > self.vbase + 2.0 * span
            if renew:
                self.vgain_id = True
                heads = [self.world_head(tg, float(t)) for t in range(fs, fe + 1)]
                if self.circle is not None:
                    heads += [self.head(tg, float(t)) for t in range(fs, fe + 1)]
                lo = [min(p[i] for p in heads) for i in range(3)]
                hi = [max(p[i] for p in heads) for i in range(3)]
                self.size_ref = max(sum((h - l) ** 2 for h, l in zip(hi, lo)) ** 0.5, tg.length, 0.1)
                if vs:
                    self.vbase = min(vs)
                    rng = max(vs) - min(vs)
                    big = max(abs(max(vs)), abs(min(vs)), 1.0)
                    if rng < 1e-4 * big:
                        # a value that doesn't change (both keys 1.0): no range to fit, so a
                        # unit of the value's own size (a range of ~0 gave a scale of millions,
                        # and dragging a key barely changed the value)
                        rng = big
                    self.vgain = self.size_ref * VALUE_HEIGHT / rng
                else:
                    self.vgain = self.size_ref * VALUE_HEIGHT
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
        if self.ptype in (SCALE, ROTATION):
            # scale ribbons / rotation ribbon: the target's own axes at every sample
            self.sample_axes = [self.axes(tg, t) for t in self.times]
        if self.ptype == VALUE:
            # speed colors show how fast the value changes (not how fast its drawing moves: the
            # track also travels along the location path)
            vs = [v[0] for v in self.sample_vals]
            self.speeds = [abs(b - a) * self.vgain / (tb - ta) if tb - ta > EPS else 0.0
                           for a, b, ta, tb in zip(vs, vs[1:], self.times, self.times[1:])]
        else:
            self.speeds = []
            for i in range(len(self.points) - 1):
                dt = self.times[i + 1] - self.times[i]
                self.speeds.append((self.points[i + 1] - self.points[i]).length / dt if dt > EPS else 0.0)
        self.keys = [k for k in keys if fs - EPS <= k.t <= fe + EPS]
        for i, k in enumerate(self.keys):
            self._fill_key(tg, ch, k, i)
        self._visibility(tg)
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

    def _visibility(self, tg):
        """Hidden keys take the path segments next to them with them; what stays visible falls
        apart into pieces. On looping motion, the path outside the loop's keys is a repeat."""
        ks = self.keys
        piece = 0
        for i, k in enumerate(ks):
            k.hidden = round(k.t, FRAME_ROUND) in self.hidden
            if k.hidden:
                k.sel = k.sel_l = k.sel_r = False     # (even if selected in the Graph Editor)
            if i > 0 and (k.hidden or ks[i - 1].hidden):
                piece += 1
            k.piece = piece
        used = {LOCATION: (tg.loc,), SCALE: (tg.loc, tg.scale), VALUE: (tg.loc, tg.value)}.get(
            self.ptype, (tg.loc, tg.rot, tg.scale))
        loops = any(cycle_info(fc) for ch in used for fc in ch.fcurves)
        self.loop_range = (ks[0].t, ks[-1].t) if loops and len(ks) > 1 and ks[-1].t - ks[0].t > EPS else None
        kt = [k.t for k in ks]
        vis = []
        j = 0
        for t in self.times:
            while j < len(kt) and kt[j] < t - 1e-6:
                j += 1
            if not self.hidden:
                vis.append(True)
                continue
            # keys around t: ks[j-1] (before) and ks[j] (at or after)
            if j < len(kt) and abs(kt[j] - t) < 1e-6:
                vis.append(not ks[j].hidden)
                continue
            before = ks[j - 1] if j > 0 else None
            after = ks[j] if j < len(ks) else None
            vis.append(not ((before is not None and before.hidden) or (after is not None and after.hidden)))
        rep = [False] * len(self.times)
        if self.loop_range is not None:
            first, last = self.loop_range
            for i, t in enumerate(self.times):
                if t < first - 1e-6 or t > last + 1e-6:
                    rep[i] = True
                    if self.hidden:
                        # a repeat shows what the loop shows: hidden there, hidden here
                        tw = first + ((t - first) % (last - first))
                        vis[i] = self._vis_at(vis, tw)
        self.vis = vis
        self.rep = rep

    def _vis_at(self, vis, t):
        i = bisect.bisect_left(self.times, t)
        i = min(max(i, 0), len(vis) - 1)
        return vis[i]

    def _circle_layout(self, tg):
        """(center, e1, e2, normal, radius, first frame, last frame) when scale / value paths are
        drawn around a circle, else None. The circle lies flat around the world Z axis and
        stays put while the view turns, so its handles are easy to catch."""
        st = state.settings()
        mode = st.layout if st is not None else 'AUTO'
        if mode == 'PATH':
            return None
        fs, fe = self.fs, self.fe
        heads = [self.world_head(tg, float(t)) for t in range(fs, fe + 1)]
        size = max(tg.length, max(getattr(tg.obj, "dimensions", (1.0,))) if not tg.is_bone else tg.length, 0.1)
        travel = sum((b - a).length for a, b in zip(heads, heads[1:]))
        if mode == 'AUTO' and travel > size * 0.05:
            return None
        center = sum(heads, Vector()) / max(len(heads), 1)
        e1, e2, n = Vector((1.0, 0.0, 0.0)), Vector((0.0, 1.0, 0.0)), Vector((0.0, 0.0, 1.0))
        r = st.circle_radius if st is not None and st.circle_radius > 0.0 else size * 1.2
        scene = bpy.context.scene
        if st is not None and st.circle_use_range:
            f0, f1 = st.circle_start, max(st.circle_end, st.circle_start + 1)
        elif scene.use_preview_range:
            f0, f1 = scene.frame_preview_start, scene.frame_preview_end
        else:
            f0, f1 = scene.frame_start, scene.frame_end
        return (center, e1, e2, n, r, float(f0), float(f1))

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
        k.dts_l = [0.0] * ch.count
        k.dts_r = [0.0] * ch.count
        k.types_l = [None] * ch.count
        k.types_r = [None] * ch.count
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
            if any(not m.mute and m.type != 'CYCLES' for m in fc.modifiers):
                # F-modifiers (Noise...) add to the key: a write takes their part off again, so
                # they aren't baked into the key
                if k.offs is None:
                    k.offs = [0.0] * ch.count
                k.offs[c] = fc.evaluate(t) - y
            hlx, hly = kp.handle_left
            hrx, hry = kp.handle_right
            k.types_l[c] = kp.handle_left_type
            k.types_r[c] = kp.handle_right_type
            if x - hlx > EPS:
                s_l[c] = (y - hly) / (x - hlx)
                dts_l.append(x - hlx)
                k.dts_l[c] = x - hlx
            if hrx - x > EPS:
                s_r[c] = (hry - y) / (hrx - x)
                dts_r.append(hrx - x)
                k.dts_r[c] = hrx - x
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
        old = k.s_r if side == 'R' else k.s_l
        if old and len(old) == len(vals) and self.ptype == ROTATION:
            # relative to what the solve gives for the handle as it is: a solve isn't an exact
            # inverse of a rotation (quaternions get normalized), so the first move would jump
            base = self.solve(tg, k.t, self.handle_point(tg, k, side, None, dt), ref, dt * sign, k.vp)
            kv = k.vals
            return [s0 + ((v - b) - (kvn - kv0)) * sign / dt
                    for s0, v, b, kvn, kv0 in zip(old, vals, base, key_vals, kv)]
        return [(hv - v) * sign / dt for hv, v in zip(vals, key_vals)]


# ----------------------------------------------------------------------------
# Writing to F-Curves
# ----------------------------------------------------------------------------

def raw_values(ch, ts):
    """Channel values at the times ts without the F-modifiers that add to the curve (all but
    Cycles, which only maps time): what the keys themselves make. Retiming works on this, the
    modifiers then add theirs on top again (instead of being baked into the keys)."""
    mods = [m for fc in ch.fcurves if fc is not None for m in fc.modifiers
            if not m.mute and m.type != 'CYCLES']
    if not mods:
        return [ch.values(t) for t in ts]
    try:
        for m in mods:
            m.mute = True
        return [ch.values(t) for t in ts]
    finally:
        for m in mods:
            m.mute = False


def raw_curve_values(fc, ts):
    """One curve's values at the times ts without its adding F-modifiers (see raw_values)."""
    mods = [m for m in fc.modifiers if not m.mute and m.type != 'CYCLES']
    if not mods:
        return [fc.evaluate(t) for t in ts]
    try:
        for m in mods:
            m.mute = True
        return [fc.evaluate(t) for t in ts]
    finally:
        for m in mods:
            m.mute = False


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


def write_handle(ch, k, side, slopes, dt, key_vals, htype=None, touched=None, only=None):
    """Set one handle side on all channels of key k (or the channels in `only`) to the given
    slopes and length. dt the key's own length on that side (dt_l / dt_r, an average over the
    channels): each channel keeps its own."""
    own = (k.dts_r if side == 'R' else k.dts_l) if abs(dt - (k.dt_r if side == 'R' else k.dt_l)) < 1e-9 else None
    for c, fc in enumerate(ch.fcurves):
        idx = k.idx[c] if c < len(k.idx) else -1
        if fc is None or idx < 0 or (only is not None and c not in only):
            continue
        kp = fc.keyframe_points[idx]
        x = kp.co[0]
        dt_c = dt
        if own is not None and c < len(own) and own[c] > EPS:
            dt_c = own[c]
        y = key_vals[c] - (k.offs[c] if k.offs else 0.0)
        if htype is not None:
            if side == 'R':
                kp.handle_right_type = htype
            else:
                kp.handle_left_type = htype
        if side == 'R':
            kp.handle_right = (x + dt_c, y + slopes[c] * dt_c)
        else:
            kp.handle_left = (x - dt_c, y - slopes[c] * dt_c)
        old = (k.s_r if side == 'R' else k.s_l) if (k.s_r if side == 'R' else k.s_l) else None
        _sync_cycle_handle(fc, idx, side, slopes[c], htype, old[c] if old is not None and c < len(old) else None)
        if touched is not None:
            touched.add(fc)


def set_handle_types(ch, k, left=None, right=None, touched=None):
    for c, fc in enumerate(ch.fcurves):
        idx = k.idx[c] if c < len(k.idx) else -1
        if fc is None or idx < 0:
            continue
        kp = fc.keyframe_points[idx]
        sides = [side for side, new, old in (('L', left, kp.handle_left_type), ('R', right, kp.handle_right_type))
                 if new is not None and new not in AUTO_HANDLES and old in AUTO_HANDLES]
        if sides:
            _freeze_partner(fc, idx, sides)
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
        # (the keys' own values: a Noise... modifier adds on top of them, it isn't baked in)
        vals = raw_curve_values(fc, sorted(want.values()))
        for t, v in zip(sorted(want.values()), vals):
            _insert(fc, t, v, sel)
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
                     kp.type, kp.easing, kp.back, kp.amplitude, kp.period)
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
                kp.back, kp.amplitude, kp.period = d[11], d[12], d[13]
            fc.update()


# ----------------------------------------------------------------------------
# Baking
# ----------------------------------------------------------------------------

def _pose_state(objs):
    """What stepping through frames would overwrite with the animation: the transforms of these
    objects and their bones, and every other animated value they have (custom properties such as
    an IK / FK switch, constraint influences, the shape key sliders of their meshes...), as they
    are now - changes not keyed yet included."""
    saved = []
    values = []
    for obj in objs:
        tf = (tuple(obj.location), tuple(obj.rotation_quaternion), tuple(obj.rotation_euler),
              tuple(obj.rotation_axis_angle), tuple(obj.scale))
        bones = None
        if obj.type == 'ARMATURE' and obj.pose is not None:
            bones = [(pb.name, tuple(pb.location), tuple(pb.rotation_quaternion),
                      tuple(pb.rotation_euler), tuple(pb.rotation_axis_angle), tuple(pb.scale))
                     for pb in obj.pose.bones]
        saved.append((obj.name, tf, bones))
        data = getattr(obj, "data", None)
        for idb in (obj, data, getattr(data, "shape_keys", None)):
            values += _animated_values(idb)
    saved.append(("#values", values, None))
    return saved


def _animated_values(idb):
    """[(ID, data path, index, value)] of the animated values of idb that aren't transforms (those
    are kept on their own) and aren't driven (a driver sets them, not the curve)."""
    coll = compat.get_fcurves(idb) if idb is not None else None
    if coll is None:
        return []
    ad = idb.animation_data
    driven = {(d.data_path, d.array_index) for d in ad.drivers}
    out = []
    for fc in coll:
        dp = fc.data_path
        if dp.rsplit(".", 1)[-1] in _TRANSFORM_NAMES or (dp, fc.array_index) in driven:
            continue
        try:
            v = idb.path_resolve(dp)
        except (ValueError, TypeError, AttributeError):
            continue
        if hasattr(v, "__len__") and not isinstance(v, str):
            if fc.array_index >= len(v):
                continue
            v = v[fc.array_index]
        if isinstance(v, (int, float, bool)):
            out.append((idb, dp, fc.array_index, v))
    return out


_TRANSFORM_NAMES = {"location", "rotation_quaternion", "rotation_euler", "rotation_axis_angle", "scale"}


def _set_value(idb, dp, index, v):
    """Set an animated value by its data path (a property or a custom property, an array item)."""
    if dp.endswith('"]'):
        cut = dp.rfind('["')
        owner = idb.path_resolve(dp[:cut]) if cut > 0 else idb
        key = dp[cut + 2:-2].replace('\\"', '"')
        cur = owner[key]
        if hasattr(cur, "__len__"):
            if cur[index] != v:
                cur[index] = v
                return True
        elif cur != v:
            owner[key] = v
            return True
        return False
    owner_path, _dot, attr = dp.rpartition(".")
    owner = idb.path_resolve(owner_path) if owner_path else idb
    cur = getattr(owner, attr)
    if hasattr(cur, "__len__") and not isinstance(cur, str):
        if cur[index] != v:
            cur[index] = v
            return True
    elif cur != v:
        setattr(owner, attr, v)
        return True
    return False


def _restore_pose_state(saved):
    """Put the saved transforms and values back; True if any had changed."""
    changed = False
    for name, tf, bones in saved:
        if name == "#values":
            for idb, dp, index, v in tf:
                try:
                    changed |= _set_value(idb, dp, index, v)
                except (ReferenceError, ValueError, TypeError, AttributeError, KeyError, IndexError):
                    pass
            continue
        obj = bpy.data.objects.get(name)
        if obj is None:
            continue
        for prop, v in zip(("location", "rotation_quaternion", "rotation_euler",
                            "rotation_axis_angle", "scale"), tf):
            if tuple(getattr(obj, prop)) != v:
                setattr(obj, prop, v)
                changed = True
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
                        changed = True
    return changed


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


SINGULAR = 1e-12       # |determinant| below this: a zero scale, the matrix can't be inverted


def _fill_singular(mats, ok):
    """Parent spaces of frames with a zero scale: blended from the nearest good frames before and
    after (the parent space doesn't depend on the bone's / object's own scale; a parent that is
    scaled to nothing has no usable space, so editing there works as on the frames around)."""
    n = len(mats)
    if all(ok) or not any(ok):
        return
    prev = [None] * n
    last = None
    for i in range(n):
        if ok[i]:
            last = i
        prev[i] = last
    nxt = [None] * n
    last = None
    for i in range(n - 1, -1, -1):
        if ok[i]:
            last = i
        nxt[i] = last
    for i in range(n):
        if ok[i]:
            continue
        a, b = prev[i], nxt[i]
        if a is None:
            mats[i] = mats[b].copy()
        elif b is None:
            mats[i] = mats[a].copy()
        else:
            f = (i - a) / float(b - a)
            mats[i] = mats[a] + (mats[b] - mats[a]) * f


def _meshes_not_needed(scene, objs):
    """Meshes bent by an armature that the paths' bones / objects don't depend on (not a parent,
    a constraint's target, a driver's): they can be left out while stepping through frames."""
    needed = set(objs)
    todo = list(objs.values())
    while todo:
        o = todo.pop()
        refs = []
        owners = [o] + (list(o.pose.bones) if o.type == 'ARMATURE' and o.pose is not None else [])
        for ow in owners:
            for con in ow.constraints:
                refs.append(getattr(con, "target", None))
                for t in getattr(con, "targets", ()):       # (the Armature constraint)
                    refs.append(getattr(t, "target", None))
        for idb in (o, getattr(o, "data", None)):
            ad = getattr(idb, "animation_data", None)
            for d in (ad.drivers if ad is not None else ()):
                for v in d.driver.variables:
                    refs += [t.id for t in v.targets]
        if o.parent is not None:
            refs.append(o.parent)
        for r in refs:
            if isinstance(r, bpy.types.Object) and r.name not in needed:
                needed.add(r.name)
                todo.append(r)
    return [o for o in scene.objects if o.type == 'MESH' and not o.hide_viewport and o.name not in needed
            and any(m.type == 'ARMATURE' and m.show_viewport for m in o.modifiers)]


def scene_by_name(name):
    return bpy.data.scenes.get(name)


class BakeJob:
    """The bake (parent spaces of the caches, frame by frame) done in steps, so a heavy scene
    can be baked in the background without freezing Blender: each step steps through as many
    frames as fit in its time and puts the frame and the poses back. The old parent spaces stay
    drawn until finish() puts the new ones in."""

    def __init__(self, scene, caches):
        # (by name: undo frees the scene, a job outlives it)
        self.scene_name = scene.name
        self.ok_start = False
        settings = state.settings(scene)
        if not caches or settings is None:
            return
        self.fs, self.fe = bake_range(scene, settings, caches)
        self.f = self.fs
        self.items = []         # [cache, its matrices, frames ok]
        for c in caches:
            obj, owner = resolve(c.obj_name, c.bone_name)
            if owner is None:
                c.B = []
                c.valid = False
                c.failed = True
                continue
            self.items.append([c, [], []])
        self.ok_start = bool(self.items)
        self.t_spent = 0.0
        # the hashes of now: what changes while this runs is seen as a change afterwards
        state.remember_action_hashes()

    @property
    def done(self):
        return not self.ok_start or self.f > self.fe

    def step(self, budget=None):
        """Bake frames for up to `budget` seconds (None: all of them). Returns True when done."""
        if self.done:
            return True
        scene = scene_by_name(self.scene_name)
        if scene is None:
            return self._abort()
        state.flush_changes()       # (what changed a moment ago is seen as a change, not baked in quietly)
        live = []
        objs = {}
        for item in self.items:
            obj, owner = resolve(item[0].obj_name, item[0].bone_name)
            if owner is None:
                return self._abort()
            live.append((item, obj, owner))
            o = obj
            while o is not None:
                objs[o.name] = o
                o = o.parent
        if budget is None:
            frames = self.fe - self.f + 1
        else:
            # frames this step will get through (from the last step's pace)
            frames = max(1, min(self.fe - self.f + 1, int(budget / max(getattr(self, "pace", 0.01), 1e-4))))
        hide = [o for o in _meshes_not_needed(scene, objs) if state.can_hide(o, frames, budget is not None)]
        for o in scene.objects:
            if state.is_animated(o):
                objs.setdefault(o.name, o)      # (their unkeyed changes stay too)
        saved = _pose_state(objs.values())
        cur = scene.frame_current
        sub = scene.frame_subframe
        state.baking = True
        hidden = []
        t0 = time.time()
        f_start = self.f
        try:
            for o in hide:
                # bent meshes nothing here depends on: left out of the evaluation (like the
                # onion ghosts do), so a heavy character steps through its frames fast
                o.hide_viewport = True
                hidden.append(o)
            while self.f <= self.fe:
                scene.frame_set(self.f)
                for (c, mats, ok), obj, owner in live:
                    if c.bone_name:
                        W = obj.matrix_world @ owner.matrix
                    else:
                        W = owner.matrix_world.copy()
                    basis = owner.matrix_basis
                    mats.append(W @ basis.inverted_safe())
                    # a zero scale (its own, or a parent's) on this frame: W / basis can't be
                    # inverted, so the parent space here is taken from the frames around it
                    ok.append(abs(basis.to_3x3().determinant()) > SINGULAR and
                              abs(W.to_3x3().determinant()) > SINGULAR)
                self.f += 1
                if budget is not None and time.time() - t0 > budget:
                    break
            self.pace = (time.time() - t0) / max(self.f - f_start, 1)
            if self.f > self.fe:
                # how long a whole bake takes here (steps: their frames; once: all of it)
                if budget is not None:
                    state.bake_seconds = self.pace * (self.fe - self.fs + 1)
        finally:
            for o in hidden:
                o.hide_viewport = False
            t_back = time.time()
            scene.frame_set(cur, subframe=sub)
            state.unhide_cost(hidden, time.time() - t_back)
            if _restore_pose_state(saved):
                # worked in now, while it counts as ours: later it would look like an edit (the
                # ghosts, the center of mass and the auto update would start again - a loop)
                bpy.context.view_layer.update()
            state.baking = False
            if budget is None and self.f > self.fe:
                state.bake_seconds = time.time() - t0      # (all of it, showing meshes again too)
        return self.done

    def _abort(self):
        self.ok_start = False
        self.items = []
        return True

    def finish(self):
        """The new parent spaces into the caches (those still in use)."""
        if not self.ok_start or self.f <= self.fe:
            return
        alive = set(id(c) for c in state.caches.values())
        for c, mats, ok in self.items:
            if id(c) not in alive:
                continue
            _fill_singular(mats, ok)
            c.fs = self.fs
            c.B = mats
            c.failed = False
            c.bake_id += 1
            c.dirty = True
            c.refresh(force=True)


def bake(scene, caches):
    """Bake parent spaces of the given caches by stepping through the frame range (all at once;
    the automatic re-bake uses BakeJob in steps instead)."""
    job = BakeJob(scene, caches)
    job.step(None)
    job.finish()
