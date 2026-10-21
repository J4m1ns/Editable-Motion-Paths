"""Slide selected keys along the path (G G).

Timing:   keys keep their pose and move in time, like dragging them in the timeline.
Spacing:  keys keep their frame and slide along the trajectory (changes the spacing).
Continue: (C) the pose follows the trajectory and keeps going past the first / last key,
          extrapolating the motion - like vertex slide with clamp off.
"""

import bisect

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty
from bpy_extras.view3d_utils import location_3d_to_region_2d
from mathutils import Vector

from . import core, pick, state
from .ops_keys import frames_from_mouse
from .ops_transform import NumInput

SAMPLE_STEP = 0.1
UNBOUNDED = 1e9


def _window_region(area):
    return next((r for r in area.regions if r.type == 'WINDOW'), None)


def _lerp(a, b, f):
    if isinstance(a, Vector):
        return a.lerp(b, f)
    return [x + (y - x) * f for x, y in zip(a, b)]


def _interp(times, values, t):
    i = bisect.bisect_left(times, t)
    if i <= 0:
        return values[0]
    if i >= len(times):
        return values[-1]
    t0, t1 = times[i - 1], times[i]
    f = 0.0 if t1 - t0 < 1e-9 else (t - t0) / (t1 - t0)
    return _lerp(values[i - 1], values[i], f)


class Trajectory:
    """Original motion of a path (world points or channel values) as a function of time,
    optionally extrapolated linearly past the first / last key."""

    def __init__(self, cache, tg, kind):
        ch = tg.channels(cache.ptype)
        kts = [k.t for k in cache.keys]
        self.t_first, self.t_last = (kts[0], kts[-1]) if kts else (cache.fs, cache.fe)
        lo = min(cache.fs, self.t_first)
        hi = max(cache.fe, self.t_last)
        n = int((hi - lo) / SAMPLE_STEP) + 1
        self.times = [lo + i * SAMPLE_STEP for i in range(n)]
        if kind == 'WORLD':
            def f(t):
                return cache.point(tg, t)
        else:
            def f(t):
                return ch.values(t)
        self.values = [f(t) for t in self.times]
        self.lo, self.hi = lo, hi
        # extrapolation: average velocity of the first / last segment between keys
        if len(kts) >= 2:
            a, b = kts[0], kts[1]
            self.v_first = self._vel(f(a), f(b), b - a)
            a, b = kts[-2], kts[-1]
            self.v_last = self._vel(f(a), f(b), b - a)
        else:
            z = f(self.t_first)
            self.v_first = self.v_last = (Vector() if isinstance(z, Vector) else [0.0] * len(z))
        self.p_first = f(self.t_first)
        self.p_last = f(self.t_last)

    @staticmethod
    def _vel(a, b, dt):
        if isinstance(a, Vector):
            return (b - a) / dt
        return [(y - x) / dt for x, y in zip(a, b)]

    def at(self, t, extrapolate):
        if extrapolate and (t > self.t_last or t < self.t_first):
            if t > self.t_last:
                p, v, t0 = self.p_last, self.v_last, self.t_last
            else:
                p, v, t0 = self.p_first, self.v_first, self.t_first
            if isinstance(p, Vector):
                return p + v * (t - t0)
            return [a + b * (t - t0) for a, b in zip(p, v)]
        return _interp(self.times, self.values, min(max(t, self.lo), self.hi))


class _Job:
    """One path with selected keys, plus everything needed to slide them."""

    def __init__(self, cache, all_channels, slide_mode, extrapolate, ensure=True):
        self.cache = cache
        tg = cache.target()
        sel_t = [k.t for k in cache.keys if k.sel]
        ch = tg.channels(cache.ptype)
        # the original motion, before anything is changed
        self.traj = Trajectory(cache, tg, 'WORLD' if cache.ptype == core.LOCATION else 'VALUES')
        # key every channel of the edited property on the slid frames first, so a key that
        # was only set on some axes keeps (Timing) or reaches (Spacing) its full pose
        self.pre = core.FCurveSnapshot(tg, ch)
        if ensure and core.ensure_keys(tg, cache.ptype, sel_t, None, state.settings().pin_partial):
            cache.refresh(force=True)
            tg = cache.target()
            ch = tg.channels(cache.ptype)
        self.keys = [k for k in cache.keys if k.sel]
        self.times = {round(k.t, 3) for k in self.keys}
        ks = cache.keys
        # every key stays between its unselected neighbors; at the ends it may leave the range
        self.bounds = []
        for k in self.keys:
            i = ks.index(k)
            prev = next((ks[j].t for j in range(i - 1, -1, -1) if not ks[j].sel), None)
            nxt = next((ks[j].t for j in range(i + 1, len(ks)) if not ks[j].sel), None)
            if slide_mode == 'SPACING' and not extrapolate:
                prev = cache.fs - 1.0 if prev is None else prev
                nxt = cache.fe + 1.0 if nxt is None else nxt
            self.bounds.append((-UNBOUNDED if prev is None else prev, UNBOUNDED if nxt is None else nxt))

        if slide_mode == 'TIMING':
            if all_channels and tg.coll is not None:
                fcs = [fc for fc in tg.coll if fc.data_path.startswith(tg.prefix)] if tg.prefix \
                    else list(tg.coll)
            else:
                fcs = [fc for fc in ch.fcurves if fc is not None]
            self.snapshot = core.FCurveSnapshot(tg, items=[(fc.data_path, fc.array_index, fc) for fc in fcs])
            # (data_path, index, [(kp index, x, hl_x, hr_x)], lo, hi, is_path_channel)
            self.moving = []
            self.lo, self.hi = -UNBOUNDED, UNBOUNDED
            main = {(ch.path, c) for c in range(ch.count)}
            for fc in fcs:
                mv = []
                still = []
                for i, kp in enumerate(fc.keyframe_points):
                    x = kp.co[0]
                    if round(x, 3) in self.times:
                        mv.append((i, x, kp.handle_left[0], kp.handle_right[0]))
                    else:
                        still.append(x)
                if not mv:
                    continue
                lo, hi = -UNBOUNDED, UNBOUNDED
                for _i, x, _l, _r in mv:
                    before = [s for s in still if s < x]
                    after = [s for s in still if s > x]
                    if before:
                        lo = max(lo, max(before) - x)
                    if after:
                        hi = min(hi, min(after) - x)
                is_main = (fc.data_path, fc.array_index) in main
                self.moving.append((fc.data_path, fc.array_index, mv, lo, hi, is_main))
                if is_main:
                    # only the keys you see on this path limit the slide; the bone's other
                    # channels just stop at their own neighbors
                    self.lo, self.hi = max(self.lo, lo), min(self.hi, hi)
        else:
            self.snapshot = core.FCurveSnapshot(tg, ch)

    def apply(self, slide_mode, delta, snap, extrapolate):
        cache = self.cache
        tg = cache.target()
        if tg is None:
            return
        touched = set()
        ch = tg.channels(cache.ptype)
        if slide_mode == 'TIMING':
            coll = tg.coll
            gap = 1.0 if snap else 0.01
            for path, index, mv, lo, hi, is_main in self.moving:
                fc = coll.find(path, index=index)
                if fc is None:
                    continue
                d = delta
                if not is_main:
                    if lo > -UNBOUNDED:
                        d = max(d, min(0.0, lo + gap))
                    if hi < UNBOUNDED:
                        d = min(d, max(0.0, hi - gap))
                kps = fc.keyframe_points
                for i, x, hlx, hrx in mv:
                    kp = kps[i]
                    kp.co = (x + d, kp.co[1])
                    kp.handle_left = (hlx + d, kp.handle_left[1])
                    kp.handle_right = (hrx + d, kp.handle_right[1])
                touched.add(fc)
            if extrapolate:
                # Continue: the pose follows the (extrapolated) trajectory to the new frame
                for k in self.keys:
                    t = k.t + delta
                    core.write_key(ch, k, self._vals(tg, k, t, t), True, touched)
        else:
            gap = 1.0 if snap else 0.05
            for k, (lo, hi) in zip(self.keys, self.bounds):
                t = min(max(k.t + delta, lo + gap), hi - gap) if hi - lo > 2 * gap else k.t
                core.write_key(ch, k, self._vals(tg, k, t, k.t, extrapolate), True, touched)
        core.update_fcurves(touched)
        cache.dirty = True

    def _vals(self, tg, k, t_pose, t_key, extrapolate=True):
        """Channel values that put the key (at frame t_key) on the trajectory at t_pose."""
        v = self.traj.at(t_pose, extrapolate)
        if self.cache.ptype == core.LOCATION:
            return self.cache.solve(tg, t_key, v, k.vals)
        return list(v)


class EMP_OT_slide(bpy.types.Operator):
    """Slide the selected keys along the path (G G). Timing: keys keep their pose and move """ \
        """in time, like in the timeline. Spacing: keys keep their frame and slide along the """ \
        """trajectory. Continue (C): the motion goes on past the first / last key"""
    bl_idname = "emp.slide"
    bl_label = "Slide Keys Along Path"
    bl_options = {'REGISTER', 'UNDO', 'BLOCKING'}

    slide_mode: EnumProperty(name="Mode", items=[
        ('SPACING', "Spacing", "Keys keep their frame and slide along the trajectory under the "
                               "mouse, so the bone arrives there earlier or later"),
        ('TIMING', "Timing", "Keys keep their pose and move in time (like dragging them in the "
                             "timeline). The frame dots around them spread out or bunch up"),
    ], default='TIMING')
    extrapolate: BoolProperty(
        name="Continue Motion", default=False,
        description="The pose follows the trajectory and keeps going past the first / last key, "
                    "extrapolating the motion (C while sliding)")
    offset: FloatProperty(name="Frames", default=0.0, description="Time offset along the path")
    all_channels: BoolProperty(
        name="Whole Pose", default=True,
        description="Timing: also move the bone's other keys on the same frames (rotation, scale, "
                    "custom properties), like the Dope Sheet summary row")
    start_x: IntProperty(default=-100000, options={'HIDDEN', 'SKIP_SAVE'})
    start_y: IntProperty(default=-100000, options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        return context.mode in {'POSE', 'OBJECT'}

    def _jobs(self, context, ensure=True):
        jobs = []
        for cache in state.editable_caches(context.scene):
            if any(k.sel for k in cache.keys):
                jobs.append(_Job(cache, self.all_channels, self.slide_mode, self.extrapolate, ensure))
        return jobs

    def _clamp(self, delta, snap):
        gap = 1.0 if snap else 0.01
        lo, hi = -UNBOUNDED, UNBOUNDED
        if self.slide_mode == 'TIMING':
            lo = max(j.lo for j in self.jobs)
            hi = min(j.hi for j in self.jobs)
        if lo > -UNBOUNDED:
            lo += gap
        if hi < UNBOUNDED:
            hi -= gap
        if lo > hi:
            return 0.0
        return min(max(delta, lo), hi)

    def _apply(self, delta, snap):
        core.cycle_skip = {round(k.t, 3) for job in self.jobs for k in job.keys}
        for job in self.jobs:
            job.apply(self.slide_mode, delta, snap, self.extrapolate)
        pick.tag_anim_editors()

    def execute(self, context):
        self.jobs = self._jobs(context)
        if not self.jobs:
            self.report({'WARNING'}, "Select keys on a path first")
            return {'CANCELLED'}
        snap = abs(self.offset - round(self.offset)) < 1e-6
        self._apply(self._clamp(self.offset, snap), snap)
        return {'FINISHED'}

    # -- interactive -----------------------------------------------------------

    def invoke(self, context, event):
        area = context.area
        if area is None or area.type != 'VIEW_3D':
            area = next((a for a in context.screen.areas if a.type == 'VIEW_3D'), None)
        if area is None:
            return {'CANCELLED'}
        self.area = area
        self.region = _window_region(area)
        self.rv3d = self.region.data
        if self.start_x > -100000:
            self.mouse0 = Vector((self.start_x, self.start_y))
        else:
            self.mouse0 = Vector((event.mouse_x - self.region.x, event.mouse_y - self.region.y))
        self.jobs = self._jobs(context, ensure=False)
        self.ensured = False
        if not self.jobs:
            return {'PASS_THROUGH'}
        self._build_track()
        # the mouse works like in the timeline: right = later, left = earlier
        self.x0 = self.mouse0.x
        self.xv = float(event.mouse_x - self.region.x)
        self.xl = event.mouse_x - self.region.x
        self.offset = 0.0
        self.numinput = NumInput(1)
        for job in self.jobs:
            state.ghosts[job.cache.key] = list(job.cache.points)
        state.modal_running += 1
        state.hud_area = area.as_pointer()
        state.force_numbers = True
        context.window_manager.modal_handler_add(self)
        self._update(context, event)
        return {'RUNNING_MODAL'}

    # -- following the path with the mouse -------------------------------------

    def _build_track(self):
        """The path between its first and last key plus a straight continuation on both ends
        (keys can go past the ends): the slide marker is shown on it."""
        best = None
        for job in self.jobs:
            for k in job.keys:
                s = location_3d_to_region_2d(self.region, self.rv3d, k.co)
                d = (s - self.mouse0).length if s is not None else 1e9
                if best is None or d < best[0]:
                    best = (d, job, k)
        _d, job, k = best
        self.active_t = k.t
        cache = job.cache
        traj = Trajectory(cache, cache.target(), 'WORLD')
        t0, t1 = traj.t_first, traj.t_last
        span = max(t1 - t0, 1.0)
        ext = max(20.0, span * 0.6)
        times = [t for t in cache.times if t0 <= t <= t1]
        pts = [p for t, p in zip(cache.times, cache.points) if t0 <= t <= t1]
        if not times:
            times, pts = list(cache.times), list(cache.points)
        # make sure the ends move on screen even if the motion stops there
        arc = sum((b - a).length for a, b in zip(pts, pts[1:]))
        avg = arc / span if arc > 1e-9 else 0.01
        right = self.rv3d.view_rotation @ Vector((1.0, 0.0, 0.0))

        def end_vel(v, fallback):
            if v.length > avg * 0.15:
                return v
            return (fallback.normalized() if fallback.length > 1e-9 else right) * avg

        chord = pts[-1] - pts[0]
        v_last = end_vel(traj.v_last, chord)
        v_first = end_vel(traj.v_first, chord)
        n = int(ext)
        pre_t = [t0 - ext + i for i in range(n)]
        post_t = [t1 + 1 + i for i in range(n)]
        pre_p = [traj.p_first + v_first * (t - t0) for t in pre_t]
        post_p = [traj.p_last + v_last * (t - t1) for t in post_t]
        self.track_times = pre_t + times + post_t
        self.track_points = pre_p + pts + post_p
        state.slide_extension = (pre_p + [pts[0]], [pts[-1]] + post_p)

    def _update(self, context, event):
        snap = not event.ctrl
        if self.numinput.active:
            v = self.numinput.value(0)
            delta = v if v is not None else 0.0
        else:
            delta = frames_from_mouse(self.xv - self.x0)
            if snap:
                delta = round(self.active_t + delta) - self.active_t
        delta = self._clamp(delta, snap)
        if abs(delta) > 1e-9 and not self.ensured:
            # first real slide: add missing channel keys now (not before, so G G alone
            # never changes the curve)
            self._restore()
            for c in state.caches.values():
                c.refresh(force=True)
            self.jobs = self._jobs(context, ensure=True)
            self.ensured = True
            delta = self._clamp(delta, snap)
        self.offset = delta
        if self.ensured:
            self._apply(delta, snap)

        t_new = self.active_t + delta
        p = _interp(self.track_times, self.track_points, t_new)
        name = "Timing" if self.slide_mode == 'TIMING' else "Spacing"
        if self.extrapolate:
            name += " + Continue"
        frame = ("%d" % round(t_new)) if abs(t_new - round(t_new)) < 1e-3 else ("%.2f" % t_new)
        txt = "%s: frame %s (%s)" % (name, frame, self.numinput.text(0, delta, "%+.2f"))
        state.slide_marker = (p, "%s  %+g" % (frame, round(delta, 2)))
        state.hud = (txt, (event.mouse_x - self.region.x, event.mouse_y - self.region.y))
        self.area.header_text_set(
            txt + "   move left / right   T: Timing / Spacing   C: continue past the ends   "
                  "Ctrl: subframes   Shift: precise   "
                  "type a frame offset   LMB/Enter: confirm   RMB/Esc: cancel")
        state.tag_redraw()

    def _finish(self, context):
        state.modal_running = max(0, state.modal_running - 1)
        state.ghosts.clear()
        state.hud = None
        state.slide_marker = None
        state.slide_extension = None
        state.force_numbers = False
        self.area.header_text_set(None)
        state.tag_redraw()

    def _restore(self):
        for job in self.jobs:
            job.snapshot.restore()
            job.pre.restore()       # also undo the keys added by ensure_keys
            job.cache.dirty = True

    def _rebuild(self, context, event):
        self._restore()
        for c in state.caches.values():
            c.refresh(force=True)
        self.jobs = [_Job(j.cache, self.all_channels, self.slide_mode, self.extrapolate, False)
                     for j in self.jobs]
        self.ensured = False
        self._update(context, event)

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
            x = event.mouse_x - self.region.x
            self.xv += (x - self.xl) * (0.1 if event.shift else 1.0)
            self.xl = x
            self._update(context, event)
        elif t in {'LEFT_CTRL', 'RIGHT_CTRL'}:
            self._update(context, event)
        elif (t == 'LEFTMOUSE' and v == 'PRESS') or (t in {'RET', 'NUMPAD_ENTER', 'SPACE'} and v == 'PRESS'):
            self._finish(context)
            return {'FINISHED'}
        elif t in {'RIGHTMOUSE', 'ESC'} and v == 'PRESS':
            self._restore()
            self._finish(context)
            return {'CANCELLED'}
        elif t == 'T' and v == 'PRESS':
            self.slide_mode = 'SPACING' if self.slide_mode == 'TIMING' else 'TIMING'
            self._rebuild(context, event)
        elif t == 'C' and v == 'PRESS':
            self.extrapolate = not self.extrapolate
            self._rebuild(context, event)
        elif self.numinput.handle(event):
            self._update(context, event)
        return {'RUNNING_MODAL'}


classes = (EMP_OT_slide,)
