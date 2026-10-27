"""Timing along the path: change the spacing (where the frame dots fall) while the shape of
the path stays, like LoopTools Space does for vertices."""

import bisect

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty

from . import compat, core, fit, state
from .ops_tools import NumInput, _AdjustOp

PROFILES = [
    ('EVEN', "Even", "Constant speed: the frame dots are evenly spaced (E)"),
    # like Blender's easing: "in" is the start of the segment, "out" its end
    ('SLOW_OUT', "Slow Out", "Slow down at the end of the segment: decelerate (O)"),
    ('SLOW_IN', "Slow In", "Start slowly at the beginning of the segment: accelerate (I)"),
    ('SLOW_IN_OUT', "Slow In & Out", "Start and end slowly, fastest in the middle (B)"),
]


def profile(kind, u, power):
    """Share of the distance covered after the share u of the time."""
    u = min(1.0, max(0.0, u))
    if kind == 'SLOW_IN':
        return u ** power
    if kind == 'SLOW_OUT':
        return 1.0 - (1.0 - u) ** power
    if kind == 'SLOW_IN_OUT':
        a, b = u ** power, (1.0 - u) ** power
        return a / (a + b) if a + b > 0.0 else u
    return u


def blend_weight(u, blend_in, blend_out):
    """How much of the new spacing at the share u of the range: rising smoothly over blend_in at
    the start, falling over blend_out at the end (smoothstep: the speed meets the old one)."""
    w = 1.0
    if blend_in > 1e-6 and u < blend_in:
        x = max(0.0, u / blend_in)
        w = x * x * (3.0 - 2.0 * x)
    if blend_out > 1e-6 and u > 1.0 - blend_out:
        x = max(0.0, (1.0 - u) / blend_out)
        w = min(w, x * x * (3.0 - 2.0 * x))
    return w


def pchip(xs, ys):
    """Monotone cubic through (xs, ys) (Fritsch-Carlson): a smooth time warp without
    overshoot, so time never runs backwards."""
    n = len(xs)
    d = [(ys[i + 1] - ys[i]) / (xs[i + 1] - xs[i]) for i in range(n - 1)]
    m = [0.0] * n
    m[0], m[-1] = d[0], d[-1]
    for i in range(1, n - 1):
        if d[i - 1] * d[i] <= 0.0:
            m[i] = 0.0
        else:
            w1 = 2.0 * (xs[i + 1] - xs[i]) + (xs[i] - xs[i - 1])
            w2 = (xs[i + 1] - xs[i]) + 2.0 * (xs[i] - xs[i - 1])
            m[i] = (w1 + w2) / (w1 / d[i - 1] + w2 / d[i])

    def f(x):
        i = max(0, min(n - 2, bisect.bisect_right(xs, x) - 1))
        h = xs[i + 1] - xs[i]
        u = (x - xs[i]) / h
        h00 = (1 + 2 * u) * (1 - u) ** 2
        h10 = u * (1 - u) ** 2
        h01 = u * u * (3 - 2 * u)
        h11 = u * u * (u - 1)
        return h00 * ys[i] + h10 * h * m[i] + h01 * ys[i + 1] + h11 * h * m[i + 1]
    return f


def refit(job, tg, target, select=True, tolerance=0.4):
    """Replace the motion of job's range by `target(t)` (channel values), keeping the keys on
    their frames and adding keys only where needed. The range ends keep their outer side."""
    cache = job.cache
    fitter = fit.Fitter(cache, tg, job.r0, job.r1, target)
    knots = [t for t in job.knots if job.r0 - 1e-9 <= t <= job.r1 + 1e-9]
    fixed = {job.r0, job.r1}
    half = {job.r0: 'L', job.r1: 'R'}
    knots, slopes = fitter.refine(knots, fixed, fitter.size * tolerance / 100.0, half)
    touched = fit.write(fitter, knots, slopes, select)
    cache.dirty = True
    return touched


def warp_target(job, tg, warp):
    """Target motion: the original path of `job`, reached at other times (warp: new frame ->
    original frame). The shape stays, only the spacing changes."""
    cache = job.cache

    def fn(t):
        tt = warp(t)
        return cache.solve(tg, t, job._at(job.pts, tt), job._at(job.vals, tt))
    return fn


def progress_target(before, after, tg):
    """Target motion: the shape of `after`, travelled with the spacing of `before` (both
    SpaceJobs over the same range). Shape tools use it to keep the timing."""
    cache = after.cache
    tb, ta = before.arc[-1], after.arc[-1]

    def fn(t):
        u = before._at(before.arc, t) / tb if tb > 1e-12 else (t - before.r0) / (before.r1 - before.r0)
        tt = after._time_at_arc(u * ta)
        return cache.solve(tg, t, after._at(after.pts, tt), after._at(after.vals, tt))
    return fn


class SpaceJob:
    """One path range with its original motion sampled densely."""

    STEP = 0.1

    def __init__(self, cache, tg, r0, r1):
        self.cache = cache
        self.r0, self.r1 = r0, r1
        self.keys, self.mask = [], []       # (for _AdjustOp)
        ch = tg.channels(cache.ptype)
        n = max(2, int(round((r1 - r0) / self.STEP)))
        self.ts = [r0 + (r1 - r0) * i / n for i in range(n + 1)]
        self.vals = core.raw_values(ch, self.ts)        # (the keys' own motion: no Noise...)
        self.pts = [cache.point(tg, t, v) for t, v in zip(self.ts, self.vals)]
        self.arc = [0.0]
        for a, b in zip(self.pts, self.pts[1:]):
            self.arc.append(self.arc[-1] + (b - a).length)
        self.knots = sorted({float(r0), float(r1)} |
                            {float(k.t) for k in cache.keys if r0 - 1e-3 <= k.t <= r1 + 1e-3})
        self.snapshot = core.FCurveSnapshot(tg, ch)

    def _at(self, values, t):
        i = bisect.bisect_left(self.ts, t)
        if i <= 0:
            return values[0]
        if i >= len(self.ts):
            return values[-1]
        t0, t1 = self.ts[i - 1], self.ts[i]
        f = (t - t0) / (t1 - t0)
        a, b = values[i - 1], values[i]
        if isinstance(a, list):
            return [x + (y - x) * f for x, y in zip(a, b)]
        return a + (b - a) * f

    def _time_at_arc(self, s):
        arc = self.arc
        i = bisect.bisect_left(arc, s)
        if i <= 0:
            return self.ts[0]
        if i >= len(arc):
            return self.ts[-1]
        a0, a1 = arc[i - 1], arc[i]
        f = 0.0 if a1 - a0 < 1e-12 else (s - a0) / (a1 - a0)
        return self.ts[i - 1] + (self.ts[i] - self.ts[i - 1]) * f

    def target(self, tg, kind, factor, power, blend_in=0.0, blend_out=0.0):
        """blend_in / blend_out: over that share of the range at its start / end the new spacing
        takes over from the old one gradually (the speed carries on from the timing around it:
        no seam where the range begins and ends)."""
        total = self.arc[-1]
        r0, r1 = self.r0, self.r1
        cache = self.cache
        arc = []
        for t, s0 in zip(self.ts, self.arc):
            u = (t - r0) / (r1 - r0)
            w = factor * blend_weight(u, blend_in, blend_out)
            s = s0 + (total * profile(kind, u, power) - s0) * w
            arc.append(max(s, arc[-1]) if arc else s)      # (never back along the path)

        def fn(t):
            tt = self._time_at_arc(self._at(arc, t))
            return cache.solve(tg, t, self._at(self.pts, tt), self._at(self.vals, tt))
        return fn


class EMP_OT_space(_AdjustOp, bpy.types.Operator):
    """Change the spacing between the selected keys (whole path if fewer than two are """ \
        """selected) while the path keeps its shape: even spacing (constant speed) or a """ \
        """slow in / slow out profile. Drag right to apply more"""
    bl_idname = "emp.space"
    bl_label = "Space"
    bl_options = {'REGISTER', 'UNDO', 'BLOCKING'}
    label = "Space"

    factor: FloatProperty(name="Factor", default=1.0, min=0.0, max=1.0, subtype='FACTOR',
                          description="0 = spacing as it was, 1 = exactly the profile")
    profile: EnumProperty(name="Profile", items=PROFILES, default='EVEN')
    power: FloatProperty(name="Strength", default=2.0, min=1.0, soft_max=5.0,
                         description="How strong the slow in / slow out is (mouse wheel)")
    blend_in: FloatProperty(name="Start Blend", default=0.0, min=0.0, max=0.5, subtype='FACTOR',
                            description="How softly the new spacing takes over at the start: over that share "
                                        "of the range the speed carries on from the timing before it (no "
                                        "seam). I while dragging, then move the mouse")
    blend_out: FloatProperty(name="End Blend", default=0.0, min=0.0, max=0.5, subtype='FACTOR',
                             description="How softly the new spacing hands over to the timing after the "
                                         "range at its end. O while dragging, then move the mouse")
    add_keys: BoolProperty(name="Add Keys If Needed", default=True,
                           description="Insert keys where the existing ones can't follow the new "
                                       "spacing closely enough")
    tolerance: FloatProperty(name="Tolerance", subtype='PERCENTAGE', default=0.5, min=0.01,
                             soft_max=5.0, description="Allowed distance from the exact new motion "
                                                       "(percent of the path size)")

    ADJUST = {'factor': "Factor", 'blend_in': "Start Blend", 'blend_out': "End Blend"}

    def _label(self):
        name = {i[0]: i[1] for i in PROFILES}[self.profile]
        txt = "Space: %s" % name
        if self.profile != 'EVEN':
            txt += " (strength %.1f, wheel)" % self.power
        adj = getattr(self, "adjust", 'factor')
        return txt + "   I: Start Blend %.2f   O: End Blend %.2f   - %s" % (
            self.blend_in, self.blend_out, self.ADJUST[adj])

    def _set_amount(self, v):
        adj = getattr(self, "adjust", 'factor')
        if adj == 'factor':
            self.factor = v
        else:
            setattr(self, adj, min(0.5, max(0.0, v)))

    def _amount(self):
        return getattr(self, getattr(self, "adjust", 'factor'))

    def _modal_key(self, context, event):
        if event.type in {'I', 'O'}:
            # the mouse now sets the start / end blend; the same key again: back to the factor
            want = 'blend_in' if event.type == 'I' else 'blend_out'
            adj = 'factor' if getattr(self, "adjust", 'factor') == want else want
            self.adjust = adj
            # (the mouse goes on from the value as it is: nothing jumps)
            ppu = self.pixels_per_unit * compat.ui_scale()
            self.x0 = self.x_virtual - getattr(self, adj) * ppu
            self.numinput = NumInput(1)
            return True
        if event.type in {'WHEELUPMOUSE', 'PAGE_UP'}:
            self.power = min(8.0, self.power + 0.25)
            return True
        if event.type in {'WHEELDOWNMOUSE', 'PAGE_DOWN'}:
            self.power = max(1.0, self.power - 0.25)
            return True
        return False

    def _jobs(self, context, snapshots=None, ensure=True):
        jobs = []
        for cache in state.editable_caches(context.scene):
            tg = cache.target()
            if tg is None or len(cache.keys) < 2:
                continue
            sel = [k.t for k in cache.keys if k.sel]
            r0, r1 = (min(sel), max(sel)) if len(sel) >= 2 else (cache.keys[0].t, cache.keys[-1].t)
            ch = tg.channels(cache.ptype)
            for fc in ch.fcurves:
                if fc is not None and len(fc.keyframe_points):
                    # every refitted channel needs keys over the whole range
                    r0 = max(r0, fc.keyframe_points[0].co[0])
                    r1 = min(r1, fc.keyframe_points[-1].co[0])
            if r1 - r0 < 2.0:
                continue
            job = SpaceJob(cache, tg, float(r0), float(r1))
            if snapshots is not None:
                snapshots.append(job.snapshot)
            jobs.append(job)
        return jobs

    def _apply_job(self, job, tg, touched):
        job.snapshot.restore()
        cache = job.cache
        cache.refresh(force=True)
        tg = cache.target()
        f = min(1.0, max(0.0, self.factor))
        fitter = fit.Fitter(cache, tg, job.r0, job.r1, job.target(tg, self.profile, f, self.power,
                                                                  self.blend_in, self.blend_out))
        fixed = {job.r0, job.r1}
        half = {job.r0: 'L', job.r1: 'R'}
        knots = [t for t in job.knots if job.r0 - 1e-9 <= t <= job.r1 + 1e-9]
        if self.add_keys:
            knots, slopes = fitter.refine(knots, fixed, fitter.size * self.tolerance / 100.0, half)
        else:
            slopes = fitter.fit(knots, fixed, None, half)
        err = fitter.max_error(knots, slopes)[0] / fitter.size * 100.0
        self.result_text = "Space: %d keys, %.2f%% from the exact new spacing" % (len(knots), err)
        touched.update(fit.write(fitter, knots, slopes))


classes = (EMP_OT_space,)
