"""Key topology operators: delete, dissolve, insert (like vertices of a curve)."""

import math

import bpy
from bpy.props import (BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty,
                       StringProperty)
from mathutils import Vector

from . import compat, core, keys, pick, space, state


def _sel_keys(cache):
    return [k for k in cache.keys if k.sel]


def _channel_times(ch, ks, c):
    """Frames of the channel's own keys that belong to the path keys `ks` (looping channels
    point into their range)."""
    fc = ch.fcurves[c]
    out = []
    for k in ks:
        i = k.idx[c] if c < len(k.idx) else -1
        if i >= 0:
            out.append(fc.keyframe_points[i].co[0])
    return out


def _path_size(points):
    if not points:
        return 1.0
    return max(sum((max(p[i] for p in points) - min(p[i] for p in points)) ** 2 for i in range(3)) ** 0.5,
               1e-6)


class _KeyRemoval:
    """Shared: remove the selected keys of every editable path with `remove(fc, times)`."""
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode in {'POSE', 'OBJECT'}

    def invoke(self, context, event):
        if not any(_sel_keys(c) for c in state.editable_caches(context.scene)):
            return {'PASS_THROUGH'}
        return self.execute(context)

    def execute(self, context):
        n = 0
        worst = 0.0
        for cache in state.editable_caches(context.scene):
            sel = _sel_keys(cache)
            if not sel:
                continue
            tg = cache.target()
            ch = tg.channels(cache.ptype)
            before = dict(zip(cache.times, cache.points))
            size = _path_size(cache.points)
            for c, fc in enumerate(ch.fcurves):
                if fc is None:
                    continue
                times = _channel_times(ch, sel, c)
                if times:
                    self.remove(fc, times)
                if len(fc.keyframe_points) == 0:
                    compat.remove_fcurve(tg.obj, ch.path, fc.array_index)
            n += len(sel)
            cache.refresh(force=True)
            for t, p in zip(cache.times, cache.points):
                q = before.get(t)
                if q is not None:
                    worst = max(worst, (p - q).length / size * 100.0)
        if not n:
            return {'CANCELLED'}
        pick.tag_anim_editors()
        self.report({'INFO'}, "%s %d key%s (path changed by up to %.2f%% of its size)" % (
            self.verb, n, "" if n == 1 else "s", worst))
        return {'FINISHED'}


class EMP_OT_delete_keys(_KeyRemoval, bpy.types.Operator):
    """Delete the selected path keys. The keys around them keep their handles"""
    bl_idname = "emp.delete_keys"
    bl_label = "Delete Keys"
    verb = "Deleted"

    @staticmethod
    def remove(fc, times):
        keys.delete_channel(fc, times)


class EMP_OT_dissolve_keys(_KeyRemoval, bpy.types.Operator):
    """Remove the selected path keys but keep the shape of the motion: only the handles of """ \
        """the keys next to them are refitted (never changes more than Delete)"""
    bl_idname = "emp.dissolve_keys"
    bl_label = "Dissolve Keys"
    verb = "Dissolved"

    @staticmethod
    def remove(fc, times):
        keys.dissolve_channel(fc, times)


class EMP_OT_delete_menu(bpy.types.Operator):
    """Delete or dissolve the selected path keys"""
    bl_idname = "emp.delete_menu"
    bl_label = "Delete Path Keys Menu"

    def invoke(self, context, event):
        if not any(_sel_keys(c) for c in state.editable_caches(context.scene)):
            return {'PASS_THROUGH'}
        bpy.ops.wm.call_menu(name="EMP_MT_delete")
        return {'FINISHED'}


class EMP_MT_delete(bpy.types.Menu):
    bl_label = "Delete"
    bl_idname = "EMP_MT_delete"

    def draw(self, context):
        layout = self.layout
        layout.operator_context = 'EXEC_DEFAULT'
        layout.operator("emp.delete_keys", text="Keys", icon='X')
        layout.separator()
        layout.operator("emp.dissolve_keys", text="Dissolve Keys")
        op = layout.operator("emp.cleanup", text="Limited Dissolve (Clean Up)")
        op.method = 'CLEAN'


class EMP_OT_insert_key(bpy.types.Operator):
    """Insert a key on the editable paths at the current frame (the motion doesn't change)"""
    bl_idname = "emp.insert_key"
    bl_label = "Insert Path Key"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        t = float(context.scene.frame_current)
        caches = state.editable_caches(context.scene)
        if not caches:
            self.report({'WARNING'}, "Select a bone/object that has a path")
            return {'CANCELLED'}
        pick.deselect_all(caches)
        for cache in caches:
            k = insert_key(cache, t)
            if k is not None:
                pick.select_part(cache, k, 'KEY', True)
        pick.tag_anim_editors()
        return {'FINISHED'}


def insert_key(cache, t):
    """Insert a key at frame t on the edited channels of a path, keeping the motion."""
    tg = cache.target()
    if tg is None:
        return None
    cache.refresh()
    keys.insert_path_keys(cache, tg, [t])
    cache.refresh(force=True)
    for k in cache.keys:
        if abs(k.t - t) < 1e-3:
            return k
    return None


# ----------------------------------------------------------------------------
# Subdivide / Loop Cut / Knife
# ----------------------------------------------------------------------------

def cut_frames(a, b, cuts, whole):
    ts = [a + (b - a) * i / (cuts + 1) for i in range(1, cuts + 1)]
    if whole:
        ts = sorted({float(round(t)) for t in ts if a + 0.5 < round(t) < b - 0.5})
    return ts


def _insert_and_select(cache, frames):
    tg = cache.target()
    if tg is None or not frames:
        return 0
    cache.refresh()
    done = keys.insert_path_keys(cache, tg, frames)
    cache.refresh(force=True)
    want = {round(t, 3) for t in done}
    for k in cache.keys:
        if round(k.t, 3) in want:
            pick.select_part(cache, k, 'KEY', True)
    cache.refresh(force=True)
    return len(done)


class EMP_OT_subdivide(bpy.types.Operator):
    """Insert evenly spaced keys in every selected segment (between two selected keys). """ \
        """The motion doesn't change"""
    bl_idname = "emp.subdivide"
    bl_label = "Subdivide Path"
    bl_options = {'REGISTER', 'UNDO'}

    cuts: IntProperty(name="Number of Cuts", default=1, min=1, soft_max=20, max=200)
    whole_frames: BoolProperty(name="Whole Frames", default=True,
                               description="Put the new keys on whole frames (otherwise exactly "
                                           "evenly spaced, on subframes)")

    @classmethod
    def poll(cls, context):
        return context.mode in {'POSE', 'OBJECT'}

    def execute(self, context):
        n = 0
        for c in state.editable_caches(context.scene):
            ks = c.keys
            segs = [(a.t, b.t) for a, b in zip(ks, ks[1:]) if a.sel and b.sel]
            frames = [t for a, b in segs for t in cut_frames(a, b, self.cuts, self.whole_frames)]
            n += _insert_and_select(c, frames)
        if not n:
            self.report({'WARNING'}, "Select two neighboring keys (a segment) to subdivide")
            return {'CANCELLED'}
        pick.tag_anim_editors()
        return {'FINISHED'}


class EMP_OT_loopcut(bpy.types.Operator):
    """Insert evenly spaced keys in the segment under the mouse (Ctrl+R). Wheel: number of """ \
        """cuts, click: confirm"""
    bl_idname = "emp.loopcut"
    bl_label = "Loop Cut Path Segment"
    bl_options = {'REGISTER', 'UNDO'}

    cuts: IntProperty(name="Number of Cuts", default=1, min=1, soft_max=20, max=200)
    whole_frames: BoolProperty(name="Whole Frames", default=True)
    obj_name: StringProperty(options={'HIDDEN'})
    bone_name: StringProperty(options={'HIDDEN'})
    seg_start: FloatProperty(options={'HIDDEN'})
    seg_end: FloatProperty(options={'HIDDEN'})

    def _cache(self, context):
        for c in state.editable_caches(context.scene):
            if c.obj_name == self.obj_name and c.bone_name == self.bone_name:
                return c
        return None

    def execute(self, context):
        c = self._cache(context)
        if c is None:
            return {'CANCELLED'}
        frames = cut_frames(self.seg_start, self.seg_end, self.cuts, self.whole_frames)
        if not _insert_and_select(c, frames):
            return {'CANCELLED'}
        pick.tag_anim_editors()
        return {'FINISHED'}

    def invoke(self, context, event):
        if context.region_data is None or not state.editable_caches(context.scene):
            return {'PASS_THROUGH'}
        self.seg = None
        self._hover(context, event)
        context.window_manager.modal_handler_add(self)
        state.tag_redraw()
        return {'RUNNING_MODAL'}

    def _hover(self, context, event):
        hit = pick.line_hit(context, event.mouse_region_x, event.mouse_region_y, 40.0 * compat.ui_scale())
        self.seg = None
        state.markers = None
        if hit is not None:
            c = hit.cache
            prev = [k.t for k in c.keys if k.t <= hit.t]
            nxt = [k.t for k in c.keys if k.t > hit.t]
            if prev and nxt:
                self.seg = (c, prev[-1], nxt[0])
                tg = c.target()
                pts = [c.point(tg, t) for t in cut_frames(prev[-1], nxt[0], self.cuts, self.whole_frames)]
                state.markers = (pts, context.area.as_pointer())
        context.area.header_text_set("Loop Cut: %d cut%s   wheel / PgUp PgDn: cuts   click: confirm   "
                                     "Esc: cancel" % (self.cuts, "" if self.cuts == 1 else "s"))

    def modal(self, context, event):
        t, v = event.type, event.value
        if t in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            self._hover(context, event)
        elif t in {'WHEELUPMOUSE', 'PAGE_UP', 'NUMPAD_PLUS'} and v == 'PRESS':
            self.cuts += 1
            self._hover(context, event)
        elif t in {'WHEELDOWNMOUSE', 'PAGE_DOWN', 'NUMPAD_MINUS'} and v == 'PRESS':
            self.cuts = max(1, self.cuts - 1)
            self._hover(context, event)
        elif t in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER', 'SPACE'} and v == 'PRESS':
            return self._end(context, confirm=self.seg is not None)
        elif t in {'RIGHTMOUSE', 'ESC'} and v == 'PRESS':
            return self._end(context, confirm=False)
        state.tag_redraw()
        return {'RUNNING_MODAL'}

    def _end(self, context, confirm):
        state.markers = None
        context.area.header_text_set(None)
        state.tag_redraw()
        if not confirm:
            return {'CANCELLED'}
        c, a, b = self.seg
        self.obj_name, self.bone_name = c.obj_name, c.bone_name
        self.seg_start, self.seg_end = a, b
        pick.deselect_all(state.editable_caches(context.scene))
        c.refresh(force=True)
        return self.execute(context)


def _seg_cross(p, q, a, b):
    """Parameters (u on p-q, w on a-b) where two 2D segments cross, or None."""
    r = q - p
    s = b - a
    den = r.x * s.y - r.y * s.x
    if abs(den) < 1e-9:
        return None
    d = a - p
    u = (d.x * s.y - d.y * s.x) / den
    w = (d.x * r.y - d.y * r.x) / den
    if 0.0 <= u <= 1.0 and 0.0 <= w <= 1.0:
        return u, w
    return None


class EMP_OT_knife(bpy.types.Operator):
    """Cut the paths like the mesh Knife: keys are inserted where the line crosses them, the """ \
        """motion stays (Ctrl+K). LMB: add a point or drag to draw, RMB: fix this cut and start """ \
        """a new one, Shift: snap to """ \
        """the middle of a segment, Ctrl: no snapping, A: angle constraint, X / Y / Z: axis, """ \
        """S: frame numbers, Ctrl+Z: undo a point, Enter / Space / double-click: cut, Esc: cancel """ \
        """all the cuts (like the mesh Knife)"""
    bl_idname = "emp.knife"
    bl_label = "Knife Path"
    bl_options = {'REGISTER', 'UNDO'}

    whole_frames: BoolProperty(name="Whole Frames", default=True,
                               description="Round the cuts to whole frames")
    angle_increment: FloatProperty(name="Angle Snapping Increment", subtype='ANGLE', default=math.radians(30.0),
                                   min=math.radians(1.0), max=math.radians(180.0),
                                   description="The angle constraint (A) snaps the line to multiples "
                                               "of this angle")
    # the frames cut, per path (JSON): the redo panel makes the same cuts again
    made: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        """Redo (Adjust Last Operation): the same cuts again, rounded as Whole Frames says."""
        import json
        try:
            data = json.loads(self.made or "[]")
        except ValueError:
            data = []
        caches = {c.key: c for c in state.editable_caches(context.scene)}
        n = 0
        if any(fs for _k, fs in data):
            # like the knife itself: only the new keys end up selected (the redo starts from
            # before the knife, with the keys selected then)
            pick.deselect_all(list(caches.values()))
        for key, fs in data:
            c = caches.get(tuple(key))
            if c is None:
                continue
            c.refresh(force=True)
            have = {round(k.t, 3) for k in c.keys}
            fs = sorted({float(round(f)) if self.whole_frames else float(f) for f in fs})
            n += _insert_and_select(c, [f for f in fs if round(f, 3) not in have and c.fs < f < c.fe])
        if not n:
            return {'CANCELLED'}
        pick.tag_anim_editors()
        return {'FINISHED'}

    def _record(self, cross):
        import json
        for c, fs in cross:
            if fs:
                self._made.setdefault(c.key, []).extend(fs)
        self.made = json.dumps([[list(k), v] for k, v in self._made.items()])

    def invoke(self, context, event):
        if context.region_data is None or not state.editable_caches(context.scene):
            return {'PASS_THROUGH'}
        self.cuts = [[]]            # cut lines still open, as world points
        self.pending = {}           # cuts fixed with RMB: cache key -> frames (made on Enter)
        self._made = {}
        # the cut lies at the depth of the paths, so it stays on them when the view turns
        pts = [p for c in state.editable_caches(context.scene) for p in c.points]
        self.depth = sum(pts, Vector()) / len(pts) if pts else context.region_data.view_location.copy()
        self.drawing = False
        self.angle_mode = None      # None / 'SCREEN' / 'RELATIVE'
        self.axis = None            # None / 0 / 1 / 2 (world X / Y / Z)
        self.show_frames = True
        self.snap = None            # (world point, kind) the cursor snapped to
        self.raw = Vector((event.mouse_region_x, event.mouse_region_y))
        self.cur = self.raw.copy()
        self.mods = (False, False)
        context.window_manager.modal_handler_add(self)
        context.window.cursor_modal_set('KNIFE')
        self._update_cursor(context)
        self._show(context)
        return {'RUNNING_MODAL'}

    # -- the cursor: snapping and constraints ---------------------------------------

    def _snap_point(self, context, m, midpoint):
        """Snap to a key or a frame dot near the mouse (Shift: the middle of the segment under
        the mouse, in time)."""
        from bpy_extras.view3d_utils import location_3d_to_region_2d as proj
        region, rv3d = context.region, context.region_data
        us = compat.ui_scale()
        best = None
        for c in state.editable_caches(context.scene):
            tg = c.target()
            if tg is None:
                continue
            if midpoint:
                # the sample nearest to the mouse, then the middle of its segment
                near = None
                for t, p in zip(c.times, c.points):
                    s = proj(region, rv3d, p)
                    if s is not None and (near is None or (s - m).length < near[0]):
                        near = ((s - m).length, t)
                if near is None or near[0] > 40.0 * us:
                    continue
                ks = [k.t for k in c.keys]
                before = [t for t in ks if t <= near[1]]
                after = [t for t in ks if t > near[1]]
                if not before or not after:
                    continue
                tm = (before[-1] + after[0]) * 0.5
                p = c.point(tg, tm)
                s = proj(region, rv3d, p)
                if s is not None and (best is None or (s - m).length < best[0]):
                    best = ((s - m).length, s, p, "middle of %g - %g" % (before[-1], after[0]))
                continue
            for t, p in zip(c.times, c.points):
                if abs(t - round(t)) > 1e-6:
                    continue
                s = proj(region, rv3d, p)
                if s is None:
                    continue
                d = (s - m).length
                if d <= 12.0 * us and (best is None or d < best[0]):
                    is_key = any(abs(k.t - t) < 1e-3 for k in c.keys)
                    best = (d, s, p, ("key %d" if is_key else "frame %d") % round(t))
        return best

    def _scr(self, context, p):
        from bpy_extras.view3d_utils import location_3d_to_region_2d
        return location_3d_to_region_2d(context.region, context.region_data, p)

    def _world(self, context, s):
        from bpy_extras.view3d_utils import region_2d_to_location_3d
        return region_2d_to_location_3d(context.region, context.region_data, s, self.depth)

    def _constrained(self, context, m):
        pts = [q for q in (self._scr(context, p) for p in self.cuts[-1]) if q is not None]
        if not pts:
            return m
        last = pts[-1]
        if self.axis is not None:
            from bpy_extras.view3d_utils import location_3d_to_region_2d as proj, region_2d_to_location_3d
            region, rv3d = context.region, context.region_data
            w = region_2d_to_location_3d(region, rv3d, last, rv3d.view_location)
            ax = Vector([1.0 if i == self.axis else 0.0 for i in range(3)])
            a, b = proj(region, rv3d, w), proj(region, rv3d, w + ax)
            if a is None or b is None or (b - a).length < 1e-6:
                return m
            d = (b - a).normalized()
            return last + d * (m - last).dot(d)
        if self.angle_mode is not None:
            v = m - last
            if v.length < 1e-6:
                return m
            ref = 0.0
            if self.angle_mode == 'RELATIVE' and len(pts) >= 2:
                r = pts[-1] - pts[-2]
                ref = math.atan2(r.y, r.x)
            a = math.atan2(v.y, v.x) - ref
            inc = self.angle_increment
            a = round(a / inc) * inc + ref
            return last + Vector((math.cos(a), math.sin(a))) * v.length
        return m

    def _update_cursor(self, context):
        ctrl, shift = self.mods
        m = self._constrained(context, self.raw)
        self.snap = None
        if not ctrl and not self.drawing and self.axis is None and self.angle_mode is None:
            hit = self._snap_point(context, m, shift)
            if hit is not None:
                m = hit[1]
                self.snap = (hit[2], hit[3])
        self.cur = Vector(m)

    # -- crossings ----------------------------------------------------------------

    def _lines(self, context):
        """The cut lines on the screen (the current one runs on to the cursor)."""
        lines = [[q for q in (self._scr(context, p) for p in c) if q is not None] for c in self.cuts if c]
        if self.cuts[-1] and not self.drawing:
            lines[-1] = lines[-1] + [self.cur]
        return [ln for ln in lines if len(ln) >= 2]

    def _crossings(self, context, lines=None):
        from bpy_extras.view3d_utils import location_3d_to_region_2d
        region, rv3d = context.region, context.region_data
        if lines is None:
            lines = self._lines(context)
        out = []
        for c in state.editable_caches(context.scene):
            proj = [location_3d_to_region_2d(region, rv3d, p) for p in c.points]
            frames = set()
            for i in range(len(proj) - 1):
                p, q = proj[i], proj[i + 1]
                if p is None or q is None:
                    continue
                for line in lines:
                    for a, b in zip(line, line[1:]):
                        hit = _seg_cross(p, q, a, b)
                        if hit is not None:
                            t = c.times[i] + (c.times[i + 1] - c.times[i]) * hit[0]
                            frames.add(round(t) if self.whole_frames else round(t, 2))
            # a point snapped right onto a frame dot cuts there too
            for line in lines:
                for pt in line:
                    for i, s in enumerate(proj):
                        if s is not None and (s - pt).length < 0.5 and abs(c.times[i] - round(c.times[i])) < 1e-6:
                            frames.add(round(c.times[i]))
            have = {round(k.t, 3) for k in c.keys}
            fs = sorted(f for f in frames if round(float(f), 3) not in have and c.fs < f < c.fe)
            out.append((c, [float(f) for f in fs]))
        return out

    def _show(self, context):
        cross = self._all_cuts(context)
        marks, labels = [], []
        for c, fs in cross:
            tg = c.target()
            for f in fs:
                marks.append(c.point(tg, f))
                labels.append("%g" % f if self.show_frames else "")
        # (the line runs on to the cursor only from a cut that is still open)
        state.knife = ([list(c) for c in self.cuts if c], context.area.as_pointer(),
                       None if self.drawing else self.cur.copy(), self.snap, bool(self.cuts[-1]))
        state.markers = (marks, context.area.as_pointer(), labels)
        n = len(marks)
        cons = ""
        if self.axis is not None:
            cons = "   along %s" % "XYZ"[self.axis]
        elif self.angle_mode is not None:
            cons = "   angle %d° (%s)" % (round(math.degrees(self.angle_increment)),
                                              "screen" if self.angle_mode == 'SCREEN' else "relative")
        snap = ("   snapped: %s" % self.snap[1]) if self.snap else ""
        context.area.header_text_set(
            "Knife: %d new key%s%s%s   LMB: point / drag   RMB: fix this cut, next cut   Shift: middle of a segment   "
            "Ctrl: no snapping   A: angle   X Y Z: axis   S: frame numbers   Ctrl+Z: undo point   "
            "Enter / Space / double-click: cut   Esc: cancel all" % (n, "" if n == 1 else "s", cons, snap))
        state.tag_redraw()

    # -- events -------------------------------------------------------------------

    def modal(self, context, event):
        t, v = event.type, event.value
        mods = (event.ctrl, event.shift)
        if mods != self.mods:
            self.mods = mods
            self._update_cursor(context)
            self._show(context)
        if t in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            self.raw = Vector((event.mouse_region_x, event.mouse_region_y))
            self._update_cursor(context)
            pts = self.cuts[-1]
            last = self._scr(context, pts[-1]) if pts else None
            if self.drawing and (last is None or (self.cur - last).length > 8.0 * compat.ui_scale()):
                pts.append(self._world(context, self.cur))
            self._show(context)
        elif t == 'LEFTMOUSE' and v == 'DOUBLE_CLICK':
            return self._end(context, True)
        elif t == 'LEFTMOUSE':
            if v == 'PRESS':
                # a snapped point sits exactly on the path
                self.cuts[-1].append(self.snap[0].copy() if self.snap else self._world(context, self.cur))
                self.drawing = True
            elif v == 'RELEASE':
                self.drawing = False
            self._update_cursor(context)
            self._show(context)
        elif t == 'RIGHTMOUSE' and v == 'PRESS':
            # this cut is fixed (turning the view won't change it); Enter makes all the cuts
            if self.cuts[-1]:
                self._commit(context)
            self._update_cursor(context)
            self._show(context)
        elif t == 'Z' and v == 'PRESS' and event.ctrl:
            if not self.cuts[-1] and len(self.cuts) > 1:
                self.cuts.pop()
            if self.cuts[-1]:
                self.cuts[-1].pop()
            self._show(context)
        elif t == 'A' and v == 'PRESS':
            self.axis = None
            self.angle_mode = {None: 'SCREEN', 'SCREEN': 'RELATIVE', 'RELATIVE': None}[self.angle_mode]
            self._update_cursor(context)
            self._show(context)
        elif t in {'X', 'Y', 'Z'} and v == 'PRESS' and not event.ctrl:
            i = "XYZ".index(t)
            self.axis = None if self.axis == i else i
            self.angle_mode = None
            self._update_cursor(context)
            self._show(context)
        elif t == 'S' and v == 'PRESS':
            self.show_frames = not self.show_frames
            self._show(context)
        elif t in {'RET', 'NUMPAD_ENTER', 'SPACE'} and v == 'PRESS':
            return self._end(context, True)
        elif t == 'ESC' and v == 'PRESS':
            return self._end(context, False)
        elif t in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'TRACKPADPAN', 'TRACKPADZOOM'}:
            return {'PASS_THROUGH'}     # the view can be turned while cutting, like in modeling
        return {'RUNNING_MODAL'}

    def _commit(self, context):
        """RMB, like the mesh Knife: this cut is fixed (its frames are worked out now, so turning
        the view won't change them; the line goes, the marks stay), nothing is inserted yet:
        Enter makes all the cuts, Esc drops them all."""
        pts = [q for q in (self._scr(context, p) for p in self.cuts[-1]) if q is not None]
        self.cuts = [c for c in self.cuts[:-1] if c] + [[]]
        if len(pts) < 2:
            return
        for c, fs in self._crossings(context, [pts]):
            if fs:
                self.pending.setdefault(c.key, set()).update(fs)

    def _all_cuts(self, context, with_open=True):
        """[(cache, frames)]: the fixed cuts and (with_open) the ones still being drawn."""
        out = {}
        for key, fs in self.pending.items():
            c = state.caches.get(key)
            if c is not None:
                out[key] = (c, set(fs))
        if with_open:
            for c, fs in self._crossings(context):
                out.setdefault(c.key, (c, set()))[1].update(fs)
        return [(c, sorted(fs)) for c, fs in out.values()]

    def _end(self, context, confirm):
        self.drawing = False
        cross = self._all_cuts(context) if confirm else []
        state.knife = None
        state.markers = None
        context.window.cursor_modal_restore()
        context.area.header_text_set(None)
        state.tag_redraw()
        n = 0
        if any(fs for _c, fs in cross):
            pick.deselect_all(state.editable_caches(context.scene))
        self._record(cross)
        for c, fs in cross:
            c.refresh(force=True)
            have = {round(k.t, 3) for k in c.keys}
            n += _insert_and_select(c, [f for f in fs if round(f, 3) not in have])
        if not n:
            return {'CANCELLED'}
        pick.tag_anim_editors()
        self.report({'INFO'}, "Knife: %d new key%s" % (n, "" if n == 1 else "s"))
        return {'FINISHED'}


# ----------------------------------------------------------------------------
# Merge
# ----------------------------------------------------------------------------

class EMP_OT_merge(bpy.types.Operator):
    """Merge the selected keys of each path into one key"""
    bl_idname = "emp.merge"
    bl_label = "Merge Path Keys"
    bl_options = {'REGISTER', 'UNDO'}

    type: EnumProperty(name="Type", items=[
        ('LAST', "At Last", "Everything goes to the last selected (active) key"),
        ('FIRST', "At First", "Everything goes to the first selected key"),
        ('CENTER', "At Center", "One key in the middle (frame and position)"),
        ('CURSOR', "At Cursor", "The active key, moved to the 3D cursor"),
    ], default='LAST')

    def invoke(self, context, event):
        if not any(sum(1 for k in c.keys if k.sel) >= 2 for c in state.editable_caches(context.scene)):
            return {'PASS_THROUGH'}
        bpy.ops.wm.call_menu(name="EMP_MT_merge")
        return {'FINISHED'}

    def execute(self, context):
        from . import edit
        n = 0
        for c in state.editable_caches(context.scene):
            sel = [k for k in c.keys if k.sel]
            if len(sel) < 2:
                continue
            if self.type in {'FIRST', 'LAST', 'CURSOR'}:
                # selection order, like mesh Merge: the history of clicked keys
                order = [t for key, t in state.select_history(context.scene) if key == c.key]
                ranked = [k for t in order for k in sel if abs(k.t - t) < 1e-3]
                if self.type == 'FIRST':
                    keep = ranked[0] if ranked else sel[0]
                else:
                    keep = ranked[-1] if ranked else sel[-1]
                keep_t = keep.t
                W = context.scene.cursor.location.copy() if self.type == 'CURSOR' else None
            else:
                keep_t = float(round(sum(k.t for k in sel) / len(sel)))
                W = space.median([k.co for k in sel])
                if not any(abs(k.t - keep_t) < 1e-3 for k in c.keys):
                    tg = c.target()
                    keys.insert_path_keys(c, tg, [keep_t])
                    c.refresh(force=True)
            tg = c.target()
            ch = tg.channels(c.ptype)
            gone = [k for k in c.keys if k.sel and abs(k.t - keep_t) > 1e-3]
            for ci, fc in enumerate(ch.fcurves):
                if fc is not None:
                    keys.delete_channel(fc, _channel_times(ch, gone, ci))
            c.refresh(force=True)
            if W is not None:
                tg, fresh = edit.prepare(context, c, [keep_t])
                w = edit.Writer([keep_t])
                for k in fresh:
                    w.key(c, tg, k, W)
                w.done([c])
            for k in c.keys:
                pick.select_part(c, k, 'KEY', abs(k.t - keep_t) < 1e-3)
            state.set_active(c.key, keep_t)
            n += 1
        if not n:
            return {'CANCELLED'}
        pick.tag_anim_editors()
        return {'FINISHED'}


class EMP_MT_merge(bpy.types.Menu):
    bl_label = "Merge"
    bl_idname = "EMP_MT_merge"

    def draw(self, context):
        self.layout.operator_context = 'EXEC_DEFAULT'
        self.layout.operator_enum("emp.merge", "type")


# ----------------------------------------------------------------------------
# Horizontal mouse -> frames (Duplicate, Slide)
# ----------------------------------------------------------------------------

PX_PER_FRAME = 10.0


def frames_from_mouse(dx):
    return dx / (PX_PER_FRAME * compat.ui_scale())


def _pose_fcurves(tg, cache, whole):
    if whole and tg.coll is not None:
        return [fc for fc in tg.coll if fc.data_path.startswith(tg.prefix)] if tg.prefix else list(tg.coll)
    return [fc for fc in tg.channels(cache.ptype).fcurves if fc is not None]


def kp_entry(kp):
    """A key as data relative to its own position (value, handles, types)."""
    x, y = kp.co
    return (x, y, (kp.handle_left[0] - x, kp.handle_left[1] - y),
            (kp.handle_right[0] - x, kp.handle_right[1] - y), kp.handle_left_type,
            kp.handle_right_type, kp.interpolation, kp.type, kp.easing)


def put_keys(fc, entries, select_new=True):
    """Put keys (x, y, hl offset, hr offset, types, interpolation, key type, easing) on `fc`,
    replacing keys on the same frames. The other keys keep their handles. Returns the frames."""
    kps = fc.keyframe_points
    xs = {round(e[0], 3) for e in entries}
    keep = [(kp.co[0], tuple(kp.handle_left), tuple(kp.handle_right), kp.handle_left_type,
             kp.handle_right_type) for kp in kps if round(kp.co[0], 3) not in xs]
    for i in reversed(range(len(kps))):
        if round(kps[i].co[0], 3) in xs:
            kps.remove(kps[i], fast=True)
    for e in entries:
        kps.insert(e[0], e[1], options={'FAST'})
    by_x = {round(kp.co[0], 3): kp for kp in kps}
    # inserting reshapes the neighbors' handles: put the old keys back as they were
    for x, hl, hr, tl, tr in keep:
        kp = by_x.get(round(x, 3))
        if kp is not None:
            kp.handle_left_type, kp.handle_right_type = tl, tr
            kp.handle_left, kp.handle_right = hl, hr
            if select_new:
                kp.select_control_point = kp.select_left_handle = kp.select_right_handle = False
    for x, y, hl, hr, tl, tr, interp, ktype, easing in entries:
        kp = by_x.get(round(x, 3))
        if kp is None:
            continue
        kp.interpolation, kp.type, kp.easing = interp, ktype, easing
        kp.handle_left_type, kp.handle_right_type = tl, tr
        kp.co = (x, y)
        kp.handle_left = (x + hl[0], y + hl[1])
        kp.handle_right = (x + hr[0], y + hr[1])
        kp.select_control_point = kp.select_left_handle = kp.select_right_handle = select_new
    fc.update()
    return [e[0] for e in entries]


def copy_keys_to(fc, frames, offset, select_new=True):
    """Copy the keys of `fc` at `frames` to frame + offset (replacing keys there). The other
    keys keep their handles. Returns the new key frames."""
    cyc = core.cycle_info(fc)
    want = {round(t, 3) for t in frames}
    entries = []
    for kp in fc.keyframe_points:
        if round(kp.co[0], 3) in want:
            e = kp_entry(kp)
            entries.append((core.wrap_time(e[0] + offset, cyc),) + e[1:])
    if not entries:
        return []
    return put_keys(fc, entries, select_new)


# ----------------------------------------------------------------------------
# Copy / paste a key's pose
# ----------------------------------------------------------------------------

_clipboard = None       # {"frame": t, "items": [(data path suffix, index, entry or None, value)]}


def _suffix(tg, data_path):
    return data_path[len(tg.prefix):] if tg.prefix and data_path.startswith(tg.prefix) else data_path


def _copy_source(caches):
    """(cache, key) to copy from: the active key, else the first selected one."""
    a = space.active_key(caches)
    if a is not None:
        return a
    for c in caches:
        for k in c.keys:
            if k.sel:
                return c, k
    return None


class EMP_OT_copy_keys(bpy.types.Operator):
    """Copy the pose of the active key (Ctrl+C). Ctrl+V pastes the channels of the current """ \
        """mode on the current frame (Whole Pose in its redo panel: all of them)"""
    bl_idname = "emp.copy_keys"
    bl_label = "Copy Key Pose"

    all_channels: BoolProperty(name="Whole Pose", default=True,
                               description="Copy every channel of the bone / object, not only the "
                                           "edited ones")

    def invoke(self, context, event):
        global _clipboard
        if _copy_source(state.editable_caches(context.scene)) is None:
            _clipboard = None       # Blender's own copy (pose / objects) is the newer one now
            return {'PASS_THROUGH'}
        return self.execute(context)

    def execute(self, context):
        global _clipboard
        src = _copy_source(state.editable_caches(context.scene))
        if src is None:
            return {'CANCELLED'}
        c, k = src
        tg = c.target()
        items = []
        for fc in _pose_fcurves(tg, c, self.all_channels):
            tt = core.wrap_time(k.t, core.cycle_info(fc))
            kp = next((kp for kp in fc.keyframe_points if abs(kp.co[0] - tt) < 1e-3), None)
            items.append((_suffix(tg, fc.data_path), fc.array_index,
                          kp_entry(kp) if kp is not None else None, fc.evaluate(k.t)))
        _clipboard = {"frame": k.t, "items": items}
        self.report({'INFO'}, "Copied the pose of frame %g" % k.t)
        return {'FINISHED'}


class EMP_OT_paste_keys(bpy.types.Operator):
    """Paste the copied key on the current frame (Ctrl+V): the channels of the current mode """ \
        """(Location, Rotation, Scale or the Value), or the whole pose"""
    bl_idname = "emp.paste_keys"
    bl_label = "Paste Key Pose"
    bl_options = {'REGISTER', 'UNDO'}

    whole_pose: BoolProperty(name="Whole Pose", default=False,
                             description="Paste every copied channel of the bone / object, not "
                                         "only the ones of the current mode")

    def invoke(self, context, event):
        if _clipboard is None or not state.editable_caches(context.scene):
            return {'PASS_THROUGH'}
        return self.execute(context)

    def execute(self, context):
        if _clipboard is None:
            self.report({'WARNING'}, "Copy a key first (Ctrl+C on a selected key)")
            return {'CANCELLED'}
        frame = float(context.scene.frame_current)
        caches = state.editable_caches(context.scene)
        for c in caches:
            tg = c.target()
            ch = tg.channels(c.ptype)
            own = {(ch.path, fc.array_index if fc is not None else i) for i, fc in enumerate(ch.fcurves)}
            for suffix, index, entry, value in _clipboard["items"]:
                path = tg.prefix + suffix if tg.prefix else suffix
                if not self.whole_pose and (path, index) not in own:
                    continue        # only the channels of the current mode
                fc = tg.coll.find(path, index=index) if tg.coll is not None else None
                if fc is None:
                    if (path, index) not in own:
                        continue
                    fc = compat.ensure_fcurve(tg.obj, path, index, tg.group_name)
                x = core.wrap_time(frame, core.cycle_info(fc))
                if entry is None:
                    entry = (x, value, (-1.0, 0.0), (1.0, 0.0), 'AUTO_CLAMPED', 'AUTO_CLAMPED',
                             'BEZIER', 'KEYFRAME', 'AUTO')
                put_keys(fc, [(x,) + tuple(entry[1:])], select_new=(path, index) in own)
            c.extend_to(frame)
            c.dirty = True
            c.refresh(force=True)
            state.set_active(c.key, frame)
        state.schedule_rebake(0.3)
        pick.tag_anim_editors()
        self.report({'INFO'}, "Pasted %s of frame %g on frame %g" % (
            "the whole pose" if self.whole_pose else "the %s keys" % state.edit_mode(context.scene).lower(),
            _clipboard["frame"], frame))
        return {'FINISHED'}


def auto_offset(caches):
    """Frames to put copies of the selection at: halfway to the next key after it, or one
    key spacing past the end of the path."""
    for c in caches:
        sel = [k.t for k in c.keys if k.sel]
        if not sel:
            continue
        first, last = min(sel), max(sel)
        after = [k.t for k in c.keys if k.t > last + 1e-3]
        if after:
            gap = max(1.0, float(round((after[0] - last) / 2.0)))
        else:
            ts = [k.t for k in c.keys]
            gaps = sorted(y - x for x, y in zip(ts, ts[1:]))
            gap = max(1.0, float(round(gaps[len(gaps) // 2]))) if gaps else 5.0
        return last + gap - first
    return 0.0


class EMP_OT_duplicate_keys(bpy.types.Operator):
    """Copy the selected keys to later frames (Shift+D then moves the copies)"""
    bl_idname = "emp.duplicate_keys"
    bl_label = "Duplicate Path Keys"
    bl_options = {'REGISTER', 'UNDO'}

    offset: FloatProperty(name="Frames", default=0.0, options={'SKIP_SAVE'},
                          description="How many frames later the copies go (0: halfway to the "
                                      "next key)")
    all_channels: BoolProperty(name="Whole Pose", default=True,
                               description="Also copy the other keys of the bone / object on the "
                                           "same frames (rotation, scale, custom properties)")

    def invoke(self, context, event):
        if not any(k.sel for c in state.editable_caches(context.scene) for k in c.keys):
            return {'PASS_THROUGH'}
        return self.execute(context)

    def execute(self, context):
        caches = [c for c in state.editable_caches(context.scene) if any(k.sel for k in c.keys)]
        if not caches:
            return {'CANCELLED'}
        if abs(self.offset) < 1e-6:
            self.offset = auto_offset(caches)
        for c in caches:
            sel = [k.t for k in c.keys if k.sel]
            tg = c.target()
            for fc in _pose_fcurves(tg, c, self.all_channels):
                copy_keys_to(fc, sel, self.offset)
            c.extend_to(max(sel) + self.offset)
            c.dirty = True
            c.refresh(force=True)
        state.schedule_rebake(0.3)
        pick.tag_anim_editors()
        return {'FINISHED'}


class EMP_OT_duplicate_move(bpy.types.Macro):
    """Duplicate the selected keys and move the copies (Shift+D)"""
    bl_idname = "emp.duplicate_move"
    bl_label = "Duplicate and Move Path Keys"
    bl_options = {'REGISTER', 'UNDO'}


def register_macros():
    EMP_OT_duplicate_move.define("EMP_OT_duplicate_keys")
    move = EMP_OT_duplicate_move.define("EMP_OT_transform")
    move.properties.mode = 'TRANSLATE'
    move.properties.time_wheel = True


def shift_selected_keys(caches, d, all_channels=True):
    """Move the selected keys d frames in time (whole pose). Frames that already have keys are
    jumped over, so the keys can travel through the motion. Returns the frames moved by."""
    moved = None
    for c in caches:
        sel = sorted(k.t for k in c.keys if k.sel)
        if not sel:
            continue
        others = {round(k.t, 3) for k in c.keys if not k.sel}
        step = 1.0 if d > 0 else -1.0
        off = 0.0
        for _ in range(int(abs(d))):
            off += step
            for _j in range(1000):
                if not any(round(t + off, 3) in others for t in sel):
                    break
                off += step
        if abs(off) < 1e-9:
            continue
        tg = c.target()
        for fc in _pose_fcurves(tg, c, all_channels):
            cyc = core.cycle_info(fc)
            want = {round(t, 3) for t in sel}
            entries = [kp_entry(kp) for kp in fc.keyframe_points if round(kp.co[0], 3) in want]
            if not entries:
                continue
            kps = fc.keyframe_points
            for i in reversed(range(len(kps))):
                if round(kps[i].co[0], 3) in want:
                    kps.remove(kps[i], fast=True)
            put_keys(fc, [(core.wrap_time(e[0] + off, cyc),) + e[1:] for e in entries])
        c.extend_to(sel[0] + off)
        c.extend_to(sel[-1] + off)
        c.dirty = True
        c.refresh(force=True)
        moved = off
    return moved


class EMP_OT_duplicate(bpy.types.Operator):
    """Copy the selected keys to other frames: move the mouse left / right to place the """ \
        """copies, click to confirm. Copies of one key make a hold"""
    bl_idname = "emp.duplicate"
    bl_label = "Duplicate in Time"
    bl_options = {'REGISTER', 'UNDO'}

    offset: FloatProperty(name="Frames", default=0.0)
    all_channels: BoolProperty(name="Whole Pose", default=True,
                               description="Also copy the other keys of the bone / object on the "
                                           "same frames (rotation, scale, custom properties)")

    def _apply(self, context):
        n = 0
        for c in state.editable_caches(context.scene):
            sel = [k.t for k in c.keys if k.sel]
            if not sel:
                continue
            tg = c.target()
            for fc in _pose_fcurves(tg, c, self.all_channels):
                copy_keys_to(fc, sel, self.offset)
            c.dirty = True
            n += 1
        return n

    def execute(self, context):
        if abs(self.offset) < 1e-6 or not self._apply(context):
            return {'CANCELLED'}
        state.schedule_rebake(0.2)
        pick.tag_anim_editors()
        return {'FINISHED'}

    def invoke(self, context, event):
        caches = [c for c in state.editable_caches(context.scene) if any(k.sel for k in c.keys)]
        if not caches:
            return {'PASS_THROUGH'}
        self.snaps = []
        for c in caches:
            tg = c.target()
            fcs = _pose_fcurves(tg, c, self.all_channels)
            self.snaps.append(core.FCurveSnapshot(tg, items=[(fc.data_path, fc.array_index, fc) for fc in fcs]))
        self.x0 = event.mouse_x
        self.xv = float(event.mouse_x)
        self.xl = event.mouse_x
        self.offset = 0.0
        self.first = min(k.t for c in caches for k in c.keys if k.sel)
        state.modal_running += 1
        state.force_numbers = True
        context.window_manager.modal_handler_add(self)
        self._update(context, event)
        return {'RUNNING_MODAL'}

    def _restore(self):
        for s in reversed(self.snaps):
            s.restore()
        for c in state.caches.values():
            c.dirty = True

    def _update(self, context, event):
        off = frames_from_mouse(self.xv - self.x0)
        off = round(off) if not event.ctrl else round(off, 2)
        if off != self.offset or not hasattr(self, "shown"):
            self.shown = True
            self._restore()
            self.offset = off
            if abs(off) > 1e-6:
                self._apply(context)
            pick.tag_anim_editors()
        txt = "Duplicate: %+g frames (copies start at frame %g)   move left / right, Ctrl: subframes, " \
              "click: confirm, Esc: cancel" % (self.offset, self.first + self.offset)
        context.area.header_text_set(txt)
        state.tag_redraw()

    def modal(self, context, event):
        t, v = event.type, event.value
        if t in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            self.xv += (event.mouse_x - self.xl) * (0.1 if event.shift else 1.0)
            self.xl = event.mouse_x
            self._update(context, event)
        elif t in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER', 'SPACE'} and v == 'PRESS':
            self._finish(context)
            if abs(self.offset) < 1e-6:
                self._restore()
                return {'CANCELLED'}
            state.schedule_rebake(0.2)
            return {'FINISHED'}
        elif t in {'RIGHTMOUSE', 'ESC'} and v == 'PRESS':
            self._restore()
            self._finish(context)
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}

    def _finish(self, context):
        state.modal_running = max(0, state.modal_running - 1)
        state.force_numbers = False
        context.area.header_text_set(None)
        state.tag_redraw()


# ----------------------------------------------------------------------------
# Extrude
# ----------------------------------------------------------------------------

class EMP_OT_extrude(bpy.types.Operator):
    """Add a new key that continues the motion from the selected end key (E). Mouse: where it """ \
        """goes, wheel: how many frames later"""
    bl_idname = "emp.extrude"
    bl_label = "Extrude Path Key"
    bl_options = {'REGISTER', 'UNDO'}

    frames: IntProperty(name="Frames", default=6, min=1, soft_max=100,
                        description="How many frames after (or before) the selected key")
    offset: FloatVectorProperty(name="Offset", size=3, subtype='TRANSLATION',
                                description="Move from where the motion would continue")

    MIDDLE_HINT = "Extrude works on the open ends of a path: use Duplicate (Shift+D) inside it"

    def _sources(self, context):
        out = []
        self.middle = False
        for c in state.editable_caches(context.scene):
            ks = c.keys
            sel = [i for i, k in enumerate(ks) if k.sel]
            if not sel or len(ks) < 1:
                continue
            tg = c.target()
            if any(core.cycle_info(fc) for fc in tg.channels(c.ptype).fcurves):
                continue
            if sel[-1] == len(ks) - 1 or len(ks) == 1:
                out.append((c, ks[-1], 1.0))
            elif sel[0] == 0:
                out.append((c, ks[0], -1.0))
            else:
                self.middle = True    # a key inside the path: nowhere to grow
        return out

    def _apply(self, context):
        from . import edit
        made = 0
        for c, src, sign in self._sources(context):
            tg = c.target()
            ch = tg.channels(c.ptype)
            ks = c.keys
            i = ks.index(src)
            t_new = src.t + sign * self.frames
            nb = ks[i + 1].t if sign > 0 and i + 1 < len(ks) else (ks[i - 1].t if sign < 0 and i > 0 else None)
            if nb is not None and (t_new - nb) * sign >= -1e-3:
                continue        # would land on or past the next key
            # continue with the velocity of the last segment (or stay put on a single key)
            other = ks[i - int(sign)] if 0 <= i - int(sign) < len(ks) else None
            if other is not None and abs(src.t - other.t) > 1e-6:
                vel = [(a - b) / (src.t - other.t) for a, b in zip(src.vals, other.vals)]
                vel_w = (src.co - other.co) / (src.t - other.t)
            else:
                vel = [0.0] * len(src.vals)
                vel_w = Vector()
            dt = t_new - src.t
            for fc in ch.fcurves:
                if fc is not None and len(fc.keyframe_points):
                    kp = fc.keyframe_points.insert(t_new, core.raw_curve_values(fc, [t_new])[0], options={'FAST'})
                    kp.interpolation = src.interp
                    fc.update()
            core.ensure_keys(tg, c.ptype, [t_new])
            c.extend_to(t_new)
            c.refresh(force=True)
            tg = c.target()
            k = next((k for k in c.keys if abs(k.t - t_new) < 1e-3), None)
            if k is None:
                continue
            if c.ptype == core.LOCATION:
                W = src.co + vel_w * dt
            else:
                ref = [v + s * dt for v, s in zip(src.vals, vel)]
                W = c.point(tg, t_new, ref)
            w = edit.Writer([t_new])
            w.key(c, tg, k, W + Vector(self.offset))
            # the new key gets the handle types of the key it grows from (Vector stays Vector)
            core.set_handle_types(tg.channels(c.ptype), k, src.type_l, src.type_r, w.touched)
            w.done([c])
            pick.deselect_all([c])
            c.refresh(force=True)
            for kk in c.keys:
                if abs(kk.t - t_new) < 1e-3:
                    pick.select_part(c, kk, 'KEY', True)
            state.set_active(c.key, t_new)
            made += 1
        return made

    def execute(self, context):
        if not self._apply(context):
            self.report({'WARNING'}, self.MIDDLE_HINT if getattr(self, "middle", False) else
                        "Select the first or last key of a path to extrude (not on looping paths)")
            return {'CANCELLED'}
        state.schedule_rebake(0.2, force=True)
        pick.tag_anim_editors()
        return {'FINISHED'}

    def invoke(self, context, event):
        from .ops_transform import _plane_hit, _view_dir
        src = self._sources(context)
        if not src and self.middle:
            self.report({'WARNING'}, self.MIDDLE_HINT)
            return {'CANCELLED'}
        if not src or context.region_data is None:
            return {'PASS_THROUGH'}
        # a good default: the length of the last segment
        c, k, sign = src[0]
        i = c.keys.index(k)
        j = i - int(sign)
        if 0 <= j < len(c.keys):
            self.frames = max(1, int(round(abs(k.t - c.keys[j].t))))
        self.snaps = []
        for c, _k, _s in src:
            tg = c.target()
            self.snaps.append((c, core.FCurveSnapshot(tg, tg.channels(c.ptype)), c.fs, list(c.B)))
        self.pivot = src[0][1].co.copy()
        self.sign = src[0][2]
        self.m0 = Vector((event.mouse_region_x, event.mouse_region_y))
        self.offset = (0.0, 0.0, 0.0)
        self._plane = (_plane_hit, _view_dir)
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
        for c in state.caches.values():
            c.dirty = True
            c.refresh()

    def _update(self, context, event):
        plane_hit, view_dir = self._plane
        region, rv3d = context.region, context.region_data
        m = Vector((event.mouse_region_x, event.mouse_region_y))
        n = view_dir(rv3d)
        a = plane_hit(region, rv3d, self.m0, self.pivot, n)
        b = plane_hit(region, rv3d, m, self.pivot, n)
        self.offset = tuple(b - a) if a is not None and b is not None else (0.0, 0.0, 0.0)
        self._restore()
        self._apply(context)
        context.area.header_text_set("Extrude: new key %d frame%s %s   wheel / PgUp PgDn: frames   "
                                     "mouse: position   click: confirm   Esc: cancel" % (
                                         self.frames, "" if self.frames == 1 else "s",
                                         "later" if self.sign > 0 else "earlier"))
        pick.tag_anim_editors()
        state.tag_redraw()

    def modal(self, context, event):
        t, v = event.type, event.value
        if t in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            self._update(context, event)
        elif t in {'WHEELUPMOUSE', 'PAGE_UP', 'NUMPAD_PLUS'} and v == 'PRESS':
            self.frames += 1
            self._update(context, event)
        elif t in {'WHEELDOWNMOUSE', 'PAGE_DOWN', 'NUMPAD_MINUS'} and v == 'PRESS':
            self.frames = max(1, self.frames - 1)
            self._update(context, event)
        elif t in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER', 'SPACE'} and v == 'PRESS':
            self._finish(context)
            state.schedule_rebake(0.2, force=True)
            return {'FINISHED'}
        elif t in {'RIGHTMOUSE', 'ESC'} and v == 'PRESS':
            self._restore()
            self._finish(context)
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}

    def _finish(self, context):
        state.modal_running = max(0, state.modal_running - 1)
        state.force_numbers = False
        context.area.header_text_set(None)
        state.tag_redraw()


# ----------------------------------------------------------------------------
# Loops
# ----------------------------------------------------------------------------

class EMP_OT_toggle_cycles(bpy.types.Operator):
    """Make the motion loop (Cycles modifier on its F-Curves), or stop looping"""
    bl_idname = "emp.toggle_cycles"
    bl_label = "Toggle Cycles"
    bl_options = {'REGISTER', 'UNDO'}

    all_channels: BoolProperty(name="Whole Pose", default=True,
                               description="Loop every channel of the bone / object, not only the "
                                           "ones of the current mode")

    @classmethod
    def description(cls, context, properties):
        if properties.all_channels:
            return "Make the whole pose of the bone / object loop (Cycles), or stop looping"
        return "Make only the channels of the current mode loop (Cycles), or stop looping"

    def execute(self, context):
        caches = state.editable_caches(context.scene)
        if not caches:
            return {'CANCELLED'}
        groups = [(_pose_fcurves(c.target(), c, self.all_channels), c) for c in caches]
        on = any(m.type == 'CYCLES' for fcs, _c in groups for fc in fcs for m in fc.modifiers)
        for fcs, c in groups:
            for fc in fcs:
                if on:
                    for m in [m for m in fc.modifiers if m.type == 'CYCLES']:
                        fc.modifiers.remove(m)
                elif not any(m.type == 'CYCLES' for m in fc.modifiers):
                    fc.modifiers.new('CYCLES')
                fc.update()
            c.dirty = True
        self.report({'INFO'}, "Looping off" if on else "Looping on (Cycles)")
        state.schedule_rebake(0.1, force=True)
        pick.tag_anim_editors()
        return {'FINISHED'}


class EMP_MT_loop(bpy.types.Menu):
    bl_label = "Loop"
    bl_idname = "EMP_MT_loop"

    @classmethod
    def poll(cls, context):
        return bool(state.editable_caches(context.scene))

    def draw(self, context):
        layout = self.layout
        layout.operator("emp.toggle_cycles", text="Toggle Cycles (Whole Pose)", icon='FILE_REFRESH').all_channels = True
        mode = state.edit_mode(context.scene).title()
        layout.operator("emp.toggle_cycles", text="Toggle Cycles (%s Only)" % mode,
                        icon='FILE_REFRESH').all_channels = False
        layout.operator("emp.fix_loop", text="Fix Loop Seam", icon='LOOP_BACK')


class EMP_OT_loop_menu(bpy.types.Operator):
    """Loop menu: toggle Cycles, fix the loop seam (Alt+C)"""
    bl_idname = "emp.loop_menu"
    bl_label = "Loop Menu"

    def invoke(self, context, event):
        if not state.editable_caches(context.scene):
            return {'PASS_THROUGH'}
        bpy.ops.wm.call_menu(name="EMP_MT_loop")
        return {'FINISHED'}


class EMP_OT_interpolation_menu(bpy.types.Operator):
    """Set the interpolation of the selected path keys (T)"""
    bl_idname = "emp.interpolation_menu"
    bl_label = "Interpolation Menu"

    def invoke(self, context, event):
        if not any(k.sel for c in state.editable_caches(context.scene) for k in c.keys):
            return {'PASS_THROUGH'}
        bpy.ops.wm.call_menu(name="EMP_MT_interpolation")
        return {'FINISHED'}


classes = (EMP_OT_delete_keys, EMP_OT_dissolve_keys, EMP_OT_delete_menu, EMP_MT_delete,
           EMP_OT_insert_key, EMP_OT_subdivide, EMP_OT_loopcut, EMP_OT_knife, EMP_OT_merge,
           EMP_MT_merge, EMP_OT_duplicate, EMP_OT_duplicate_keys, EMP_OT_duplicate_move,
           EMP_OT_extrude, EMP_OT_copy_keys, EMP_OT_paste_keys, EMP_OT_toggle_cycles, EMP_MT_loop,
           EMP_OT_loop_menu, EMP_OT_interpolation_menu)
