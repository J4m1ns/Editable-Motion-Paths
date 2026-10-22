"""Timing along the path: change the spacing (where the frame dots fall) while the shape of
the path stays, like LoopTools Space does for vertices."""

import bisect

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty

from . import core, fit, state
from .ops_tools import _AdjustOp

PROFILES = [
    ('EVEN', "Even", "Constant speed: the frame dots are evenly spaced (E)"),
    ('SLOW_OUT', "Slow Out", "Leave slowly and speed up: accelerate (O)"),
    ('SLOW_IN', "Slow In", "Arrive slowly: decelerate (I)"),
    ('SLOW_IN_OUT', "Slow In & Out", "Start and end slowly, fastest in the middle (B)"),
]


def profile(kind, u, power):
    """Share of the distance covered after the share u of the time."""
    u = min(1.0, max(0.0, u))
    if kind == 'SLOW_OUT':
        return u ** power
    if kind == 'SLOW_IN':
        return 1.0 - (1.0 - u) ** power
    if kind == 'SLOW_IN_OUT':
        a, b = u ** power, (1.0 - u) ** power
        return a / (a + b) if a + b > 0.0 else u
    return u


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
        self.vals = [ch.values(t) for t in self.ts]
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

    def target(self, tg, kind, factor, power):
        total = self.arc[-1]
        r0, r1 = self.r0, self.r1
        cache = self.cache

        def fn(t):
            u = (t - r0) / (r1 - r0)
            s0 = self._at(self.arc, t)
            s = s0 + (total * profile(kind, u, power) - s0) * factor
            tt = self._time_at_arc(s)
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
    add_keys: BoolProperty(name="Add Keys If Needed", default=True,
                           description="Insert keys where the existing ones can't follow the new "
                                       "spacing closely enough")
    tolerance: FloatProperty(name="Tolerance", subtype='PERCENTAGE', default=0.5, min=0.01,
                             soft_max=5.0, description="Allowed distance from the exact new motion "
                                                       "(percent of the path size)")

    def _label(self):
        name = {i[0]: i[1] for i in PROFILES}[self.profile]
        txt = "Space: %s" % name
        if self.profile != 'EVEN':
            txt += " (strength %.1f, wheel)" % self.power
        return txt + "   E / O / I / B"

    def _modal_key(self, context, event):
        m = {'E': 'EVEN', 'O': 'SLOW_OUT', 'I': 'SLOW_IN', 'B': 'SLOW_IN_OUT'}.get(event.type)
        if m is not None:
            self.profile = m
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
        fitter = fit.Fitter(cache, tg, job.r0, job.r1, job.target(tg, self.profile, f, self.power))
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
