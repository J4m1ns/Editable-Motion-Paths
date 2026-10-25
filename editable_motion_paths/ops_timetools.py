"""Timing tools: sculpt the spacing by dragging frame dots (or the ticks of the timing chart),
scale time, add / remove frames, tween, moving hold, offset cascade, snap to frames.

All of them are dragged left / right like the timeline (type a value, Ctrl: subframes or fine
steps, Esc: cancel) and change whole poses, so the bone's other keys stay in sync."""

import math

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty
from mathutils import Vector

from . import compat, core, pick, space, state
from .ops_keys import _pose_fcurves, frames_from_mouse, kp_entry, put_keys
from .ops_timing import SpaceJob, pchip, refit, warp_target
from .ops_transform import NumInput


def pose_snapshot(cache, all_channels=True):
    tg = cache.target()
    return core.FCurveSnapshot(tg, items=[(fc.data_path, fc.array_index, fc)
                                          for fc in _pose_fcurves(tg, cache, all_channels)])


def move_keys_in_time(cache, mapping, select=None, all_channels=True):
    """Move keys of the path's bone / object (whole pose) in time: mapping {old: new frame}.
    `select`: old frames whose keys are selected afterwards."""
    tg = cache.target()
    old = {round(a, 3): b for a, b in mapping.items()}
    sel_new = {round(old[round(t, 3)], 3) for t in (select or ()) if round(t, 3) in old}
    for fc in _pose_fcurves(tg, cache, all_channels):
        cyc = core.cycle_info(fc)
        kps = fc.keyframe_points
        ents = [kp_entry(kp) for kp in kps if round(kp.co[0], 3) in old]
        if not ents:
            continue
        for i in reversed(range(len(kps))):
            if round(kps[i].co[0], 3) in old:
                kps.remove(kps[i], fast=True)
        put_keys(fc, [(core.wrap_time(old[round(e[0], 3)], cyc),) + e[1:] for e in ents], select_new=False)
        for kp in kps:
            if round(kp.co[0], 3) in sel_new:
                kp.select_control_point = kp.select_left_handle = kp.select_right_handle = True
    if mapping:
        cache.extend_to(min(mapping.values()))
        cache.extend_to(max(mapping.values()))
    cache.dirty = True
    cache.refresh(force=True)


def _selected_caches(context):
    return [c for c in state.editable_caches(context.scene) if any(k.sel for k in c.keys)]


# ----------------------------------------------------------------------------
# Shared modal: drag left / right
# ----------------------------------------------------------------------------

class _HorizontalDrag:
    bl_options = {'REGISTER', 'UNDO', 'BLOCKING'}
    hint = ""

    def _caches(self, context):
        return _selected_caches(context)

    def _from_mouse(self, dx, event):
        f = frames_from_mouse(dx)
        return f if event.ctrl else float(round(f))

    def _label(self):
        return "%s: %+g" % (self.bl_label, self.amount)

    def _modal_key(self, context, event):
        return False

    def execute(self, context):
        self.caches = self._caches(context)
        if not self.caches:
            self.report({'WARNING'}, "Select keys on a path first")
            return {'CANCELLED'}
        self._apply(context)
        pick.tag_anim_editors()
        return {'FINISHED'}

    def invoke(self, context, event):
        self.caches = self._caches(context)
        if not self.caches:
            return {'PASS_THROUGH'}
        self.snaps = [pose_snapshot(c) for c in self.caches]
        self.x0 = event.mouse_x
        self.xv = float(event.mouse_x)
        self.xl = event.mouse_x
        self.numinput = NumInput(1)
        self.area = context.area
        state.modal_running += 1
        state.force_numbers = True
        context.window_manager.modal_handler_add(self)
        self._update(context, event)
        return {'RUNNING_MODAL'}

    def _restore(self):
        for s in reversed(self.snaps):
            s.restore()
        for c in self.caches:
            c.dirty = True
            c.refresh(force=True)

    def _update(self, context, event):
        if self.numinput.active:
            v = self.numinput.value(0)
            self.amount = v if v is not None else self._neutral()
        else:
            self.amount = self._from_mouse(self.xv - self.x0, event)
        self._restore()
        moved = self.xv != self.x0 or self.numinput.active
        if moved and abs(self.amount - self._neutral()) > 1e-9:
            self._apply(context)
        txt = self._label()
        state.hud = (txt, (event.mouse_region_x, event.mouse_region_y))
        if self.area is not None:
            self.area.header_text_set(txt + "   move left / right   " + self.hint +
                                      "Ctrl: fine   type a value   click: confirm   Esc: cancel")
        pick.tag_anim_editors()
        state.tag_redraw()

    def _neutral(self):
        return 0.0

    def modal(self, context, event):
        t, v = event.type, event.value
        if t in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            self.xv += (event.mouse_x - self.xl) * (0.1 if event.shift else 1.0)
            self.xl = event.mouse_x
            self._update(context, event)
        elif t in {'LEFT_CTRL', 'RIGHT_CTRL'}:
            self._update(context, event)
        elif (t == 'LEFTMOUSE' and v == 'PRESS') or (t in {'RET', 'NUMPAD_ENTER', 'SPACE'} and v == 'PRESS'):
            self._finish(context)
            state.schedule_rebake(0.3)
            return {'FINISHED'}
        elif t in {'RIGHTMOUSE', 'ESC'} and v == 'PRESS':
            self._restore()
            self._finish(context)
            return {'CANCELLED'}
        elif v == 'PRESS' and self._modal_key(context, event):
            self._update(context, event)
        elif self.numinput.handle(event):
            self._update(context, event)
        return {'RUNNING_MODAL'}

    def _finish(self, context):
        state.modal_running = max(0, state.modal_running - 1)
        state.force_numbers = False
        state.hud = None
        if self.area is not None:
            self.area.header_text_set(None)
        pick.tag_anim_editors()


# ----------------------------------------------------------------------------
# Sculpt the spacing: drag a frame dot along the path (or a tick of the timing chart)
# ----------------------------------------------------------------------------

class EMP_OT_drag_frame(bpy.types.Operator):
    """Drag a frame dot along the path to change where the bone is on that frame: the frames """ \
        """around it follow smoothly, the keys and the shape of the path stay"""
    bl_idname = "emp.drag_frame"
    bl_label = "Drag Frame"
    bl_options = {'REGISTER', 'UNDO', 'BLOCKING'}

    frame: FloatProperty(name="Frame")
    obj_name: StringProperty(options={'HIDDEN'})
    bone_name: StringProperty(options={'HIDDEN'})
    source: FloatProperty(name="Shows Frame", description="The original pose this frame shows now")
    chart: BoolProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def _cache(self, context):
        for c in state.editable_caches(context.scene):
            if c.obj_name == self.obj_name and c.bone_name == self.bone_name:
                return c
        return None

    def _setup(self, context):
        c = self._cache(context)
        if c is None:
            return False
        before = [k.t for k in c.keys if k.t < self.frame - 1e-3]
        after = [k.t for k in c.keys if k.t > self.frame + 1e-3]
        if not before or not after:
            return False
        self.c = c
        tg = c.target()
        self.job = SpaceJob(c, tg, float(before[-1]), float(after[0]))
        self.sel = {round(k.t, 3) for k in c.keys if k.sel}
        return True

    def _apply(self):
        job = self.job
        s = min(max(self.source, job.r0 + 0.05), job.r1 - 0.05)
        warp = pchip([job.r0, self.frame, job.r1], [job.r0, s, job.r1])
        tg = self.c.target()
        core.update_fcurves(refit(job, tg, warp_target(job, tg, warp), self.sel))
        self.c.refresh(force=True)

    def execute(self, context):
        if not self._setup(context):
            return {'CANCELLED'}
        self._apply()
        pick.tag_anim_editors()
        return {'FINISHED'}

    def invoke(self, context, event):
        if not self._setup(context):
            return {'CANCELLED'}
        self.source = self.frame
        self.area = context.area
        if self.chart and state.chart is not None:
            ch = state.chart
            self.chart_job = SpaceJob(self.c, self.c.target(), float(ch["r0"]), float(ch["r1"]))
        state.modal_running += 1
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _source_from_mouse(self, context, event):
        from bpy_extras.view3d_utils import location_3d_to_region_2d as proj
        job = self.job
        m = Vector((event.mouse_region_x, event.mouse_region_y))
        if self.chart and state.chart is not None:
            ch = state.chart
            u = (m.x - ch["x0"]) / max(ch["x1"] - ch["x0"], 1.0)
            cj = getattr(self, "chart_job", None)
            if cj is None:
                return None
            return cj._time_at_arc(min(max(u, 0.0), 1.0) * cj.arc[-1])
        best = None
        for t, p in zip(job.ts, job.pts):
            s = proj(context.region, context.region_data, p)
            if s is not None:
                d = (s - m).length
                if best is None or d < best[0]:
                    best = (d, t)
        return best[1] if best else None

    def modal(self, context, event):
        t, v = event.type, event.value
        if t in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            s = self._source_from_mouse(context, event)
            if s is not None:
                self.job.snapshot.restore()
                self.c.refresh(force=True)
                self.source = s
                self._apply()
                self.area.header_text_set("Frame %g now shows the pose of frame %.2f   click / release: "
                                          "confirm   Esc: cancel" % (self.frame, self.source))
                pick.tag_anim_editors()
        elif (t == 'LEFTMOUSE' and v in {'RELEASE', 'PRESS'}) or (t in {'RET', 'SPACE'} and v == 'PRESS'):
            return self._end(context, True)
        elif t in {'RIGHTMOUSE', 'ESC'} and v == 'PRESS':
            return self._end(context, False)
        return {'RUNNING_MODAL'}

    def _end(self, context, ok):
        state.modal_running = max(0, state.modal_running - 1)
        self.area.header_text_set(None)
        if not ok:
            self.job.snapshot.restore()
            self.c.dirty = True
            pick.tag_anim_editors()
            return {'CANCELLED'}
        return {'FINISHED'}


# ----------------------------------------------------------------------------
# Time tools
# ----------------------------------------------------------------------------

class EMP_OT_time_scale(_HorizontalDrag, bpy.types.Operator):
    """Stretch or squash the timing of the selected keys (around the active or first one): """ \
        """drag right for slower, left for faster. R: ripple (move the later keys too)"""
    bl_idname = "emp.time_scale"
    bl_label = "Scale Time"
    hint = "R: ripple   "

    amount: FloatProperty(name="Factor", default=1.0, min=0.05, soft_max=4.0)
    ripple: BoolProperty(name="Ripple", default=False, description="Move the keys after the selection too")

    def _neutral(self):
        return 1.0

    def _from_mouse(self, dx, event):
        f = max(0.05, 1.0 + dx / (250.0 * compat.ui_scale()))
        return f if event.ctrl else round(f, 2)

    def _label(self):
        return "Scale Time: x%.2f%s" % (self.amount, "  (ripple)" if self.ripple else "")

    def _modal_key(self, context, event):
        if event.type == 'R':
            self.ripple = not self.ripple
            return True
        return False

    def _apply(self, context):
        for c in self.caches:
            sel = sorted(k.t for k in c.keys if k.sel)
            if len(sel) < 2:
                continue
            a = space.active_key([c])
            pivot = a[1].t if a is not None else sel[0]
            new = {t: pivot + (t - pivot) * self.amount for t in sel}
            if not self.ripple:
                # the keys stay between their unselected neighbors
                others = [k.t for k in c.keys if not k.sel]
                lo = max([t for t in others if t < sel[0]], default=-1e9) + 1.0
                hi = min([t for t in others if t > sel[-1]], default=1e9) - 1.0
                new = {t: min(max(v, lo), hi) for t, v in new.items()}
            new = {t: float(round(v)) for t, v in new.items()}
            if len(set(new.values())) < len(new):
                continue        # squashed too much: keys would meet
            mapping = dict(new)
            if self.ripple:
                d = new[sel[-1]] - sel[-1]
                for k in c.keys:
                    if not k.sel and k.t > sel[-1]:
                        mapping[k.t] = k.t + d
            move_keys_in_time(c, mapping, sel)


class EMP_OT_add_frames(_HorizontalDrag, bpy.types.Operator):
    """Add (drag right) or remove (drag left) frames after the active key: everything after it """ \
        """moves (ripple). Removing stops at the next key"""
    bl_idname = "emp.add_frames"
    bl_label = "Add / Remove Frames"

    amount: FloatProperty(name="Frames", default=0.0)

    def _caches(self, context):
        return [c for c in state.editable_caches(context.scene) if c.keys]

    def _label(self):
        return "%s %d frame%s" % ("Add" if self.amount >= 0 else "Remove", abs(self.amount),
                                  "" if abs(self.amount) == 1 else "s")

    def _apply(self, context):
        cur = context.scene.frame_current
        for c in self.caches:
            a = space.active_key([c])
            pivot = a[1].t if a is not None else cur
            later = [k.t for k in c.keys if k.t > pivot + 1e-3]
            if not later:
                continue
            d = self.amount
            if d < 0:
                d = max(d, -(later[0] - pivot - 1.0))
            if abs(d) < 1e-9:
                continue
            move_keys_in_time(c, {t: t + d for t in later}, [k.t for k in c.keys if k.sel])


class EMP_OT_tween(_HorizontalDrag, bpy.types.Operator):
    """Breakdown: blend the selected keys between the poses of their neighbors, closer to the """ \
        """previous one (drag left) or the next one (drag right), like Tween Machine. (To move """ \
        """keys in time or along the path, use Slide, G G)"""
    bl_idname = "emp.tween"
    bl_label = "Breakdown"

    amount: FloatProperty(name="Favor", default=0.0, min=-1.0, max=1.0, subtype='FACTOR',
                          description="-1: the previous key's pose, 0: halfway, 1: the next key's pose")

    def _neutral(self):
        return -9.0         # any drag applies (0 = halfway is a real result)

    def _from_mouse(self, dx, event):
        f = max(-1.0, min(1.0, dx / (200.0 * compat.ui_scale())))
        return f if event.ctrl else round(f / 0.05) * 0.05

    def _label(self):
        side = "the previous pose" if self.amount < 0 else "the next pose"
        return "Breakdown: %+.2f%s" % (self.amount, "  (halfway)" if abs(self.amount) < 1e-6 else
                                       "  (toward %s)" % side)

    def _apply(self, context):
        from . import edit
        u = (self.amount + 1.0) * 0.5
        for c in self.caches:
            ks = c.keys
            sel = [i for i, k in enumerate(ks) if k.sel]
            jobs = []
            for i in sel:
                lo = next((j for j in range(i - 1, -1, -1) if not ks[j].sel), None)
                hi = next((j for j in range(i + 1, len(ks)) if not ks[j].sel), None)
                if lo is not None and hi is not None:
                    jobs.append((ks[i].t, ks[lo], ks[hi]))
            if not jobs:
                continue
            tg, fresh = edit.prepare(context, c, [t for t, _a, _b in jobs])
            by_t = {round(k.t, 3): k for k in fresh}
            w = edit.Writer([t for t, _a, _b in jobs])
            for t, a, b in jobs:
                k = by_t.get(round(t, 3))
                if k is None:
                    continue
                vals = [x + (y - x) * u for x, y in zip(a.vals, b.vals)]
                core.write_key(tg.channels(c.ptype), k, vals, True, w.touched)
            w.done([c])


class EMP_OT_offset_cascade(_HorizontalDrag, bpy.types.Operator):
    """Offset the selected keys of several paths one after another (overlap / follow through): """ \
        """each path waits that many frames longer than the one before"""
    bl_idname = "emp.offset_cascade"
    bl_label = "Offset Cascade"

    amount: FloatProperty(name="Frames per Path", default=0.0)

    WHY = "Offset Cascade works across several paths: select keys on 2 or more bones / objects " \
          "(each one then starts a few frames after the one before)"

    def _label(self):
        return "Offset Cascade: %+g frames per path (%d paths)" % (self.amount, len(self.caches))

    def execute(self, context):
        if len(self._caches(context)) < 2:
            self.report({'WARNING'}, self.WHY)
            return {'CANCELLED'}
        return _HorizontalDrag.execute(self, context)

    def invoke(self, context, event):
        if len(self._caches(context)) < 2:
            self.report({'WARNING'}, self.WHY)
            return {'CANCELLED'}
        return _HorizontalDrag.invoke(self, context, event)

    def _caches(self, context):
        caches = _selected_caches(context)

        def depth(c):
            obj, owner = core.resolve(c.obj_name, c.bone_name)
            if c.bone_name and owner is not None:
                return len(owner.parent_recursive)
            n, o = 0, obj
            while o is not None and o.parent is not None:
                n, o = n + 1, o.parent
            return n
        order = {(e.object.name if e.object else "", e.bone_name): i
                 for i, e in enumerate(state.settings(context.scene).paths)}
        return sorted(caches, key=lambda c: (depth(c), order.get(c.key[:2], 0)))

    def _apply(self, context):
        for i, c in enumerate(self.caches):
            d = self.amount * i
            if abs(d) < 1e-9:
                continue
            sel = [k.t for k in c.keys if k.sel]
            move_keys_in_time(c, {t: t + d for t in sel}, sel)


class EMP_OT_moving_hold(bpy.types.Operator):
    """Moving hold: copy the selected key's pose a few frames later and let it drift a little in """ \
        """the direction it was moving, so the hold doesn't look frozen. Wheel: how long, """ \
        """mouse left / right: how far it drifts"""
    bl_idname = "emp.moving_hold"
    bl_label = "Moving Hold"
    bl_options = {'REGISTER', 'UNDO', 'BLOCKING'}

    frames: FloatProperty(name="Frames", default=0.0, min=0.0,
                          description="Length of the hold (0: halfway to the next key)")
    drift: FloatProperty(name="Drift", default=0.15, min=0.0, soft_max=0.6, subtype='FACTOR',
                         description="How far it keeps drifting, as part of the distance it covered in "
                                     "the same number of frames before the key")

    @staticmethod
    def _source(c):
        return (space.active_key([c]) or (None, next(k for k in c.keys if k.sel)))[1]

    @staticmethod
    def _auto_frames(c, k):
        after = [kk.t for kk in c.keys if kk.t > k.t + 1e-3]
        return max(1.0, float(round((after[0] - k.t) / 2.0))) if after else 6.0

    def invoke(self, context, event):
        caches = _selected_caches(context)
        if not caches:
            return {'PASS_THROUGH'}
        c = caches[0]
        if not self.frames:
            self.frames = self._auto_frames(c, self._source(c))
        self.drift0 = self.drift
        self.caches = caches
        self.snaps = [(c, pose_snapshot(c), c.fs, list(c.B)) for c in caches]
        self.x0 = event.mouse_x
        self.area = context.area
        state.modal_running += 1
        state.force_numbers = True
        context.window_manager.modal_handler_add(self)
        self._update(context, event)
        return {'RUNNING_MODAL'}

    def _restore(self):
        for c, s, fs, B in self.snaps:
            s.restore()
            c.fs, c.B = fs, list(B)
            c.dirty = True
            c.refresh(force=True)

    def _update(self, context, event):
        self.drift = max(0.0, self.drift0 + (event.mouse_x - self.x0) / (400.0 * compat.ui_scale()))
        self._restore()
        ok = self._apply(context)
        txt = "Moving Hold: %d frame%s, drift %.2f%s" % (
            self.frames, "" if self.frames == 1 else "s", self.drift, "" if ok else "  (no room after the key)")
        state.hud = (txt, (event.mouse_region_x, event.mouse_region_y))
        if self.area is not None:
            self.area.header_text_set(txt + "   wheel / PgUp PgDn: frames   mouse left / right: drift   "
                                            "click: confirm   Esc: cancel")
        pick.tag_anim_editors()
        state.tag_redraw()

    def modal(self, context, event):
        t, v = event.type, event.value
        if t in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            self._update(context, event)
        elif t in {'WHEELUPMOUSE', 'PAGE_UP', 'NUMPAD_PLUS'} and v == 'PRESS':
            self.frames += 1.0
            self._update(context, event)
        elif t in {'WHEELDOWNMOUSE', 'PAGE_DOWN', 'NUMPAD_MINUS'} and v == 'PRESS':
            self.frames = max(1.0, self.frames - 1.0)
            self._update(context, event)
        elif t in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER', 'SPACE'} and v == 'PRESS':
            self._finish(context)
            state.schedule_rebake(0.3)
            return {'FINISHED'}
        elif t in {'RIGHTMOUSE', 'ESC'} and v == 'PRESS':
            self._restore()
            self._finish(context)
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}

    def _finish(self, context):
        state.modal_running = max(0, state.modal_running - 1)
        state.force_numbers = False
        state.hud = None
        if self.area is not None:
            self.area.header_text_set(None)
        pick.tag_anim_editors()
        state.tag_redraw()

    def execute(self, context):
        if not self._apply(context):
            self.report({'WARNING'}, "Select a key with room after it")
            return {'CANCELLED'}
        pick.tag_anim_editors()
        state.schedule_rebake(0.3)
        return {'FINISHED'}

    def _apply(self, context):
        from . import edit
        from .ops_keys import copy_keys_to
        done = 0
        for c in _selected_caches(context):
            k = self._source(c)
            after = [kk.t for kk in c.keys if kk.t > k.t + 1e-3]
            n = self.frames or self._auto_frames(c, k)
            t_new = k.t + n
            if after and t_new >= after[0] - 0.5:
                continue
            tg = c.target()
            prev = c.point(tg, k.t - n)
            drift = (k.co - prev) * self.drift
            for fc in _pose_fcurves(tg, c, True):
                copy_keys_to(fc, [k.t], n)
            c.extend_to(t_new)
            c.refresh(force=True)
            tg, fresh = edit.prepare(context, c, [t_new])
            w = edit.Writer([t_new])
            for kk in fresh:
                w.key(c, tg, kk, kk.co + drift)
            w.done([c])
            done += 1
        return done


class EMP_OT_snap_frames(bpy.types.Operator):
    """Put the selected keys (all keys if none are selected) on whole frames"""
    bl_idname = "emp.snap_frames"
    bl_label = "Snap to Frames"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        n = 0
        for c in state.editable_caches(context.scene):
            ks = [k for k in c.keys if k.sel] or c.visible_keys()
            taken = {round(k.t) for k in c.keys if abs(k.t - round(k.t)) < 1e-3}
            mapping = {}
            for k in ks:
                r = float(round(k.t))
                if abs(k.t - r) > 1e-3 and r not in taken:
                    mapping[k.t] = r
                    taken.add(r)
            if mapping:
                move_keys_in_time(c, mapping, [k.t for k in c.keys if k.sel])
                n += len(mapping)
        self.report({'INFO'}, "%d key%s moved to whole frames" % (n, "" if n == 1 else "s"))
        pick.tag_anim_editors()
        return {'FINISHED'}


class EMP_MT_timing(bpy.types.Menu):
    bl_label = "Timing"
    bl_idname = "EMP_MT_timing"

    @classmethod
    def poll(cls, context):
        return bool(state.editable_caches(context.scene))

    def draw(self, context):
        layout = self.layout
        layout.operator_context = 'INVOKE_REGION_WIN'
        st = state.settings(context.scene)
        layout.operator("emp.ease", text="Ease", icon='IPO_EASE_IN_OUT')
        layout.menu("EMP_MT_space", icon='ALIGN_JUSTIFY')
        layout.operator("emp.tween", text="Breakdown", icon='IPO_SINE')
        op = layout.operator("wm.call_panel", text="Easing Presets...", icon='IPO_EASE_IN_OUT')
        op.name, op.keep_open = "EMP_PT_easing_presets", True
        layout.operator("emp.slide", text="Slide (G G)", icon='TRACKING_FORWARDS')
        layout.separator()
        layout.operator("emp.time_scale", icon='ARROW_LEFTRIGHT')
        layout.operator("emp.add_frames", icon='ADD')
        layout.operator("emp.moving_hold", icon='PAUSE')
        layout.operator("emp.offset_cascade", icon='MOD_ARRAY')
        layout.operator("emp.snap_frames", icon='SNAP_INCREMENT')
        layout.separator()
        layout.prop(st, "show_timing_chart")
        layout.prop(st, "show_speed_graph")


class EMP_OT_timing_menu(bpy.types.Operator):
    """Timing tools (Shift+T)"""
    bl_idname = "emp.timing_menu"
    bl_label = "Timing Menu"

    def invoke(self, context, event):
        if not state.editable_caches(context.scene):
            return {'PASS_THROUGH'}
        bpy.ops.wm.call_menu(name="EMP_MT_timing")
        return {'FINISHED'}


# ----------------------------------------------------------------------------
# The speed graph, dragged: the current frame, a key in time, a segment's ease
# ----------------------------------------------------------------------------

class EMP_OT_speed_drag(bpy.types.Operator):
    """Drag on the speed graph: on empty space the current frame follows; a key's line moves """ \
        """that key in time (the whole pose; Ctrl: subframes); the curve: the part of it under the """ \
        """mouse goes up (faster there) or down (slower), the rest of the segment makes up for it """ \
        """(the path keeps its shape, the keys their frames). Esc / right click: cancel"""
    bl_idname = "emp.speed_drag"
    bl_label = "Speed Graph"
    bl_options = {'REGISTER', 'UNDO', 'BLOCKING'}

    mode: EnumProperty(items=[('SCRUB', "Frame", ""), ('KEY', "Key Time", ""), ('EASE', "Ease", "")],
                       default='SCRUB')
    frame: FloatProperty(name="Frame", description="Key Time: the key moved; Ease: the segment's first key")
    frame_b: FloatProperty(name="Last Frame", description="Ease: the segment's last key")
    obj_name: StringProperty(options={'HIDDEN'})
    bone_name: StringProperty(options={'HIDDEN'})
    new_frame: FloatProperty(name="New Frame", description="Key Time: where the key goes")
    grab: FloatProperty(name="At Frame", description="Ease: where the curve was grabbed")
    amount: FloatProperty(name="Speed Change", default=0.0,
                          description="Ease: how much faster (or slower, below 0) the motion is where the curve "
                                      "was grabbed, in the graph's units (distance per frame)")

    def _cache(self, context):
        for c in state.editable_caches(context.scene):
            if c.obj_name == self.obj_name and c.bone_name == self.bone_name:
                return c
        return None

    def _setup(self, context):
        c = self._cache(context)
        if c is None:
            return False
        self.c = c
        tg = c.target()
        self.sel = {round(k.t, 3) for k in c.keys if k.sel}
        if self.mode == 'KEY':
            ks = [k.t for k in c.keys]
            if not any(abs(t - self.frame) < 1e-3 for t in ks):
                return False
            self.prev = max((t for t in ks if t < self.frame - 1e-3), default=None)
            self.next = min((t for t in ks if t > self.frame + 1e-3), default=None)
            self.snapshot = pose_snapshot(c)
        elif self.mode == 'EASE':
            self.snapshot = core.FCurveSnapshot(tg, tg.channels(c.ptype))
            self.job = SpaceJob(c, tg, float(self.frame), float(self.frame_b))
            if self.job.arc[-1] < 1e-9:
                return False        # a hold: nothing to ease
        return True

    def _apply(self):
        c = self.c
        if self.mode == 'KEY':
            self.snapshot.restore()
            c.refresh(force=True)
            if abs(self.new_frame - self.frame) > 1e-6:
                move_keys_in_time(c, {self.frame: self.new_frame}, select=self.sel)
        elif self.mode == 'EASE':
            self.snapshot.restore()
            c.refresh(force=True)
            tg = c.target()
            if abs(self.amount) > 1e-9:
                u0 = (self.grab - self.frame) / max(self.frame_b - self.frame, 1e-6)
                # (Select New Keys: the segment's keys and the new ones are selected, so the
                # graph keeps its frames)
                from .props import prefs
                p = prefs()
                refit(self.job, tg, bump_target(self.job, tg, u0, self.amount),
                      True if p is None or p.speed_select_new else self.sel)
            c.refresh(force=True)

    def execute(self, context):
        if self.mode == 'SCRUB' or not self._setup(context):
            return {'CANCELLED'}
        self._apply()
        pick.tag_anim_editors()
        return {'FINISHED'}

    def invoke(self, context, event):
        sg = state.speed_graph
        if sg is None or not self._setup(context):
            return {'CANCELLED'}
        self.area = context.area
        self.sg = dict(sg)
        self.mx0, self.my0 = event.mouse_region_x, event.mouse_region_y
        self.frame0 = context.scene.frame_current
        self.new_frame = self.frame
        self.amount = 0.0
        # the graph keeps its frames and its height while it is dragged
        state.speed_hold = {"cache": self.c.key, "lo": sg["lo"], "hi": sg["hi"], "vmax": sg["vmax"]}
        state.modal_running += 1
        context.window_manager.modal_handler_add(self)
        self._move(context, event)
        return {'RUNNING_MODAL'}

    def _frame_at(self, mx):
        sg = self.sg
        return sg["t0"] + (sg["t1"] - sg["t0"]) * (mx - sg["x0"]) / max(sg["x1"] - sg["x0"], 1.0)

    def _move(self, context, event):
        mx, my = event.mouse_region_x, event.mouse_region_y
        if self.mode == 'SCRUB':
            f = int(round(min(max(self._frame_at(mx), self.sg["lo"]), self.sg["hi"])))
            if f != context.scene.frame_current:
                context.scene.frame_set(f)
            return
        if self.mode == 'KEY':
            t = self.frame + self._frame_at(mx) - self._frame_at(self.mx0)
            t = t if event.ctrl else float(round(t))
            gap = 0.05 if event.ctrl else 1.0
            if self.prev is not None:
                t = max(t, self.prev + gap)
            if self.next is not None:
                t = min(t, self.next - gap)
            self.new_frame = t
            text = "Key %g -> %g   (Ctrl: subframes)" % (self.frame, t)
        else:
            sg = self.sg
            self.amount = (my - self.my0) / max(sg["gh"], 1.0) * sg["vmax"] * 1.2
            text = "Speed at frame %.1f: %+.4f   up: faster there, down: slower" % (self.grab, self.amount)
        self._apply()
        self.area.header_text_set(text + "   click / release: confirm   Esc: cancel")
        pick.tag_anim_editors()
        state.tag_redraw()

    def modal(self, context, event):
        t, v = event.type, event.value
        if t in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            self._move(context, event)
        elif (t == 'LEFTMOUSE' and v == 'RELEASE') or (t in {'RET', 'SPACE'} and v == 'PRESS'):
            return self._end(context, True)
        elif t in {'RIGHTMOUSE', 'ESC'} and v == 'PRESS':
            return self._end(context, False)
        return {'RUNNING_MODAL'}

    def _end(self, context, ok):
        state.modal_running = max(0, state.modal_running - 1)
        state.speed_hold = None
        self.area.header_text_set(None)
        if self.mode == 'SCRUB':
            if not ok:
                context.scene.frame_set(self.frame0)
            state.tag_redraw()
            return {'CANCELLED'}        # (a frame change isn't an undo step, like the timeline)
        if not ok:
            self.snapshot.restore()
            self.c.dirty = True
            self.c.refresh(force=True)
        else:
            from .props import prefs
            p = prefs()
            keep = (p is None or p.speed_keep_ends) and state.settings(context.scene).speed_range == 'SEGMENT'
            moved = self.new_frame if self.mode == 'KEY' else None
            want = set()
            if keep:
                # Keep the Graph's Keys: its ends (one key selected: its neighbors) get selected,
                # so the graph keeps showing the same keys
                want = {round(moved if moved is not None and abs(t - self.frame) < 1e-3 else t, 3)
                        for t in (self.sg["lo"], self.sg["hi"])}
            if moved is not None and round(self.frame, 3) in self.sel:
                want.add(round(moved, 3))           # (the moved key stays selected)
            self.c.refresh(force=True)
            for k in self.c.keys:
                if round(k.t, 3) in want:
                    pick.select_part(self.c, k, 'KEY', True)
        pick.tag_anim_editors()
        state.tag_redraw()
        return {'FINISHED'} if ok else {'CANCELLED'}


def bump_target(job, tg, u0, dv):
    """Target motion of one segment: the same path, its speed changed by dv around u0 (0..1 of
    the segment, a smooth bump), the rest of the segment slower / faster so it still takes the
    same frames."""
    cache = job.cache
    r0, r1 = job.r0, job.r1
    total = job.arc[-1]
    n = 200
    dt = (r1 - r0) / n
    s0 = [job._at(job.arc, r0 + dt * i) for i in range(n + 1)]
    v0 = [max(b - a, 0.0) for a, b in zip(s0, s0[1:])]
    sig = 0.16
    v = []
    for i, a in enumerate(v0):
        k = math.exp(-0.5 * (((i + 0.5) / n - u0) / sig) ** 2)
        v.append(max(a + dv * dt * k, a * 0.05))
    scale = total / (sum(v) or 1.0)
    S = [0.0]
    for x in v:
        S.append(S[-1] + x * scale)

    def fn(t):
        u = min(max((t - r0) / dt, 0.0), float(n))
        i = min(int(u), n - 1)
        s = S[i] + (S[i + 1] - S[i]) * (u - i)
        tt = job._time_at_arc(s)
        return cache.solve(tg, t, job._at(job.pts, tt), job._at(job.vals, tt))
    return fn


class EMP_OT_graph_hover(bpy.types.Operator):
    """(What the mouse is on in the speed graph is lit up)"""
    bl_idname = "emp.graph_hover"
    bl_label = "Speed Graph Hover"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        from . import draw
        hit = draw.speed_hit(context.area, event.mouse_region_x, event.mouse_region_y)
        sg = state.speed_graph
        hot = (sg["cache"], hit[0], hit[1]) if hit is not None and hit[0] != 'SCRUB' else None
        if hot != state.speed_hot:
            state.speed_hot = hot
            if context.area is not None:
                context.area.tag_redraw()
        return {'PASS_THROUGH'}


classes = (EMP_OT_drag_frame, EMP_OT_time_scale, EMP_OT_add_frames, EMP_OT_tween,
           EMP_OT_offset_cascade, EMP_OT_moving_hold, EMP_OT_snap_frames, EMP_MT_timing,
           EMP_OT_timing_menu, EMP_OT_speed_drag, EMP_OT_graph_hover)
