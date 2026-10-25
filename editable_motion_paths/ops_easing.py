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

    easing: EnumProperty(name="Easing", items=[('LINEAR', "Linear", "")] + [(i, n, "") for i, n in easing.TYPES],
                         default='SINE')
    gallery: BoolProperty(options={'HIDDEN', 'SKIP_SAVE'},
                          description="Pick the easing from a gallery under the mouse (P)")
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
        if properties.easing == 'LINEAR':
            return "Linear: an even speed on the selected part of the path (%s)" % (
                "keeping the path" if properties.mode == 'TIMING' else "Linear interpolation on the chosen channels")
        name = dict(easing.TYPES).get(properties.easing, "")
        d = dict(easing.DIRECTIONS).get(properties.direction, "")
        how = ("keeping the path (only the timing changes)" if properties.mode == 'TIMING'
               else "on the chosen channels (Blender's easing interpolation)")
        return "%s %s on the selected part of the path, %s" % (name, d, how)

    @classmethod
    def poll(cls, context):
        return context.mode in {'POSE', 'OBJECT'} and bool(state.editable_caches(context.scene))

    # -- the gallery under the mouse (P) ---------------------------------------------

    def invoke(self, context, event):
        if not self.gallery or context.area is None or context.region is None:
            return self.execute(context)
        us = _ui_scale()
        w, h = gallery_size(us)
        region = context.region
        x0 = min(max(event.mouse_region_x - w * 0.5, 4.0), max(region.width - w - 4.0, 4.0))
        y0 = min(max(event.mouse_region_y - h * 0.5, 4.0), max(region.height - h - 4.0, 4.0))
        state.gallery = self._g = dict(area=context.area.as_pointer(), x0=x0, y0=y0, hover=None, drag=False)
        context.window_manager.modal_handler_add(self)
        state.tag_redraw()
        return {'RUNNING_MODAL'}

    def _close(self, context, result):
        state.gallery = None
        state.tag_redraw()
        return result

    def modal(self, context, event):
        st = state.settings(context.scene)
        t, v = event.type, event.value
        g = self._g
        mx, my = event.mouse_region_x, event.mouse_region_y
        hit = gallery_hit(context, g, mx, my)
        if t in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            g['hover'] = hit
            if g['drag']:
                _set_strength(st, g, mx)
        elif t == 'LEFTMOUSE' and v == 'RELEASE':
            g['drag'] = False
        elif t == 'LEFTMOUSE' and v == 'PRESS':
            if hit is None:
                if not gallery_inside(g, mx, my):
                    return self._close(context, {'CANCELLED'})      # a click outside: close
            elif hit[0] == 'mode':
                st.easing_mode = hit[1]
            elif hit[0] == 'dir':
                st.easing_direction = hit[1]
            elif hit[0] == 'chan':
                ch = list(st.easing_channels)
                ch[hit[1]] = not ch[hit[1]]
                st.easing_channels = ch
            elif hit[0] == 'strength':
                g['drag'] = True
                _set_strength(st, g, mx)
            elif hit[0] == 'ease':
                self._close(context, None)
                self.easing = hit[1]
                self.direction = st.easing_direction
                self.mode = st.easing_mode
                self.channels = st.easing_channels
                self.strength = st.easing_strength
                return self.execute(context)
        elif t in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'} and st.easing_mode == 'TIMING':
            st.easing_strength = min(1.0, max(0.0, st.easing_strength + (0.05 if t == 'WHEELUPMOUSE' else -0.05)))
        elif t in {'ESC', 'RIGHTMOUSE', 'P'} and v == 'PRESS':
            return self._close(context, {'CANCELLED'})
        elif t in {'MIDDLEMOUSE', 'TRACKPADPAN', 'TRACKPADZOOM'}:
            return {'PASS_THROUGH'}
        state.tag_redraw()
        return {'RUNNING_MODAL'}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, "easing")
        if self.easing != 'LINEAR':
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


# ---- the gallery (P): big pictures to click, drawn in the viewport ----------------------
# (Blender's buttons keep their icons small whatever their size, so the easing pictures are
# drawn here, like the sculpt brush strip)

_COLS, _TILE, _GAP, _PAD, _ROW, _WIDE = 5, 86.0, 6.0, 10.0, 24.0, 40.0


def _ui_scale():
    from . import compat
    return compat.ui_scale()


def gallery_size(us):
    w = _COLS * _TILE + (_COLS - 1) * _GAP + 2 * _PAD
    rows = (len(easing.TYPES) + _COLS - 1) // _COLS
    h = 2 * _PAD + 3 * (_ROW + _GAP) + _WIDE + _GAP + rows * _TILE + (rows - 1) * _GAP
    return w * us, h * us


def gallery_items(context, g, us):
    """[(kind, value, x, y, w, h, label)] of everything in the gallery, top to bottom."""
    st = state.settings(context.scene)
    w, h = gallery_size(us)
    x0, top = g['x0'], g['y0'] + h
    pad, gap, row = _PAD * us, _GAP * us, _ROW * us
    inner = w - 2 * pad
    out = []
    y = top - pad - row

    def segments(kind, items, y):
        n = len(items)
        sw = (inner - (n - 1) * 2 * us) / n
        for i, (val, label) in enumerate(items):
            out.append((kind, val, x0 + pad + i * (sw + 2 * us), y, sw, row, label))
    segments('mode', [(i, n) for i, n, _d in MODES], y)
    y -= row + gap
    segments('dir', easing.DIRECTIONS, y)
    y -= row + gap
    if st.easing_mode == 'CHANNELS':
        labels = channel_labels(context) or ["1", "2", "3"]
        segments('chan', list(enumerate(labels[:4])), y)
    else:
        out.append(('strength', None, x0 + pad, y, inner, row, "Strength"))
    y -= _WIDE * us + gap
    out.append(('ease', 'LINEAR', x0 + pad, y, inner, _WIDE * us, "Linear"))
    tile = _TILE * us
    for i, (kind, name) in enumerate(easing.TYPES):
        r, c = divmod(i, _COLS)
        out.append(('ease', kind, x0 + pad + c * (tile + gap), y - gap - tile - r * (tile + gap), tile, tile, name))
    return out


def gallery_inside(g, mx, my):
    w, h = gallery_size(_ui_scale())
    return g['x0'] <= mx <= g['x0'] + w and g['y0'] <= my <= g['y0'] + h


def gallery_hit(context, g, mx, my):
    for kind, val, x, y, w, h, _l in gallery_items(context, g, _ui_scale()):
        if x <= mx <= x + w and y <= my <= y + h:
            return (kind, val)
    return None


def _set_strength(st, g, mx):
    for kind, _v, x, _y, w, _h, _l in gallery_items(bpy.context, g, _ui_scale()):
        if kind == 'strength':
            st.easing_strength = min(1.0, max(0.0, (mx - x) / max(w, 1.0)))


def draw_gallery(ctx, us):
    import blf
    import gpu
    from gpu_extras.batch import batch_for_shader
    from . import draw
    from .ops_sculpt import _texture
    g = state.gallery
    st = state.settings(ctx.scene)
    if g is None or st is None:
        return
    w, h = gallery_size(us)
    gpu.state.blend_set('ALPHA')
    draw._rounded_rect(g['x0'], g['y0'], w, h, 8.0 * us, (0.09, 0.09, 0.1, 0.94))
    sh = gpu.shader.from_builtin('IMAGE')
    hover = g.get('hover')
    on = {'mode': st.easing_mode, 'dir': st.easing_direction}
    chans = list(st.easing_channels)

    def text(s, cx, cy, size=11, col=(1.0, 1.0, 1.0, 0.92)):
        blf.size(0, size * us)
        tw, th = blf.dimensions(0, s)
        blf.color(0, *col)
        blf.position(0, cx - tw * 0.5, cy - th * 0.5, 0)
        blf.draw(0, s)
    for kind, val, x, y, bw, bh, label in gallery_items(ctx, g, us):
        over = hover == (kind, val)
        if kind in {'mode', 'dir', 'chan'}:
            active = chans[val] if kind == 'chan' else on[kind] == val
            col = (0.28, 0.45, 0.78, 0.95) if active else ((0.3, 0.3, 0.3, 0.95) if over else (0.18, 0.18, 0.18, 0.95))
            draw._rounded_rect(x, y, bw, bh, 4.0 * us, col)
            text(label, x + bw * 0.5, y + bh * 0.5)
        elif kind == 'strength':
            draw._rounded_rect(x, y, bw, bh, 4.0 * us, (0.3, 0.3, 0.3, 0.95) if over else (0.18, 0.18, 0.18, 0.95))
            f = st.easing_strength
            if f > 0.0:
                draw._rounded_rect(x, y, max(bw * f, 8.0 * us), bh, 4.0 * us, (0.28, 0.45, 0.78, 0.95))
            text("Strength  %d%%" % round(f * 100), x + bw * 0.5, y + bh * 0.5)
        else:
            draw._rounded_rect(x, y, bw, bh, 6.0 * us, (0.32, 0.32, 0.34, 0.95) if over else (0.15, 0.15, 0.16, 0.95))
            name = "ease_LINEAR" if val == 'LINEAR' else "ease_%s_%s" % (val, st.easing_direction)
            tex = _texture(name)
            if val == 'LINEAR':
                s = bh - 8.0 * us
                ix, iy = x + 8.0 * us, y + 4.0 * us
                lx = x + 8.0 * us + s + (bw - s - 16.0 * us) * 0.5
                ly = y + bh * 0.5
            else:
                s = bw - 26.0 * us
                ix, iy = x + (bw - s) * 0.5, y + 20.0 * us
                lx, ly = x + bw * 0.5, y + 11.0 * us
            if tex is not None:
                quad = [(ix, iy), (ix + s, iy), (ix + s, iy + s), (ix, iy + s)]
                batch = batch_for_shader(sh, 'TRI_FAN', {"pos": quad, "texCoord": [(0, 0), (1, 0), (1, 1), (0, 1)]})
                sh.uniform_sampler("image", tex)
                batch.draw(sh)
            text(label, lx, ly, size=12 if val == 'LINEAR' else 11)
    text("Click a picture: apply      Wheel: strength      Esc: close", g['x0'] + w * 0.5, g['y0'] + h + 12.0 * us,
         size=11, col=(1.0, 1.0, 1.0, 0.7))


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
    def setup(op, kind):
        op.easing = kind
        op.direction = st.easing_direction
        op.mode = st.easing_mode
        op.channels = st.easing_channels
        op.strength = st.easing_strength
    setup(layout.operator("emp.easing_preset", text="Linear", icon_value=icon_id("ease_LINEAR")), 'LINEAR')
    grid = layout.grid_flow(row_major=True, columns=5, even_columns=True, even_rows=True, align=False)
    for kind, name in easing.TYPES:
        col = grid.box().column(align=True)       # (the picture and its button: one block)
        ic = icon_id("ease_%s_%s" % (kind, st.easing_direction))
        if ic:
            col.template_icon(icon_value=ic, scale=3.0)
        setup(col.operator("emp.easing_preset", text=name), kind)
    layout.label(text="P in the viewport: the same as big pictures to click", icon='INFO')


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
