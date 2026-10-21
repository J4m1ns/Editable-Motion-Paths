"""Move / Rotate / Scale of path keys and handles (G / R / S), Blender style."""

import bisect
import math
import random

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty
from bpy_extras import view3d_utils
from mathutils import Matrix, Vector

from . import compat, core, pick, state

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
    if cache.ptype == core.LOCATION:
        return _normalized_cols(cache.B_at(k.t).to_3x3())
    M, _u = cache.rot_frame(tg, k.t)
    q = core.vals_to_quat(k.vals, tg.rot_mode)
    return _normalized_cols(M.to_3x3() @ q.to_matrix())


def build_elements(context, snapshots=None, created=None, connected=None, ensure=True):
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
        _add_proportional(groups, connected)
        groups = [(c, es) for c, es in groups if es]
    return groups


def _add_proportional(groups, connected):
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
            if round(k.t, 3) in taken:
                continue
            if connected:
                s = _arc_at(cache, arc, k.t)
                d, src = min(((abs(s - ss), e) for ss, e in sel_s), key=lambda x: x[0])
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
    """Write an edited handle, keeping the opposite one aligned unless broken."""
    other = 'L' if side == 'R' else 'R'
    t_this, t_other = _side_type(k, side), _side_type(k, other)
    if t_this in {'FREE', 'VECTOR'} or t_other in {'FREE', 'VECTOR'}:
        core.write_handle(ch, k, side, slopes, _side_dt(k, side), k.vals, 'FREE', touched)
        return
    core.write_handle(ch, k, side, slopes, _side_dt(k, side), k.vals, 'ALIGNED', touched)
    if _side_dt(k, other) > core.EPS:
        core.write_handle(ch, k, other, slopes, _side_dt(k, other), k.vals, 'ALIGNED', touched)


# ----------------------------------------------------------------------------
# Operator
# ----------------------------------------------------------------------------

class EMP_OT_transform(bpy.types.Operator):
    """Move, rotate or scale the selected path keys and handles"""
    bl_idname = "emp.transform"
    bl_label = "Transform Path Keys"
    bl_options = {'REGISTER', 'UNDO', 'BLOCKING'}

    mode: EnumProperty(name="Mode", items=[
        ('TRANSLATE', "Move", "Move keys / handles"),
        ('ROTATE', "Rotate", "Rotate keys around their median, or handles around their key. "
                             "On rotation paths: twist the bone around its axis"),
        ('SCALE', "Scale", "Scale keys around their median, or handle length (speed) of a single key"),
    ], default='TRANSLATE')
    orient: EnumProperty(name="Orientation", items=[
        ('GLOBAL', "Global", ""), ('LOCAL', "Local", "Channel axes of each key")], default='GLOBAL')
    value: FloatVectorProperty(name="Value", size=3, default=(0.0, 0.0, 0.0))
    axis: FloatVectorProperty(name="Axis", size=3, default=(0.0, 0.0, 1.0), subtype='DIRECTION')
    angle: FloatProperty(name="Angle", subtype='ANGLE', default=0.0)
    twist: BoolProperty(name="Twist", default=False,
                        description="Rotation paths: twist the bones around their own axis (R R)")
    use_proportional: BoolProperty(name="Proportional Editing", default=False)
    proportional_size: FloatProperty(name="Proportional Size", default=0.3, min=1e-4,
                                     subtype='DISTANCE', unit='LENGTH')
    proportional_falloff: EnumProperty(name="Falloff", items=[
        ('SMOOTH', "Smooth", "", 'SMOOTHCURVE', 0), ('SPHERE', "Sphere", "", 'SPHERECURVE', 1),
        ('ROOT', "Root", "", 'ROOTCURVE', 2), ('INVERSE_SQUARE', "Inverse Square", "", 'INVERSESQUARECURVE', 3),
        ('SHARP', "Sharp", "", 'SHARPCURVE', 4), ('LINEAR', "Linear", "", 'LINCURVE', 5),
        ('CONSTANT', "Constant", "", 'NOCURVE', 6), ('RANDOM', "Random", "", 'RNDCURVE', 7)],
        default='SMOOTH')
    proportional_connected: BoolProperty(name="Connected", default=True,
                                         description="Measure the distance along the path")
    release_confirm: BoolProperty(options={'HIDDEN', 'SKIP_SAVE'})
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
        self.groups = build_elements(context, snapshots, created, connected, ensure)
        keys = [e for _c, es in self.groups for e in es if e.part == 'KEY' and e.src is None]
        self.n_elems = sum(1 for _c, es in self.groups for e in es if e.src is None)
        self.handle_mode = len(keys) == 1 and self.n_elems == 1
        if keys:
            p = Vector()
            for e in keys:
                p += e.k.co
            self.pivot = p / len(keys)
            self.individual = False
        else:
            p = Vector()
            for _c, es in self.groups:
                for e in es:
                    if e.src is None:
                        p += e.k.co
            self.pivot = p / max(1, self.n_elems)
            self.individual = True
        return self.n_elems > 0

    def _update_weights(self):
        r = max(self.proportional_size, 1e-6)
        for _c, es in self.groups:
            for e in es:
                if e.src is not None:
                    e.weight = (falloff(self.proportional_falloff, e.dist / r, e.rand)
                                if self.use_proportional else 0.0)

    def _elem_matrix(self, e):
        pivot = e.k.co if self.individual else self.pivot
        w = e.weight
        deltas = getattr(self, "elem_delta", None)
        if self.mode == 'TRANSLATE':
            src = e.src or e
            if deltas and id(src) in deltas:
                return Matrix.Translation(deltas[id(src)] * w)
            v = Vector(self.value)
            d = e.basis @ v if self.orient == 'LOCAL' else v
            return Matrix.Translation(d * w)
        if self.mode == 'ROTATE':
            ax = Vector(self.axis)
            if self.orient == 'LOCAL':
                ax = e.basis @ ax
            if ax.length < 1e-9:
                return Matrix.Identity(4)
            R = Matrix.Rotation(self.angle * w, 4, ax.normalized())
            return Matrix.Translation(pivot) @ R @ Matrix.Translation(-pivot)
        S = Matrix.Diagonal(Vector([1.0 + (v - 1.0) * w for v in self.value])).to_3x3()
        if self.orient == 'LOCAL':
            S = e.basis @ S @ e.basis.inverted_safe()
        return Matrix.Translation(pivot) @ S.to_4x4() @ Matrix.Translation(-pivot)

    def _is_identity(self):
        if self.mode == 'TRANSLATE':
            deltas = getattr(self, "elem_delta", None) or {}
            return Vector(self.value).length < 1e-9 and all(d.length < 1e-9 for d in deltas.values())
        if self.mode == 'ROTATE':
            return abs(self.angle) < 1e-9
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
        if self.individual and self.mode == 'ROTATE':
            d = (X @ e.co) - k.co
            if d.length > 1e-12:
                return k.co + rotated_handle(k, e.part, v0, d.normalized(), self.angle)
        if self.individual and self.mode == 'SCALE' and self.orient == 'GLOBAL' \
                and len(set(round(v, 6) for v in self.value)) == 1:
            return k.co + scaled_handle(k, e.part, v0, self.value[0])
        return pick.true_from_display(k.co, X @ e.co, e.m)

    def _key_handle_target(self, k, side, X):
        """New world handle end when a single key's handles are rotated/scaled (R / S)."""
        h = k.hr if side == 'R' else k.hl
        v0 = h - k.co
        if self.mode == 'ROTATE':
            d0 = v0.normalized() if v0.length > 1e-9 else _fallback_dir(k, side)
            d = (X @ (k.co + d0)) - k.co
            if d.length > 1e-12:
                return k.co + rotated_handle(k, side, v0, d.normalized(), self.angle)
        elif self.mode == 'SCALE' and len(set(round(v, 6) for v in self.value)) == 1:
            return k.co + scaled_handle(k, side, v0, self.value[0])
        return X @ h

    def _apply_key(self, cache, tg, ch, e, X, touched):
        k = e.k
        handle_mode = self.handle_mode and e.src is None
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

        # rotation path
        q0 = core.vals_to_quat(k.vals, tg.rot_mode)
        if self.mode == 'TRANSLATE':
            vals = cache.solve(tg, k.t, X @ k.co, k.vals)
            dq = core.vals_to_quat(vals, tg.rot_mode) @ q0.inverted()

            def fix(hv):
                return cache.rot_vals(tg, dq @ core.vals_to_quat(hv, tg.rot_mode), hv)
        elif self.mode == 'ROTATE' and self.twist:
            ang = self.angle * e.weight

            def fix(hv):
                return cache.twist(tg, hv, ang)
            vals = fix(k.vals)
        elif self.mode == 'ROTATE':
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
            dq = core.vals_to_quat(vals, tg.rot_mode) @ q0.inverted()

            def fix(hv):
                return cache.rot_vals(tg, dq @ core.vals_to_quat(hv, tg.rot_mode), hv)
        else:
            if not handle_mode:
                return
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
        core.write_key(ch, k, vals, False, touched)
        for side in ('L', 'R'):
            dt = _side_dt(k, side)
            if dt < core.EPS:
                continue
            sign = 1.0 if side == 'R' else -1.0
            hv = fix(cache.handle_vals(k, side))
            slopes = [(h - v) * sign / dt for h, v in zip(hv, vals)]
            core.write_handle(ch, k, side, slopes, dt, vals, None, touched)

    # -- execute (redo panel) -------------------------------------------------

    def execute(self, context):
        if not self._prepare(context):
            return {'CANCELLED'}
        self._apply(context)
        return {'FINISHED'}

    # -- interactive -----------------------------------------------------------

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D' or context.region_data is None:
            return {'PASS_THROUGH'}
        self.snapshots = []
        self.created = []
        if not state.editable_caches(context.scene):
            return {'PASS_THROUGH'}
        if pick.selected_count(state.editable_caches(context.scene)) == 0:
            return {'PASS_THROUGH'}
        st = state.settings(context.scene)
        self.use_proportional = st.use_proportional
        self.proportional_size = st.proportional_size
        self.proportional_falloff = st.proportional_falloff
        self.proportional_connected = st.proportional_connected
        self.twist = False
        self.lazy = True
        self.applied = False
        self.ensured = False
        self.mods = (False, False)
        if not self._prepare(context, self.snapshots, self.created, candidates=True, ensure=False):
            self._restore()
            return {'PASS_THROUGH'}
        region = context.region
        if self.start_x > -100000:
            self.mouse_start = Vector((self.start_x, self.start_y))
        else:
            self.mouse_start = Vector((event.mouse_region_x, event.mouse_region_y))
        self.mouse_virtual = self.mouse_start.copy()
        if self.start_x > -100000:
            self.mouse_last = self.mouse_start.copy()
        else:
            self.mouse_last = Vector((event.mouse_region_x, event.mouse_region_y))
        self.constraint = None      # (axis index, is_plane)
        self.orient = 'GLOBAL'
        self.numinput = NumInput()
        self.value = (1.0, 1.0, 1.0) if self.mode == 'SCALE' else (0.0, 0.0, 0.0)
        self.angle = 0.0
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
        self.pivot_2d = view3d_utils.location_3d_to_region_2d(region, rv3d, self.pivot) or self.mouse_start
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
        self.scale_d0 = max(self.scale_start, 40.0 * us)
        self.anchor = self.mouse_virtual.copy()

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
        st = state.settings(context.scene)
        if st is not None:
            st.use_proportional = self.use_proportional
            st.proportional_size = self.proportional_size
            st.proportional_falloff = self.proportional_falloff
        context.area.header_text_set(None)
        if context.workspace:
            context.workspace.status_text_set(None)
        state.tag_redraw()

    def _axis_world(self, i):
        if self.orient == 'LOCAL':
            e = self.groups[0][1][0]
            return e.basis.col[i].normalized()
        return Vector([1.0 if j == i else 0.0 for j in range(3)])

    def _compute(self, context, event):
        self.mods = (bool(event.ctrl), bool(event.shift))
        self.elem_delta = {}
        region, rv3d = context.region, context.region_data
        mouse = self.mouse_virtual
        snap = event.ctrl
        fine = event.shift
        num = self.numinput
        cons = self.constraint
        toward = -_view_dir(rv3d)

        if self.mode == 'TRANSLATE':
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
                if not snap:
                    self._rotation_path_deltas(region, rv3d, mouse)
                if self.orient == 'LOCAL':
                    d = self.groups[0][1][0].basis.inverted_safe() @ d
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
                if self.orient == 'LOCAL':
                    d = self.groups[0][1][0].basis.inverted_safe() @ dw
                else:
                    d = dw
                d[cons[0]] = 0.0
            if snap:
                inc = 0.01 if fine else 0.1
                d = Vector([round(x / inc) * inc for x in d])
            self.value = d
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
            if snap:
                inc = math.radians(1.0 if fine else 5.0)
                ang = round(ang / inc) * inc
            self.angle = ang
            return

        # SCALE
        if num.active:
            f = num.value(0)
            f = 1.0 if f is None else f
        else:
            # starts at exactly 1, even with the mouse on the key
            f = 1.0 + ((mouse - self.pivot_2d).length - self.scale_start) / self.scale_d0
            if snap:
                inc = 0.01 if fine else 0.1
                f = round(f / inc) * inc
        if cons is None:
            v = (f, f, f)
        elif not cons[1]:
            v = tuple(f if j == cons[0] else 1.0 for j in range(3))
        else:
            v = tuple(1.0 if j == cons[0] else f for j in range(3))
        self.value = v

    def _rotation_path_deltas(self, region, rv3d, mouse):
        """Rotation path points live on a sphere around the bone head: put them exactly
        under the mouse (ray/sphere for tips, tangent plane for handles)."""
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
            ctext += " (%s)" % ("Local" if self.orient == 'LOCAL' else "Global")
        if self.mode == 'TRANSLATE':
            v = Vector(self.value)
            parts = [num.text(i, v[i], "%.4f") for i in range(3)]
            txt = "Move  D: %s  %s  %s  (%.4f)%s" % (parts[0], parts[1], parts[2], v.length, ctext)
        elif self.mode == 'ROTATE':
            what = "Twist (R: rotate)" if self.twist else (
                "Rotate (R again: twist)" if any(c.ptype == core.ROTATION for c, _es in self.groups)
                else "Rotate")
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
                "LMB/Enter: confirm   RMB/Esc: cancel   X/Y/Z: axis (twice: local)   "
                "Shift+X/Y/Z: plane   Ctrl: snap   Shift: precise   G/R/S: switch mode   type numbers   "
                "O: proportional   Wheel / PgUp PgDn: proportional size   Shift+O: falloff")

    def _ensure_now(self, context):
        """First real movement: add the missing channel keys, then rebuild the elements."""
        self.ensured = True
        mode = self.mode
        self._prepare(context, None, self.created, candidates=True, ensure=True)
        self.mode = mode
        for c, _es in self.groups:
            c.refresh(force=True)
        ev = type("Mods", (), {"ctrl": self.mods[0], "shift": self.mods[1]})()
        self._compute(context, ev)

    def _update(self, context):
        if getattr(self, "lazy", False) and not self.ensured and not self._is_identity():
            self._ensure_now(context)
        self._apply(context)
        self._header(context)
        if self.constraint is not None and not self.constraint[1]:
            ax = self._axis_world(self.constraint[0])
            state.constraint = (self.pivot.copy(), ax, AXIS_COLORS[self.constraint[0]])
        else:
            state.constraint = None
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

        if v == 'PRESS' and t in {'X', 'Y', 'Z'}:
            i = "XYZ".index(t)
            plane = event.shift
            if self.constraint is None or self.constraint != (i, plane):
                self.constraint = (i, plane)
                self.orient = 'GLOBAL'
            elif self.orient == 'GLOBAL':
                self.orient = 'LOCAL'
            else:
                self.constraint = None
                self.orient = 'GLOBAL'
            self._compute(context, event)
            self._update(context)
            return {'RUNNING_MODAL'}

        if v == 'PRESS' and t == 'G' and self.mode == 'TRANSLATE' and not self.numinput.active                 and any(e.part == 'KEY' and e.src is None for _c, es in self.groups for e in es):
            # G G: slide the keys along the path instead (like vertex slide)
            self._restore()
            self._finish(context)
            bpy.ops.emp.slide('INVOKE_DEFAULT', start_x=int(self.mouse_start.x),
                              start_y=int(self.mouse_start.y))
            return {'CANCELLED'}
        if v == 'PRESS' and t == 'R' and self.mode == 'ROTATE' and not self.numinput.active \
                and any(c.ptype == core.ROTATION for c, _es in self.groups):
            self.twist = not self.twist
            self._compute(context, event)
            self._update(context)
            return {'RUNNING_MODAL'}
        if v == 'PRESS' and t in {'G', 'R', 'S'}:
            new = {'G': 'TRANSLATE', 'R': 'ROTATE', 'S': 'SCALE'}[t]
            if new != self.mode:
                self._restore()
                self.snapshots = []
                self.created = []
                self.applied = False
                self.ensured = False
                self.twist = False
                self._prepare(context, self.snapshots, self.created, candidates=True, ensure=False)
                self.mode = new
                self.value = (0.0, 0.0, 0.0) if new == 'TRANSLATE' else (1.0, 1.0, 1.0)
                self.angle = 0.0
                self.numinput = NumInput()
                self._reset_mode_state(context)
                self._compute(context, event)
                self._update(context)
            return {'RUNNING_MODAL'}

        if v == 'PRESS' and t == 'O' and not event.ctrl and not event.alt:
            if event.shift:
                items = [i.identifier for i in self.properties.bl_rna.properties['proportional_falloff'].enum_items]
                self.proportional_falloff = items[(items.index(self.proportional_falloff) + 1) % len(items)]
            else:
                self.use_proportional = not self.use_proportional
            self._update(context)
            return {'RUNNING_MODAL'}
        if self.use_proportional and v == 'PRESS' and t in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE',
                                                              'PAGE_UP', 'PAGE_DOWN'}:
            grow = t in {'WHEELDOWNMOUSE', 'PAGE_UP'}
            self.proportional_size *= 1.1 if grow else 1.0 / 1.1
            self._update(context)
            return {'RUNNING_MODAL'}
        if t in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'TRACKPADPAN', 'TRACKPADZOOM'}:
            return {'RUNNING_MODAL'}
        return {'RUNNING_MODAL'}

    def _confirm(self, context):
        self._finish(context)
        if self.n_elems == 0:
            return {'CANCELLED'}
        return {'FINISHED'}


classes = (EMP_OT_transform,)
