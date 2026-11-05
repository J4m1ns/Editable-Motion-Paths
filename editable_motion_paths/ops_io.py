"""Paths to curves and back: export a path as a Curve object, or make a path follow a curve
(drawn with the Draw tool, or an exported path edited as a curve)."""

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty
from mathutils import Vector

from . import core, edit, fit, geom, keys, pick, state


# ----------------------------------------------------------------------------
# Export
# ----------------------------------------------------------------------------

class EMP_OT_export_curve(bpy.types.Operator):
    """Make a Curve object from the paths of the selected bones / objects"""
    bl_idname = "emp.export_curve"
    bl_label = "Path to Curve"
    bl_options = {'REGISTER', 'UNDO'}

    kind: EnumProperty(name="Points", items=[
        ('KEYS', "Keys (Bezier)", "One Bezier point per key, with the key's handles"),
        ('FRAMES', "Frames (Poly)", "One point per frame: exactly the path"),
    ], default='KEYS')

    @classmethod
    def poll(cls, context):
        return context.mode in {'POSE', 'OBJECT'}

    def execute(self, context):
        caches = state.editable_caches(context.scene)
        if not caches:
            self.report({'WARNING'}, "Select a bone / object that has a path")
            return {'CANCELLED'}
        made = []
        for c in caches:
            tg = c.target()
            if tg is None or not c.points:
                continue
            cu = bpy.data.curves.new("Path " + c.label, 'CURVE')
            cu.dimensions = '3D'
            if self.kind == 'KEYS' and len(c.keys) >= 2:
                sp = cu.splines.new('BEZIER')
                sp.bezier_points.add(len(c.keys) - 1)
                for bp, k in zip(sp.bezier_points, c.keys):
                    bp.co = k.co
                    bp.handle_left_type = bp.handle_right_type = 'FREE'
                    bp.handle_left = k.hl if k.hl is not None else k.co - (k.cat_r if k.cat_r.length else Vector())
                    bp.handle_right = k.hr if k.hr is not None else k.co + k.cat_r
                frames = [round(k.t, 3) for k in c.keys]
            else:
                pts = [(t, p) for t, p in zip(c.times, c.points) if abs(t - round(t)) < 1e-6]
                sp = cu.splines.new('POLY')
                sp.points.add(len(pts) - 1)
                for q, (_t, p) in zip(sp.points, pts):
                    q.co = (p.x, p.y, p.z, 1.0)
                frames = [round(t, 3) for t, _p in pts]
            ob = bpy.data.objects.new("Path " + c.label, cu)
            ob["emp_frames"] = frames
            ob["emp_source"] = "%s|%s|%s" % c.key
            context.scene.collection.objects.link(ob)
            made.append(ob.name)
        self.report({'INFO'}, "Created " + ", ".join(made))
        return {'FINISHED'}


# ----------------------------------------------------------------------------
# Import
# ----------------------------------------------------------------------------

def curve_polyline(context, ob):
    """World points along the curve object as it is displayed (vertices of its mesh follow
    the spline)."""
    dg = context.evaluated_depsgraph_get()
    ev = ob.evaluated_get(dg)
    me = ev.to_mesh()
    try:
        if len(me.vertices) < 2:
            return None
        mw = ob.matrix_world
        pts = [mw @ v.co for v in me.vertices]
        if ob.data.splines and ob.data.splines[0].use_cyclic_u:
            pts.append(pts[0].copy())
        return pts
    finally:
        ev.to_mesh_clear()


def curve_control_points(ob):
    """[(co, handle_left, handle_right)] in world space of the first spline."""
    if ob.type != 'CURVE' or not ob.data.splines:
        return []
    sp = ob.data.splines[0]
    mw = ob.matrix_world
    if sp.type == 'BEZIER':
        return [(mw @ p.co, mw @ p.handle_left, mw @ p.handle_right) for p in sp.bezier_points]
    return [(mw @ Vector(p.co[:3]), None, None) for p in sp.points]


class EMP_OT_import_curve(bpy.types.Operator):
    """Make the path (the selected keys' range, or all of it) follow a curve object"""
    bl_idname = "emp.import_curve"
    bl_label = "Follow Curve"
    bl_options = {'REGISTER', 'UNDO'}

    curve: StringProperty(name="Curve", description="Curve object the path should follow")
    timing: EnumProperty(name="Timing", items=[
        ('KEEP', "Keep Timing", "Move along the curve with the same spacing as before (slow parts "
                                "stay slow)"),
        ('EVEN', "Even Speed", "Constant speed along the curve"),
        ('POINTS', "Points to Keys", "A key on every point of the curve (exported paths keep their "
                                     "frames)"),
    ], default='KEEP')
    reverse: BoolProperty(name="Reverse", default=False, description="Follow the curve backwards")
    tolerance: FloatProperty(name="Tolerance", subtype='PERCENTAGE', default=0.5, min=0.01,
                             soft_max=5.0, description="How closely the keys follow the curve "
                                                       "(more keys are added where needed)")

    @classmethod
    def poll(cls, context):
        return context.mode in {'POSE', 'OBJECT'}

    def invoke(self, context, event):
        if not state.editable_caches(context.scene):
            self.report({'WARNING'}, "Select a bone / object that has a path")
            return {'CANCELLED'}
        if not self.curve or self.curve not in bpy.data.objects:
            curves = [o for o in context.scene.objects if o.type == 'CURVE']
            sel = [o for o in curves if o.select_get()]
            self.curve = (sel or curves[-1:] or [None])[0].name if (sel or curves) else ""
        return context.window_manager.invoke_props_dialog(self, width=320)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop_search(self, "curve", context.scene, "objects")
        layout.prop(self, "timing")
        layout.prop(self, "reverse")
        if self.timing != 'POINTS':
            layout.prop(self, "tolerance")

    def execute(self, context):
        ob = bpy.data.objects.get(self.curve)
        if ob is None or ob.type != 'CURVE':
            self.report({'WARNING'}, "Pick a curve object")
            return {'CANCELLED'}
        done = 0
        for c in state.editable_caches(context.scene):
            tg = c.target()
            sel = [k.t for k in c.keys if k.sel]
            r0, r1 = (min(sel), max(sel)) if len(sel) >= 2 else (c.keys[0].t, c.keys[-1].t)
            if r1 - r0 < 1.0:
                continue
            if self.timing == 'POINTS':
                done += self._points(context, c, ob, r0, r1)
            else:
                done += self._fit(context, c, tg, ob, r0, r1)
        if not done:
            self.report({'WARNING'}, "Nothing to do: the path needs a range of 2+ frames")
            return {'CANCELLED'}
        state.schedule_rebake(0.2)
        pick.tag_anim_editors()
        return {'FINISHED'}

    def _fit(self, context, c, tg, ob, r0, r1):
        poly = curve_polyline(context, ob)
        if not poly:
            return 0
        if self.reverse:
            poly.reverse()
        carc = geom.cumulative(poly)
        ch = tg.channels(c.ptype)
        for fc in ch.fcurves:
            if fc is not None and len(fc.keyframe_points):
                r0 = max(r0, fc.keyframe_points[0].co[0])
                r1 = min(r1, fc.keyframe_points[-1].co[0])
        n = max(2, int((r1 - r0) / 0.1))
        ts = [r0 + (r1 - r0) * i / n for i in range(n + 1)]
        vals = [ch.values(t) for t in ts]
        pts = [c.point(tg, t, v) for t, v in zip(ts, vals)]
        parc = geom.cumulative(pts)
        P = parc[-1] or 1.0

        def at(t):
            i = min(n, max(1, int((t - r0) / (r1 - r0) * n) + 1))
            f = (t - ts[i - 1]) / (ts[i] - ts[i - 1])
            return i, min(1.0, max(0.0, f))

        def target(t):
            i, f = at(t)
            if self.timing == 'EVEN':
                u = (t - r0) / (r1 - r0)
            else:
                u = (parc[i - 1] + (parc[i] - parc[i - 1]) * f) / P
            W = geom.point_on_polyline(poly, carc, carc[-1] * u)
            ref = [a + (b - a) * f for a, b in zip(vals[i - 1], vals[i])]
            return c.solve(tg, t, W, ref)

        fitter = fit.Fitter(c, tg, float(r0), float(r1), target)
        knots = sorted({float(r0), float(r1)} | {float(k.t) for k in c.keys if r0 < k.t < r1})
        knots, slopes = fitter.refine(knots, set(), fitter.size * self.tolerance / 100.0)
        fit.write(fitter, knots, slopes)
        c.dirty = True
        return 1

    def _points(self, context, c, ob, r0, r1):
        cps = curve_control_points(ob)
        if len(cps) < 2:
            return 0
        if self.reverse:
            cps = [(p, r, l) for p, l, r in reversed(cps)]
        frames = list(ob.get("emp_frames", []))
        if len(frames) != len(cps) or self.reverse:
            frames = [float(round(r0 + (r1 - r0) * i / (len(cps) - 1))) for i in range(len(cps))]
            if len(set(frames)) != len(frames):
                frames = [r0 + (r1 - r0) * i / (len(cps) - 1) for i in range(len(cps))]
        tg = c.target()
        keys.insert_path_keys(c, tg, frames)
        c.refresh(force=True)
        # keys between the curve's points go: the curve decides the shape
        ch = tg.channels(c.ptype)
        lo, hi = frames[0], frames[-1]
        want = {round(f, 3) for f in frames}
        extra = [k for k in c.keys if lo - 1e-3 <= k.t <= hi + 1e-3 and round(k.t, 3) not in want]
        for ci, fc in enumerate(ch.fcurves):
            if fc is not None:
                keys.delete_channel(fc, [fc.keyframe_points[k.idx[ci]].co[0] for k in extra if k.idx[ci] >= 0])
        c.refresh(force=True)
        tg, fresh = edit.prepare(context, c, frames)
        by_t = {round(k.t, 3): k for k in fresh}
        w = edit.Writer(frames)
        vals = {}
        for f, (p, _l, _r) in zip(frames, cps):
            k = by_t.get(round(f, 3))
            if k is not None:
                vals[f] = w.key(c, tg, k, p, move_handles=False)
        for i, (f, (p, hl, hr)) in enumerate(zip(frames, cps)):
            k = by_t.get(round(f, 3))
            if k is None or hl is None:
                continue
            if i > 0:
                w.handle(c, tg, k, 'L', hl, vals.get(f), 'FREE', (f - frames[i - 1]) / 3.0)
            if i + 1 < len(frames):
                w.handle(c, tg, k, 'R', hr, vals.get(f), 'FREE', (frames[i + 1] - f) / 3.0)
        w.done([c])
        return 1


classes = (EMP_OT_export_curve, EMP_OT_import_curve)
