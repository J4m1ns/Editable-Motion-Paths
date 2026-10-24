"""Sculpt Motion Path: brushes for the timing and the shape of a path, like sculpt mode.

Timing brushes (Push, Slow Down, Speed Up, Even) move the frames along the path; the shape
stays. While you paint, the new frame dots are previewed; when you let go, the keys are
refitted to the new timing once. Shape brushes (Grab, Smooth, Pinch) move the keys.
F: radius, Shift+F: strength, Ctrl: the opposite effect."""

import bpy
from bpy.props import BoolProperty
from bpy_extras.view3d_utils import location_3d_to_region_2d, region_2d_to_location_3d
from mathutils import Vector

from . import compat, core, edit, pick, state

BRUSHES = [
    ('PUSH', "Push", "Timing: push the frames under the brush along the path, the way you drag (the "
                     "shape stays)"),
    ('SLOW', "Slow Down", "Timing: the frames gather under the brush, so the motion is slower there "
                          "(Ctrl: speed up)"),
    ('FAST', "Speed Up", "Timing: the frames spread out from under the brush, so the motion is faster "
                         "there (Ctrl: slow down)"),
    ('EVEN', "Even", "Timing: even out the spacing under the brush"),
    ('GRAB', "Grab", "Shape: pull the path under the brush along with the mouse"),
    ('SMOOTH', "Smooth", "Shape: smooth the path under the brush"),
    ('PINCH', "Pinch", "Shape: draw the path toward the brush center (Ctrl: push it away)"),
]
TIMING = {'PUSH', 'SLOW', 'FAST', 'EVEN'}

_items = []


def brush_items(_self, _context):
    """Enum items with the brush pictures (built once the icons are loaded)."""
    global _items
    if not _items or not _items[0][3]:
        from .ops_easing import icon_id
        _items = [(i, n, d, icon_id("brush_" + i), k) for k, (i, n, d) in enumerate(BRUSHES)]
    return _items


def _falloff(st, d, radius):
    from .ops_transform import falloff
    if d >= radius:
        return 0.0
    return falloff(st.sculpt_falloff, d / radius) * st.sculpt_strength


class _TimingJob:
    """One path: its original motion (a SpaceJob over all its keys) and the frame -> original
    time warp being painted."""

    def __init__(self, cache, tg):
        from .ops_timing import SpaceJob
        ks = cache.keys
        self.cache = cache
        self.r0, self.r1 = float(ks[0].t), float(ks[-1].t)
        self.job = SpaceJob(cache, tg, self.r0, self.r1)
        self.frames = list(range(int(self.r0), int(self.r1) + 1))
        self.w = {f: float(f) for f in self.frames}

    def point(self, t):
        return self.job._at(self.job.pts, min(max(t, self.r0), self.r1))

    def tidy(self):
        """Ends stay, time never runs backwards."""
        fs = self.frames
        self.w[fs[0]], self.w[fs[-1]] = self.r0, self.r1
        gap = 0.05
        for a, b in zip(fs, fs[1:]):
            self.w[b] = max(self.w[b], self.w[a] + gap)
        for a, b in zip(reversed(fs[:-1]), reversed(fs[1:])):
            self.w[a] = min(self.w[a], self.w[b] - gap)
        self.w[fs[0]], self.w[fs[-1]] = self.r0, self.r1

    def warp(self, t):
        fs = self.frames
        if t <= fs[0]:
            return self.w[fs[0]]
        if t >= fs[-1]:
            return self.w[fs[-1]]
        i = int(t)
        f = t - i
        a = self.w.get(i, float(i))
        b = self.w.get(i + 1, float(i + 1))
        return a + (b - a) * f


class EMP_OT_sculpt_stroke(bpy.types.Operator):
    """Paint on the paths with the active brush (Ctrl: the opposite effect)"""
    bl_idname = "emp.sculpt_stroke"
    bl_label = "Sculpt Path"
    bl_options = {'REGISTER', 'UNDO', 'BLOCKING'}

    invert: BoolProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def invoke(self, context, event):
        if context.region_data is None:
            return {'PASS_THROUGH'}
        st = state.settings(context.scene)
        caches = [c for c in state.editable_caches(context.scene) if len(c.keys) >= 2]
        if st is None or not caches:
            self.report({'WARNING'}, "Select a bone / object with a path to sculpt it")
            return {'CANCELLED'}
        self.brush = st.sculpt_brush
        self.radius = st.sculpt_radius * compat.ui_scale()
        self.snaps = []
        for c in caches:
            tg = c.target()
            self.snaps.append((c, core.FCurveSnapshot(tg, tg.channels(c.ptype))))
        self.caches = caches
        m = Vector((event.mouse_region_x, event.mouse_region_y))
        self.start = m.copy()
        self.last = m.copy()
        self.jobs = []
        if self.brush in TIMING:
            skipped = []
            for c in caches:
                if c.ptype in (core.LOCATION, core.ROTATION) and c.keys[-1].t - c.keys[0].t >= 2.0:
                    self.jobs.append(_TimingJob(c, c.target()))
                else:
                    skipped.append(c.label)
            if skipped:
                self.report({'INFO'}, "Timing brushes work on Location / Rotation paths")
            if not self.jobs:
                return {'CANCELLED'}
            state.sculpt_hide = {j.cache.key for j in self.jobs}
        elif self.brush == 'GRAB':
            region, rv3d = context.region, context.region_data
            self.grab = []
            for c in caches:
                for k in c.keys:
                    s = location_3d_to_region_2d(region, rv3d, k.co)
                    if s is None or k.hidden:
                        continue
                    wt = _falloff(st, (s - m).length, self.radius)
                    if wt > 0.0:
                        self.grab.append((c, k.t, k.co.copy(), wt))
        state.modal_running += 1
        self._cursor(context, m)
        context.window_manager.modal_handler_add(self)
        if self.brush != 'GRAB':
            self._dab(context, event, m, Vector((0.0, 0.0)))
        return {'RUNNING_MODAL'}

    def _cursor(self, context, m):
        state.sculpt_cursor = (m.x, m.y, self.radius, context.area.as_pointer(), True)
        state.tag_redraw()

    def modal(self, context, event):
        t, v = event.type, event.value
        if t in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            m = Vector((event.mouse_region_x, event.mouse_region_y))
            self._cursor(context, m)
            if self.brush == 'GRAB':
                self._grab(context, m)
            elif (m - self.last).length >= max(2.0, self.radius * 0.12):
                self._dab(context, event, m, m - self.last)
                self.last = m
        elif t == 'LEFTMOUSE' and v == 'RELEASE':
            self._commit(context)
            return self._end(context, {'FINISHED'})
        elif t in {'ESC', 'RIGHTMOUSE'} and v == 'PRESS':
            for c, s in self.snaps:
                s.restore()
                c.dirty = True
                c.refresh(force=True)
            return self._end(context, {'CANCELLED'})
        return {'RUNNING_MODAL'}

    def _end(self, context, result):
        state.modal_running = max(0, state.modal_running - 1)
        state.sculpt_preview = None
        state.sculpt_hide = set()
        if state.sculpt_cursor is not None:
            x, y, r, a, _on = state.sculpt_cursor
            state.sculpt_cursor = (x, y, r, a, False)
        pick.tag_anim_editors()
        state.tag_redraw()
        return result

    # -- timing ------------------------------------------------------------------

    def _dab(self, context, event, m, delta):
        st = state.settings(context.scene)
        sign = -1.0 if (self.invert or event.ctrl) else 1.0
        region, rv3d = context.region, context.region_data
        if self.brush in TIMING:
            pts = []
            for j in self.jobs:
                self._timing_dab(j, st, region, rv3d, m, delta, sign)
                pts += [j.point(j.w[f]) for f in j.frames]
            state.sculpt_preview = (pts, context.area.as_pointer())
        else:
            self._shape_dab(context, st, region, rv3d, m, sign)
        state.tag_redraw()

    def _timing_dab(self, j, st, region, rv3d, m, delta, sign):
        job = j.job
        b = self.brush
        tc = None
        if b in {'SLOW', 'FAST'}:
            # the original time of the path under the brush center
            best = None
            for t, p in zip(job.ts, job.pts):
                s = location_3d_to_region_2d(region, rv3d, p)
                if s is not None and (best is None or (s - m).length < best[0]):
                    best = ((s - m).length, t)
            if best is None or best[0] > self.radius:
                return
            tc = best[1]
            if b == 'FAST':
                sign = -sign
        new = dict(j.w)
        for f in j.frames[1:-1]:
            p = j.point(j.w[f])
            s = location_3d_to_region_2d(region, rv3d, p)
            if s is None:
                continue
            wt = _falloff(st, (s - m).length, self.radius)
            if wt <= 0.0:
                continue
            if b == 'PUSH':
                a = location_3d_to_region_2d(region, rv3d, j.point(j.w[f] - 0.5))
                c = location_3d_to_region_2d(region, rv3d, j.point(j.w[f] + 0.5))
                if a is None or c is None:
                    continue
                tang = c - a                     # pixels per frame of the original motion
                if tang.length_squared < 1e-6:
                    continue
                new[f] = j.w[f] + wt * sign * delta.dot(tang) / tang.length_squared
            elif b in {'SLOW', 'FAST'}:
                new[f] = j.w[f] + wt * sign * 0.2 * (tc - j.w[f])
            elif b == 'EVEN':
                s0 = job._at(job.arc, j.w[f - 1])
                s1 = job._at(job.arc, j.w[f + 1])
                tt = job._time_at_arc((s0 + s1) * 0.5)
                new[f] = j.w[f] + wt * 0.5 * (tt - j.w[f])
        j.w = new
        j.tidy()

    def _commit(self, context):
        """Timing brushes: refit the keys to the painted timing (once, at the end)."""
        from .ops_timing import refit, warp_target
        for j in self.jobs:
            if all(abs(j.w[f] - f) < 1e-4 for f in j.frames):
                continue
            c = j.cache
            tg = c.target()
            sel = {round(k.t, 3) for k in c.keys if k.sel}
            core.update_fcurves(refit(j.job, tg, warp_target(j.job, tg, j.warp), sel or False))
            c.dirty = True
            c.refresh(force=True)
        state.schedule_rebake(0.3)

    # -- shape ---------------------------------------------------------------------

    def _grab(self, context, m):
        region, rv3d = context.region, context.region_data
        for c, s in self.snaps:
            s.restore()
            c.refresh(force=True)
        by_cache = {}
        for c, t, co0, wt in self.grab:
            a = region_2d_to_location_3d(region, rv3d, self.start, co0)
            b = region_2d_to_location_3d(region, rv3d, m, co0)
            by_cache.setdefault(c, []).append((t, co0 + (b - a) * wt))
        self._write(context, by_cache)

    def _shape_dab(self, context, st, region, rv3d, m, sign):
        by_cache = {}
        for c in self.caches:
            ks = [k for k in c.keys if not k.hidden]
            for i, k in enumerate(ks):
                s = location_3d_to_region_2d(region, rv3d, k.co)
                if s is None:
                    continue
                wt = _falloff(st, (s - m).length, self.radius)
                if wt <= 0.0:
                    continue
                if self.brush == 'SMOOTH':
                    if i == 0 or i == len(ks) - 1:
                        continue        # the ends of the path stay
                    target = (ks[i - 1].co + ks[i + 1].co) * 0.5
                    W = k.co.lerp(target, min(1.0, wt * 0.5))
                else:   # PINCH
                    center = region_2d_to_location_3d(region, rv3d, m, k.co)
                    W = k.co.lerp(center, max(-1.0, min(1.0, wt * 0.3 * sign)))
                by_cache.setdefault(c, []).append((k.t, W))
        self._write(context, by_cache)

    def _write(self, context, by_cache):
        for c, moves in by_cache.items():
            tg, fresh = edit.prepare(context, c, [t for t, _w in moves])
            at = {round(k.t, 3): k for k in fresh}
            w = edit.Writer([t for t, _w in moves])
            for t, W in moves:
                k = at.get(round(t, 3))
                if k is not None:
                    w.key(c, tg, k, W)
            w.done([c])


class EMP_OT_sculpt_size(bpy.types.Operator):
    """Change the brush radius (F) or strength (Shift+F) with the mouse"""
    bl_idname = "emp.sculpt_size"
    bl_label = "Brush Size"
    bl_options = {'INTERNAL'}

    strength: BoolProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def invoke(self, context, event):
        st = state.settings(context.scene)
        if st is None or context.area is None:
            return {'CANCELLED'}
        self.st = st
        self.start = Vector((event.mouse_region_x, event.mouse_region_y))
        self.r0, self.s0 = st.sculpt_radius, st.sculpt_strength
        self.center = self.start - Vector((self.r0 * compat.ui_scale(), 0.0))
        context.window_manager.modal_handler_add(self)
        self._show(context, self.start)
        return {'RUNNING_MODAL'}

    def _show(self, context, m):
        us = compat.ui_scale()
        if self.strength:
            self.st.sculpt_strength = min(1.0, max(0.0, self.s0 + (m.x - self.start.x) / (200.0 * us)))
        else:
            self.st.sculpt_radius = int(max(5, (m - self.center).length / us))
        state.sculpt_cursor = (self.center.x, self.center.y, self.st.sculpt_radius * us,
                               context.area.as_pointer(), True)
        context.area.header_text_set("Brush %s: %s   move the mouse   click / Enter: done   Esc: cancel" % (
            "strength" if self.strength else "radius",
            "%.2f" % self.st.sculpt_strength if self.strength else "%d px" % self.st.sculpt_radius))
        state.tag_redraw()

    def modal(self, context, event):
        t, v = event.type, event.value
        if t in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            self._show(context, Vector((event.mouse_region_x, event.mouse_region_y)))
        elif (t in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER', 'SPACE'} and v == 'PRESS'):
            context.area.header_text_set(None)
            return {'FINISHED'}
        elif t in {'ESC', 'RIGHTMOUSE'} and v == 'PRESS':
            self.st.sculpt_radius, self.st.sculpt_strength = self.r0, self.s0
            context.area.header_text_set(None)
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}


class EMP_OT_sculpt_hover(bpy.types.Operator):
    """(The brush circle follows the mouse)"""
    bl_idname = "emp.sculpt_hover"
    bl_label = "Brush Cursor"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        st = state.settings(context.scene)
        if st is not None and context.area is not None:
            state.sculpt_cursor = (event.mouse_region_x, event.mouse_region_y,
                                   st.sculpt_radius * compat.ui_scale(), context.area.as_pointer(), False)
            context.area.tag_redraw()
        return {'PASS_THROUGH'}


def _k(op, key, props=None, **mods):
    ev = {"type": key, "value": mods.pop("value", 'PRESS')}
    ev.update(mods)
    return (op, ev, {"properties": list(props.items())} if props else None)


SCULPT_KEYMAP = (
    _k("emp.sculpt_stroke", 'LEFTMOUSE'),
    _k("emp.sculpt_stroke", 'LEFTMOUSE', {"invert": True}, ctrl=True),
    _k("emp.sculpt_size", 'F'),
    _k("emp.sculpt_size", 'F', {"strength": True}, shift=True),
    _k("emp.sculpt_hover", 'MOUSEMOVE', value='ANY', any=True),
    _k("wm.context_set_enum", 'ONE', {"data_path": "scene.emp.edit_mode", "value": 'LOCATION'}),
    _k("wm.context_set_enum", 'TWO', {"data_path": "scene.emp.edit_mode", "value": 'ROTATION'}),
)


def _draw_settings(context, layout, tool):
    st = state.settings(context.scene)
    if st is None:
        return
    layout.template_icon_view(st, "sculpt_brush", show_labels=True, scale=1.0, scale_popup=4.0)
    name = {i: n for i, n, _d in BRUSHES}.get(st.sculpt_brush, "")
    layout.label(text=name)
    layout.prop(st, "sculpt_radius")
    layout.prop(st, "sculpt_strength", slider=True)
    layout.prop(st, "sculpt_falloff", text="")
    layout.prop(st, "edit_mode", text="")


class EMP_SculptPoseTool(bpy.types.WorkSpaceTool):
    bl_space_type = 'VIEW_3D'
    bl_context_mode = 'POSE'
    bl_idname = "emp.sculpt_path_pose"
    bl_label = "Sculpt Motion Path"
    bl_description = ("Paint on motion paths with brushes: the timing (Push, Slow Down, Speed Up, Even: "
                      "the shape stays) or the shape (Grab, Smooth, Pinch). F: radius, Shift+F: strength, "
                      "Ctrl: the opposite effect")
    bl_icon = "brush.particle.comb"
    bl_widget = None
    bl_keymap = SCULPT_KEYMAP
    draw_settings = _draw_settings


class EMP_SculptObjectTool(bpy.types.WorkSpaceTool):
    bl_space_type = 'VIEW_3D'
    bl_context_mode = 'OBJECT'
    bl_idname = "emp.sculpt_path_object"
    bl_label = EMP_SculptPoseTool.bl_label
    bl_description = EMP_SculptPoseTool.bl_description
    bl_icon = EMP_SculptPoseTool.bl_icon
    bl_widget = None
    bl_keymap = SCULPT_KEYMAP
    draw_settings = _draw_settings


classes = (EMP_OT_sculpt_stroke, EMP_OT_sculpt_size, EMP_OT_sculpt_hover)


def register_tools():
    bpy.utils.register_tool(EMP_SculptPoseTool, after={"emp.edit_path_pose"})
    bpy.utils.register_tool(EMP_SculptObjectTool, after={"emp.edit_path_object"})


def unregister_tools():
    bpy.utils.unregister_tool(EMP_SculptObjectTool)
    bpy.utils.unregister_tool(EMP_SculptPoseTool)
