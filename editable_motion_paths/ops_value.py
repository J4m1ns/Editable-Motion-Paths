"""Edit Value: type exact values for a key, in a small popup under the mouse."""

import bpy
from bpy.props import (BoolProperty, BoolVectorProperty, FloatVectorProperty, IntProperty,
                       IntVectorProperty, StringProperty)

from mathutils import Vector

from . import core, edit, pick, space, state


def _source(context):
    caches = state.editable_caches(context.scene)
    a = space.active_key(caches)
    if a is not None:
        return a
    for c in caches:
        for k in c.keys:
            if k.sel:
                return c, k
    return None


def _value_info(tg):
    """(kind, count, current values) of the property behind a Value path."""
    ch = tg.value
    try:
        cur = tg.obj.path_resolve(ch.path)
    except (ValueError, AttributeError):
        cur = ch.static[0]
    try:
        vals = list(cur)
    except TypeError:
        vals = [cur]
    vals = vals[:4]
    first = vals[0] if vals else 0.0
    kind = 'BOOL' if isinstance(first, bool) else ('INT' if isinstance(first, int) else 'FLOAT')
    return kind, len(vals), vals


class EMP_OT_edit_value(bpy.types.Operator):
    """Type exact values for the active key (location, rotation, scale or the animated value)"""
    bl_idname = "emp.edit_value"
    bl_label = "Edit Value"
    bl_options = {'REGISTER', 'UNDO'}

    location: FloatVectorProperty(name="Location", size=3, subtype='TRANSLATION', unit='LENGTH')
    euler: FloatVectorProperty(name="Rotation", size=3, subtype='EULER')
    quaternion: FloatVectorProperty(name="Rotation", size=4, subtype='QUATERNION', default=(1, 0, 0, 0))
    axis_angle: FloatVectorProperty(name="Rotation", size=4, subtype='AXISANGLE', default=(0, 0, 1, 0))
    scale: FloatVectorProperty(name="Scale", size=3, subtype='XYZ', default=(1, 1, 1))
    floats: FloatVectorProperty(name="Value", size=4)
    ints: IntVectorProperty(name="Value", size=4)
    bools: BoolVectorProperty(name="Value", size=4)
    kind: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    count: IntProperty(default=1, options={'HIDDEN', 'SKIP_SAVE'})
    all_selected: BoolProperty(name="All Selected Keys", default=False,
                               description="Set these values on every selected key, not only the "
                                           "active one")

    @classmethod
    def poll(cls, context):
        return context.mode in {'POSE', 'OBJECT'}

    def invoke(self, context, event):
        src = _source(context)
        if src is None:
            self.report({'WARNING'}, "Select a key first")
            return {'CANCELLED'}
        c, k = src
        tg = c.target()
        vals = list(k.vals)
        if c.ptype == core.LOCATION:
            self.kind, self.location = 'LOCATION', vals[:3]
        elif c.ptype == core.SCALE:
            self.kind, self.scale = 'SCALE', vals[:3]
        elif c.ptype == core.ROTATION:
            if tg.rot_mode == 'QUATERNION':
                self.kind, self.quaternion = 'QUATERNION', vals[:4]
            elif tg.rot_mode == 'AXIS_ANGLE':
                self.kind, self.axis_angle = 'AXIS_ANGLE', vals[:4]
            else:
                self.kind, self.euler = 'EULER', vals[:3]
        else:
            kind, n, cur = _value_info(tg)
            ch = tg.value
            # the animated components at this key, the others as they are
            for i in range(n):
                fc = tg.coll.find(ch.path, index=i) if tg.coll is not None else None
                if fc is not None and len(fc.keyframe_points):
                    cur[i] = fc.evaluate(k.t)
            self.kind, self.count = kind, n
            if kind == 'BOOL':
                self.bools = [bool(round(v)) for v in cur] + [False] * (4 - n)
            elif kind == 'INT':
                self.ints = [int(round(v)) for v in cur] + [0] * (4 - n)
            else:
                self.floats = [float(v) for v in cur] + [0.0] * (4 - n)
        self.frame = k.t
        try:
            return context.window_manager.invoke_props_dialog(self, width=280, title="Key at frame %g" % k.t)
        except TypeError:
            return context.window_manager.invoke_props_dialog(self, width=280)

    def draw(self, context):
        layout = self.layout
        kind = self.kind
        if kind in {'LOCATION', 'EULER', 'QUATERNION', 'AXIS_ANGLE', 'SCALE'}:
            prop = {'LOCATION': "location", 'EULER': "euler", 'QUATERNION': "quaternion",
                    'AXIS_ANGLE': "axis_angle", 'SCALE': "scale"}[kind]
            layout.column(align=True).prop(self, prop, text="")
        else:
            prop = {'BOOL': "bools", 'INT': "ints"}.get(kind, "floats")
            col = layout.column(align=True)
            for i in range(max(1, self.count)):
                if kind == 'BOOL':
                    # a lone checkbox says nothing: it switches the value on
                    text = "Active" if self.count <= 1 else "Active %s" % "XYZW"[i]
                else:
                    text = "" if self.count == 1 else "XYZW"[i]
                col.prop(self, prop, index=i, text=text)
        layout.prop(self, "all_selected")

    def _values(self):
        kind = self.kind
        if kind == 'LOCATION':
            return list(self.location)
        if kind == 'SCALE':
            return list(self.scale)
        if kind == 'EULER':
            return list(self.euler)
        if kind == 'QUATERNION':
            return list(self.quaternion)
        if kind == 'AXIS_ANGLE':
            return list(self.axis_angle)
        n = max(1, self.count)
        if kind == 'BOOL':
            return [1.0 if b else 0.0 for b in self.bools[:n]]
        if kind == 'INT':
            return [float(v) for v in self.ints[:n]]
        return list(self.floats[:n])

    def execute(self, context):
        if not self.kind:
            return {'CANCELLED'}
        vals = self._values()
        done = 0
        for c in state.editable_caches(context.scene):
            ks = [k for k in c.keys if k.sel]
            if not self.all_selected:
                a = space.active_key([c])
                ks = [a[1]] if a is not None else ks[:1]
            if not ks:
                continue
            if c.ptype == core.VALUE:
                done += self._set_value(c, [k.t for k in ks], vals)
                continue
            tg, fresh = edit.prepare(context, c, [k.t for k in ks])
            w = edit.Writer([k.t for k in fresh])
            ch = tg.channels(c.ptype)
            for k in fresh:
                v = vals[:ch.count]
                if len(v) == ch.count:
                    core.write_key(ch, k, v, True, w.touched)
                    done += 1
            w.done([c])
        if not done:
            return {'CANCELLED'}
        pick.tag_anim_editors()
        return {'FINISHED'}

    @staticmethod
    def _set_value(c, frames, vals):
        """Key every animated component of the value property at `frames`."""
        tg = c.target()
        path = tg.value.path
        n = 0
        for i, v in enumerate(vals):
            fc = tg.coll.find(path, index=i) if tg.coll is not None else None
            if fc is None:
                continue
            for t in frames:
                x = core.wrap_time(t, core.cycle_info(fc))
                kp = next((kp for kp in fc.keyframe_points if abs(kp.co[0] - x) < 1e-3), None)
                if kp is None:
                    fc.keyframe_points.insert(x, v)
                else:
                    d = v - kp.co[1]
                    kp.co = (kp.co[0], v)
                    kp.handle_left = (kp.handle_left[0], kp.handle_left[1] + d)
                    kp.handle_right = (kp.handle_right[0], kp.handle_right[1] + d)
                n += 1
            fc.update()
        c.dirty = True
        return n


class EMP_OT_scale_whisker(bpy.types.Operator):
    """Drag a scale handle (whisker): along the path changes its length in time, away from the """ \
        """path changes the value"""
    bl_idname = "emp.scale_whisker"
    bl_label = "Edit Scale Handle"
    bl_options = {'REGISTER', 'UNDO', 'BLOCKING'}

    frame: bpy.props.FloatProperty(options={'HIDDEN'})
    part: StringProperty(options={'HIDDEN'})

    def _cache(self, context):
        for c in state.editable_caches(context.scene):
            if c.ptype == core.SCALE:
                for k in c.keys:
                    if abs(k.t - self.frame) < 1e-3:
                        return c, k
        return None, None

    def invoke(self, context, event):
        c, k = self._cache(context)
        st = state.settings(context.scene)
        ch = {'X': 0, 'Y': 1, 'Z': 2}.get(st.scale_channel)
        if c is None or ch is None:
            return {'CANCELLED'}
        self.ch = ch
        tg = c.target()
        fc = tg.scale.fcurves[ch]
        if fc is None or k.idx[ch] < 0:
            return {'CANCELLED'}
        self.snap = core.FCurveSnapshot(tg, tg.scale)
        self.side = self.part[1]
        self.sign = 1.0 if self.part[2] == '+' else -1.0
        kp = fc.keyframe_points[k.idx[ch]]
        self.x, self.y = kp.co
        self.idx = k.idx[ch]
        lo = [kk.t for kk in c.keys if kk.t < k.t - 1e-3]
        hi = [kk.t for kk in c.keys if kk.t > k.t + 1e-3]
        self.limit = (lo[-1] if lo else c.fs, hi[0] if hi else c.fe)
        self.cache = c
        context.window_manager.modal_handler_add(self)
        state.modal_running += 1
        return {'RUNNING_MODAL'}

    def _update(self, context, event):
        from bpy_extras.view3d_utils import location_3d_to_region_2d as proj
        from . import draw
        c = self.cache
        tg = c.target()
        region, rv3d = context.region, context.region_data
        m = Vector((event.mouse_region_x, event.mouse_region_y))
        st = state.settings(context.scene)
        half = draw.ribbon_half(st, c)
        # the frame under the mouse along the path, on this handle's side of the key
        a, b = (self.limit[0], self.x) if self.side == 'L' else (self.x, self.limit[1])
        best = None
        n = 48
        for i in range(n + 1):
            t = a + (b - a) * i / n
            s = proj(region, rv3d, c.head(tg, t))
            if s is not None:
                d = (s - m).length
                if best is None or d < best[0]:
                    best = (d, t)
        if best is None:
            return
        t = best[1]
        dt = max(abs(t - self.x), 0.05)
        base = c.head(tg, t)
        ax = c.axes(tg, t)
        y_rib = self.ch == 1 and st.scale_y_display == 'RIBBON'
        if y_rib:
            base, across = draw.y_ribbon(st, c, tg, t)
            a3 = across * self.sign
        else:
            a3 = ax[2] if self.ch == 1 else ax[self.ch] * self.sign
        s0 = proj(region, rv3d, base)
        s1 = proj(region, rv3d, base + a3 * half)
        if s0 is None or s1 is None or (s1 - s0).length < 1e-6:
            return
        u = (m - s0).dot(s1 - s0) / (s1 - s0).length_squared     # in units of the ribbon half width
        if self.ch == 1 and not y_rib:
            v = self.y + u
        else:
            v = u if self.y >= 0.0 else -u
        slope = (v - self.y) / (dt if self.side == 'R' else -dt)
        fc = tg.scale.fcurves[self.ch]
        kp = fc.keyframe_points[self.idx]
        if kp.handle_left_type in {'AUTO', 'AUTO_CLAMPED', 'ALIGNED'} and \
                kp.handle_right_type in {'AUTO', 'AUTO_CLAMPED', 'ALIGNED'}:
            # smooth key: the other handle stays in line
            other = kp.handle_left if self.side == 'R' else kp.handle_right
            odt = abs(other[0] - self.x)
            kp.handle_left_type = kp.handle_right_type = 'ALIGNED'
            if self.side == 'R':
                kp.handle_left = (self.x - odt, self.y - slope * odt)
            else:
                kp.handle_right = (self.x + odt, self.y + slope * odt)
        elif self.side == 'R':
            kp.handle_right_type = 'FREE'
        else:
            kp.handle_left_type = 'FREE'
        if self.side == 'R':
            kp.handle_right = (self.x + dt, self.y + slope * dt)
        else:
            kp.handle_left = (self.x - dt, self.y - slope * dt)
        fc.update()
        c.dirty = True
        context.area.header_text_set("Scale handle: %d frame%s, value %.3f   click: confirm, Esc: cancel" % (
            round(dt), "" if round(dt) == 1 else "s", v))
        pick.tag_anim_editors()

    def modal(self, context, event):
        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            self._update(context, event)
        elif event.type == 'LEFTMOUSE' and event.value in {'RELEASE', 'PRESS'}:
            return self._end(context, True)
        elif event.type in {'RET', 'NUMPAD_ENTER', 'SPACE'} and event.value == 'PRESS':
            return self._end(context, True)
        elif event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            return self._end(context, False)
        return {'RUNNING_MODAL'}

    def _end(self, context, ok):
        state.modal_running = max(0, state.modal_running - 1)
        context.area.header_text_set(None)
        if not ok:
            self.snap.restore()
            self.cache.dirty = True
            pick.tag_anim_editors()
            return {'CANCELLED'}
        return {'FINISHED'}


classes = (EMP_OT_edit_value, EMP_OT_scale_whisker)
