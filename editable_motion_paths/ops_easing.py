"""Easing presets: put an easing curve (Sine, Cubic, Back, Bounce...) on the selected part of a
path. Keep the Path (default) retimes along the path, its shape stays; Channels sets Blender's
easing interpolation on the chosen channels, like in the Graph Editor."""

import os
import time

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
    start: FloatProperty(name="From", default=0.0, min=0.0, max=1.0, subtype='FACTOR',
                         description="Keep the Path: where in the selected part the easing begins (before "
                                     "it the timing stays; the speed carries on smoothly into it)")
    end: FloatProperty(name="To", default=1.0, min=0.0, max=1.0, subtype='FACTOR',
                       description="Keep the Path: where in the selected part the easing ends (after it "
                                   "the timing stays)")
    blend: FloatProperty(name="Blend", default=0.0, min=0.0, max=0.5, subtype='FACTOR',
                         description="Keep the Path: how softly the easing joins the timing left as it is "
                                     "before From and after To (share of the part)")
    invert: BoolProperty(name="Invert", default=False,
                         description="Keep the Path: the easing goes on outside From - To, the timing between them "
                                     "stays")
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
        region = context.region
        us = _ui_scale()
        w, h = gallery_size(us)
        top = region.height - _covered_top(context.area, region)
        # a small view: the whole gallery made smaller to fit (with room for its hint line)
        us *= min(1.0, (top - 30.0 * us) / h, (region.width - 8.0) / w)
        w, h = gallery_size(us)
        x0 = min(max(event.mouse_region_x - w * 0.5, 4.0), max(region.width - w - 4.0, 4.0))
        y0 = min(max(event.mouse_region_y - h * 0.5, 4.0), max(top - h - 22.0 * us, 4.0))
        state.gallery = self._g = dict(area=context.area.as_pointer(), x0=x0, y0=y0, hover=None, drag=None,
                                       edit=None, us=us, since=time.time(), mouse=(event.mouse_region_x,
                                                                                   event.mouse_region_y))
        _preview_jobs.clear()
        # (a tick now and then: the tip under a resting mouse shows without the mouse moving)
        self._timer = context.window_manager.event_timer_add(0.1, window=context.window)
        context.window_manager.modal_handler_add(self)
        state.tag_redraw()
        return {'RUNNING_MODAL'}

    def _close(self, context, result):
        if getattr(self, "_timer", None) is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        state.gallery = None
        _preview_jobs.clear()
        state.tag_redraw()
        return result

    def _apply(self, context):
        st = state.settings(context.scene)
        self._close(context, None)
        self.easing = st.easing_type
        self.direction = st.easing_direction
        self.mode = st.easing_mode
        self.channels = st.easing_channels
        self.strength = st.easing_strength
        self.start, self.end, self.blend = st.easing_from, st.easing_to, st.easing_blend
        self.invert = st.easing_invert
        return self.execute(context)

    def _typing(self, context, st, event):
        """A click on a slider: its value typed in percent (Enter: set, Esc: leave it)."""
        e = self._g['edit']
        t = event.type
        digits = {'ZERO': '0', 'ONE': '1', 'TWO': '2', 'THREE': '3', 'FOUR': '4', 'FIVE': '5', 'SIX': '6',
                  'SEVEN': '7', 'EIGHT': '8', 'NINE': '9', 'PERIOD': '.', 'NUMPAD_PERIOD': '.'}
        digits.update({'NUMPAD_%d' % i: str(i) for i in range(10)})
        if t in digits:
            e['text'] += digits[t]
        elif t == 'BACK_SPACE':
            e['text'] = e['text'][:-1]
        elif t in {'RET', 'NUMPAD_ENTER'}:
            try:
                set_slider(st, e['key'], float(e['text']) / 100.0)
            except ValueError:
                pass
            self._g['edit'] = None
        elif t == 'ESC':
            self._g['edit'] = None
        state.tag_redraw()
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        st = state.settings(context.scene)
        t, v = event.type, event.value
        g = self._g
        mx, my = event.mouse_region_x, event.mouse_region_y
        if g.get('edit') and v == 'PRESS' and t not in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE', 'LEFTMOUSE'}:
            return self._typing(context, st, event)
        if g.get('edit') and t == 'LEFTMOUSE' and v in {'PRESS', 'DOUBLE_CLICK'}:
            # a click anywhere while typing sets the value typed (like leaving a Blender field)
            e = g['edit']
            if e['text']:
                try:
                    set_slider(st, e['key'], float(e['text']) / 100.0)
                except ValueError:
                    pass
            g['edit'] = None
            state.tag_redraw()
            return {'RUNNING_MODAL'}
        hit = gallery_hit(context, g, mx, my)
        if t == 'TIMER':
            if g['hover'] is not None and not g.get('tip') and time.time() - g['since'] >= TIP_DELAY:
                g['tip'] = True
                state.tag_redraw()
            return {'RUNNING_MODAL'}
        if t in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            if hit != g['hover']:
                g['since'], g['tip'] = time.time(), False
            g['mouse'] = (mx, my)
            g['hover'] = hit
            if g.get('move'):
                # dragged by its frame: it goes with the mouse (kept inside the view)
                region = context.region
                w, h = gallery_size(_gus(g))
                g['x0'] = min(max(mx - g['move'][0], 4.0), max(region.width - w - 4.0, 4.0))
                g['y0'] = min(max(my - g['move'][1], 4.0), max(region.height - _covered_top(context.area, region) - h - 4.0, 4.0))
            elif g['drag']:
                if abs(mx - g['drag_x']) >= DRAG_PX or g.get('dragged'):
                    g['dragged'] = True     # (moved: a drag - a click alone types a value, like Blender)
                    _slider_from_mouse(context, st, g, g['drag'], mx)
        elif t == 'LEFTMOUSE' and v == 'RELEASE':
            if g['drag'] and not g.get('dragged'):
                # a click on a slider (no drag): type its value, like Blender's number fields
                set_slider(st, g['drag'], g['drag_from'])
                g['edit'] = {'key': g['drag'], 'text': ""}
            g['drag'] = None
            g['dragged'] = False
            g['move'] = None
        elif t == 'LEFTMOUSE' and v == 'DOUBLE_CLICK' and hit is not None and hit[0] == 'ease':
            st.easing_type = hit[1]
            return self._apply(context)
        elif t == 'LEFTMOUSE' and v in {'PRESS', 'DOUBLE_CLICK'}:
            g['edit'] = None
            g['since'], g['tip'] = time.time() + 10.0, False     # (no tip right after a click)
            if hit is None:
                if not gallery_inside(g, mx, my):
                    return self._close(context, {'CANCELLED'})      # a click outside: close
                g['move'] = (mx - g['x0'], my - g['y0'])          # its frame: drag it somewhere else
                g['move_from'] = (g['x0'], g['y0'])
            elif hit[0] == 'mode':
                st.easing_mode = hit[1]
            elif hit[0] == 'dir':
                st.easing_direction = hit[1]
            elif hit[0] == 'chan':
                ch = list(st.easing_channels)
                ch[hit[1]] = not ch[hit[1]]
                st.easing_channels = ch
            elif hit[0] == 'slider' and (st.easing_mode == 'TIMING' or hit[1] == 'strength'):
                if event.ctrl or v == 'DOUBLE_CLICK':
                    g['edit'] = {'key': hit[1], 'text': ""}
                else:
                    g['drag'] = hit[1]
                    g['drag_from'] = slider_value(st, hit[1])
                    g['drag_x'] = mx
                    g['dragged'] = False
            elif hit[0] == 'ease':
                st.easing_type = hit[1]                 # picked: the preview shows it, Apply puts it on
            elif hit[0] == 'invert' and st.easing_mode == 'TIMING':
                st.easing_invert = not st.easing_invert
            elif hit[0] == 'apply':
                return self._apply(context)
        elif t in {'RET', 'NUMPAD_ENTER', 'SPACE'} and v == 'PRESS':
            return self._apply(context)
        elif t in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'} and st.easing_mode == 'TIMING':
            st.easing_strength = min(1.0, max(0.0, st.easing_strength + (0.05 if t == 'WHEELUPMOUSE' else -0.05)))
        elif t in {'ESC', 'RIGHTMOUSE'} and v == 'PRESS' and (g['drag'] or g.get('move')):
            # (like Blender's sliders: right click while dragging puts the value back, the
            # gallery stays open)
            if g['drag']:
                set_slider(st, g['drag'], g['drag_from'])
            else:
                g['x0'], g['y0'] = g['move_from']
            g['drag'] = g['move'] = None
        elif t in {'ESC', 'RIGHTMOUSE', 'P'} and v == 'PRESS':
            return self._close(context, {'CANCELLED'})
        elif t in {'MIDDLEMOUSE', 'TRACKPADPAN', 'TRACKPADZOOM', 'NDOF_MOTION'}:
            # turning the view: the gallery goes (it would hang in front of the new view)
            self._close(context, None)
            return {'CANCELLED', 'PASS_THROUGH'}
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
            col = layout.column(align=True)
            col.prop(self, "strength", slider=True)
            col.prop(self, "start", slider=True)
            col.prop(self, "end", slider=True)
            col.prop(self, "blend", slider=True)
            col.prop(self, "invert")
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
        pts, vals = job.pts, job.vals
        d0 = (pts[1] - pts[0]).normalized() if len(pts) > 1 else None
        d1 = (pts[-1] - pts[-2]).normalized() if len(pts) > 1 else None
        # (the easing between From and To - or, inverted, outside them; elsewhere the timing stays)
        S, kept = eased_arc(job, self._fn(), self.strength, self.start, self.end, self.blend, self.invert)

        def target(t):
            if kept(t):
                return cache.solve(tg, t, job._at(pts, t), job._at(vals, t))
            s = S(t)
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
        # (From / To: Keep the Path only - Blender's easing interpolation spans a whole segment)
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


# ---- the gallery (P): pick an easing, see what it does, tune it, apply ------------------------
# (drawn in the viewport like the sculpt brush strip: Blender's buttons keep their icons small)

_COLS, _TILE, _GAP, _PAD, _ROW, _WIDE, _PREVIEW, _APPLY = 5, 86.0, 6.0, 10.0, 24.0, 40.0, 110.0, 30.0
SLIDERS = (('strength', "Strength", "easing_strength"), ('from', "From", "easing_from"),
           ('to', "To", "easing_to"), ('blend', "Blend", "easing_blend"))


TIPS = {
    ('mode', 'TIMING'): "Keep the Path: retime along the path - its shape stays, only where the bone is on "
                        "each frame changes",
    ('mode', 'CHANNELS'): "Channels: Blender's easing interpolation on the chosen channels, like in the "
                          "Graph Editor (the shape of the path changes too)",
    ('dir', 'EASE_IN'): "In: starts slowly and speeds up",
    ('dir', 'EASE_OUT'): "Out: starts fast and slows down to the end",
    ('dir', 'EASE_IN_OUT'): "In & Out: slow at both ends, fastest in the middle",
    ('chan', None): "Channels mode: this channel gets the easing (one always stays on)",
    ('preview', None): "The speed of the selected part: gray as it is now, blue with the easing (the picture "
                       "under the mouse, else the one picked). The light band: From - To",
    ('slider', 'strength'): "How much of the easing (0: the timing as it was). The mouse wheel too. "
                            "Click: type a value, drag: set it",
    ('slider', 'from'): "Where the easing begins: before it the timing stays as it is. Click: type a value, drag: set it",
    ('slider', 'to'): "Where the easing ends: after it the timing stays as it is. Click: type a value, drag: set it",
    ('slider', 'blend'): "How softly the eased part joins the timing kept as it is (at least two frames). "
                         "Click: type a value, drag: set it",
    ('invert', None): "Invert: the easing goes on outside From - To, the timing between them stays",
    ('apply', None): "Put the picked easing on the selected part (Enter). After it, the redo panel at the "
                     "bottom left has all the settings",
    ('ease', 'LINEAR'): "Linear: an even speed all the way",
    ('ease', 'SINE'): "Sine: the gentlest easing",
    ('ease', 'QUAD'): "Quadratic: a soft easing",
    ('ease', 'CUBIC'): "Cubic: a clear easing - the usual one",
    ('ease', 'QUART'): "Quartic: a strong easing",
    ('ease', 'QUINT'): "Quintic: a very strong easing",
    ('ease', 'EXPO'): "Exponential: almost a hold, then a burst (or the other way around)",
    ('ease', 'CIRC'): "Circular: eases like a quarter circle - sharp at the very end",
    ('ease', 'BACK'): "Back: goes a bit past the end and comes back (or backs up before it starts)",
    ('ease', 'BOUNCE'): "Bounce: lands and bounces like a dropped ball",
    ('ease', 'ELASTIC'): "Elastic: overshoots and wobbles in like a spring",
}
TIP_DELAY = 0.5     # seconds the mouse rests on something before its tip shows


def eased_arc(job, fn, strength, start, end, blend, invert=False):
    """(S, kept): S(t) is how far along the path the motion is on frame t after the easing (the arc
    length of the part, from 0 to its length), `strength` of it; kept(t): the timing there stays as
    it was. Applying it and the preview use the same.

    The easing of the whole selection, put on between From and To only (invert: outside them):
    elsewhere the timing stays. It is the speed that is mixed, not the position: the eased part
    has the easing's speed, scaled so it still gets from where the kept timing leaves off to where
    it goes on (no catching up - no stops or bursts at the joins), and the two speeds blend over
    `blend` of the part (at least two frames) with a smooth step, so the speed itself turns
    smoothly into the easing."""
    import numpy as np
    r0, r1 = job.r0, job.r1
    lo, hi = sorted((start, end))
    if hi - lo < 0.02:
        lo, hi = 0.0, 1.0
    A, B = r0 + (r1 - r0) * lo, r0 + (r1 - r0) * hi
    inside_lo, inside_hi = lo > 1e-3, hi < 1.0 - 1e-3
    total = job.arc[-1]
    # (at least two frames, or the speed would jump; Blend adds to that, so every bit of it counts)
    ramp = min(2.0 + blend * (B - A), (B - A) * 0.5)

    def smooth(x):
        # (smootherstep: its slope and its bend are 0 at both ends - no kink in the speed)
        x = min(max(x, 0.0), 1.0)
        return x * x * x * (x * (6.0 * x - 15.0) + 10.0)

    def weight(t):
        """How much of the easing on frame t (0..1)."""
        if t <= A or t >= B:
            w = 0.0
        else:
            w = 1.0
            if inside_lo:
                w *= smooth((t - A) / ramp)
            if inside_hi:
                w *= smooth((B - t) / ramp)
        return (1.0 - w if invert else w) * strength

    n = int(min(4000, max(400, (r1 - r0) * 10)))
    g = np.linspace(r0, r1, n + 1)
    s0 = np.interp(g, job.ts, job.arc)
    E = total * np.array([fn((t - r0) / (r1 - r0)) for t in g])
    w = np.array([weight(t) for t in (g[:-1] + g[1:]) * 0.5])
    d0, dE = np.diff(s0), np.diff(E)
    ds = d0 * (1.0 - w)
    on = w > 0.0
    i = 0
    while i < n:
        if not on[i]:
            i += 1
            continue
        j = i
        while j < n and on[j]:
            j += 1
        # one eased run: it covers what the kept timing leaves to it
        need = float((d0[i:j] * w[i:j]).sum())
        got = float((dE[i:j] * w[i:j]).sum())
        if got > 1e-12 and need >= 0.0 and got >= 0.25 * need:
            ds[i:j] += dE[i:j] * w[i:j] * (need / got)
        else:
            # (the easing goes mostly backwards there - Back / Elastic: shifted, not scaled)
            span = float(w[i:j].sum())
            ds[i:j] += (dE[i:j] + (need - got) / span) * w[i:j]
        i = j
    arc = np.concatenate([[0.0], np.cumsum(ds)])

    def S(t):
        return float(np.interp(t, g, arc))

    def kept(t):
        return weight(t) <= 0.0
    return S, kept


def _ui_scale():
    from . import compat
    return compat.ui_scale()


def gallery_size(us):
    w = _COLS * _TILE + (_COLS - 1) * _GAP + 2 * _PAD
    rows = (len(easing.TYPES) + _COLS - 1) // _COLS
    h = (2 * _PAD + 2 * (_ROW + _GAP) + _PREVIEW + _GAP + _ROW + _GAP + _WIDE + _GAP + rows * _TILE +
         (rows - 1) * _GAP + _GAP + _APPLY)
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
    if st.easing_mode == 'CHANNELS':
        labels = channel_labels(context) or ["1", "2", "3"]
        n_dir = len(easing.DIRECTIONS)
        # (Channels: the direction and the channels share the row)
        sw = (inner - 6 * us) * 0.5
        sub = [(v, lab) for v, lab in easing.DIRECTIONS]
        for i, (val, label) in enumerate(sub):
            bw = (sw - (n_dir - 1) * 2 * us) / n_dir
            out.append(('dir', val, x0 + pad + i * (bw + 2 * us), y, bw, row, label))
        chans = list(enumerate(labels[:4]))
        for i, (val, label) in enumerate(chans):
            bw = (sw - (len(chans) - 1) * 2 * us) / len(chans)
            out.append(('chan', val, x0 + pad + sw + 6 * us + i * (bw + 2 * us), y, bw, row, label))
    else:
        segments('dir', easing.DIRECTIONS, y)
    y -= _PREVIEW * us + gap
    out.append(('preview', None, x0 + pad, y, inner, _PREVIEW * us, ""))
    y -= row + gap
    fw = 58.0 * us                          # (the Invert switch at the end of the row)
    sw = (inner - fw - 4 * 4 * us) / 4
    for i, (key, label, _prop) in enumerate(SLIDERS):
        out.append(('slider', key, x0 + pad + i * (sw + 4 * us), y, sw, row, label))
    out.append(('invert', None, x0 + pad + inner - fw, y, fw, row, "Invert"))
    y -= _WIDE * us + gap
    out.append(('ease', 'LINEAR', x0 + pad, y, inner, _WIDE * us, "Linear"))
    tile = _TILE * us
    for i, (kind, name) in enumerate(easing.TYPES):
        r, c = divmod(i, _COLS)
        out.append(('ease', kind, x0 + pad + c * (tile + gap), y - gap - tile - r * (tile + gap), tile, tile, name))
    rows = (len(easing.TYPES) + _COLS - 1) // _COLS
    y_apply = y - gap - rows * tile - (rows - 1) * gap - gap - _APPLY * us
    out.append(('apply', None, x0 + pad, y_apply, inner, _APPLY * us, "Apply"))
    return out


def _covered_top(area, region):
    """How much of the top of the view's region the headers cover (they lie over it when regions
    overlap)."""
    top = region.y + region.height
    out = 0
    for r in area.regions:
        if r.type in {'HEADER', 'TOOL_HEADER'} and r.height > 1 and region.y <= r.y < top:
            out = max(out, top - r.y)
    return out


def _gus(g):
    return g.get('us') or _ui_scale()


def gallery_inside(g, mx, my):
    w, h = gallery_size(_gus(g))
    return g['x0'] <= mx <= g['x0'] + w and g['y0'] <= my <= g['y0'] + h


def gallery_hit(context, g, mx, my):
    for kind, val, x, y, w, h, _l in gallery_items(context, g, _gus(g)):
        if x <= mx <= x + w and y <= my <= y + h:
            return (kind, val)
    return None


def hit_box(context, g, kind, val=None):
    """(x, width) of a gallery item."""
    for k, v, x, _y, w, _h, _l in gallery_items(context, g, _gus(g)):
        if k == kind and (val is None or v == val):
            return x, w
    return 0.0, 1.0


def slider_value(st, key):
    return getattr(st, dict((k, p) for k, _l, p in SLIDERS)[key])


def set_slider(st, key, value):
    """A slider's value (0..1), From kept before To."""
    value = min(1.0, max(0.0, value))
    if key == 'from':
        value = min(value, max(st.easing_to - 0.05, 0.0))
    elif key == 'to':
        value = max(value, min(st.easing_from + 0.05, 1.0))
    elif key == 'blend':
        value = min(value, 0.5)
    setattr(st, dict((k, p) for k, _l, p in SLIDERS)[key], value)


DRAG_PX = 3        # a press on a slider becomes a drag once the mouse moves this far


def _slider_from_mouse(context, st, g, key, mx):
    x, w = hit_box(context, g, 'slider', key)
    set_slider(st, key, (mx - x) / max(w, 1.0) * (0.5 if key == 'blend' else 1.0))


def _set_strength(st, g, mx):
    _slider_from_mouse(bpy.context, st, g, 'strength', mx)


# ---- the preview: the speed of the selected part, before and after ---------------------------

_preview_jobs = {}      # (path key, its signature, range) -> SpaceJob


def preview_cache(context):
    """The path the gallery previews: the first editable one with selected keys (or any)."""
    cs = [c for c in state.editable_caches(context.scene) if c.ptype in (core.LOCATION, core.ROTATION)]
    for c in cs:
        if any(k.sel for k in c.keys) and _ranges(c) is not None:
            return c
    return next((c for c in cs if _ranges(c) is not None), None)


def preview_speeds(context, st, kind, n=120):
    """([frame], [speed before], [speed after]) of the selected part with easing `kind` and the
    gallery's settings, or None."""
    from .ops_timing import SpaceJob
    c = preview_cache(context)
    if c is None:
        return None
    r0, r1 = (float(x) for x in _ranges(c))
    if r1 - r0 < 2.0:
        return None
    key = (c.key, id(c.sig), r0, r1)
    job = _preview_jobs.get(key)
    if job is None:
        tg = c.target()
        if tg is None:
            return None
        _preview_jobs.clear()
        job = _preview_jobs[key] = SpaceJob(c, tg, r0, r1)
    if job.arc[-1] < 1e-9:
        return None
    p = (1.70158, 1.0, 0.3, 3, 0.5)
    direction = st.easing_direction

    def fn(u):
        return easing.ease(kind, direction, u, *p) if kind != 'LINEAR' else u
    timing = st.easing_mode == 'TIMING'
    S, _kept = eased_arc(job, fn, st.easing_strength if timing else 1.0, st.easing_from if timing else 0.0,
                         st.easing_to if timing else 1.0, st.easing_blend if timing else 0.0,
                         st.easing_invert and timing)
    ts = [r0 + (r1 - r0) * i / n for i in range(n + 1)]
    h = (r1 - r0) / n * 0.5
    before = [abs(job._at(job.arc, min(t + h, r1)) - job._at(job.arc, max(t - h, r0))) /
              (min(t + h, r1) - max(t - h, r0)) for t in ts]
    after = [abs(S(min(t + h, r1)) - S(max(t - h, r0))) / (min(t + h, r1) - max(t - h, r0)) for t in ts]
    return ts, before, after


# ---- drawing -------------------------------------------------------------------------------

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
    us = _gus(g)
    w, h = gallery_size(us)
    gpu.state.blend_set('ALPHA')
    draw._rounded_rect(g['x0'], g['y0'], w, h, 8.0 * us, (0.09, 0.09, 0.1, 0.94))
    sh = gpu.shader.from_builtin('IMAGE')
    hover = g.get('hover')
    on = {'mode': st.easing_mode, 'dir': st.easing_direction}
    chans = list(st.easing_channels)
    timing = st.easing_mode == 'TIMING'
    blue, dark, lit = (0.28, 0.45, 0.78, 0.95), (0.18, 0.18, 0.18, 0.95), (0.3, 0.3, 0.3, 0.95)

    def text(s, cx, cy, size=11, col=(1.0, 1.0, 1.0, 0.92), left=False):
        blf.size(0, size * us)
        tw, th = blf.dimensions(0, s)
        blf.color(0, *col)
        blf.position(0, cx if left else cx - tw * 0.5, cy - th * 0.5, 0)
        blf.draw(0, s)
        return tw

    def picture(name, x, y, s):
        tex = _texture(name)
        if tex is not None:
            quad = [(x, y), (x + s, y), (x + s, y + s), (x, y + s)]
            batch = batch_for_shader(sh, 'TRI_FAN', {"pos": quad, "texCoord": [(0, 0), (1, 0), (1, 1), (0, 1)]})
            sh.uniform_sampler("image", tex)
            batch.draw(sh)
    shown = hover[1] if hover and hover[0] == 'ease' else st.easing_type
    for kind, val, x, y, bw, bh, label in gallery_items(ctx, g, us):
        over = hover == (kind, val)
        if kind in {'mode', 'dir', 'chan'}:
            active = chans[val] if kind == 'chan' else on[kind] == val
            draw._rounded_rect(x, y, bw, bh, 4.0 * us, blue if active else (lit if over else dark))
            text(label, x + bw * 0.5, y + bh * 0.5)
        elif kind == 'preview':
            draw._rounded_rect(x, y, bw, bh, 5.0 * us, (0.06, 0.06, 0.07, 0.95))
            _draw_preview(ctx, st, shown, x, y, bw, bh, us, text)
        elif kind == 'slider':
            usable = timing or val == 'strength'
            value = slider_value(st, val)
            editing = g.get('edit') and g['edit']['key'] == val
            draw._rounded_rect(x, y, bw, bh, 4.0 * us, lit if over and usable else dark)
            frac = value / 0.5 if val == 'blend' else value
            if usable and not editing:
                if val == 'to':
                    # (To fills from its value to the end: the part is between the two)
                    draw._rounded_rect(x + bw * frac, y, max(bw * (1.0 - frac), 4.0 * us), bh, 4.0 * us,
                                       (0.22, 0.32, 0.5, 0.95))
                elif frac > 0.0:
                    draw._rounded_rect(x, y, max(bw * frac, 6.0 * us), bh, 4.0 * us, blue)
            col = (1.0, 1.0, 1.0, 0.92 if usable else 0.35)
            if editing:
                text("%s: %s|" % (label, g['edit']['text']), x + bw * 0.5, y + bh * 0.5, col=(1.0, 0.85, 0.4, 1.0))
            else:
                text("%s %d%%" % (label, round(value * 100)), x + bw * 0.5, y + bh * 0.5, col=col)
        elif kind == 'invert':
            on = timing and st.easing_invert
            draw._rounded_rect(x, y, bw, bh, 4.0 * us, blue if on else (lit if over and timing else dark))
            text(label, x + bw * 0.5, y + bh * 0.5, col=(1.0, 1.0, 1.0, 0.92 if timing else 0.35))
        elif kind == 'apply':
            draw._rounded_rect(x, y, bw, bh, 5.0 * us, (0.32, 0.55, 0.9, 0.98) if over else blue)
            name = dict([('LINEAR', "Linear")] + list(easing.TYPES)).get(st.easing_type, "")
            text("Apply %s" % name, x + bw * 0.5, y + bh * 0.5, size=12)
        else:
            chosen = st.easing_type == val
            draw._rounded_rect(x, y, bw, bh, 6.0 * us, (0.32, 0.32, 0.34, 0.95) if over else (0.15, 0.15, 0.16, 0.95))
            if chosen:
                _outline(x, y, bw, bh, us, (0.45, 0.65, 1.0, 1.0))
            if val == 'LINEAR':
                # the picture right next to the word, the two in the middle
                s = bh - 10.0 * us
                blf.size(0, 12 * us)
                tw, _th = blf.dimensions(0, label)
                gap = 8.0 * us
                ix = x + (bw - s - gap - tw) * 0.5
                picture("ease_LINEAR", ix, y + 5.0 * us, s)
                text(label, ix + s + gap, y + bh * 0.5, size=12, left=True)
            else:
                s = bw - 26.0 * us
                picture("ease_%s_%s" % (val, st.easing_direction), x + (bw - s) * 0.5, y + 20.0 * us, s)
                text(label, x + bw * 0.5, y + 11.0 * us)
    tip = g.get('tip') and g.get('hover') and not g.get('drag') and not g.get('move') and not g.get('edit')
    if tip:
        _draw_tip(ctx, g, us, text)
    # the hint above the gallery, or below it where there is no room above
    region = ctx.region
    room = region.height - _covered_top(ctx.area, region) if region is not None and ctx.area is not None else 1e9
    hint_y = g['y0'] + h + 12.0 * us if g['y0'] + h + 22.0 * us < room else g['y0'] - 12.0 * us
    text("Click: pick (the preview shows it)     Enter / double click: apply     Click a slider: type, drag: set     "
         "Drag its frame: move it     Esc: close", g['x0'] + w * 0.5, hint_y, size=10, col=(1.0, 1.0, 1.0, 0.7))


def _draw_tip(ctx, g, us, text):
    """The tip of what the mouse rests on: a dark box next to it, like Blender's tooltips."""
    import blf
    from . import draw
    kind, val = g['hover']
    msg = TIPS.get((kind, val)) or TIPS.get((kind, None))
    if not msg:
        return
    words, lines, line = msg.split(), [], ""
    for w_ in words:
        if len(line) + len(w_) + 1 > 46:
            lines.append(line)
            line = w_
        else:
            line = (line + " " + w_).strip()
    lines.append(line)
    blf.size(0, 11 * us)
    lh = 15.0 * us
    tw = max(blf.dimensions(0, ln)[0] for ln in lines)
    w, h = tw + 16.0 * us, lh * len(lines) + 10.0 * us
    mx, my = g['mouse']
    region = ctx.region
    x = min(mx + 14.0 * us, region.width - w - 4.0) if region else mx + 14.0 * us
    y = my - 18.0 * us - h
    if y < 4.0:
        y = my + 18.0 * us
    draw._rounded_rect(x, y, w, h, 4.0 * us, (0.05, 0.05, 0.06, 0.97))
    for i, ln in enumerate(lines):
        text(ln, x + 8.0 * us, y + h - 5.0 * us - lh * (i + 0.5), size=11, left=True)


def _outline(x, y, w, h, us, col):
    from . import draw
    pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    draw._flat(pts, col, 'LINE_LOOP', 2.0 * us)


def _draw_preview(ctx, st, kind, x, y, w, h, us, text):
    """The selected part's speed, before (gray) and with the easing (blue, filled): like the speed
    graph, only the shape."""
    from . import draw
    try:
        data = preview_speeds(ctx, st, kind)
    except Exception:
        data = None
    if data is None:
        text("Select keys of a path: their speed shows here", x + w * 0.5, y + h * 0.5, col=(1, 1, 1, 0.45))
        return
    ts, before, after = data
    top = max(max(before), max(after), 1e-9) * 1.1
    m = 8.0 * us
    gx, gy, gw, gh = x + m, y + m, w - 2 * m, h - 2 * m - 10.0 * us

    def P(i, v):
        return (gx + gw * i / (len(ts) - 1), gy + gh * min(v / top, 1.05))
    if st.easing_mode == 'TIMING' and (st.easing_from > 1e-3 or st.easing_to < 1.0 - 1e-3):
        # the part the easing works on (inverted: the parts outside From - To)
        a, b = sorted((st.easing_from, st.easing_to))
        band = (0.2, 0.25, 0.35, 0.5)
        if st.easing_invert:
            for u0, u1 in ((0.0, a), (b, 1.0)):
                if u1 - u0 > 1e-3:
                    draw._rounded_rect(gx + gw * u0, gy, max(gw * (u1 - u0), 2.0), gh, 2.0 * us, band)
        else:
            draw._rounded_rect(gx + gw * a, gy, max(gw * (b - a), 2.0), gh, 2.0 * us, band)
    fill = []
    for i in range(len(ts) - 1):
        (x0, y0), (x1, y1) = P(i, after[i]), P(i + 1, after[i + 1])
        fill += [(x0, gy), (x1, gy), (x1, y1), (x0, gy), (x1, y1), (x0, y0)]
    draw._flat(fill, (0.3, 0.5, 0.9, 0.25), 'TRIS')
    draw._flat([P(i, v) for i, v in enumerate(before)], (0.7, 0.7, 0.7, 0.55), 'LINE_STRIP', 1.5 * us)
    draw._flat([P(i, v) for i, v in enumerate(after)], (0.45, 0.7, 1.0, 1.0), 'LINE_STRIP', 2.2 * us)
    name = dict([('LINEAR', "Linear")] + list(easing.TYPES)).get(kind, "")
    dname = dict(easing.DIRECTIONS).get(st.easing_direction, "") if kind != 'LINEAR' else ""
    text("Speed: now (gray), %s %s (blue)" % (name, dname), x + 8.0 * us, y + h - 9.0 * us, size=10,
         col=(1.0, 1.0, 1.0, 0.6), left=True)


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
        col = layout.column(align=True)
        col.prop(st, "easing_strength", slider=True)
        row = col.row(align=True)
        row.prop(st, "easing_from", slider=True)
        row.prop(st, "easing_to", slider=True)
        col.prop(st, "easing_blend", slider=True)
        col.prop(st, "easing_invert")

    def setup(op, kind):
        op.easing = kind
        op.direction = st.easing_direction
        op.mode = st.easing_mode
        op.channels = st.easing_channels
        op.strength = st.easing_strength
        op.start, op.end, op.blend = st.easing_from, st.easing_to, st.easing_blend
        op.invert = st.easing_invert
    setup(layout.operator("emp.easing_preset", text="Linear", icon_value=icon_id("ease_LINEAR")), 'LINEAR')
    grid = layout.grid_flow(row_major=True, columns=5, even_columns=True, even_rows=True, align=False)
    for kind, name in easing.TYPES:
        col = grid.box().column(align=True)       # (the picture and its button: one block)
        ic = icon_id("ease_%s_%s" % (kind, st.easing_direction))
        if ic:
            col.template_icon(icon_value=ic, scale=3.0)
        setup(col.operator("emp.easing_preset", text=name), kind)
    layout.label(text="P in the viewport: pick, see the speed it gives, tune, apply", icon='INFO')


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
