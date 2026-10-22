"""Edit Value: type exact values for a key, in a small popup under the mouse."""

import bpy
from bpy.props import (BoolProperty, BoolVectorProperty, FloatVectorProperty, IntProperty,
                       IntVectorProperty, StringProperty)

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
                col.prop(self, prop, index=i, text="" if self.count == 1 else "XYZW"[i])
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


classes = (EMP_OT_edit_value,)
