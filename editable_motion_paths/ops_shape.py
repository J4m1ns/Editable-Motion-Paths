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
                fc.keyframe_points.insert(t, core.raw_curve_values(fc, [t])[0], options={'FAST'})
        fc.update()
    core.ensure_keys(tg, cache.ptype, frames)


def bevel_key(cache, t, width, segments, profile, whole=True, clamp=False):
    """Replace the key at frame t by segments+1 keys rounding the corner. Returns new frames.

    Each side goes as far as it can on its own: when one side stops (at the next key with
    `clamp`, else at the end of the path, going through the keys between), the other one
    goes on and the arc gets longer on that side."""
    cache.refresh(force=True)
    ks = cache.keys
    i = next((j for j, k in enumerate(ks) if abs(k.t - t) < 1e-3), None)
    if i is None or i == 0 or i == len(ks) - 1:
        return []
    k = ks[i]
    lo, hi = (ks[i - 1].t, ks[i + 1].t) if clamp else (ks[0].t, ks[-1].t)
    gap = 1.0 if whole else 0.05
    wa = min(width, k.t - lo - gap)
    wb = min(width, hi - k.t - gap)
    if whole:
        wa, wb = float(int(wa)), float(int(wb))
    if min(wa, wb) < gap:
        return []
    ta, tb = k.t - wa, k.t + wb
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
    """Round the selected keys: each one becomes a smooth arc of new keys (Ctrl+B). It goes """ \
        """through the keys around it (the path outside the arc keeps its shape and timing); """ \
        """C: Clamp at the next keys. A: width, S or wheel: segments, P: profile"""
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
    clamp: BoolProperty(name="Clamp", default=False,
                        description="Stop at the keys next to the beveled one (off: go through them; "
                                    "the path outside the arc keeps its shape and timing)")

    def _apply(self, context):
        made = []
        for c in state.editable_caches(context.scene):
            ts = sorted((k.t for k in c.keys[1:-1] if k.sel), reverse=True)
            new = []
            for t in ts:
                new += bevel_key(c, t, self.width, self.segments, self.profile, self.whole_frames, self.clamp)
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
            "A: width   S / wheel: segments   P: profile   C: clamp %s   click: confirm   Esc: cancel" % (
                self.width, self.segments, "" if self.segments == 1 else "s", self.profile,
                " (circle)" if abs(self.profile - 0.5) < 0.01 else "", self.adjust.lower(),
                "ON (stops at the next keys)" if self.clamp else "off (through the keys)"))
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
        elif t == 'C' and v == 'PRESS':
            self.clamp = not self.clamp
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
            # the keys go the way the mouse turns (also when spinning back from the first key)
            pts = [cen + Matrix.Rotation(self.angle * i / self.steps, 3, ax) @ (p0 - cen)
                   for i in range(1, self.steps + 1)]
            vals = {}
            for tt, p in zip(times, pts):
                kk = _key_at(c, tt)
                if kk is not None:
                    vals[tt] = w.key(c, tg, kk, p, move_handles=False)
            w.done([c])
            # handles of a true circular arc, so the path between the keys is round
            tg = c.target()
            r = (p0 - cen - ax * (p0 - cen).dot(ax)).length
            step = abs(self.angle) / self.steps
            L = 4.0 / 3.0 * math.tan(step / 4.0) * r
            seq = [p0] + pts
            w = edit.Writer(times)
            for i, (tt, p) in enumerate(zip(times, pts), 1):
                kk = _key_at(c, tt)
                if kk is None:
                    continue
                T = ax.cross(p - cen)
                if T.length < 1e-9:
                    continue
                T.normalize()
                ahead = seq[min(i + 1, len(seq) - 1)] - seq[i - 1]
                if T.dot(ahead) * sign < 0.0:
                    T = -T          # T points forward in time
                dt = self.frames / 3.0
                v = vals.get(tt)
                w.handle(c, tg, kk, 'R', p + T * L, v, 'ALIGNED', dt)
                w.handle(c, tg, kk, 'L', p - T * L, v, 'ALIGNED', dt)
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
    smooth_turn = 'OUTER'       # Smooth Ends: which handle turns in line (see _line_up_ends)

    influence: FloatProperty(name="Influence", default=1.0, min=0.0, max=1.0, subtype='FACTOR')
    keep_timing: BoolProperty(name="Keep Timing", default=True,
                              description="The new shape is travelled with the old spacing: the "
                                          "bone covers the same share of the way on every frame")

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
            before = self._timing_before(cache, tg, fresh)
            self._smooth_ends_of = None
            res = self.shape(context, cache, tg, fresh)
            if res is False:
                return {'CANCELLED'}
            self._timing_after(cache, before, fresh)
            if self._smooth_ends_of is not None:
                _line_up_ends(cache, *self._smooth_ends_of)
        pick.tag_anim_editors()
        return {'FINISHED'}

    def _timing_before(self, cache, tg, ks):
        if not self.keep_timing or len(ks) < 2:
            return None
        from .ops_timing import SpaceJob
        return SpaceJob(cache, tg, float(ks[0].t), float(ks[-1].t))

    def _timing_after(self, cache, before, ks):
        """Refit the new shape so it is travelled with the old spacing."""
        if before is None:
            return
        from .ops_timing import SpaceJob, progress_target, refit
        cache.refresh(force=True)
        tg = cache.target()
        after = SpaceJob(cache, tg, before.r0, before.r1)
        sel = {round(k.t, 3) for k in ks}
        core.update_fcurves(refit(after, tg, progress_target(before, after, tg), sel))
        cache.refresh(force=True)

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
                # (the ends: Free while the shape is made - Aligned to an Auto handle outside, Blender
                # would turn them; Smooth Ends lines them up at the end, see _line_up_ends. Arc heads the way the path around goes: Aligned is fine for it.)
                free_end = edge and (end_free or self.smooth_turn == 'OUTER')
                htype = 'FREE' if free_end or cur in {'FREE', 'VECTOR'} else 'ALIGNED'
                key_new = keys_w.get(k.t, k.co)
                v = (h - k.co).lerp(W - key_new, f)
                target = k.co.lerp(key_new, f) + v
                w.handle(cache, tg, k, side, target, vals.get(k.t), htype)
            if k.t not in (first, last) and k.type_l in AUTO and k.type_r in AUTO:
                core.set_handle_types(ch, k, 'ALIGNED', 'ALIGNED', w.touched)
        if not end_free:
            # (lined up last: after Keep Timing)
            self._smooth_ends_of = (ks[0].t, ks[-1].t, self.smooth_turn)
        w.done([cache])


AUTO = {'AUTO', 'AUTO_CLAMPED'}


def _line_up_ends(cache, t_first, t_last, turn='OUTER'):
    """Smooth ends: at the first and the last key the two handles are put in line, keeping their
    lengths - on every channel, so the motion goes on in the same direction there. turn 'OUTER':
    the handle facing the path around turns to the new shape (Circle: the circle stays round);
    'INNER': the new shape turns to the path around (Arc, which already heads that way)."""
    cache.refresh(force=True)
    tg = cache.target()
    ch = tg.channels(cache.ptype)
    by_t = {round(k.t, 3): k for k in cache.keys}
    touched = set()
    for t, inner in ((t_first, 'R'), (t_last, 'L')):
        k = by_t.get(round(t, 3))
        if k is None:
            continue
        for c, fc in enumerate(ch.fcurves):
            idx = k.idx[c] if c < len(k.idx) else -1
            if fc is None or idx < 0:
                continue
            kp = fc.keyframe_points[idx]
            x, y = kp.co
            h_in = kp.handle_right if inner == 'R' else kp.handle_left
            h_out = kp.handle_left if inner == 'R' else kp.handle_right
            dt_in, dt_out = abs(h_in[0] - x), abs(h_out[0] - x)
            if dt_in < 1e-9 or dt_out < 1e-9:
                continue
            kp.handle_left_type = kp.handle_right_type = 'ALIGNED'
            # the handle that turns: on the line through the key and the other one, its length kept
            src, dt = (h_in, dt_out) if turn == 'OUTER' else (h_out, dt_in)
            slope = (src[1] - y) / (src[0] - x)
            left = (inner == 'R') == (turn == 'OUTER')
            if left:
                kp.handle_left = (x - dt, y - slope * dt)
            else:
                kp.handle_right = (x + dt, y + slope * dt)
            touched.add(fc)
    core.update_fcurves(touched)
    cache.refresh(force=True)


def _turned(points, a, b, angle):
    """Points turned around the line through a and b by `angle`."""
    from mathutils import Quaternion
    axis = b - a
    if axis.length < 1e-9 or abs(angle) < 1e-9:
        return points
    q = Quaternion(axis.normalized(), angle)
    return {key: a + q @ (p - a) for key, p in points.items()}


class EMP_OT_circle(_LoopTool, bpy.types.Operator):
    """Put the selected keys on a perfect circular arc (LoopTools Circle). Angle turns the arc """ \
        """around the line between its ends (like a globe), Invert bends it the other way, Rotate """ \
        """slides the keys along the circle (the arc swings to and fro)"""
    bl_idname = "emp.circle"
    bl_label = "Circle"

    angle: FloatProperty(name="Angle", subtype='ANGLE', default=0.0, soft_min=-math.pi, soft_max=math.pi,
                         description="Turn the arc around the line between its first and last key")
    invert: BoolProperty(name="Invert", default=False,
                         description="Bend the arc the other way (concave becomes convex)")
    rotate: FloatProperty(name="Rotate", subtype='ANGLE', default=0.0, soft_min=-math.pi, soft_max=math.pi,
                          description="Turn the arc around the middle of the circle, across its plane: the "
                                      "keys slide along the circle and the arc swings to and fro")
    radius: FloatProperty(name="Radius", default=0.0, min=0.0, soft_max=10.0, unit='LENGTH',
                          description="Radius of the circle (0: fit to the keys)")
    regular: BoolProperty(name="Regular", default=False,
                          description="Spread the keys evenly around the arc")
    smooth_ends: BoolProperty(name="Smooth Ends", default=True,
                              description="The path around the arc turns to meet it in its direction at the "
                                          "first and last key, without kinks (off: the ends keep a corner)")

    def shape(self, context, cache, tg, ks):
        cen, n, r = geom.best_circle([k.co for k in ks])
        if self.radius > 0.0:
            r = self.radius
        pos = {}
        dirs = {}
        for k in ks:
            q = geom.project_plane(k.co, cen, n) - cen
            if q.length < 1e-9:
                q = n.orthogonal()
            dirs[k.t] = q.normalized()
        if self.regular and len(ks) > 2:
            # the same arc from the first to the last key, the keys at equal angles
            d0, d1 = dirs[ks[0].t], dirs[ks[-1].t]
            total = 0.0
            for a, b in zip(ks, ks[1:]):
                da, db = dirs[a.t], dirs[b.t]
                total += math.atan2(n.dot(da.cross(db)), da.dot(db))
            from mathutils import Quaternion
            for i, k in enumerate(ks):
                dirs[k.t] = Quaternion(n, total * i / (len(ks) - 1)) @ d0
        for k in ks:
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
        turn = self.angle + (math.pi if self.invert else 0.0)
        if abs(turn) > 1e-9:
            # Angle / Invert first: the arc, and its circle, turn around the line between its ends
            a, b = pos[ks[0].t].copy(), pos[ks[-1].t].copy()
            pos = _turned(pos, a, b, turn)
            hw = _turned(hw, a, b, turn)
            cen2 = _turned({0: cen}, a, b, turn)[0]
            n2 = _turned({0: cen + n}, a, b, turn)[0] - cen2
            cen, n = cen2, (n2 if n2.dot(n) >= 0.0 else -n2)   # (Rotate keeps its direction)
        if abs(self.rotate) > 1e-9:
            # then Rotate: the keys slide along that circle, around its own middle
            from mathutils import Quaternion
            q = Quaternion(n.normalized(), self.rotate)
            pos = {key: cen + q @ (p - cen) for key, p in pos.items()}
            hw = {key: cen + q @ (p - cen) for key, p in hw.items()}
        self._write(cache, tg, ks, pos, hw, end_free=not self.smooth_ends)


class EMP_OT_arc(_LoopTool, bpy.types.Operator):
    """Smooth the selected part of the path into one clean arc that follows its shape """ \
        """(no circle needed); its ends flow on into the path around it, without kinks"""
    bl_idname = "emp.arc"
    bl_label = "Arc"
    min_keys = 2
    smooth_turn = 'INNER'       # (the arc already heads the way the path around goes)

    bulge: FloatProperty(name="Bulge", default=1.0, min=0.0, soft_max=3.0,
                         description="How far the arc swings out (1: like the path was)")
    smooth_ends: BoolProperty(name="Smooth Ends", default=True,
                              description="The arc leaves and joins the path around it in the same "
                                          "direction (off: it keeps its own directions at the ends)")

    def shape(self, context, cache, tg, ks):
        from mathutils import Vector
        first, last = ks[0], ks[-1]
        a, b = first.co.copy(), last.co.copy()
        chord = b - a
        if chord.length < 1e-9:
            return False

        def unit(v, fallback):
            return v.normalized() if v is not None and v.length > 1e-9 else fallback
        c = chord.normalized()
        if self.smooth_ends:
            # continue the motion coming into the first key and leaving the last one
            ta = unit(a - first.hl if first.hl is not None else None, unit(first.hr - a if first.hr is not None else None, c))
            tb = unit(last.hr - b if last.hr is not None else None, unit(b - last.hl if last.hl is not None else None, c))
        else:
            ta = unit(first.hr - a if first.hr is not None else None, c)
            tb = unit(b - last.hl if last.hl is not None else None, c)
        # the old shape: the path between the two keys, by its share of the length
        pts = [p for t, p in zip(cache.times, cache.points) if first.t - 1e-6 <= t <= last.t + 1e-6]
        if len(pts) < 2:
            pts = [k.co for k in ks]
        arc = geom.cumulative(pts)
        total = arc[-1] or 1.0
        # least squares for the two handle lengths of one cubic a -> b
        s00 = s01 = s11 = r0 = r1 = 0.0
        for p, s in zip(pts, arc):
            u = s / total
            b0, b1, b2, b3 = (1 - u) ** 3, 3 * (1 - u) ** 2 * u, 3 * (1 - u) * u * u, u ** 3
            base = a * (b0 + b1) + b * (b2 + b3)
            e0, e1 = ta * b1, -tb * b2
            d = p - base
            s00 += e0.dot(e0)
            s01 += e0.dot(e1)
            s11 += e1.dot(e1)
            r0 += e0.dot(d)
            r1 += e1.dot(d)
        det = s00 * s11 - s01 * s01
        if abs(det) > 1e-12:
            l0 = (r0 * s11 - r1 * s01) / det
            l1 = (s00 * r1 - s01 * r0) / det
        else:
            l0 = l1 = chord.length / 3.0
        floor = chord.length * 0.05
        l0, l1 = max(l0, floor) * self.bulge, max(l1, floor) * self.bulge
        P = (a, a + ta * l0, b - tb * l1, b)

        def at(u):
            v = 1.0 - u
            return P[0] * v ** 3 + P[1] * 3 * v * v * u + P[2] * 3 * v * u * u + P[3] * u ** 3

        def deriv(u):
            v = 1.0 - u
            return (P[1] - P[0]) * 3 * v * v + (P[2] - P[1]) * 6 * v * u + (P[3] - P[2]) * 3 * u * u
        # the keys keep their share of the length along the new arc
        n = 200
        ups = [i / n for i in range(n + 1)]
        bpts = [at(u) for u in ups]
        barc = geom.cumulative(bpts)
        btot = barc[-1] or 1.0
        import bisect
        times = [t for t, _p in zip(cache.times, cache.points) if first.t - 1e-6 <= t <= last.t + 1e-6]
        us = {}
        for k in ks:
            i = bisect.bisect_left(times, k.t - 1e-6)
            share = (arc[min(i, len(arc) - 1)] / total) if len(times) == len(arc) else \
                (ks.index(k) / (len(ks) - 1))
            target = share * btot
            j = min(max(bisect.bisect_left(barc, target), 1), n)
            f = 0.0 if barc[j] - barc[j - 1] < 1e-12 else (target - barc[j - 1]) / (barc[j] - barc[j - 1])
            us[k.t] = ups[j - 1] + (ups[j] - ups[j - 1]) * f
        us[first.t], us[last.t] = 0.0, 1.0
        pos = {k.t: at(us[k.t]) for k in ks}
        hw = {}
        for i, k in enumerate(ks):
            d = deriv(us[k.t])
            if i > 0:
                hw[(k.t, 'L')] = pos[k.t] - d * (us[k.t] - us[ks[i - 1].t]) / 3.0
            if i + 1 < len(ks):
                hw[(k.t, 'R')] = pos[k.t] + d * (us[ks[i + 1].t] - us[k.t]) / 3.0
        self._write(cache, tg, ks, pos, hw, end_free=not self.smooth_ends)


class EMP_OT_flatten(_LoopTool, bpy.types.Operator):
    """Put the selected keys and their handles on one plane (LoopTools Flatten)"""
    bl_idname = "emp.flatten"
    bl_label = "Flatten"

    plane: EnumProperty(name="Plane", items=[
        ('BEST', "Best Fit", "The plane closest to the keys"),
        ('VIEW', "View", "Facing the view, through the middle of the keys")], default='BEST')

    def shape(self, context, cache, tg, ks):
        pts = [k.co for k in ks]
        cen, n = geom.best_plane(pts)
        rv3d = getattr(context, "region_data", None)
        if self.plane == 'VIEW' and rv3d is not None:
            from mathutils import Vector
            n = (rv3d.view_rotation @ Vector((0.0, 0.0, 1.0))).normalized()
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
            before = self._timing_before(cache, tg, fresh)
            self.shape(context, cache, tg, fresh)
            self._timing_after(cache, before, fresh)
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
           EMP_OT_mirror, EMP_OT_bevel, EMP_OT_spin, EMP_OT_circle, EMP_OT_arc, EMP_OT_flatten,
           EMP_OT_straighten, EMP_OT_gstretch)
