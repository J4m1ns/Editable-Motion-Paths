"""Key topology operators: delete, dissolve, insert (like vertices of a curve)."""

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
    """Cut the paths with a line: keys are inserted where it crosses them, the motion stays """ \
        """(K). Click to add points or drag to draw, Enter / Space: cut, Esc: cancel"""
    bl_idname = "emp.knife"
    bl_label = "Knife Path"
    bl_options = {'REGISTER', 'UNDO'}

    whole_frames: BoolProperty(name="Whole Frames", default=True,
                               description="Round the cuts to whole frames")

    def invoke(self, context, event):
        if context.region_data is None or not state.editable_caches(context.scene):
            return {'PASS_THROUGH'}
        self.pts = []
        self.cur = Vector((event.mouse_region_x, event.mouse_region_y))
        self.drawing = False
        context.window_manager.modal_handler_add(self)
        context.window.cursor_modal_set('KNIFE')
        self._show(context)
        return {'RUNNING_MODAL'}

    def _crossings(self, context):
        from bpy_extras.view3d_utils import location_3d_to_region_2d
        region, rv3d = context.region, context.region_data
        line = self.pts + ([self.cur] if self.pts else [])
        out = []
        for c in state.editable_caches(context.scene):
            proj = [location_3d_to_region_2d(region, rv3d, p) for p in c.points]
            frames = set()
            for i in range(len(proj) - 1):
                p, q = proj[i], proj[i + 1]
                if p is None or q is None:
                    continue
                for a, b in zip(line, line[1:]):
                    hit = _seg_cross(p, q, a, b)
                    if hit is not None:
                        t = c.times[i] + (c.times[i + 1] - c.times[i]) * hit[0]
                        frames.add(round(t) if self.whole_frames else round(t, 2))
            have = {round(k.t, 3) for k in c.keys}
            fs = sorted(f for f in frames if round(float(f), 3) not in have)
            out.append((c, [float(f) for f in fs]))
        return out

    def _show(self, context):
        cross = self._crossings(context) if len(self.pts) >= 1 else []
        tgs = []
        for c, fs in cross:
            tg = c.target()
            tgs += [c.point(tg, f) for f in fs]
        state.knife = ([p.copy() for p in self.pts] + [self.cur.copy()], context.area.as_pointer())
        state.markers = (tgs, context.area.as_pointer())
        n = len(tgs)
        context.area.header_text_set("Knife: click points or drag   Enter / Space: cut (%d new key%s)   "
                                     "Esc: cancel" % (n, "" if n == 1 else "s"))
        state.tag_redraw()

    def modal(self, context, event):
        t, v = event.type, event.value
        if t in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            self.cur = Vector((event.mouse_region_x, event.mouse_region_y))
            if self.drawing and (not self.pts or (self.cur - self.pts[-1]).length > 8.0):
                self.pts.append(self.cur.copy())
            self._show(context)
        elif t == 'LEFTMOUSE':
            if v == 'PRESS':
                self.pts.append(self.cur.copy())
                self.drawing = True
            elif v == 'RELEASE':
                self.drawing = False
            self._show(context)
        elif t in {'RET', 'NUMPAD_ENTER', 'SPACE'} and v == 'PRESS':
            return self._end(context, True)
        elif t in {'ESC', 'RIGHTMOUSE'} and v == 'PRESS':
            return self._end(context, False)
        return {'RUNNING_MODAL'}

    def _end(self, context, confirm):
        cross = self._crossings(context) if confirm and self.pts else []
        state.knife = None
        state.markers = None
        context.window.cursor_modal_restore()
        context.area.header_text_set(None)
        state.tag_redraw()
        n = 0
        if cross:
            pick.deselect_all(state.editable_caches(context.scene))
        for c, fs in cross:
            c.refresh(force=True)
            n += _insert_and_select(c, fs)
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
    """Copy the pose of the active key (Ctrl+C). Ctrl+V pastes it on the current frame"""
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
    """Paste the copied key pose on the current frame (Ctrl+V)"""
    bl_idname = "emp.paste_keys"
    bl_label = "Paste Key Pose"
    bl_options = {'REGISTER', 'UNDO'}

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
        self.report({'INFO'}, "Pasted the pose of frame %g on frame %g" % (_clipboard["frame"], frame))
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

    def _sources(self, context):
        out = []
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
                out.append((c, ks[sel[-1]], 1.0))
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
                    kp = fc.keyframe_points.insert(t_new, fc.evaluate(t_new), options={'FAST'})
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
            self.report({'WARNING'}, "Select the first or last key of a path to extrude "
                                     "(not on looping paths)")
            return {'CANCELLED'}
        state.schedule_rebake(0.2, force=True)
        pick.tag_anim_editors()
        return {'FINISHED'}

    def invoke(self, context, event):
        from .ops_transform import _plane_hit, _view_dir
        src = self._sources(context)
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
                                           "edited ones")

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

    def draw(self, context):
        layout = self.layout
        layout.operator("emp.toggle_cycles", icon='FILE_REFRESH')
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
