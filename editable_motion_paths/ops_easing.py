"""Easing presets: put an easing curve (Sine, Cubic, Back, Bounce...) on the selected part of a
path. Keep the Path (default) retimes along the path, its shape stays; Channels sets Blender's
easing interpolation on the chosen channels, like in the Graph Editor."""

import os

import bpy
from bpy.props import BoolProperty, BoolVectorProperty, EnumProperty, FloatProperty, IntProperty

from . import core, easing, pick, state

_previews = None


def icon_id(name):
    """Icon id of one of the add-on's PNG icons (0 if it's missing)."""
    if _previews is None or name not in _previews:
        return 0
    return _previews[name].icon_id


def register_icons():
    global _previews
    import bpy.utils.previews
    _previews = bpy.utils.previews.new()
    folder = os.path.join(os.path.dirname(__file__), "icons")
    if os.path.isdir(folder):
        for f in sorted(os.listdir(folder)):
            if f.endswith(".png"):
                _previews.load(f[:-4], os.path.join(folder, f), 'IMAGE')


def unregister_icons():
    global _previews
    if _previews is not None:
        import bpy.utils.previews
        bpy.utils.previews.remove(_previews)
        _previews = None


MODES = [
    ('TIMING', "Keep the Path", "Retime along the path: the shape stays, only where the bone is on "
                                "each frame changes (Location and Rotation paths)"),
    ('CHANNELS', "Channels", "Blender's easing interpolation on the chosen channels, like in the "
                             "Graph Editor (the shape of the path changes too)"),
]


def _ranges(cache):
    """(first, last) frame to ease: the selected keys, the segments around a single selected
    key, or the whole path."""
    ks = [k.t for k in cache.keys]
    sel = [k.t for k in cache.keys if k.sel]
    if len(sel) >= 2:
        return min(sel), max(sel)
    if len(sel) == 1 and len(ks) > 1:
        i = ks.index(sel[0])
        return ks[max(i - 1, 0)], ks[min(i + 1, len(ks) - 1)]
    return (ks[0], ks[-1]) if len(ks) > 1 else None


class EMP_OT_easing_preset(bpy.types.Operator):
    """Put this easing on the selected part of the path (the selected keys; one key: the """ \
        """segments around it; none: the whole path)"""
    bl_idname = "emp.easing_preset"
    bl_label = "Easing Preset"
    bl_options = {'REGISTER', 'UNDO'}

    easing: EnumProperty(name="Easing", items=[(i, n, "") for i, n in easing.TYPES], default='SINE')
    direction: EnumProperty(name="Direction", items=[(i, n, "") for i, n in easing.DIRECTIONS],
                            default='EASE_IN_OUT')
    mode: EnumProperty(name="Mode", items=MODES, default='TIMING')
    channels: BoolVectorProperty(name="Channels", size=4, default=(True, True, True, True),
                                 description="Channels mode: which channels get the easing")
    strength: FloatProperty(name="Strength", default=1.0, min=0.0, max=1.0, subtype='FACTOR',
                            description="Keep the Path: how much of the easing (0: the timing as it was)")
    back: FloatProperty(name="Overshoot", default=1.70158, min=0.0, soft_max=5.0,
                        description="Back: how far it goes past the end")
    amplitude: FloatProperty(name="Amplitude", default=1.0, min=0.0, soft_max=3.0,
                             description="Elastic: how big the swings are")
    period: FloatProperty(name="Period", default=0.3, min=0.05, soft_max=1.0,
                          description="Elastic: how long a swing is (share of the part)")
    bounces: IntProperty(name="Bounces", default=3, min=1, max=10,
                         description="Bounce: how many times it bounces (Keep the Path)")
    bounciness: FloatProperty(name="Bounciness", default=0.5, min=0.05, max=0.9, subtype='FACTOR',
                              description="Bounce: how much of each bounce is left for the next one "
                                          "(Keep the Path)")

    @classmethod
    def description(cls, context, properties):
        name = dict(easing.TYPES).get(properties.easing, "")
        d = dict(easing.DIRECTIONS).get(properties.direction, "")
        how = ("keeping the path (only the timing changes)" if properties.mode == 'TIMING'
               else "on the chosen channels (Blender's easing interpolation)")
        return "%s %s on the selected part of the path, %s" % (name, d, how)

    @classmethod
    def poll(cls, context):
        return context.mode in {'POSE', 'OBJECT'} and bool(state.editable_caches(context.scene))

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, "easing")
        layout.prop(self, "direction")
        layout.prop(self, "mode")
        if self.mode == 'TIMING':
            layout.prop(self, "strength")
        else:
            labels = channel_labels(context) or ["1", "2", "3", "4"]
            row = layout.row(align=True, heading="Channels")
            for i, label in enumerate(labels[:4]):
                row.prop(self, "channels", index=i, text=label, toggle=True)
        if self.easing == 'BACK':
            layout.prop(self, "back")
        if self.easing == 'ELASTIC':
            layout.prop(self, "amplitude")
            layout.prop(self, "period")
        if self.easing == 'BOUNCE':
            col = layout.column()
            col.active = self.mode == 'TIMING'      # (Blender's own Bounce has no settings)
            col.prop(self, "bounces")
            col.prop(self, "bounciness")

    def _fn(self):
        kind, d = self.easing, self.direction
        p = (self.back, self.amplitude, self.period, self.bounces, self.bounciness)
        return lambda u: easing.ease(kind, d, u, *p)

    def execute(self, context):
        done = 0
        moved = []
        for cache in state.editable_caches(context.scene):
            rng = _ranges(cache)
            if rng is None:
                continue
            if self.mode == 'TIMING' and cache.ptype in (core.LOCATION, core.ROTATION):
                done += self._timing(context, cache, rng)
            else:
                if self.mode == 'TIMING':
                    moved.append(cache.label)
                done += self._channels(cache, rng)
            cache.dirty = True
        if moved:
            self.report({'INFO'}, "Keep the Path works on Location / Rotation paths: the channels got the "
                                  "easing instead (%s)" % ", ".join(moved))
        if not done:
            self.report({'WARNING'}, "Select keys of a path (2 or more: the part between them)")
            return {'CANCELLED'}
        pick.tag_anim_editors()
        state.schedule_rebake(0.3)
        return {'FINISHED'}

    def _timing(self, context, cache, rng):
        from .ops_timing import SpaceJob, refit
        r0, r1 = float(rng[0]), float(rng[1])
        if r1 - r0 < 2.0:
            return 0
        tg = cache.target()
        if core.ensure_keys(tg, cache.ptype, [r0, r1], None, state.settings(context.scene).pin_partial):
            cache.refresh(force=True)
            tg = cache.target()
        job = SpaceJob(cache, tg, r0, r1)
        total = job.arc[-1]
        if total < 1e-9:
            return 0
        fn = self._fn()
        w = self.strength
        pts, vals = job.pts, job.vals
        d0 = (pts[1] - pts[0]).normalized() if len(pts) > 1 else None
        d1 = (pts[-1] - pts[-2]).normalized() if len(pts) > 1 else None

        def target(t):
            u = (t - r0) / (r1 - r0)
            s0 = job._at(job.arc, t)
            s = s0 + (total * fn(u) - s0) * w
            if s > total and d1 is not None:
                # Back / Elastic overshoot: on along the direction of the motion
                return cache.solve(tg, t, pts[-1] + d1 * (s - total), vals[-1])
            if s < 0.0 and d0 is not None:
                return cache.solve(tg, t, pts[0] + d0 * s, vals[0])
            tt = job._time_at_arc(s)
            return cache.solve(tg, t, job._at(pts, tt), job._at(vals, tt))
        sel = {round(k.t, 3) for k in cache.keys if k.sel}
        core.update_fcurves(refit(job, tg, target, sel or True))
        cache.refresh(force=True)
        return 1

    def _channels(self, cache, rng):
        tg = cache.target()
        ch = tg.channels(cache.ptype)
        use = list(self.channels)
        if not any(use[:ch.count]):
            use = [True] * 4          # (at least one channel: like the modifier channels)
        sel = [k for k in cache.keys if k.sel]
        if len(sel) >= 2:
            starts = sel[:-1]
        elif len(sel) == 1:
            starts = sel
        else:
            starts = cache.keys[:-1]
        touched = set()
        times = [kk.t for kk in cache.keys]
        for k in starts:
            i = times.index(k.t)
            duration = times[i + 1] - k.t if i + 1 < len(times) else 10.0
            for c, fc in enumerate(ch.fcurves):
                if fc is None or c >= 4 or not use[c] or k.idx[c] < 0:
                    continue
                kp = fc.keyframe_points[k.idx[c]]
                kp.interpolation = self.easing
                kp.easing = self.direction
                if self.easing == 'BACK':
                    kp.back = self.back
                if self.easing == 'ELASTIC':
                    kp.amplitude = self.amplitude
                    kp.period = self.period * duration     # (Blender's period is in frames)
                touched.add(fc)
        core.update_fcurves(touched)
        return 1 if touched else 0


def channel_labels(context):
    from . import ops_fmods
    cache = ops_fmods.active_cache(context)
    return [label for label, _fc in ops_fmods.channels(cache)] if cache is not None else []


def draw_presets(layout, context):
    """The easing library: one big picture per easing, for the direction chosen above it."""
    st = state.settings(context.scene)
    layout.operator_context = 'EXEC_REGION_WIN'
    row = layout.row()
    row.prop(st, "easing_mode", expand=True)
    row = layout.row()
    row.prop(st, "easing_direction", expand=True)
    if st.easing_mode == 'CHANNELS':
        labels = channel_labels(context)
        if labels:
            row = layout.row(align=True)
            row.label(text="Channels:")
            for i, label in enumerate(labels[:4]):
                row.prop(st, "easing_channels", index=i, text=label, toggle=True)
    else:
        layout.prop(st, "easing_strength", slider=True)
    grid = layout.grid_flow(row_major=True, columns=5, even_columns=True, even_rows=True, align=False)
    for kind, name in easing.TYPES:
        col = grid.column(align=True)
        ic = icon_id("ease_%s_%s" % (kind, st.easing_direction))
        if ic:
            col.template_icon(icon_value=ic, scale=3.0)
        op = col.operator("emp.easing_preset", text=name)
        op.easing = kind
        op.direction = st.easing_direction
        op.mode = st.easing_mode
        op.channels = st.easing_channels
        op.strength = st.easing_strength


class EMP_PT_easing_presets(bpy.types.Panel):
    """Easing presets for the selected part of the path"""
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'HEADER'
    bl_label = "Easing Presets"
    bl_idname = "EMP_PT_easing_presets"
    bl_ui_units_x = 22

    def draw(self, context):
        draw_presets(self.layout, context)


class EMP_PT_easing_sidebar(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Motion Paths"
    bl_label = "Easing Presets"
    bl_parent_id = "EMP_PT_timing"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        draw_presets(self.layout, context)


classes = (EMP_OT_easing_preset,)
