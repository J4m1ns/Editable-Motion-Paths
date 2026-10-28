"""Move / Rotate / Scale of path keys and handles (G / R / S), Blender style."""

import bisect
import math
import random

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty
from bpy_extras import view3d_utils
from mathutils import Matrix, Quaternion, Vector

from . import compat, core, pick, snap, space, state

AUTO_TYPES = {'AUTO', 'AUTO_CLAMPED'}
AXIS_NAMES = "XYZ"
AXIS_COLORS = ((1.0, 0.25, 0.3, 0.9), (0.45, 0.9, 0.2, 0.9), (0.25, 0.5, 1.0, 0.9))


# ----------------------------------------------------------------------------
# Numeric input
# ----------------------------------------------------------------------------

_NUMPAD = {
    'NUMPAD_0': '0', 'NUMPAD_1': '1', 'NUMPAD_2': '2', 'NUMPAD_3': '3', 'NUMPAD_4': '4',
    'NUMPAD_5': '5', 'NUMPAD_6': '6', 'NUMPAD_7': '7', 'NUMPAD_8': '8', 'NUMPAD_9': '9',
    'NUMPAD_PERIOD': '.', 'NUMPAD_MINUS': '-', 'NUMPAD_PLUS': '+', 'NUMPAD_ASTERIX': '*',
    'NUMPAD_SLASH': '/',
}
_CHARS = set("0123456789.+-*/()")
_KEYS = {'ZERO': '0', 'ONE': '1', 'TWO': '2', 'THREE': '3', 'FOUR': '4', 'FIVE': '5', 'SIX': '6',
         'SEVEN': '7', 'EIGHT': '8', 'NINE': '9', 'PERIOD': '.', 'MINUS': '-'}


def _safe_eval(s):
    if not s or any(c not in _CHARS for c in s):
        return None
    try:
        return float(eval(s, {"__builtins__": {}}, {}))
    except Exception:
        if s in ("-", "+"):
            return 0.0
        return None


class NumInput:
    def __init__(self, count=3):
        self.count = count
        self.fields = [''] * count
        self.idx = 0

    @property
    def active(self):
        return any(self.fields)

    def handle(self, event):
        if event.value != 'PRESS':
            return False
        t = event.type
        if t == 'BACK_SPACE':
            if not self.active:
                return False
            if event.ctrl:
                self.fields[self.idx] = ''
            else:
                self.fields[self.idx] = self.fields[self.idx][:-1]
            return True
        if t == 'TAB' and self.active:
            self.idx = (self.idx + 1) % self.count
            return True
        ch = _NUMPAD.get(t) or (event.ascii if event.ascii and event.ascii in _CHARS else None)
        if ch is None and not event.shift:
            ch = _KEYS.get(t)
        if not ch or event.ctrl or event.alt:
            return False
        self.fields[self.idx] += ch
        return True

    def value(self, i):
        return _safe_eval(self.fields[i])

    def text(self, i, current, fmt):
        if self.fields[i] or self.idx == i and self.active:
            s = self.fields[i]
            return "[%s|]" % s if i == self.idx else "[%s]" % s
        return fmt % current


# ----------------------------------------------------------------------------
# View helpers
# ----------------------------------------------------------------------------

def _ray(region, rv3d, co):
    return (view3d_utils.region_2d_to_origin_3d(region, rv3d, co),
            view3d_utils.region_2d_to_vector_3d(region, rv3d, co))


def _plane_hit(region, rv3d, co, p0, n):
    o, d = _ray(region, rv3d, co)
    den = d.dot(n)
    if abs(den) < 1e-8:
        return None
    return o + d * ((p0 - o).dot(n) / den)


def _view_dir(rv3d):
    """Direction the view looks into (away from the viewer)."""
    return (rv3d.view_rotation @ Vector((0.0, 0.0, -1.0))).normalized()


def _normalized_cols(m3):
    cols = []
    for i in range(3):
        c = m3.col[i].copy()
        if c.length < 1e-9:
            c = Vector([1.0 if j == i else 0.0 for j in range(3)])
        cols.append(c.normalized())
    m = Matrix((cols[0], cols[1], cols[2])).transposed()
    return m


# ----------------------------------------------------------------------------
# Elements
# ----------------------------------------------------------------------------

class Elem:
    __slots__ = ("cache", "k", "part", "basis", "m", "co", "center", "weight", "dist", "src",
                 "rand", "touched")

    def __init__(self, cache, k, part, basis, m=0.0, center=None):
        self.cache = cache
        self.k = k
        self.part = part
        self.basis = basis
        self.m = m      # soft minimum handle length used for display (see pick.display_handle)
        self.co = k.co if part == 'KEY' else pick.display_handle(k, part, m)
        self.center = center    # rotation paths: bone head the tip rotates around
        self.weight = 1.0       # proportional editing influence (1 for selected elements)
        self.dist = None        # proportional: distance to the nearest selected key
        self.src = None         # proportional: that nearest selected element
        self.rand = random.random()
        self.touched = False


def falloff(kind, x, rnd=0.5):
    """Blender's proportional editing falloff curves; x = distance / radius."""
    if x >= 1.0:
        return 0.0
    t = 1.0 - max(0.0, x)
    if kind == 'SMOOTH':
        return 3.0 * t * t - 2.0 * t * t * t
    if kind == 'SPHERE':
        return math.sqrt(max(0.0, 2.0 * t - t * t))
    if kind == 'ROOT':
        return math.sqrt(t)
    if kind == 'INVERSE_SQUARE':
        return t * (2.0 - t)
    if kind == 'SHARP':
        return t * t
    if kind == 'CONSTANT':
        return 1.0
    if kind == 'RANDOM':
        return t * rnd
    return t


def _arc_lengths(cache):
    """Cumulative length along the path at each sample time."""
    s = [0.0]
    for a, b in zip(cache.points, cache.points[1:]):
        s.append(s[-1] + (b - a).length)
    return s


def _arc_at(cache, arc, t):
    times = cache.times
    i = bisect.bisect_left(times, t)
    if i <= 0:
        return arc[0]
    if i >= len(times):
        return arc[-1]
    t0, t1 = times[i - 1], times[i]
    f = 0.0 if t1 - t0 < 1e-9 else (t - t0) / (t1 - t0)
    return arc[i - 1] + (arc[i] - arc[i - 1]) * f


def _ray_sphere(o, d, c, r, front):
    """Point on the sphere under the ray (or the closest one if the ray misses)."""
    d = d.normalized()
    oc = o - c
    b = d.dot(oc)
    disc = b * b - (oc.dot(oc) - r * r)
    if disc >= 0.0:
        s = -b - math.sqrt(disc) if front else -b + math.sqrt(disc)
        return o + d * s
    p = o + d * (-b)
    v = p - c
    if v.length < 1e-12:
        return None
    return c + v.normalized() * r


def element_basis(cache, tg, k):
    return space.element_basis(cache, tg, k)


def build_elements(context, snapshots=None, created=None, connected=None, ensure=True,
                   projected=False):
    """Collect selected keys/handles of editable paths, adding missing channel keys.

    With `connected` set to True/False, unselected keys are added as proportional
    editing candidates (weight 0 until the radius reaches them).
    """
    region, rv3d = context.region, context.region_data
    has_view = rv3d is not None and context.area is not None and context.area.type == 'VIEW_3D'
    groups = []
    editable = state.editable_caches(context.scene)
    for cache in editable:
        sel_keys = [k for k in cache.keys
                    if k.sel or (k.sel_l and k.hl is not None) or (k.sel_r and k.hr is not None)]
        if not sel_keys and connected is not False:
            continue
        tg = cache.target()
        if tg is None:
            continue
        if snapshots is not None:
            snapshots.append(core.FCurveSnapshot(tg, tg.channels(cache.ptype)))
        pin = state.settings(context.scene).pin_partial
        if ensure and sel_keys and core.ensure_keys(tg, cache.ptype, [k.t for k in sel_keys], created, pin):
            cache.refresh(force=True)
            tg = cache.target()
        if ensure and cache.ptype != core.SCALE:
            # a handle dragged next to a Linear segment of some curves: that segment turns Bezier (the
            # motion stays) so the handle moves freely, not only along the other curves
            turned = set()
            ch = tg.channels(cache.ptype)
            for k in cache.keys:
                if not k.sel:
                    for side, on in (('L', k.sel_l and k.hl is not None), ('R', k.sel_r and k.hr is not None)):
                        if on:
                            core.bezier_side(ch, k, side, turned)
            if turned:
                core.update_fcurves(turned)
                cache.refresh(force=True)
                tg = cache.target()
        elems = []
        for k in cache.keys:
            center = None
            if cache.ptype == core.ROTATION and (k.sel or k.sel_l or k.sel_r):
                center = cache.rot_frame(tg, k.t)[0].translation.copy()
            if k.sel:
                elems.append(Elem(cache, k, 'KEY', element_basis(cache, tg, k), 0.0, center))
                continue
            m = pick.handle_min_world(region, rv3d, k.co) if has_view else 0.0
            if k.sel_l and k.hl is not None:
                elems.append(Elem(cache, k, 'L', element_basis(cache, tg, k), m, center))
            if k.sel_r and k.hr is not None:
                elems.append(Elem(cache, k, 'R', element_basis(cache, tg, k), m, center))
        if elems or connected is False:
            groups.append((cache, elems))
    if connected is not None:
        view = _view_dir(rv3d) if projected and rv3d is not None else None
        _add_proportional(groups, connected, view)
        groups = [(c, es) for c, es in groups if es]
    return groups


def _add_proportional(groups, connected, view=None):
    """Unselected keys as proportional candidates. Distance: along the path (connected),
    in the view plane (projected, `view` = view direction) or straight in 3D."""
    sel_all = [e for _c, es in groups for e in es if e.part == 'KEY']
    if not sel_all:
        return
    for cache, elems in groups:
        tg = cache.target()
        if tg is None:
            continue
        sel = [e for e in elems if e.part == 'KEY']
        taken = {round(e.k.t, 3) for e in elems if e.part == 'KEY'}
        if connected:
            if not sel:
                continue
            arc = _arc_lengths(cache)
            sel_s = [(_arc_at(cache, arc, e.k.t), e) for e in sel]
        for k in cache.keys:
            if round(k.t, 3) in taken or k.hidden:
                continue    # hidden keys work as a mask
            if connected:
                # along the path, and never across a hidden part of it
                near = [(ss, e) for ss, e in sel_s if e.k.piece == k.piece]
                if not near:
                    continue
                s = _arc_at(cache, arc, k.t)
                d, src = min(((abs(s - ss), e) for ss, e in near), key=lambda x: x[0])
            elif view is not None:
                def flat(v):
                    return (v - view * v.dot(view)).length
                d, src = min(((flat(k.co - e.k.co), e) for e in sel_all), key=lambda x: x[0])
            else:
                d, src = min((((k.co - e.k.co).length, e) for e in sel_all), key=lambda x: x[0])
            center = None
            if cache.ptype == core.ROTATION:
                center = cache.rot_frame(tg, k.t)[0].translation.copy()
            e = Elem(cache, k, 'KEY', element_basis(cache, tg, k), 0.0, center)
            e.weight = 0.0
            e.dist = d
            e.src = src
            elems.append(e)


SHORT = 0.3                 # a handle shorter than this part of its natural length is "flat"
OPEN_ANGLE = math.radians(30.0)


def _natural(k, side):
    return k.cat_r if side == 'R' else k.cat_l


def _fallback_dir(k, side):
    return k.dir_r if side == 'R' else k.dir_l


def rotated_handle(k, side, v0, new_dir, angle):
    """Handle vector after rotating it to `new_dir`. A flat (zero length) Auto Clamped
    handle can't be rotated as is, so it opens up to its natural length over the first
    30 degrees of rotation."""
    l0 = v0.length
    lc = _natural(k, side).length
    if l0 >= SHORT * lc:
        return new_dir * l0
    s = min(1.0, abs(angle) / OPEN_ANGLE)
    return new_dir * (l0 + (lc - l0) * s)


def scaled_handle(k, side, v0, f):
    """Handle vector after scaling by f; flat handles grow from their natural length."""
    l0 = v0.length
    lc = _natural(k, side).length
    if l0 >= SHORT * lc:
        return v0 * f
    d = v0.normalized() if l0 > 1e-9 else _fallback_dir(k, side)
    return d * max(0.0, l0 + (f - 1.0) * lc)


def _side_dt(k, side):
    return k.dt_r if side == 'R' else k.dt_l


def _side_type(k, side):
    return k.type_r if side == 'R' else k.type_l


def _handle_edit(cache, ch, k, side, slopes, touched):
    """Write an edited handle, keeping the opposite one aligned unless broken - channel by channel
    (one curve's handles may be broken while another's are aligned: each keeps its own way)."""
    other = 'L' if side == 'R' else 'R'
    this_t = (k.types_r if side == 'R' else k.types_l) or [_side_type(k, side)] * ch.count
    other_t = (k.types_l if side == 'R' else k.types_r) or [_side_type(k, other)] * ch.count
    broken = {c for c in range(ch.count) if this_t[c] in {'FREE', 'VECTOR'} or other_t[c] in {'FREE', 'VECTOR'}}
    joined = set(range(ch.count)) - broken
    if broken:
        core.write_handle(ch, k, side, slopes, _side_dt(k, side), k.vals, 'FREE', touched, only=broken)
    if joined:
        core.write_handle(ch, k, side, slopes, _side_dt(k, side), k.vals, 'ALIGNED', touched, only=joined)
        if _side_dt(k, other) > core.EPS or any(d > core.EPS for d in ((k.dts_l if other == 'L' else k.dts_r) or ())):
            core.write_handle(ch, k, other, slopes, _side_dt(k, other), k.vals, 'ALIGNED', touched, only=joined)


# ----------------------------------------------------------------------------
# Operator
# ----------------------------------------------------------------------------

FALLOFFS = [
    ('SMOOTH', "Smooth", "", 'SMOOTHCURVE', 0), ('SPHERE', "Sphere", "", 'SPHERECURVE', 1),
    ('ROOT', "Root", "", 'ROOTCURVE', 2), ('INVERSE_SQUARE', "Inverse Square", "", 'INVERSESQUARECURVE', 3),
    ('SHARP', "Sharp", "", 'SHARPCURVE', 4), ('LINEAR', "Linear", "", 'LINCURVE', 5),
    ('CONSTANT', "Constant", "", 'NOCURVE', 6), ('RANDOM', "Random", "", 'RNDCURVE', 7)]
ROT_MODES = {'ROTATE', 'TRACKBALL'}
TRACKBALL_SPEED = 0.01      # radians per pixel


def _ts_get(ts, name, default):
    return getattr(ts, name, default)


class EMP_OT_transform(bpy.types.Operator):
    """Move (G), rotate (R) or scale (S) the selected path keys and handles"""
    bl_idname = "emp.transform"
    bl_label = "Transform Path Keys"
    bl_options = {'REGISTER', 'UNDO', 'BLOCKING'}

    mode: EnumProperty(name="Mode", items=[
        ('TRANSLATE', "Move", "Move keys / handles"),
        ('ROTATE', "Rotate", "Rotate keys around the pivot point; a single key (or every key with "
                             "Individual Origins) turns its handles"),
        ('TRACKBALL', "Trackball", "Rotate freely around the pivot point (R R)"),
        ('SCALE', "Scale", "Scale keys around the pivot point; a single key (or every key with "
                           "Individual Origins) scales its handles (speed through the key)"),
        ('SHEAR', "Shear", "Slant the selected keys: they move sideways in proportion to their "
                           "height above the pivot (Shift+Ctrl+Alt+S)"),
    ], default='TRANSLATE')
    orient: EnumProperty(name="Orientation", items=space.ORIENTATIONS, default='GLOBAL')
    orient_matrix: FloatVectorProperty(size=9, default=(1, 0, 0, 0, 1, 0, 0, 0, 1),
                                       options={'HIDDEN'})
    value: FloatVectorProperty(name="Value", size=3, default=(0.0, 0.0, 0.0))
    axis: FloatVectorProperty(name="Axis", size=3, default=(0.0, 0.0, 1.0), subtype='DIRECTION')
    axis2: FloatVectorProperty(size=3, default=(1.0, 0.0, 0.0), options={'HIDDEN'})
    angle: FloatProperty(name="Angle", subtype='ANGLE', default=0.0)
    twist: BoolProperty(name="Twist", default=False, options={'SKIP_SAVE'},
                        description="Rotation paths: twist the bones around their own axis (Ctrl+T)")
    use_proportional: BoolProperty(name="Proportional Editing", default=False)
    proportional_size: FloatProperty(name="Proportional Size", default=0.3, min=1e-4,
                                     subtype='DISTANCE', unit='LENGTH')
    proportional_falloff: EnumProperty(name="Falloff", items=FALLOFFS, default='SMOOTH')
    proportional_connected: BoolProperty(name="Connected", default=True,
                                         description="Measure the distance along the path")
    proportional_projected: BoolProperty(name="Projected (2D)", default=False,
                                         description="Measure the distance on the screen")
    release_confirm: BoolProperty(options={'HIDDEN', 'SKIP_SAVE'})
    axis_index: IntProperty(default=-1, options={'HIDDEN', 'SKIP_SAVE'})
    # scaling: measure the mouse from here instead of the pivot (the Y scale ribbon's middle line)
    measure_from: FloatVectorProperty(size=3, options={'HIDDEN', 'SKIP_SAVE'})
    use_measure_from: BoolProperty(options={'HIDDEN', 'SKIP_SAVE'})
    axis_plane: BoolProperty(options={'HIDDEN', 'SKIP_SAVE'})
    ring_axis: IntProperty(default=-1, options={'HIDDEN', 'SKIP_SAVE'})
    key_trackball: BoolProperty(options={'HIDDEN', 'SKIP_SAVE'},
                                description="Rotation keys turn like a trackball (dragging the key)")
    time_wheel: BoolProperty(options={'HIDDEN', 'SKIP_SAVE'},
                             description="Mouse wheel moves the keys in time (Shift+D)")
    start_x: IntProperty(default=-100000, options={'HIDDEN', 'SKIP_SAVE'})
    start_y: IntProperty(default=-100000, options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == 'VIEW_3D'

    # -- shared --------------------------------------------------------------

    def _prepare(self, context, snapshots=None, created=None, candidates=False, ensure=True):
        connected = None
        if candidates or self.use_proportional:
            connected = self.proportional_connected
        self.groups = build_elements(context, snapshots, created, connected, ensure,
                                     self.proportional_projected)
        keys = [(c, e.k) for c, es in self.groups for e in es if e.part == 'KEY' and e.src is None]
        handles = [(c, e.k) for c, es in self.groups for e in es if e.part != 'KEY' and e.src is None]
        self.n_elems = len(keys) + len(handles)
        try:
            # Blender's Continuous Acceleration re-solves a whole curve's auto handles when one
            # key changes: say so, the path will move beyond the edited key
            from .ops_tools import cont_accel_curves
            from .props import prefs
            p = prefs()
            if p is not None and p.ca_notice and cont_accel_curves(context):
                state.flash_message("Continuous Acceleration is on for these curves: the whole curve re-solves "
                                    "(Sidebar > Curve Smoothing > Smoothing: None)", 4.0)
        except Exception:
            pass
        self.pivot, individual = space.pivot_point(context, keys, handles)
        self.median = space.median([k.co for _c, k in keys + handles]) if keys or handles \
            else self.pivot
        if keys:
            # one key turns / scales its handles (like a curve point with its handles), unless
            # it turns around something else (3D cursor)
            single = len(keys) == 1 and not handles and \
                context.scene.tool_settings.transform_pivot_point != 'CURSOR'
            self.individual = individual
            self.handle_mode = single or individual
        else:
            self.individual = True
            self.handle_mode = False
        return self.n_elems > 0

    def _main_elem(self):
        for _c, es in self.groups:
            for e in es:
                if e.src is None and e.part == 'KEY':
                    return e
        for _c, es in self.groups:
            for e in es:
                if e.src is None:
                    return e
        return None

    def _axes(self, e):
        """World axes of the orientation for element e (None = global)."""
        if self.orient == 'GLOBAL':
            return None
        if self.orient == 'LOCAL':
            return e.basis
        m = self.orient_matrix
        return Matrix((m[0:3], m[3:6], m[6:9]))

    def _set_orient(self, context, otype):
        self.orient = otype
        e = self._main_elem()
        if otype in {'GLOBAL', 'LOCAL'} or e is None:
            return
        M = space.orientation_matrix(context, otype, e.cache, e.k)
        self.orient_matrix = [x for row in M for x in row]

    def _update_weights(self):
        r = max(self.proportional_size, 1e-6)
        for _c, es in self.groups:
            for e in es:
                if e.src is not None:
                    e.weight = (falloff(self.proportional_falloff, e.dist / r, e.rand)
                                if self.use_proportional else 0.0)

    def _rot_amount(self):
        if self.mode == 'TRACKBALL':
            return math.hypot(self.value[0], self.value[1])
        return self.angle

    def _elem_matrix(self, e):
        pivot = e.k.co if self.individual else self.pivot
        w = e.weight
        A = self._axes(e)
        if self.mode == 'TRANSLATE':
            deltas = getattr(self, "elem_delta", None)
            src = e.src or e
            if deltas and id(src) in deltas:
                return Matrix.Translation(deltas[id(src)] * w)
            v = Vector(self.value)
            d = A @ v if A is not None else v
            return Matrix.Translation(d * w)
        if self.mode == 'SHEAR':
            a, b = Vector(self.axis), Vector(self.axis2)
            M = Matrix.Identity(3)
            f = self.value[0] * w
            for i in range(3):
                for j in range(3):
                    M[i][j] += f * a[i] * b[j]
            return Matrix.Translation(pivot) @ M.to_4x4() @ Matrix.Translation(-pivot)
        if self.mode == 'TRACKBALL':
            R = Matrix.Rotation(self.value[0] * w, 4, Vector(self.axis)) @ \
                Matrix.Rotation(self.value[1] * w, 4, Vector(self.axis2))
            return Matrix.Translation(pivot) @ R @ Matrix.Translation(-pivot)
        if self.mode == 'ROTATE':
            ax = Vector(self.axis)
            if A is not None:
                ax = A @ ax
            if ax.length < 1e-9:
                return Matrix.Identity(4)
            R = Matrix.Rotation(self.angle * w, 4, ax.normalized())
            return Matrix.Translation(pivot) @ R @ Matrix.Translation(-pivot)
        S = Matrix.Diagonal(Vector([1.0 + (v - 1.0) * w for v in self.value])).to_3x3()
        if A is not None:
            S = A @ S @ A.transposed()
        return Matrix.Translation(pivot) @ S.to_4x4() @ Matrix.Translation(-pivot)

    def _is_identity(self):
        if self.mode == 'TRANSLATE':
            deltas = getattr(self, "elem_delta", None) or {}
            return Vector(self.value).length < 1e-9 and all(d.length < 1e-9 for d in deltas.values())
        if self.mode == 'ROTATE':
            return abs(self.angle) < 1e-9
        if self.mode == 'TRACKBALL':
            return abs(self.value[0]) < 1e-9 and abs(self.value[1]) < 1e-9
        if self.mode == 'SHEAR':
            return abs(self.value[0]) < 1e-9
        return all(abs(v - 1.0) < 1e-9 for v in self.value)

    def _apply(self, context):
        if getattr(self, "lazy", False) and not self.applied and self._is_identity():
            return      # nothing moved yet: leave the curves exactly as they are
        self.applied = True
        touched = set()
        self._update_weights()
        core.cycle_skip = {round(e.k.t, 3) for _c, es in self.groups for e in es
                           if e.src is None or e.weight > 0.0}
        weights = {}
        for cache, elems in self.groups:
            tg = cache.target()
            if tg is None:
                continue
            ch = tg.channels(cache.ptype)
            for e in elems:
                if e.src is not None:
                    if e.weight <= 0.0:
                        if not e.touched:
                            continue    # still untouched: nothing to restore
                        e.touched = False
                    else:
                        e.touched = True
                        weights[(cache.key, round(e.k.t, 3))] = e.weight
                X = self._elem_matrix(e)
                if e.part == 'KEY':
                    self._apply_key(cache, tg, ch, e, X, touched)
                else:
                    W = self._handle_target(e, X)
                    slopes = cache.slopes_from_point(tg, e.k, e.part, W, e.k.vals, _side_dt(e.k, e.part))
                    _handle_edit(cache, ch, e.k, e.part, slopes, touched)
            cache.dirty = True
        state.prop_weights = weights
        core.update_fcurves(touched)
        pick.tag_anim_editors()

    def _handle_target(self, e, X):
        """World position of an edited handle end (element = handle)."""
        k = e.k
        h = k.hr if e.part == 'R' else k.hl
        v0 = h - k.co
        if self.individual and self.mode in ROT_MODES:
            d = (X @ e.co) - k.co
            if d.length > 1e-12:
                return k.co + rotated_handle(k, e.part, v0, d.normalized(), self._rot_amount())
        if self.individual and self.mode == 'SCALE' and self.orient == 'GLOBAL' \
                and len(set(round(v, 6) for v in self.value)) == 1:
            return k.co + scaled_handle(k, e.part, v0, self.value[0])
        # the handle moves with the mouse (relative), so short handles drawn longer than they
        # are can still be pulled right up to the key or through it
        return X @ h

    def _key_handle_target(self, k, side, X):
        """New world handle end when a single key's handles are rotated/scaled (R / S)."""
        h = k.hr if side == 'R' else k.hl
        v0 = h - k.co
        if self.mode in ROT_MODES:
            d0 = v0.normalized() if v0.length > 1e-9 else _fallback_dir(k, side)
            d = (X @ (k.co + d0)) - k.co
            if d.length > 1e-12:
                return k.co + rotated_handle(k, side, v0, d.normalized(), self._rot_amount())
        elif self.mode == 'SCALE' and len(set(round(v, 6) for v in self.value)) == 1:
            return k.co + scaled_handle(k, side, v0, self.value[0])
        return X @ h

    def _apply_key(self, cache, tg, ch, e, X, touched):
        k = e.k
        handle_mode = self.handle_mode and e.src is None
        if cache.ptype == core.SCALE:
            # scale paths: S / Alt+S make the ribbon wider or thinner (X / Y / Z: one axis)
            if self.mode != 'SCALE':
                return
            locks = tg.scale_locks()
            vals = [v if locks[i] else v * (1.0 + (f - 1.0) * e.weight)
                    for i, (v, f) in enumerate(zip(k.vals, self.value))]
            core.write_key(ch, k, vals, True, touched)
            return
        if cache.ptype == core.LOCATION:
            vals = cache.solve(tg, k.t, X @ k.co, k.vals)
            core.write_key(ch, k, vals, True, touched)
            if self.mode == 'TRANSLATE':
                return
            for side, h in (('L', k.hl), ('R', k.hr)):
                if h is None:
                    continue
                htype = _side_type(k, side)
                if htype in AUTO_TYPES and not handle_mode:
                    continue
                new_type = 'ALIGNED' if htype in AUTO_TYPES else ('FREE' if htype == 'VECTOR' else None)
                W = self._key_handle_target(k, side, X) if handle_mode else X @ h
                slopes = cache.slopes_from_point(tg, k, side, W, vals, _side_dt(k, side))
                core.write_handle(ch, k, side, slopes, _side_dt(k, side), vals, new_type, touched)
            return

        # rotation path: keys move like in the Graph Editor, their handles move with them
        # (same slopes on every channel), so the curves never change in unexpected ways
        if self.mode == 'TRANSLATE':
            vals = cache.solve(tg, k.t, X @ k.co, k.vals)
        elif self.mode == 'ROTATE' and self.twist:
            vals = cache.twist(tg, k.vals, self.angle * e.weight)
        elif self.mode in ROT_MODES:
            vals = cache.solve(tg, k.t, X @ k.co, k.vals)
            if handle_mode:
                # one key: turn its handles around it (like on location paths)
                for side in ('L', 'R'):
                    h = k.hr if side == 'R' else k.hl
                    dt = _side_dt(k, side)
                    if h is None or dt < core.EPS:
                        continue
                    W = self._key_handle_target(k, side, X)
                    s = cache.slopes_from_point(tg, k, side, W, vals, dt)
                    htype = _side_type(k, side)
                    new_type = 'ALIGNED' if htype in AUTO_TYPES else ('FREE' if htype == 'VECTOR' else None)
                    core.write_handle(ch, k, side, s, dt, vals, new_type, touched)
                return
        elif self.mode != 'SCALE' or not handle_mode:
            # scale / shear / mirror: the tips go to the transformed points
            vals = cache.solve(tg, k.t, X @ k.co, k.vals)
        else:
            f = sum(self.value) / 3.0
            vals = list(k.vals)
            for side in ('L', 'R'):
                dt = _side_dt(k, side)
                if dt < core.EPS:
                    continue
                h = k.hr if side == 'R' else k.hl
                if h is not None and (h - k.co).length < SHORT * _natural(k, side).length:
                    # flat handle: grow it in world space from its natural length
                    W = k.co + scaled_handle(k, side, h - k.co, f)
                    s = cache.slopes_from_point(tg, k, side, W, vals, dt)
                else:
                    s = [x * f for x in (k.s_r if side == 'R' else k.s_l)]
                htype = _side_type(k, side)
                new_type = 'ALIGNED' if htype in AUTO_TYPES else ('FREE' if htype == 'VECTOR' else None)
                core.write_handle(ch, k, side, s, dt, vals, new_type, touched)
            return
        core.write_key(ch, k, vals, True, touched)

    # -- execute (redo panel) -------------------------------------------------

    def execute(self, context):
        if not self._prepare(context):
            return {'CANCELLED'}
        self._apply(context)
        return {'FINISHED'}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, "mode")
        if self.mode in {'TRANSLATE', 'SCALE'}:
            layout.prop(self, "value", text="Move" if self.mode == 'TRANSLATE' else "Scale")
        elif self.mode == 'SHEAR':
            layout.prop(self, "value", index=0, text="Factor")
        elif self.mode == 'TRACKBALL':
            col = layout.column(align=True)
            col.prop(self, "value", index=0, text="Angle")
            col.prop(self, "value", index=1, text=" ")      # (" ": the same width as the one above)
        else:
            layout.prop(self, "angle")
            layout.prop(self, "twist")
        layout.prop(self, "orient")
        col = layout.column(heading="Proportional")
        col.prop(self, "use_proportional", text="")
        sub = col.column()
        sub.active = self.use_proportional
        sub.prop(self, "proportional_falloff")
        sub.prop(self, "proportional_size")
        sub.prop(self, "proportional_connected")
        sub.prop(self, "proportional_projected")

    # -- interactive -----------------------------------------------------------

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D' or context.region_data is None:
            return {'PASS_THROUGH'}
        self.snapshots = []
        self.created = []
        caches = state.editable_caches(context.scene)
        if not caches or pick.selected_count(caches) == 0:
            return {'PASS_THROUGH'}
        if self.twist and not any(c.ptype == core.ROTATION for c in caches):
            return {'PASS_THROUGH'}     # Ctrl+T twists bones on rotation paths only
        if all(c.ptype == core.SCALE for c in caches) and self.mode != 'SCALE':
            if self.mode == 'TRANSLATE':
                return bpy.ops.emp.slide('INVOKE_DEFAULT', slide_mode='TIMING')     # G on scale paths: timing
            return {'PASS_THROUGH'}
        ts = context.scene.tool_settings
        st = state.settings(context.scene)
        self.use_proportional = ts.use_proportional_edit
        self.proportional_size = st.proportional_size
        self.proportional_falloff = ts.proportional_edit_falloff
        self.proportional_connected = ts.use_proportional_connected
        self.proportional_projected = ts.use_proportional_projected
        self.lazy = True
        self.applied = False
        self.ensured = False
        self.mods = (False, False)
        self.snapper = None
        self.snap_hit = None
        if not self._prepare(context, self.snapshots, self.created, candidates=True, ensure=False):
            self._restore()
            return {'PASS_THROUGH'}
        if self.start_x > -100000:
            self.mouse_start = Vector((self.start_x, self.start_y))
        else:
            self.mouse_start = Vector((event.mouse_region_x, event.mouse_region_y))
        self.mouse_virtual = self.mouse_start.copy()
        if self.start_x > -100000:
            self.mouse_last = self.mouse_start.copy()
        else:
            self.mouse_last = Vector((event.mouse_region_x, event.mouse_region_y))
        self.constraint = (self.axis_index, self.axis_plane) if self.axis_index >= 0 else None
        self.orient = 'GLOBAL'
        self.numinput = NumInput()
        self.value = (1.0, 1.0, 1.0) if self.mode == 'SCALE' else (0.0, 0.0, 0.0)
        self.angle = 0.0
        self._ring_state = {}
        self._reset_mode_state(context)
        for cache, _es in self.groups:
            state.ghosts[cache.key] = list(cache.points)
        state.modal_running += 1
        state.hud_area = context.area.as_pointer()
        context.window_manager.modal_handler_add(self)
        self._compute(context, event)
        self._update(context)
        return {'RUNNING_MODAL'}

    def _reset_mode_state(self, context):
        region, rv3d = context.region, context.region_data
        p = Vector(self.measure_from) if self.use_measure_from and self.mode == 'SCALE' else self.pivot
        self.pivot_2d = view3d_utils.location_3d_to_region_2d(region, rv3d, p) or self.mouse_start
        # with the mouse right on the key (you just clicked it), angles around the key are
        # unstable: measure them around a point a bit to the left instead
        us = compat.ui_scale()
        if (self.mouse_virtual - self.pivot_2d).length < 40.0 * us:
            self.angle_center = self.mouse_virtual - Vector((80.0 * us, 0.0))
        else:
            self.angle_center = self.pivot_2d
        self.scale_start = (self.mouse_virtual - self.pivot_2d).length
        v = self.mouse_virtual - self.angle_center
        self.angle_acc = 0.0
        self.angle_prev = math.atan2(v.y, v.x)
        # a gizmo handle grabbed by the mouse follows it exactly; from the keyboard the scale
        # needs some distance to stay controllable
        self.scale_d0 = max(self.scale_start, (8.0 if self.axis_index >= 0 and self.release_confirm else 40.0) * us)
        self.anchor = self.mouse_virtual.copy()
        # a gizmo handle follows the mouse along its own line (the mouse's distance across it
        # doesn't count, like Blender's scale gizmo)
        d = self.mouse_virtual - self.pivot_2d
        self.scale_dir = d.normalized() if self.axis_index >= 0 and self.release_confirm and             d.length > 4.0 * us else None

    def _restore(self):
        for s in reversed(getattr(self, "snapshots", [])):
            s.restore()
        for c in state.caches.values():
            c.dirty = True

    def _finish(self, context):
        state.modal_running = max(0, state.modal_running - 1)
        state.ghosts.clear()
        state.constraint = None
        state.prop_circle = None
        state.prop_weights = {}
        state.snap_point = None
        ts = context.scene.tool_settings
        ts.use_proportional_edit = self.use_proportional
        ts.proportional_edit_falloff = self.proportional_falloff
        st = state.settings(context.scene)
        if st is not None:
            st.proportional_size = self.proportional_size
        context.area.header_text_set(None)
        if context.workspace:
            context.workspace.status_text_set(None)
        state.tag_redraw()

    def _axis_world(self, i):
        e = self._main_elem()
        A = self._axes(e) if e is not None else None
        if A is not None:
            return A.col[i].normalized()
        return Vector([1.0 if j == i else 0.0 for j in range(3)])

    def _constrain_world(self, dw):
        cons = self.constraint
        if cons is None:
            return dw
        ax = self._axis_world(cons[0])
        if cons[1]:
            return dw - ax * dw.dot(ax)
        return ax * dw.dot(ax)

    def _to_orient(self, dw):
        e = self._main_elem()
        A = self._axes(e) if e is not None else None
        return A.transposed() @ dw if A is not None else dw

    def _snap_source(self, context, p):
        target = context.scene.tool_settings.snap_target
        sel = [e for _c, es in self.groups for e in es if e.src is None]
        if target == 'CLOSEST' and sel:
            return min((e.co for e in sel), key=lambda q: (q - p).length)
        if target == 'ACTIVE':
            a = space.active_key([c for c, _es in self.groups])
            if a is not None:
                return a[1].co
        return self.median

    def _snap(self, context, region, rv3d):
        """Snap target under the mouse with Blender's snap elements, or None."""
        ts = context.scene.tool_settings
        geo = snap.elements(ts) & snap.GEOMETRY
        if not geo:
            return None
        if self.snapper is None:
            owners = []
            for c, _es in self.groups:
                obj = bpy.data.objects.get(c.obj_name)
                if obj is not None:
                    owners.append(obj)
            skip = {(c.key, round(e.k.t, 3)) for c, es in self.groups for e in es
                    if e.src is None or self.use_proportional}
            self.snapper = snap.Snapper(context, owners, skip)
        return self.snapper.find(region, rv3d, self.mouse_last, geo)

    def _compute(self, context, event):
        self.mods = (bool(event.ctrl), bool(event.shift))
        self.elem_delta = {}
        self.snap_hit = None
        region, rv3d = context.region, context.region_data
        ts = context.scene.tool_settings
        mouse = self.mouse_virtual
        fine = event.shift
        num = self.numinput
        cons = self.constraint
        toward = -_view_dir(rv3d)

        if self.mode == 'TRANSLATE':
            snapping = ts.use_snap != bool(event.ctrl) and _ts_get(ts, "use_snap_translate", True)
            if num.active:
                vals = [num.value(i) or 0.0 for i in range(3)]
                if cons is None:
                    v = Vector(vals)
                elif not cons[1]:
                    v = Vector([vals[0] if j == cons[0] else 0.0 for j in range(3)])
                else:
                    others = [j for j in range(3) if j != cons[0]]
                    v = Vector((0.0, 0.0, 0.0))
                    v[others[0]] = vals[0]
                    v[others[1]] = vals[1]
                self.value = v
                return
            p0 = self.pivot
            if cons is None:
                n = _view_dir(rv3d)
                a = _plane_hit(region, rv3d, self.mouse_start, p0, n)
                b = _plane_hit(region, rv3d, mouse, p0, n)
                d = (b - a) if a is not None and b is not None else Vector()
            elif not cons[1]:
                ax = self._axis_world(cons[0])
                n = ax.cross(_view_dir(rv3d)).cross(ax)
                amount = 0.0
                if n.length > 1e-4:
                    a = _plane_hit(region, rv3d, self.mouse_start, p0, n.normalized())
                    b = _plane_hit(region, rv3d, mouse, p0, n.normalized())
                    if a is not None and b is not None:
                        amount = (b - a).dot(ax)
                d = Vector([amount if j == cons[0] else 0.0 for j in range(3)])
            else:
                ax = self._axis_world(cons[0])
                if abs(ax.dot(_view_dir(rv3d))) > 0.02:
                    a = _plane_hit(region, rv3d, self.mouse_start, p0, ax)
                    b = _plane_hit(region, rv3d, mouse, p0, ax)
                    dw = (b - a) if a is not None and b is not None else Vector()
                else:
                    n = _view_dir(rv3d)
                    a = _plane_hit(region, rv3d, self.mouse_start, p0, n)
                    b = _plane_hit(region, rv3d, mouse, p0, n)
                    dw = (b - a) if a is not None and b is not None else Vector()
                    dw -= ax * dw.dot(ax)
                d = self._to_orient(dw)
                d[cons[0]] = 0.0
            if snapping:
                hit = self._snap(context, region, rv3d)
                if hit is not None:
                    p, kind = hit
                    dw = self._constrain_world(p - self._snap_source(context, p))
                    for _c, es in self.groups:
                        for e in es:
                            if e.src is None:
                                self.elem_delta[id(e)] = dw
                    self.snap_hit = (p, kind)
                    self.value = self._to_orient(dw)
                    return
            if cons is None and not snapping:
                self._rotation_path_deltas(region, rv3d, mouse)
            if snapping and snap.elements(ts) & {'INCREMENT', 'GRID'}:
                inc = 0.01 if fine else 0.1
                d = Vector([round(x / inc) * inc for x in d])
            self.value = d
            return

        snap_rot = (ts.use_snap and _ts_get(ts, "use_snap_rotate", False)) != bool(event.ctrl)
        if self.mode == 'SHEAR':
            right = rv3d.view_rotation @ Vector((1.0, 0.0, 0.0))
            up = rv3d.view_rotation @ Vector((0.0, 1.0, 0.0))
            dv = mouse - self.mouse_start
            if getattr(self, "shear_up", False):
                self.axis, self.axis2, amount = up, right, dv.y
            else:
                self.axis, self.axis2, amount = right, up, dv.x
            if num.active:
                f = num.value(0) or 0.0
            else:
                f = amount / (150.0 * compat.ui_scale())
                if bool(event.ctrl):
                    inc = 0.01 if fine else 0.1
                    f = round(f / inc) * inc
            self.value = (f, 0.0, 0.0)
            return
        if self.mode == 'TRACKBALL':
            right = rv3d.view_rotation @ Vector((1.0, 0.0, 0.0))
            up = rv3d.view_rotation @ Vector((0.0, 1.0, 0.0))
            self.axis = up
            self.axis2 = right
            if num.active:
                self.value = (math.radians(num.value(0) or 0.0), math.radians(num.value(1) or 0.0), 0.0)
                return
            dv = (mouse - self.mouse_start) * (TRACKBALL_SPEED / compat.ui_scale())
            a, b = dv.x, -dv.y
            if snap_rot:
                inc = _angle_increment(ts, fine)
                a, b = round(a / inc) * inc, round(b / inc) * inc
            self.value = (a, b, 0.0)
            return

        if self.mode == 'ROTATE' and self.twist and self.ring_axis in (1, 4) and not num.active:
            # dragging the green ring: twist by the angle swept around it
            ang = self._twist_ring_angle(region, rv3d, mouse)
            if ang is not None:
                if snap_rot:
                    inc = _angle_increment(ts, fine)
                    ang = round(ang / inc) * inc
                self.angle = ang
            return
        if self.mode == 'ROTATE':
            if cons is None:
                axis_w = toward
                self.axis = axis_w
            else:
                axis_w = self._axis_world(cons[0])
                self.axis = Vector([1.0 if j == cons[0] else 0.0 for j in range(3)])
            if num.active:
                self.angle = math.radians(num.value(0) or 0.0)
                return
            ang = self.angle_acc * (1.0 if axis_w.dot(toward) >= 0.0 else -1.0)
            if snap_rot:
                inc = _angle_increment(ts, fine)
                ang = round(ang / inc) * inc
            self.angle = ang
            return

        # SCALE
        snap_scale = (ts.use_snap and _ts_get(ts, "use_snap_scale", False)) != bool(event.ctrl)
        if num.active:
            f = num.value(0)
            f = 1.0 if f is None else f
        else:
            # starts at exactly 1, even with the mouse on the key
            if getattr(self, "scale_dir", None) is not None:
                along = (mouse - self.pivot_2d).dot(self.scale_dir)
            else:
                along = (mouse - self.pivot_2d).length
            f = 1.0 + (along - self.scale_start) / self.scale_d0
            if snap_scale:
                inc = 0.01 if fine else 0.1
                f = round(f / inc) * inc
        if cons is None:
            v = (f, f, f)
        elif not cons[1]:
            v = tuple(f if j == cons[0] else 1.0 for j in range(3))
        else:
            v = tuple(1.0 if j == cons[0] else f for j in range(3))
        self.value = v

    def _ring_axis(self):
        """Channel axis index rotation keys turn around (a dragged ring; 3: the view axis), or
        None: free on the sphere."""
        return self.ring_axis if self.ring_axis >= 0 else None

    def _ring_angle(self, region, rv3d, skey, c, n, ref, mouse, view_axis=False):
        """Angle (radians, right-handed around n) the mouse has turned a ring around center c
        since the drag started, like Blender's rotate gizmo: the angle the mouse sweeps around
        the center, measured from where the ring was grabbed and followed continuously (no
        flips). A ring seen nearly edge-on turns with the mouse along its direction on the
        screen; near the center the angle holds still. `ref` is a point on the ring (angle 0);
        view_axis: n is the view direction (the reach circle). Returns (state, angle) or None."""
        proj = view3d_utils.location_3d_to_region_2d
        st = self._ring_state.get(skey)
        if st is None:
            c2 = proj(region, rv3d, c)
            if c2 is None:
                return None
            vd = _ray(region, rv3d, c2)[1].normalized()     # the view ray through the center
            if view_axis:
                n = -vd
            u = (ref - c) - n * (ref - c).dot(n)
            r = u.length
            if r < 1e-9:
                return None
            u /= r
            v = n.cross(u)
            w = n.cross(vd)
            w = w.normalized() if w.length > 1e-9 else u
            pw = proj(region, rv3d, c + w * r)
            major = max((pw - c2).length if pw is not None else 0.0, 20.0)   # screen radius
            st = dict(n=n, u=u, v=v, r=r, c2=c2, theta=0.0, major=major,
                      edge=not view_axis and abs(n.dot(vd)) < 0.4)
            if view_axis:
                rel = self.mouse_start - c2
                st['a0'] = math.atan2(rel.y, rel.x) if rel.length > 1e-6 else 0.0
            elif not st['edge']:
                hit = _plane_hit(region, rv3d, self.mouse_start, c, n)
                q = (hit - c) if hit is not None else u
                st['a0'] = math.atan2(q.dot(v), q.dot(u))
            else:
                # the point of the ring nearest to where it was grabbed, and the ring's
                # direction on the screen there
                best = None
                for i in range(64):
                    a = i * math.pi / 32.0
                    s = proj(region, rv3d, c + (u * math.cos(a) + v * math.sin(a)) * r)
                    if s is not None and (best is None or (s - self.mouse_start).length < best[0]):
                        best = ((s - self.mouse_start).length, a)
                ag = best[1] if best else 0.0
                p0 = proj(region, rv3d, c + (u * math.cos(ag) + v * math.sin(ag)) * r)
                p1 = proj(region, rv3d, c + (u * math.cos(ag + 0.05) + v * math.sin(ag + 0.05)) * r)
                tang = (p1 - p0) / 0.05 if p0 is not None and p1 is not None else Vector((0.0, 0.0))
                if tang.length < 0.25 * major:
                    tang = (pw - c2) if pw is not None else Vector((1.0, 0.0))
                st['tdir'] = tang.normalized() if tang.length > 1e-9 else Vector((1.0, 0.0))
            self._ring_state[skey] = st
        hold = None     # the mouse is too close to the center to tell the angle
        if view_axis:
            rel = mouse - st['c2']
            if rel.length < 0.15 * st['major']:
                hold = True
            else:
                raw = math.atan2(rel.y, rel.x) - st['a0']
        elif st['edge']:
            raw = (mouse - self.mouse_start).dot(st['tdir']) / st['major']
        else:
            hit = _plane_hit(region, rv3d, mouse, c, st['n'])
            if hit is None or (hit - c).length < 0.15 * st['r']:
                hold = True
            else:
                q = hit - c
                raw = math.atan2(q.dot(st['v']), q.dot(st['u'])) - st['a0']
        if not hold:
            # continue from the last angle: no jump of a full turn (or a flip) between updates
            st['theta'] += (raw - st['theta'] + math.pi) % (2.0 * math.pi) - math.pi
        return st, st['theta']

    def _ring_point(self, region, rv3d, e, axis, co2d, mouse):
        """Tip of a rotation key turned along one ring (axis 0 / 2 of the bone) or around the
        view axis (3, the reach circle); see _ring_angle."""
        c = e.center
        n = e.basis.col[axis].normalized() if axis != 3 else None
        res = self._ring_angle(region, rv3d, (e.cache.key, round(e.k.t, 3)), c, n, e.co, mouse,
                               view_axis=axis == 3)
        if res is None:
            return None
        st, theta = res
        if axis == 3:
            return c + Quaternion(st['n'], theta) @ (e.co - c)
        return c + (st['u'] * math.cos(theta) + st['v'] * math.sin(theta)) * st['r'] + \
            st['n'] * (e.co - c).dot(st['n'])

    def _twist_ring_angle(self, region, rv3d, mouse):
        """The green (Y) ring: twist angle of the bone around its own axis. The rotation
        ribbon's handle (ring_axis 4) turns around the same axis, at the key."""
        for _c, es in self.groups:
            for e in es:
                if e.part != 'KEY' or e.center is None or e.src is not None:
                    continue
                n = e.basis.col[1].normalized()
                r = (e.co - e.center).length
                center = e.center
                if self.ring_axis == 4:
                    from . import draw
                    st = state.settings()
                    center = e.co
                    r = max(abs(x) for x in draw.rot_ribbon_span(st, e.cache))
                ref = center + e.basis.col[0 if self.ring_axis == 4 else 2].normalized() * r
                res = self._ring_angle(region, rv3d, ('twist', e.cache.key, round(e.k.t, 3)), center, n, ref,
                                       mouse)
                return None if res is None else res[1]
        return None

    def _trackball_point(self, region, rv3d, e, mouse):
        """Dragging the key of a rotation path: the tip turns like a trackball around the
        head (a drag as long as the reach circle's radius on the screen turns it 1 radian)."""
        c = e.center
        r = (e.co - c).length
        right = rv3d.view_rotation @ Vector((1.0, 0.0, 0.0))
        up = rv3d.view_rotation @ Vector((0.0, 1.0, 0.0))
        c2 = view3d_utils.location_3d_to_region_2d(region, rv3d, c)
        e2 = view3d_utils.location_3d_to_region_2d(region, rv3d, c + right * r)
        if c2 is None or e2 is None:
            return None
        rad = max((e2 - c2).length, 20.0)
        dv = mouse - self.mouse_start
        if dv.length < 1e-6:
            return e.co.copy()
        axis = (up * dv.x - right * dv.y).normalized()
        return c + Quaternion(axis, dv.length / rad) @ (e.co - c)

    def _rotation_path_deltas(self, region, rv3d, mouse):
        """Rotation path points live on a sphere around the bone head: put them exactly
        under the mouse (ray/sphere for tips, tangent plane for handles)."""
        if not hasattr(self, "_ring_state"):
            self._ring_state = {}
        for _c, es in self.groups:
            for e in es:
                if e.center is None or e.src is not None:
                    continue
                s0 = view3d_utils.location_3d_to_region_2d(region, rv3d, e.co)
                if s0 is None:
                    continue
                co2d = mouse + (s0 - self.mouse_start)
                o, d = _ray(region, rv3d, co2d)
                if e.part == 'KEY':
                    r = (e.co - e.center).length
                    axis = self._ring_axis()
                    if axis is not None:
                        # one axis: the tip stays on that ring (turning around the bone's axis)
                        p = self._ring_point(region, rv3d, e, axis, co2d, mouse)
                        if p is None:
                            continue
                    elif self.key_trackball:
                        p = self._trackball_point(region, rv3d, e, mouse)
                    else:
                        front = (e.co - e.center).dot(_view_dir(rv3d)) <= 0.0
                        p = _ray_sphere(o, d, e.center, r, front)
                else:
                    n = (e.k.co - e.center)
                    if n.length < 1e-9 or abs(n.normalized().dot(d.normalized())) < 0.05:
                        continue
                    p = _plane_hit(region, rv3d, co2d, e.co, n.normalized())
                if p is not None:
                    self.elem_delta[id(e)] = p - e.co

    def _header(self, context):
        num = self.numinput
        cons = self.constraint
        ctext = ""
        if cons is not None:
            ctext = ("  locking %s" if cons[1] else "  along %s") % AXIS_NAMES[cons[0]]
            names = {i[0]: i[1] for i in space.ORIENTATIONS}
            ctext += " (%s)" % names.get(self.orient, self.orient)
        if self.mode == 'TRANSLATE':
            v = Vector(self.value)
            parts = [num.text(i, v[i], "%.4f") for i in range(3)]
            txt = "Move  D: %s  %s  %s  (%.4f)%s" % (parts[0], parts[1], parts[2], v.length, ctext)
            if self.time_wheel:
                keys = sorted({round(e.k.t) for _c, es in self.groups for e in es if e.src is None})
                txt += "   frame %s (wheel: earlier / later)" % ", ".join("%d" % t for t in keys[:4])
            if self.snap_hit is not None:
                txt += "   snapped to %s" % self.snap_hit[1].replace('_', ' ').lower()
        elif self.mode == 'SHEAR':
            txt = "Shear  %s   (X / Y: direction)" % num.text(0, self.value[0], "%.3f")
        elif self.mode == 'TRACKBALL':
            txt = "Trackball  %s°  %s°   (R: rotate)" % (num.text(0, math.degrees(self.value[0]), "%.2f"),
                                                       num.text(1, math.degrees(self.value[1]), "%.2f"))
        elif self.mode == 'ROTATE':
            what = "Twist (R: rotate)" if self.twist else "Rotate (R again: trackball)"
            txt = "%s  %s°%s" % (what, num.text(0, math.degrees(self.angle), "%.2f"), ctext)
        else:
            v = self.value
            what = "Scale Handles" if self.handle_mode else "Scale"
            txt = "%s  %s%s" % (what, num.text(0, sum(v) / 3.0, "%.3f"), ctext)
        if self.use_proportional:
            falloff_name = self.properties.bl_rna.properties['proportional_falloff'].enum_items[
                self.proportional_falloff].name
            txt += "   Proportional: %.3f (%s)" % (self.proportional_size, falloff_name)
        context.area.header_text_set(txt)
        if context.workspace:
            context.workspace.status_text_set(
                "LMB/Enter: confirm   RMB/Esc: cancel   X/Y/Z: axis (twice: other orientation)   "
                "Shift+X/Y/Z: plane   Ctrl: snap   Shift: precise   G/R/S: switch mode   type numbers   "
                "O: proportional   Wheel / PgUp PgDn: proportional size   Shift+O: falloff")

    def _ensure_now(self, context):
        """First real movement: add the missing channel keys, then rebuild the elements."""
        self.ensured = True
        mode = self.mode
        orient, om = self.orient, tuple(self.orient_matrix)
        self._prepare(context, None, self.created, candidates=True, ensure=True)
        self.mode = mode
        self.orient, self.orient_matrix = orient, om
        self.snapper = None
        for c, _es in self.groups:
            c.refresh(force=True)
        ev = type("Mods", (), {"ctrl": self.mods[0], "shift": self.mods[1]})()
        self._compute(context, ev)

    def _update(self, context):
        if getattr(self, "lazy", False) and not self.ensured and not self._is_identity():
            self._ensure_now(context)
        self._apply(context)
        self._header(context)
        if self.constraint is not None and not self.constraint[1] and \
                any(c.ptype != core.SCALE for c, _es in self.groups):
            ax = self._axis_world(self.constraint[0])
            state.constraint = (self.pivot.copy(), ax, AXIS_COLORS[self.constraint[0]])
        else:
            state.constraint = None
        state.snap_point = self.snap_hit[0].copy() if self.snap_hit is not None else None
        has_keys = any(e.part == 'KEY' and e.src is None for _c, es in self.groups for e in es)
        state.prop_circle = (self.pivot.copy(), self.proportional_size) \
            if self.use_proportional and has_keys else None
        state.tag_redraw()

    def modal(self, context, event):
        try:
            return self._modal(context, event)
        except Exception:
            self._restore()
            self._finish(context)
            raise

    def _switch_mode(self, context, event, new):
        self._restore()
        self._ring_state = {}
        self.snapshots = []
        self.created = []
        self.applied = False
        self.ensured = False
        self._prepare(context, self.snapshots, self.created, candidates=True, ensure=False)
        self.mode = new
        self.value = (1.0, 1.0, 1.0) if new == 'SCALE' else (0.0, 0.0, 0.0)
        self.angle = 0.0
        self.numinput = NumInput()
        self._reset_mode_state(context)
        self._compute(context, event)
        self._update(context)

    def _modal(self, context, event):
        t, v = event.type, event.value
        if t in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            m = Vector((event.mouse_region_x, event.mouse_region_y))
            d = m - self.mouse_last
            self.mouse_last = m
            self.mouse_virtual += d * (0.1 if event.shift else 1.0)
            if self.mode == 'ROTATE':
                r = self.mouse_virtual - self.angle_center
                a = math.atan2(r.y, r.x)
                da = a - self.angle_prev
                while da > math.pi:
                    da -= 2 * math.pi
                while da < -math.pi:
                    da += 2 * math.pi
                self.angle_acc += da
                self.angle_prev = a
            self._compute(context, event)
            self._update(context)
            return {'RUNNING_MODAL'}

        if t in {'LEFT_CTRL', 'RIGHT_CTRL', 'LEFT_SHIFT', 'RIGHT_SHIFT'}:
            self._compute(context, event)
            self._update(context)
            return {'RUNNING_MODAL'}

        if self.release_confirm and t == 'LEFTMOUSE' and v == 'RELEASE':
            return self._confirm(context)
        if (t == 'LEFTMOUSE' and v == 'PRESS') or (t in {'RET', 'NUMPAD_ENTER', 'SPACE'} and v == 'PRESS'):
            return self._confirm(context)
        if t in {'RIGHTMOUSE', 'ESC'} and v == 'PRESS':
            self._restore()
            self._finish(context)
            return {'CANCELLED'}

        if self.numinput.handle(event):
            self._compute(context, event)
            self._update(context)
            return {'RUNNING_MODAL'}

        if v == 'PRESS' and t in {'X', 'Y'} and self.mode == 'SHEAR':
            self.shear_up = t == 'Y'
            self._compute(context, event)
            self._update(context)
            return {'RUNNING_MODAL'}
        if v == 'PRESS' and t in {'X', 'Y', 'Z'} and self.mode not in {'TRACKBALL', 'SHEAR'}:
            i = "XYZ".index(t)
            plane = event.shift
            header = space.orientation_type(context)
            if self.constraint is None or self.constraint != (i, plane):
                self.constraint = (i, plane)
                self._set_orient(context, header)
            elif self.orient == header:
                # like Blender: the second press switches to Local (or Global)
                self._set_orient(context, 'LOCAL' if header == 'GLOBAL' else 'GLOBAL')
            else:
                self.constraint = None
                self._set_orient(context, 'GLOBAL')
            self._compute(context, event)
            self._update(context)
            return {'RUNNING_MODAL'}

        if v == 'PRESS' and t == 'G' and self.mode == 'TRANSLATE' and not self.numinput.active \
                and any(e.part == 'KEY' and e.src is None for _c, es in self.groups for e in es):
            # G G: slide the keys along the path instead (like vertex slide)
            self._restore()
            self._finish(context)
            bpy.ops.emp.slide('INVOKE_DEFAULT', start_x=int(self.mouse_start.x),
                              start_y=int(self.mouse_start.y))
            return {'CANCELLED'}
        if v == 'PRESS' and t == 'R' and not self.numinput.active:
            # R R: trackball and back (Ctrl+T twist: R rotates normally)
            new = 'ROTATE' if self.twist or self.mode != 'ROTATE' else 'TRACKBALL'
            self.twist = False
            self._switch_mode(context, event, new)
            return {'RUNNING_MODAL'}
        if v == 'PRESS' and t in {'G', 'R', 'S'}:
            new = {'G': 'TRANSLATE', 'R': 'ROTATE', 'S': 'SCALE'}[t]
            if new != self.mode:
                self.twist = False
                self._switch_mode(context, event, new)
            return {'RUNNING_MODAL'}

        if v == 'PRESS' and t == 'O' and not event.ctrl and not event.alt:
            if event.shift:
                items = [i.identifier for i in self.properties.bl_rna.properties['proportional_falloff'].enum_items]
                self.proportional_falloff = items[(items.index(self.proportional_falloff) + 1) % len(items)]
            else:
                self.use_proportional = not self.use_proportional
            self._update(context)
            return {'RUNNING_MODAL'}
        if self.time_wheel and v == 'PRESS' and t in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            # Shift+D: the wheel moves the copies through the motion, a frame at a time
            self._shift_time(context, event, 1 if t == 'WHEELUPMOUSE' else -1)
            return {'RUNNING_MODAL'}
        if self.use_proportional and v == 'PRESS' and t in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE',
                                                              'PAGE_UP', 'PAGE_DOWN'}:
            grow = t in {'WHEELDOWNMOUSE', 'PAGE_UP'}
            self.proportional_size *= 1.1 if grow else 1.0 / 1.1
            self._update(context)
            return {'RUNNING_MODAL'}
        return {'RUNNING_MODAL'}

    def _shift_time(self, context, event, d):
        from .ops_keys import shift_selected_keys
        self._restore()
        caches = [c for c, _es in self.groups]
        for c in caches:
            c.refresh(force=True)
        shift_selected_keys(caches, d)
        self.snapshots = []
        self.created = []
        self.applied = False
        self.ensured = False
        self._prepare(context, self.snapshots, self.created, candidates=True, ensure=False)
        for cache, _es in self.groups:
            state.ghosts[cache.key] = list(cache.points)
        self._compute(context, event)
        self._update(context)
        pick.tag_anim_editors()

    def _confirm(self, context):
        self._finish(context)
        if self.n_elems == 0:
            return {'CANCELLED'}
        return {'FINISHED'}


def _angle_increment(ts, fine):
    if fine:
        return _ts_get(ts, "snap_angle_increment_3d_precision", math.radians(1.0))
    return _ts_get(ts, "snap_angle_increment_3d", math.radians(5.0))


classes = (EMP_OT_transform,)
