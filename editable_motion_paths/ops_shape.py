"""Shape tools on path keys: 3D cursor snapping (Shift+S), mirror (Ctrl+M), bevel (Ctrl+B),
spin and the LoopTools set (Circle, Flatten, Straighten, GStretch)."""

import math

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty
from mathutils import Matrix, Vector

from . import compat, core, edit, geom, keys, pick, space, state


def _keys_selected(context):
    return any(k.sel for c in state.editable_caches(context.scene) for k in c.keys)


# ----------------------------------------------------------------------------
# 3D cursor
# ----------------------------------------------------------------------------

class EMP_OT_cursor_to_selected(bpy.types.Operator):
    """Put the 3D cursor on the selected path keys (their median)"""
    bl_idname = "emp.cursor_to_selected"
    bl_label = "Cursor to Selected Keys"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _keys_selected(context)

    def execute(self, context):
        pts = [k.co for _c, ks in edit.selected(context) for k in ks]
        context.scene.cursor.location = space.median(pts)
        state.tag_redraw()
        return {'FINISHED'}


class EMP_OT_cursor_to_active(bpy.types.Operator):
    """Put the 3D cursor on the active (last clicked) path key"""
    bl_idname = "emp.cursor_to_active"
    bl_label = "Cursor to Active Key"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        a = space.active_key(state.editable_caches(context.scene))
        if a is None:
            self.report({'WARNING'}, "No active key: click a key first")
            return {'CANCELLED'}
        context.scene.cursor.location = a[1].co
        state.tag_redraw()
        return {'FINISHED'}


class _MoveSelected:
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _keys_selected(context)

    def targets(self, context, pts):
        """World target per selected key position (same order)."""
        raise NotImplementedError

    def execute(self, context):
        groups = edit.selected(context)
        if not groups:
            return {'CANCELLED'}
        pts = [k.co.copy() for _c, ks in groups for k in ks]
        tgts = self.targets(context, pts)
        if tgts is None:
            return {'CANCELLED'}
        frames = [k.t for _c, ks in groups for k in ks]
        w = edit.Writer(frames)
        i = 0
        for cache, ks in groups:
            mine = tgts[i:i + len(ks)]
            i += len(ks)
            tg, fresh = edit.prepare(context, cache, [k.t for k in ks])
            by_t = {round(k.t, 3): W for k, W in zip(ks, mine)}
            for k in fresh:
                W = by_t.get(round(k.t, 3))
                if W is not None:
                    w.key(cache, tg, k, W)
        w.done([c for c, _ks in groups])
        return {'FINISHED'}


class EMP_OT_selection_to_cursor(_MoveSelected, bpy.types.Operator):
    """Move the selected path keys to the 3D cursor (rotation paths: aim at it)"""
    bl_idname = "emp.selection_to_cursor"
    bl_label = "Selection to Cursor"

    use_offset: BoolProperty(name="Keep Offset", default=False,
                             description="Move the keys together, their median onto the cursor")

    def targets(self, context, pts):
        cur = context.scene.cursor.location.copy()
        if self.use_offset:
            d = cur - space.median(pts)
            return [p + d for p in pts]
        return [cur.copy() for _p in pts]


class EMP_OT_selection_to_active(_MoveSelected, bpy.types.Operator):
    """Move the selected path keys onto the active (last clicked) key"""
    bl_idname = "emp.selection_to_active"
    bl_label = "Selection to Active Key"

    def targets(self, context, pts):
        a = space.active_key(state.editable_caches(context.scene))
        if a is None:
            self.report({'WARNING'}, "No active key: click a key first")
            return None
        return [a[1].co.copy() for _p in pts]


class EMP_OT_selection_to_grid(_MoveSelected, bpy.types.Operator):
    """Snap the selected path keys to the nearest grid point"""
    bl_idname = "emp.selection_to_grid"
    bl_label = "Selection to Grid"

    def targets(self, context, pts):
        g = 1.0
        sd = context.space_data
        if sd is not None and sd.type == 'VIEW_3D':
            g = sd.overlay.grid_scale or 1.0
        return [Vector([round(x / g) * g for x in p]) for p in pts]


class EMP_MT_snap_pie(bpy.types.Menu):
    bl_label = "Snap Path Keys"
    bl_idname = "EMP_MT_snap_pie"

    def draw(self, context):
        pie = self.layout.menu_pie()
        pie.operator("view3d.snap_cursor_to_grid", text="Cursor to Grid", icon='CURSOR')
        pie.operator("emp.selection_to_grid", text="Selection to Grid", icon='RESTRICT_SELECT_OFF')
        pie.operator("emp.cursor_to_selected", text="Cursor to Selected", icon='CURSOR')
        pie.operator("emp.selection_to_cursor", text="Selection to Cursor",
                     icon='RESTRICT_SELECT_OFF').use_offset = False
        pie.operator("emp.selection_to_cursor", text="Selection to Cursor (Keep Offset)",
                     icon='RESTRICT_SELECT_OFF').use_offset = True
        pie.operator("emp.selection_to_active", text="Selection to Active", icon='RESTRICT_SELECT_OFF')
        pie.operator("view3d.snap_cursor_to_center", text="Cursor to World Origin", icon='CURSOR')
        pie.operator("emp.cursor_to_active", text="Cursor to Active", icon='CURSOR')


class EMP_OT_snap_pie(bpy.types.Operator):
    """Snap menu for path keys (Blender's snap menu when no key is selected)"""
    bl_idname = "emp.snap_pie"
    bl_label = "Snap Path Keys"

    def invoke(self, context, event):
        if not _keys_selected(context):
            return {'PASS_THROUGH'}
        bpy.ops.wm.call_menu_pie(name="EMP_MT_snap_pie")
        return {'FINISHED'}


# ----------------------------------------------------------------------------
# Mirror
# ----------------------------------------------------------------------------

class EMP_OT_mirror(bpy.types.Operator):
    """Mirror the selected path keys around the pivot point (press X, Y or Z)"""
    bl_idname = "emp.mirror"
    bl_label = "Mirror Path Keys"
    bl_options = {'REGISTER', 'UNDO'}

    axis: EnumProperty(name="Axis", items=[('X', "X", ""), ('Y', "Y", ""), ('Z', "Z", "")], default='X')
    orient: EnumProperty(name="Orientation", items=space.ORIENTATIONS, default='GLOBAL')

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == 'VIEW_3D'

    def execute(self, context):
        v = [1.0, 1.0, 1.0]
        v["XYZ".index(self.axis)] = -1.0
        props = dict(mode='SCALE', value=v, orient=self.orient)
        if self.orient not in {'GLOBAL', 'LOCAL'}:
            groups = edit.selected(context)
            if groups:
                c, ks = groups[0]
                M = space.orientation_matrix(context, self.orient, c, ks[0])
                props["orient_matrix"] = [x for row in M for x in row]
        return bpy.ops.emp.transform('EXEC_DEFAULT', **props)

    def _restore(self):
        for sn in reversed(self.snaps):
            sn.restore()
        for c in state.caches.values():
            c.dirty = True
            c.refresh()

    def invoke(self, context, event):
        if not _keys_selected(context):
            return {'PASS_THROUGH'}
        self.orient = 'GLOBAL'
        self.presses = {}
        self.snaps = [core.FCurveSnapshot(c.target(), c.target().channels(c.ptype))
                      for c in state.editable_caches(context.scene)]
        context.area.header_text_set("Mirror: press X, Y or Z (twice: other orientation), "
                                     "Esc / RMB: cancel")
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.value != 'PRESS':
            return {'RUNNING_MODAL'}
        if event.type in {'X', 'Y', 'Z'}:
            header = space.orientation_type(context)
            n = self.presses.get(event.type, 0) + 1
            self.presses = {event.type: n}
            self.axis = event.type
            self.orient = header if n % 2 else ('LOCAL' if header == 'GLOBAL' else 'GLOBAL')
            context.area.header_text_set("Mirror along %s (%s): Enter to confirm, %s again to switch "
                                         "orientation" % (self.axis, self.orient.title(), self.axis))
            self._restore()
            self.execute(context)      # preview
            return {'RUNNING_MODAL'}
        if event.type in {'RET', 'NUMPAD_ENTER', 'SPACE', 'LEFTMOUSE'} and self.presses:
            context.area.header_text_set(None)
            return {'FINISHED'}
        if event.type in {'ESC', 'RIGHTMOUSE'}:
            context.area.header_text_set(None)
            self._restore()
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}


# ----------------------------------------------------------------------------
# Bevel
# ----------------------------------------------------------------------------

def _tangent(cache, tg, t):
    a = cache.point(tg, t - 0.25)
    b = cache.point(tg, t + 0.25)
    d = b - a
    return d.normalized() if d.length > 1e-9 else None


def _key_at(cache, t):
    cache.refresh(force=True)
    return next((k for k in cache.keys if abs(k.t - t) < 1e-3), None)


def _insert_plain(cache, tg, frames):
    """Keys on every keyed channel at `frames` (their values are set right after)."""
    ch = tg.channels(cache.ptype)
    for fc in ch.fcurves:
        if fc is None or not len(fc.keyframe_points):
            continue
        for t in frames:
            if not any(abs(kp.co[0] - t) < 1e-4 for kp in fc.keyframe_points):
                fc.keyframe_points.insert(t, fc.evaluate(t), options={'FAST'})
        fc.update()
    core.ensure_keys(tg, cache.ptype, frames)


def bevel_key(cache, t, width, segments, profile, whole=True):
    """Replace the key at frame t by segments+1 keys rounding the corner. Returns new frames."""
    cache.refresh(force=True)
    ks = cache.keys
    i = next((j for j, k in enumerate(ks) if abs(k.t - t) < 1e-3), None)
    if i is None or i == 0 or i == len(ks) - 1:
        return []
    k = ks[i]
    room = min(k.t - ks[i - 1].t, ks[i + 1].t - k.t)
    w = min(width, room - (1.0 if whole else 0.05))
    if whole:
        w = float(int(w))
    if w < (1.0 if whole else 0.05):
        return []
    ta, tb = k.t - w, k.t + w
    times = [ta + (tb - ta) * j / segments for j in range(segments + 1)]
    if whole:
        times = sorted({float(round(x)) for x in times})
    segs = len(times) - 1
    tg = cache.target()
    A, B = cache.point(tg, ta), cache.point(tg, tb)
    TA, TB = _tangent(cache, tg, ta), _tangent(cache, tg, tb)
    corner = geom.line_corner(A, TA, B, TB) if TA is not None and TB is not None else None
    if corner is None or (corner - k.co).length > 2.0 * (B - A).length + 1e-9:
        corner = k.co.copy()
    pts = geom.bevel_points(A, corner, B, max(1, segs), profile)
    if segs != segments:
        # whole frames merged some steps: take points evenly along the profile again
        arc = geom.cumulative(pts)
        pts = [geom.point_on_polyline(pts, arc, arc[-1] * j / segs) for j in range(segs + 1)]
    # the two outer keys keep the curve outside exactly; everything between them goes
    keys.insert_path_keys(cache, tg, [ta, tb], exact=True)
    cache.refresh(force=True)
    tg = cache.target()
    ch = tg.channels(cache.ptype)
    inner_old = [kk for kk in cache.keys if ta + 1e-3 < kk.t < tb - 1e-3]
    for c, fc in enumerate(ch.fcurves):
        if fc is not None:
            keys.delete_channel(fc, [fc.keyframe_points[kk.idx[c]].co[0] for kk in inner_old if kk.idx[c] >= 0])
    inner = times[1:-1]
    _insert_plain(cache, tg, inner)
    cache.refresh(force=True)
    tg = cache.target()
    ch = tg.channels(cache.ptype)
    w_ = edit.Writer(times)
    for tt, p in zip(inner, pts[1:-1]):
        kk = _key_at(cache, tt)
        if kk is not None:
            w_.key(cache, tg, kk, p, move_handles=False)
            core.set_handle_types(ch, kk, 'AUTO_CLAMPED', 'AUTO_CLAMPED', w_.touched)
    # the rounded part is always a smooth Bezier, whatever the corner was
    for fc in ch.fcurves:
        if fc is None:
            continue
        for kp in fc.keyframe_points:
            if ta - 1e-3 <= kp.co[0] < tb - 1e-3:
                kp.interpolation = 'BEZIER'
        w_.touched.add(fc)
    w_.done([cache])
    # the outer keys go into the profile in the path's own direction: the inner handle gets
    # the same slope as the outer one on every channel (smooth, and Aligned stays aligned)
    tg = cache.target()
    ch = tg.channels(cache.ptype)
    touched = set()
    for side, tt in (('R', ta), ('L', tb)):
        kk = _key_at(cache, tt)
        if kk is None:
            continue
        dt = abs(times[1] - times[0]) / 3.0 if side == 'R' else abs(times[-1] - times[-2]) / 3.0
        core.write_handle(ch, kk, side, kk.s_l if side == 'R' else kk.s_r, dt, kk.vals, None, touched)
    core.update_fcurves(touched)
    cache.dirty = True
    return times


class EMP_OT_bevel(bpy.types.Operator):
    """Round the selected keys: each one becomes a smooth arc of new keys (Ctrl+B). """ \
        """A: width, S or wheel: segments, P: profile"""
    bl_idname = "emp.bevel"
    bl_label = "Bevel Path Keys"
    bl_options = {'REGISTER', 'UNDO'}

    width: FloatProperty(name="Width", default=3.0, min=0.0, soft_max=30.0,
                         description="Frames on each side of the key")
    segments: IntProperty(name="Segments", default=2, min=1, soft_max=12, max=50)
    profile: FloatProperty(name="Profile", default=0.5, min=0.0, max=1.0, subtype='FACTOR',
                           description="0.5 is a circular arc, lower is flatter / concave, higher "
                                       "keeps more of the corner")
    whole_frames: BoolProperty(name="Whole Frames", default=True)

    def _apply(self, context):
        made = []
        for c in state.editable_caches(context.scene):
            ts = sorted((k.t for k in c.keys[1:-1] if k.sel), reverse=True)
            new = []
            for t in ts:
                new += bevel_key(c, t, self.width, self.segments, self.profile, self.whole_frames)
            if new:
                pick.deselect_all([c])
                c.refresh(force=True)
                want = {round(x, 3) for x in new}
                for k in c.keys:
                    if round(k.t, 3) in want:
                        pick.select_part(c, k, 'KEY', True)
                c.refresh(force=True)
                made.append(c)
        return made

    def execute(self, context):
        if not self._apply(context):
            self.report({'WARNING'}, "Select keys between two other keys to bevel")
            return {'CANCELLED'}
        pick.tag_anim_editors()
        return {'FINISHED'}

    def invoke(self, context, event):
        from bpy_extras.view3d_utils import location_3d_to_region_2d
        caches = [c for c in state.editable_caches(context.scene) if any(k.sel for k in c.keys[1:-1])]
        if not caches or context.region_data is None:
            return {'PASS_THROUGH'}
        region, rv3d = context.region, context.region_data
        c = caches[0]
        k = next(k for k in c.keys[1:-1] if k.sel)
        tg = c.target()
        s0 = location_3d_to_region_2d(region, rv3d, k.co)
        sa = location_3d_to_region_2d(region, rv3d, c.point(tg, k.t - 1.0))
        sb = location_3d_to_region_2d(region, rv3d, c.point(tg, k.t + 1.0))
        self.center = s0 or Vector((event.mouse_region_x, event.mouse_region_y))
        self.ppf = max(3.0, ((sb - sa).length * 0.5) if sa is not None and sb is not None else 10.0)
        m = Vector((event.mouse_region_x, event.mouse_region_y))
        self.d0 = (m - self.center).length
        self.x0 = m.x
        self.adjust = 'WIDTH'
        self.snaps = [core.FCurveSnapshot(cc.target(), cc.target().channels(cc.ptype)) for cc in caches]
        self.width = 0.0
        state.modal_running += 1
        context.window_manager.modal_handler_add(self)
        self._update(context, event)
        return {'RUNNING_MODAL'}

    def _restore(self):
        for s in reversed(self.snaps):
            s.restore()
        for c in state.caches.values():
            c.dirty = True
            c.refresh()

    def _update(self, context, event):
        m = Vector((event.mouse_region_x, event.mouse_region_y))
        us = compat.ui_scale()
        if self.adjust == 'PROFILE':
            self.profile = min(1.0, max(0.0, self.p0 + (m.x - self.x0) / (300.0 * us)))
        elif self.adjust == 'SEGMENTS':
            self.segments = max(1, self.s0 + int((m.x - self.x0) / (25.0 * us)))
        else:
            self.width = max(0.0, ((m - self.center).length - self.d0) / self.ppf)
        self._restore()
        if self.width >= 1.0 or (not self.whole_frames and self.width > 0.05):
            self._apply(context)
        context.area.header_text_set(
            "Bevel: width %.1f frames, %d segment%s, profile %.2f%s   mouse: %s   "
            "A: width   S / wheel: segments   P: profile   click: confirm   Esc: cancel" % (
                self.width, self.segments, "" if self.segments == 1 else "s", self.profile,
                " (circle)" if abs(self.profile - 0.5) < 0.01 else "", self.adjust.lower()))
        pick.tag_anim_editors()

    def modal(self, context, event):
        t, v = event.type, event.value
        if t in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            self._update(context, event)
        elif t in {'WHEELUPMOUSE', 'PAGE_UP', 'NUMPAD_PLUS'} and v == 'PRESS':
            self.segments += 1
            self._update(context, event)
        elif t in {'WHEELDOWNMOUSE', 'PAGE_DOWN', 'NUMPAD_MINUS'} and v == 'PRESS':
            self.segments = max(1, self.segments - 1)
            self._update(context, event)
        elif t in {'A', 'S', 'P'} and v == 'PRESS':
            # like mesh Bevel: A width, S segments, P profile (pressed again: back to width)
            new = {'A': 'WIDTH', 'S': 'SEGMENTS', 'P': 'PROFILE'}[t]
            self.adjust = 'WIDTH' if new == self.adjust else new
            self.x0 = event.mouse_region_x
            self.p0 = self.profile
            self.s0 = self.segments
            if self.adjust == 'WIDTH':
                # keep the width: measure the mouse from here on
                m = Vector((event.mouse_region_x, event.mouse_region_y))
                self.d0 = (m - self.center).length - self.width * self.ppf
            self._update(context, event)
        elif t in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER', 'SPACE'} and v == 'PRESS':
            return self._end(context, True)
        elif t in {'RIGHTMOUSE', 'ESC'} and v == 'PRESS':
            return self._end(context, False)
        return {'RUNNING_MODAL'}

    def _end(self, context, ok):
        state.modal_running = max(0, state.modal_running - 1)
        context.area.header_text_set(None)
        if not ok or self.width < 1.0 and self.whole_frames:
            self._restore()
            state.tag_redraw()
            return {'CANCELLED'}
        state.tag_redraw()
        return {'FINISHED'}


# ----------------------------------------------------------------------------
# Spin
# ----------------------------------------------------------------------------

class EMP_OT_spin(bpy.types.Operator):
    """Continue the motion around the 3D cursor: new keys on an arc after the selected end """ \
        """key (orbits, swings). Mouse: angle, wheel: number of keys"""
    bl_idname = "emp.spin"
    bl_label = "Spin Path"
    bl_options = {'REGISTER', 'UNDO'}

    angle: FloatProperty(name="Angle", subtype='ANGLE', default=math.radians(90.0))
    steps: IntProperty(name="Steps", default=4, min=1, soft_max=32)
    frames: IntProperty(name="Frames per Step", default=4, min=1, soft_max=50)
    center: FloatVectorProperty(name="Center", subtype='XYZ', size=3)
    axis: FloatVectorProperty(name="Axis", subtype='DIRECTION', size=3, default=(0.0, 0.0, 1.0))

    def _source(self, context):
        out = []
        for c in state.editable_caches(context.scene):
            ks = c.keys
            sel = [i for i, k in enumerate(ks) if k.sel]
            if not sel:
                continue
            tg = c.target()
            if any(core.cycle_info(fc) for fc in tg.channels(c.ptype).fcurves):
                continue
            if sel[-1] == len(ks) - 1 or len(ks) == 1:
                out.append((c, ks[-1].t, 1.0))
            elif sel[0] == 0:
                out.append((c, ks[0].t, -1.0))
        return out

    def _apply(self, context):
        made = 0
        ax = Vector(self.axis)
        if ax.length < 1e-9:
            return 0
        ax.normalize()
        cen = Vector(self.center)
        for c, t0, sign in self._source(context):
            k0 = _key_at(c, t0)
            p0 = k0.co.copy()
            times = [t0 + sign * self.frames * i for i in range(1, self.steps + 1)]
            tg = c.target()
            _insert_plain(c, tg, times)
            c.extend_to(times[-1])
            c.refresh(force=True)
            tg = c.target()
            w = edit.Writer(times)
            for i, tt in enumerate(times, 1):
                R = Matrix.Rotation(sign * self.angle * i / self.steps, 3, ax)
                kk = _key_at(c, tt)
                if kk is not None:
                    w.key(c, tg, kk, cen + R @ (p0 - cen), move_handles=False)
                    core.set_handle_types(tg.channels(c.ptype), kk, 'AUTO_CLAMPED', 'AUTO_CLAMPED', w.touched)
            w.done([c])
            pick.deselect_all([c])
            c.refresh(force=True)
            for kk in c.keys:
                if any(abs(kk.t - tt) < 1e-3 for tt in times):
                    pick.select_part(c, kk, 'KEY', True)
            made += 1
        return made

    def execute(self, context):
        if not self._apply(context):
            self.report({'WARNING'}, "Select the first or last key of a path (not a looping one)")
            return {'CANCELLED'}
        state.schedule_rebake(0.2, force=True)
        pick.tag_anim_editors()
        return {'FINISHED'}

    def invoke(self, context, event):
        from bpy_extras.view3d_utils import location_3d_to_region_2d
        src = self._source(context)
        if not src or context.region_data is None:
            return {'PASS_THROUGH'}
        rv3d = context.region_data
        self.center = context.scene.cursor.location
        self.axis = rv3d.view_rotation @ Vector((0.0, 0.0, 1.0))
        c, t0, _s = src[0]
        i = next(j for j, k in enumerate(c.keys) if abs(k.t - t0) < 1e-3)
        j = i - 1 if i > 0 else i + 1
        if 0 <= j < len(c.keys) and j != i:
            self.frames = max(1, int(round(abs(c.keys[i].t - c.keys[j].t))))
        self.c2d = location_3d_to_region_2d(context.region, rv3d, Vector(self.center)) or \
            Vector((event.mouse_region_x, event.mouse_region_y))
        v = Vector((event.mouse_region_x, event.mouse_region_y)) - self.c2d
        self.prev = math.atan2(v.y, v.x)
        self.acc = 0.0
        self.snaps = [(cc, core.FCurveSnapshot(cc.target(), cc.target().channels(cc.ptype)), cc.fs, list(cc.B))
                      for cc, _t, _s in src]
        state.modal_running += 1
        context.window_manager.modal_handler_add(self)
        self._update(context, event)
        return {'RUNNING_MODAL'}

    def _restore(self):
        for c, s, fs, B in self.snaps:
            s.restore()
            c.fs, c.B = fs, list(B)
            c.dirty = True
        for c in state.caches.values():
            c.dirty = True
            c.refresh()

    def _update(self, context, event):
        v = Vector((event.mouse_region_x, event.mouse_region_y)) - self.c2d
        a = math.atan2(v.y, v.x)
        d = a - self.prev
        while d > math.pi:
            d -= 2 * math.pi
        while d < -math.pi:
            d += 2 * math.pi
        self.acc += d
        self.prev = a
        ang = self.acc
        if event.ctrl:
            inc = math.radians(5.0)
            ang = round(ang / inc) * inc
        self.angle = ang
        self._restore()
        if abs(self.angle) > 1e-4:
            self._apply(context)
        context.area.header_text_set("Spin around the 3D cursor: %.1f°, %d keys every %d frames   "
                                     "wheel: keys   Ctrl: 5° steps   click: confirm   Esc: cancel" % (
                                         math.degrees(self.angle), self.steps, self.frames))
        pick.tag_anim_editors()

    def modal(self, context, event):
        t, v = event.type, event.value
        if t in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            self._update(context, event)
        elif t in {'WHEELUPMOUSE', 'PAGE_UP'} and v == 'PRESS':
            self.steps += 1
            self._update(context, event)
        elif t in {'WHEELDOWNMOUSE', 'PAGE_DOWN'} and v == 'PRESS':
            self.steps = max(1, self.steps - 1)
            self._update(context, event)
        elif t in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER', 'SPACE'} and v == 'PRESS':
            state.modal_running = max(0, state.modal_running - 1)
            context.area.header_text_set(None)
            if abs(self.angle) < 1e-4:
                self._restore()
                return {'CANCELLED'}
            state.schedule_rebake(0.2, force=True)
            return {'FINISHED'}
        elif t in {'RIGHTMOUSE', 'ESC'} and v == 'PRESS':
            state.modal_running = max(0, state.modal_running - 1)
            context.area.header_text_set(None)
            self._restore()
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}


# ----------------------------------------------------------------------------
# LoopTools: Circle, Flatten, Straighten, GStretch
# ----------------------------------------------------------------------------

class _LoopTool:
    bl_options = {'REGISTER', 'UNDO'}
    min_keys = 3

    influence: FloatProperty(name="Influence", default=1.0, min=0.0, max=1.0, subtype='FACTOR')

    @classmethod
    def poll(cls, context):
        return context.mode in {'POSE', 'OBJECT'}

    def execute(self, context):
        groups = edit.selected(context, self.min_keys)
        if not groups:
            self.report({'WARNING'}, "Select at least %d keys of a path" % self.min_keys)
            return {'CANCELLED'}
        for cache, ks in groups:
            tg, fresh = edit.prepare(context, cache, [k.t for k in ks])
            fresh.sort(key=lambda k: k.t)
            res = self.shape(context, cache, tg, fresh)
            if res is False:
                return {'CANCELLED'}
        pick.tag_anim_editors()
        return {'FINISHED'}

    def _write(self, cache, tg, ks, keys_w, handles_w, end_free=True):
        """keys_w {t: W}, handles_w {(t, side): W}: lerped by the influence and written."""
        f = self.influence
        w = edit.Writer([k.t for k in ks])
        vals = {}
        for k in ks:
            W = keys_w.get(k.t)
            if W is not None:
                vals[k.t] = w.key(cache, tg, k, k.co.lerp(W, f))
        first, last = ks[0].t, ks[-1].t
        ch = tg.channels(cache.ptype)
        for k in ks:
            for side in ('L', 'R'):
                W = handles_w.get((k.t, side))
                h = k.hr if side == 'R' else k.hl
                if W is None or h is None:
                    continue
                cur = k.type_r if side == 'R' else k.type_l
                outer = (side == 'L' and k.t == first) or (side == 'R' and k.t == last)
                if outer:
                    continue
                edge = k.t in (first, last)
                htype = 'FREE' if (edge and end_free) or cur in {'FREE', 'VECTOR'} else 'ALIGNED'
                key_new = keys_w.get(k.t, k.co)
                v = (h - k.co).lerp(W - key_new, f)
                target = k.co.lerp(key_new, f) + v
                w.handle(cache, tg, k, side, target, vals.get(k.t), htype)
            if k.t not in (first, last) and k.type_l in AUTO and k.type_r in AUTO:
                core.set_handle_types(ch, k, 'ALIGNED', 'ALIGNED', w.touched)
        w.done([cache])


AUTO = {'AUTO', 'AUTO_CLAMPED'}


class EMP_OT_circle(_LoopTool, bpy.types.Operator):
    """Put the selected keys on a perfect circular arc (LoopTools Circle)"""
    bl_idname = "emp.circle"
    bl_label = "Circle"

    def shape(self, context, cache, tg, ks):
        cen, n, r = geom.best_circle([k.co for k in ks])
        pos = {}
        dirs = {}
        for k in ks:
            q = geom.project_plane(k.co, cen, n) - cen
            if q.length < 1e-9:
                q = n.orthogonal()
            dirs[k.t] = q.normalized()
            pos[k.t] = cen + dirs[k.t] * r
        hw = {}
        for i, k in enumerate(ks):
            for side, j in (('L', i - 1), ('R', i + 1)):
                if not 0 <= j < len(ks):
                    continue
                o = ks[j]
                th = dirs[k.t].angle(dirs[o.t], 0.0)
                L = 4.0 / 3.0 * math.tan(th / 4.0) * r
                T = n.cross(dirs[k.t])
                if T.dot(pos[o.t] - pos[k.t]) < 0.0:
                    T = -T
                hw[(k.t, side)] = pos[k.t] + T * L
        self._write(cache, tg, ks, pos, hw)


class EMP_OT_flatten(_LoopTool, bpy.types.Operator):
    """Put the selected keys and their handles on one plane (LoopTools Flatten)"""
    bl_idname = "emp.flatten"
    bl_label = "Flatten"

    def shape(self, context, cache, tg, ks):
        pts = [k.co for k in ks]
        cen, n = geom.best_plane(pts)
        pos = {k.t: geom.project_plane(k.co, cen, n) for k in ks}
        hw = {}
        for k in ks:
            for side, h in (('L', k.hl), ('R', k.hr)):
                if h is not None:
                    hw[(k.t, side)] = pos[k.t] + geom.project_plane(h, cen, n) - geom.project_plane(k.co, cen, n)
        self._write(cache, tg, ks, pos, hw)


class EMP_OT_straighten(_LoopTool, bpy.types.Operator):
    """Put the selected keys on the straight line between the first and the last one; """ \
        """the timing (spacing) between them stays"""
    bl_idname = "emp.straighten"
    bl_label = "Straighten"
    min_keys = 2

    def shape(self, context, cache, tg, ks):
        a, b = ks[0].co, ks[-1].co
        d = b - a
        if d.length < 1e-9:
            return
        d.normalize()
        pos = {k.t: geom.closest_on_line(k.co, a, d) for k in ks}
        hw = {}
        for k in ks:
            for side, h in (('L', k.hl), ('R', k.hr)):
                if h is not None:
                    v = (h - k.co).dot(d)
                    if abs(v) < 1e-9:
                        o = ks[min(len(ks) - 1, ks.index(k) + 1)] if side == 'R' else ks[max(0, ks.index(k) - 1)]
                        v = (pos[o.t] - pos[k.t]).dot(d) / 3.0
                    hw[(k.t, side)] = pos[k.t] + d * v
        self._write(cache, tg, ks, pos, hw)


def _annotation_stroke(context):
    gpd = getattr(context, "annotation_data", None) or getattr(context.scene, "annotation", None) \
            or getattr(context.scene, "grease_pencil", None)
    if gpd is None:
        return None, None, None
    layer = gpd.layers.active if hasattr(gpd.layers, "active") else None
    layers = [layer] if layer is not None else list(gpd.layers)
    for lay in layers:
        frames = [getattr(lay, "active_frame", None)] + list(reversed(lay.frames))
        for fr in frames:
            if fr is None or not len(fr.strokes):
                continue
            st = fr.strokes[-1]
            if len(st.points) >= 2:
                return [Vector(p.co) for p in st.points], fr, st
    return None, None, None


class EMP_OT_gstretch(_LoopTool, bpy.types.Operator):
    """Make the selected part of the path follow your last Annotate stroke (draw with D + LMB). """ \
        """Draw the motion, then press this (LoopTools GStretch)"""
    bl_idname = "emp.gstretch"
    bl_label = "Follow Stroke (GStretch)"
    min_keys = 2

    key_every: IntProperty(name="Key Every", default=4, min=0, soft_max=20,
                           description="Add keys every N frames so the path can follow the "
                                       "stroke (0: use only the existing keys)")
    delete_stroke: BoolProperty(name="Delete Stroke", default=True)

    def execute(self, context):
        pts, fr, st = _annotation_stroke(context)
        if pts is None:
            self.report({'WARNING'}, "Draw a stroke with the Annotate tool first (hold D and drag)")
            return {'CANCELLED'}
        self.stroke = pts
        groups = edit.selected(context, 2)
        if not groups:
            self.report({'WARNING'}, "Select the first and the last key of the part to redraw")
            return {'CANCELLED'}
        for cache, ks in groups:
            t0, t1 = ks[0].t, ks[-1].t
            if self.key_every > 0:
                n = int((t1 - t0) // self.key_every)
                new = [t0 + i * self.key_every for i in range(1, n + 1) if t0 + i * self.key_every < t1 - 0.5]
                keys.insert_path_keys(cache, cache.target(), new)
            cache.refresh(force=True)
            frames = [k.t for k in cache.keys if t0 - 1e-3 <= k.t <= t1 + 1e-3]
            tg, fresh = edit.prepare(context, cache, frames)
            fresh.sort(key=lambda k: k.t)
            self.shape(context, cache, tg, fresh)
        if self.delete_stroke and fr is not None:
            try:
                fr.strokes.remove(st)
            except Exception:
                pass
        pick.tag_anim_editors()
        return {'FINISHED'}

    def shape(self, context, cache, tg, ks):
        stroke = list(self.stroke)
        if (stroke[0] - ks[0].co).length > (stroke[-1] - ks[0].co).length:
            stroke.reverse()
        arc_s = geom.cumulative(stroke)
        path = [cache.point(tg, t) for t in cache.times if ks[0].t - 1e-6 <= t <= ks[-1].t + 1e-6]
        times = [t for t in cache.times if ks[0].t - 1e-6 <= t <= ks[-1].t + 1e-6]
        arc_p = geom.cumulative(path)
        total = arc_p[-1] or 1.0
        pos = {}
        for k in ks:
            i = min(range(len(times)), key=lambda j: abs(times[j] - k.t))
            pos[k.t] = geom.point_on_polyline(stroke, arc_s, arc_s[-1] * arc_p[i] / total)
        f = self.influence
        w = edit.Writer([k.t for k in ks])
        ch = tg.channels(cache.ptype)
        for k in ks:
            w.key(cache, tg, k, k.co.lerp(pos[k.t], f), move_handles=False)
            if k is not ks[0] and k is not ks[-1]:
                core.set_handle_types(ch, k, 'AUTO_CLAMPED', 'AUTO_CLAMPED', w.touched)
        w.done([cache])


classes = (EMP_OT_cursor_to_selected, EMP_OT_cursor_to_active, EMP_OT_selection_to_cursor,
           EMP_OT_selection_to_active, EMP_OT_selection_to_grid, EMP_MT_snap_pie, EMP_OT_snap_pie,
           EMP_OT_mirror, EMP_OT_bevel, EMP_OT_spin, EMP_OT_circle, EMP_OT_flatten,
           EMP_OT_straighten, EMP_OT_gstretch)
