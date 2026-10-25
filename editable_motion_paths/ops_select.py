"""Clicking, dragging and selecting path keys."""

import time

import bpy
from bpy.props import BoolProperty, EnumProperty
from mathutils import Vector

from . import compat, core, pick, space, state
from .ops_keys import insert_key


GIZMO_AXES = {'GX': 0, 'GXn': 0, 'GY': 1, 'GZ': 2, 'GZn': 2, 'GU': -1}


def _region_mouse(context, event):
    return event.mouse_region_x, event.mouse_region_y


class EMP_OT_click(bpy.types.Operator):
    """Select a path key or handle; drag to move it. Ctrl+Click on a path inserts a key"""
    bl_idname = "emp.click"
    bl_label = "Select Path Key"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == 'VIEW_3D'

    def invoke(self, context, event):
        if event.alt or not state.visible_caches(context.scene) or context.region_data is None:
            return {'PASS_THROUGH'}
        mx, my = _region_mouse(context, event)
        from . import draw
        sp = draw.speed_hit(context.area, mx, my)
        if sp is not None:
            # the speed graph: the current frame, a key in time, a segment's ease
            c = state.caches.get(state.speed_graph["cache"])
            if c is not None:
                kind, val = sp
                props = dict(mode=kind, obj_name=c.obj_name, bone_name=c.bone_name)
                if kind == 'EASE':
                    props.update(frame=val[0], frame_b=val[1])
                else:
                    props.update(frame=val)
                bpy.ops.emp.speed_drag('INVOKE_DEFAULT', **props)
                return {'FINISHED'}
        tick = _chart_tick(context, mx, my)
        if tick is not None:
            cache, frame = tick
            bpy.ops.emp.drag_frame('INVOKE_DEFAULT', frame=frame, obj_name=cache.obj_name,
                                   bone_name=cache.bone_name, chart=True)
            return {'FINISHED'}
        hit = pick.hit_test(context, mx, my)
        if (hit is None or not (hit.part[:1] == 'W' and len(hit.part) == 3)) and state.whisker_sel(context.scene):
            state.set_whisker_sel(set(), context.scene)     # any other click drops the picked scale handles
        if hit is not None and hit.part == 'DOT':
            if event.ctrl:
                hit = None      # Ctrl+Click on the path inserts a key, dots included
            else:
                self.dot = (hit.cache, hit.t)
                self.press = Vector((event.mouse_region_x, event.mouse_region_y))
                self.threshold = context.preferences.inputs.drag_threshold_mouse * compat.ui_scale()
                self.hit_key = None
                self.gizmo = self.ring = None
                context.window_manager.modal_handler_add(self)
                return {'RUNNING_MODAL'}
        self.dot = None

        if hit is None and event.ctrl:
            lh = pick.line_hit(context, mx, my)
            if lh is not None:
                t = float(round(lh.t))
                cache = lh.cache
                pick.deselect_all(state.editable_caches(context.scene))
                k = insert_key(cache, t)
                if k is not None:
                    pick.select_part(cache, k, 'KEY', True)
                    cache.refresh(force=True)
                    hit = pick.Hit(cache, k, 'KEY', 0.0)
                    self._start_wait(context, event, hit, None)
                    state.tag_redraw()
                    return {'RUNNING_MODAL'}

        if hit is None:
            if not event.shift and not event.ctrl:
                if pick.deselect_all(state.editable_caches(context.scene)):
                    pick.tag_anim_editors()
            return {'PASS_THROUGH'}

        cache = hit.cache
        if cache.locked:
            if not cache.owner_selected():
                pick.select_owner(context, cache, event.shift)
            self.report({'INFO'}, cache.lock_reason)
            state.tag_redraw()
            return {'FINISHED'}
        if hit.part == 'KEY' and not event.shift and not event.ctrl and self._is_double(context, event, hit):
            context.scene.frame_set(int(round(hit.t)))
            return {'FINISHED'}
        if not cache.owner_selected():
            if not pick.select_owner(context, cache, event.shift):
                return {'PASS_THROUGH'}
            cache.refresh(force=True)
            hit = pick.hit_test(context, mx, my)
            if hit is None:
                return {'FINISHED'}
            cache = hit.cache

        if event.ctrl and hit.part == 'KEY':
            # like Pick Shortest Path: select every key between the active key and this one
            self._select_range(cache, hit.key)
            pick.tag_anim_editors()
            return {'FINISHED'}

        k = hit.key
        if hit.part[:1] == 'W' and len(hit.part) == 3:
            # scale whisker: pick that handle (Shift: add / remove it), drag it to edit it
            ch = {'X': 0, 'Y': 1, 'Z': 2}.get(state.settings(context.scene).scale_channel)
            entry = (cache.key, round(k.t, 3), ch, hit.part[1])
            sel = state.whisker_sel(context.scene)
            if not event.shift:
                sel = {entry}
            elif entry in sel:
                sel.discard(entry)
            else:
                sel.add(entry)
            state.set_whisker_sel(sel, context.scene)
            state.tag_redraw()
            bpy.ops.emp.scale_whisker('INVOKE_DEFAULT', frame=hit.key.t, part=hit.part)
            return {'FINISHED'}
        if hit.part in space.RING_AXES:
            # rotation ring: drag it to turn around its axis
            self.gizmo = None
            self.ring = hit.part
            self.ring_shift = event.shift
            self._start_wait(context, event, hit, None)
            return {'RUNNING_MODAL'}
        self.ring = None
        if hit.part in GIZMO_AXES:
            # scale gizmo: drag a handle to scale that axis (the white one: all of them)
            self.gizmo = hit.part
            self._start_wait(context, event, hit, None)
            return {'RUNNING_MODAL'}
        self.gizmo = None
        if hit.part == 'KEY':
            state.set_active(cache.key, k.t)
        release_action = None
        if hit.part != 'KEY' and not event.shift:
            # grabbing a handle edits only that handle (like the Graph Editor)
            pick.deselect_all(state.editable_caches(context.scene))
            pick.select_part(cache, k, hit.part, True)
            cache.refresh(force=True)
        elif pick.part_selected(k, hit.part):
            release_action = 'TOGGLE' if event.shift else 'ONLY'
        else:
            if not event.shift:
                pick.deselect_all(state.editable_caches(context.scene))
            pick.select_part(cache, k, hit.part, True)
            cache.refresh(force=True)
        pick.tag_anim_editors()
        self._start_wait(context, event, hit, release_action)
        return {'RUNNING_MODAL'}

    _last_click = (0.0, None, None)

    @staticmethod
    def _select_range(cache, k):
        sel = [x.t for x in cache.keys if x.sel and abs(x.t - k.t) > 1e-3]
        anchor = None
        active = state.get_active()
        if active and active[0] == cache.key and any(abs(t - active[1]) < 1e-3 for t in sel):
            anchor = active[1]
        elif sel:
            anchor = min(sel, key=lambda t: abs(t - k.t))
        if anchor is None:
            pick.select_part(cache, k, 'KEY', True)
        else:
            lo, hi = sorted((anchor, k.t))
            inner = [x for x in cache.keys if lo - 1e-3 <= x.t <= hi + 1e-3]
            chosen = inner
            if _is_loop(cache) and cache.keys:
                # a looping path is a ring: go the shorter way along the curve, across the seam
                # if that is shorter in 3D
                t0, t1 = cache.keys[0].t, cache.keys[-1].t
                direct = _arc(cache, lo, hi)
                around = _arc(cache, hi, t1) + _arc(cache, t0, lo)
                if around < direct:
                    chosen = [x for x in cache.keys if x.t >= hi - 1e-3 or x.t <= lo + 1e-3]
            for x in chosen:
                pick.select_part(cache, x, 'KEY', True)
        state.set_active(cache.key, k.t)
        cache.refresh(force=True)

    @classmethod
    def _is_double(cls, context, event, hit):
        """Double click on the same key (detected here because this operator also gets
        the second press of a double click)."""
        now = time.time()
        t0, key, frame = EMP_OT_click._last_click
        EMP_OT_click._last_click = (now, hit.cache.key, hit.t)
        limit = context.preferences.inputs.mouse_double_click_time / 1000.0
        if event.value == 'DOUBLE_CLICK' or (now - t0 < limit and key == hit.cache.key
                                             and frame is not None and abs(frame - hit.t) < 1e-3):
            EMP_OT_click._last_click = (0.0, None, None)
            return True
        return False

    def _start_wait(self, context, event, hit, release_action):
        self.hit_key = hit.cache.key
        self.hit_t = hit.t
        self.hit_part = hit.part
        self.release_action = release_action
        self.press = Vector((event.mouse_region_x, event.mouse_region_y))
        self.threshold = context.preferences.inputs.drag_threshold_mouse * compat.ui_scale()
        context.window_manager.modal_handler_add(self)

    def _find_key(self):
        cache = state.caches.get(self.hit_key)
        if cache is None or not cache.refresh():
            return None, None
        for k in cache.keys:
            if abs(k.t - self.hit_t) < 1e-3:
                return cache, k
        return cache, None

    def _key_trackball(self, context):
        st = state.settings(context.scene)
        cache = state.caches.get(self.hit_key)
        return cache is not None and cache.ptype == core.ROTATION and st.show_rot_rings

    def modal(self, context, event):
        if getattr(self, "dot", None):
            # a frame dot: drag it along the path (spacing), or click to go to that frame
            cache, frame = self.dot
            if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
                m = Vector((event.mouse_region_x, event.mouse_region_y))
                if (m - self.press).length >= max(self.threshold, 3):
                    bpy.ops.emp.drag_frame('INVOKE_DEFAULT', frame=frame, obj_name=cache.obj_name,
                                           bone_name=cache.bone_name)
                    return {'FINISHED'}
                return {'RUNNING_MODAL'}
            if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
                context.scene.frame_set(int(frame))
                return {'FINISHED'}
            if event.type in {'ESC', 'RIGHTMOUSE'}:
                return {'FINISHED'}
            return {'RUNNING_MODAL'}
        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            m = Vector((event.mouse_region_x, event.mouse_region_y))
            if (m - self.press).length >= max(self.threshold, 3):
                if getattr(self, "ring", None) in {'RY', 'RB'}:
                    # the green ring / the ribbon's handle: twist the bone around its own axis
                    bpy.ops.emp.transform('INVOKE_DEFAULT', mode='ROTATE', twist=True, release_confirm=True,
                                          start_x=int(self.press.x), start_y=int(self.press.y),
                                          ring_axis=space.RING_AXES[self.ring])
                elif getattr(self, "ring", None):
                    bpy.ops.emp.transform('INVOKE_DEFAULT', mode='TRANSLATE', release_confirm=True,
                                          start_x=int(self.press.x), start_y=int(self.press.y),
                                          ring_axis=space.RING_AXES[self.ring])
                elif self.hit_part == 'KEY' and self._key_trackball(context):
                    # a rotation key with its rings shown: the key is the trackball of the gizmo
                    bpy.ops.emp.transform('INVOKE_DEFAULT', mode='TRANSLATE', release_confirm=True,
                                          start_x=int(self.press.x), start_y=int(self.press.y),
                                          key_trackball=True)
                elif getattr(self, "gizmo", None):
                    extra = {}
                    st = state.settings(context.scene)
                    if self.gizmo == 'GY' and st is not None and st.scale_y_display == 'RIBBON':
                        # the Y ribbon's width is measured from its own middle line
                        from . import draw
                        cache, k = self._find_key()
                        if k is not None:
                            inner = draw.y_ribbon(st, cache, cache.target(), k.t, k.vals)[0]
                            extra = dict(measure_from=inner, use_measure_from=True)
                    bpy.ops.emp.transform('INVOKE_DEFAULT', mode='SCALE', release_confirm=True,
                                          start_x=int(self.press.x), start_y=int(self.press.y),
                                          axis_index=GIZMO_AXES[self.gizmo], **extra)
                else:
                    bpy.ops.emp.transform('INVOKE_DEFAULT', mode='TRANSLATE', release_confirm=True,
                                          start_x=int(self.press.x), start_y=int(self.press.y))
                return {'FINISHED'}
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE' and getattr(self, "gizmo", None):
            # a click (no drag) on a scale gizmo handle shows that channel's handles
            st = state.settings(context.scene)
            st.scale_channel = {'GX': 'X', 'GXn': 'X', 'GY': 'Y', 'GZ': 'Z', 'GZn': 'Z'}.get(
                self.gizmo, 'NONE')
            # ... and picks both of them (Alt+S / Alt+Shift+S work on them right away)
            ch = {'X': 0, 'Y': 1, 'Z': 2}.get(st.scale_channel)
            state.set_whisker_sel(set() if ch is None else
                                  {(self.hit_key, round(self.hit_t, 3), ch, s) for s in ('L', 'R')}, context.scene)
            state.tag_redraw()
            return {'FINISHED'}
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE' and getattr(self, "ring", None):
            return {'FINISHED'}         # rings are only dragged
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            cache, k = self._find_key()
            if k is not None and self.release_action is not None:
                if self.release_action == 'TOGGLE':
                    pick.select_part(cache, k, self.hit_part, False)
                else:
                    pick.deselect_all(state.editable_caches(context.scene))
                    pick.select_part(cache, k, self.hit_part, True)
                pick.tag_anim_editors()
            return {'FINISHED'}
        if event.type in {'ESC', 'RIGHTMOUSE'}:
            return {'FINISHED'}
        return {'RUNNING_MODAL'}


def _chart_tick(context, mx, my):
    """(cache, frame) of a frame tick of the timing chart under the mouse."""
    ch = state.chart
    if ch is None or context.area is None or ch["area"] != context.area.as_pointer():
        return None
    us = compat.ui_scale()
    if abs(my - ch["y"]) > 12.0 * us:
        return None
    best = None
    for x, frame, is_key in ch["ticks"]:
        d = abs(x - mx)
        if not is_key and d <= 6.0 * us and (best is None or d < best[0]):
            best = (d, frame)
    if best is None:
        return None
    cache = state.caches.get(ch["cache"])
    return (cache, best[1]) if cache is not None else None


def _arc(cache, a, b):
    """Length of the path in 3D between frames a < b."""
    pts = [p for t, p in zip(cache.times, cache.points) if a - 1e-6 <= t <= b + 1e-6]
    return sum((q - p).length for p, q in zip(pts, pts[1:]))


def _is_loop(cache):
    if cache.cyclic:
        return True
    if len(cache.keys) < 3:
        return False
    pts = cache.points
    size = max((p - pts[0]).length for p in pts) or 1.0
    return (cache.keys[0].co - cache.keys[-1].co).length < size * 0.02


class EMP_OT_select_all(bpy.types.Operator):
    """Select or deselect all keys of the editable paths"""
    bl_idname = "emp.select_all"
    bl_label = "Select All Path Keys"
    bl_options = {'REGISTER', 'UNDO'}

    action: EnumProperty(items=[
        ('TOGGLE', "Toggle", ""), ('SELECT', "Select", ""),
        ('DESELECT', "Deselect", ""), ('INVERT', "Invert", "")], default='TOGGLE')

    def invoke(self, context, event):
        if not state.editable_caches(context.scene):
            return {'PASS_THROUGH'}
        return self.execute(context)

    def execute(self, context):
        caches = state.editable_caches(context.scene)
        if not caches:
            return {'CANCELLED'}
        action = self.action
        if action == 'TOGGLE':
            action = 'DESELECT' if any(k.any_sel for c in caches for k in c.keys) else 'SELECT'
        for c in caches:
            for k in c.keys:
                if action == 'SELECT':
                    pick.select_part(c, k, 'KEY', True)
                elif action == 'DESELECT':
                    pick.select_part(c, k, 'KEY', False)
                else:
                    pick.select_part(c, k, 'KEY', not k.sel)
        pick.tag_anim_editors()
        return {'FINISHED'}


class EMP_OT_box_select(bpy.types.Operator):
    """Box-select path keys and handles"""
    bl_idname = "emp.box_select"
    bl_label = "Box Select Path Keys"
    bl_options = {'REGISTER', 'UNDO'}

    mode: EnumProperty(items=[('SET', "Set", ""), ('ADD', "Extend", ""), ('SUB', "Subtract", "")],
                       default='SET', options={'SKIP_SAVE'})
    wait_for_input: BoolProperty(default=False, options={'SKIP_SAVE', 'HIDDEN'})

    def invoke(self, context, event):
        if context.region_data is None or not state.editable_caches(context.scene):
            return {'PASS_THROUGH'}
        if event.shift:
            self.mode = 'ADD'
        elif event.ctrl:
            self.mode = 'SUB'
        self.started = not self.wait_for_input
        region = context.region
        if self.started:
            self.p0 = Vector((event.mouse_prev_press_x - region.x, event.mouse_prev_press_y - region.y))
        else:
            self.p0 = Vector((event.mouse_region_x, event.mouse_region_y))
        self.p1 = Vector((event.mouse_region_x, event.mouse_region_y))
        state.box_area = context.area.as_pointer()
        self._show()
        context.window.cursor_modal_set('CROSSHAIR')
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _show(self):
        if self.started:
            state.box = (self.p0.x, self.p0.y, self.p1.x, self.p1.y, self.mode)
        else:
            state.box = None
        state.tag_redraw()

    def _end(self, context):
        state.box = None
        context.window.cursor_modal_restore()
        state.tag_redraw()

    def modal(self, context, event):
        m = Vector((event.mouse_region_x, event.mouse_region_y))
        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            self.p1 = m
            self._show()
        elif event.type in {'LEFTMOUSE', 'MIDDLEMOUSE'} and event.value == 'PRESS' and not self.started:
            self.started = True
            if event.type == 'MIDDLEMOUSE' or event.ctrl:
                self.mode = 'SUB'
            elif event.shift:
                self.mode = 'ADD'
            self.p0 = m
            self.p1 = m
            self._show()
        elif event.type in {'LEFTMOUSE', 'MIDDLEMOUSE'} and event.value == 'RELEASE' and self.started:
            self.p1 = m
            self._apply(context)
            self._end(context)
            return {'FINISHED'}
        elif event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            self._end(context)
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}

    def _apply(self, context):
        from bpy_extras.view3d_utils import location_3d_to_region_2d
        from . import draw
        region, rv3d = context.region, context.region_data
        x0, x1 = sorted((self.p0.x, self.p1.x))
        y0, y1 = sorted((self.p0.y, self.p1.y))
        st = state.settings(context.scene)

        def inside(p):
            s = location_3d_to_region_2d(region, rv3d, p)
            return s is not None and x0 <= s.x <= x1 and y0 <= s.y <= y1

        caches = state.editable_caches(context.scene)
        if self.mode == 'SET':
            pick.deselect_all(caches)
            for c in caches:
                c.refresh(force=True)
        value = self.mode != 'SUB'
        for c in caches:
            lo, hi = draw.path_window(context.scene, st, c)
            for k in c.keys:
                if not (lo - 1e-4 <= k.t <= hi + 1e-4):
                    continue
                shown = draw.handles_shown(st, c, True, k)
                if inside(k.co):
                    pick.select_part(c, k, 'KEY', value)
                    continue
                if not shown:
                    continue
                m = pick.handle_min_world(region, rv3d, k.co)
                if k.hl is not None and inside(pick.display_handle(k, 'L', m)):
                    pick.select_part(c, k, 'L', value)
                if k.hr is not None and inside(pick.display_handle(k, 'R', m)):
                    pick.select_part(c, k, 'R', value)
        pick.tag_anim_editors()


class EMP_OT_jump_to_key(bpy.types.Operator):
    """Jump to the frame of the double-clicked path key"""
    bl_idname = "emp.jump_to_key"
    bl_label = "Jump to Path Key"

    def invoke(self, context, event):
        if context.region_data is None:
            return {'PASS_THROUGH'}
        hit = pick.hit_test(context, event.mouse_region_x, event.mouse_region_y)
        if hit is None or hit.part != 'KEY':
            return {'PASS_THROUGH'}
        context.scene.frame_set(int(round(hit.t)))
        return {'FINISHED'}


class EMP_OT_context_menu(bpy.types.Operator):
    """Path editing menu (only when path keys are selected)"""
    bl_idname = "emp.context_menu"
    bl_label = "Motion Path Menu"

    def invoke(self, context, event):
        caches = state.editable_caches(context.scene)
        if not caches or pick.selected_count(caches) == 0:
            return {'PASS_THROUGH'}
        bpy.ops.wm.call_menu(name="EMP_MT_context")
        return {'FINISHED'}


class EMP_OT_handle_type_menu(bpy.types.Operator):
    """Set handle type of the selected path keys"""
    bl_idname = "emp.handle_type_menu"
    bl_label = "Handle Type Menu"

    def invoke(self, context, event):
        caches = state.editable_caches(context.scene)
        if not caches or pick.selected_count(caches) == 0:
            return {'PASS_THROUGH'}
        bpy.ops.wm.call_menu(name="EMP_MT_handle_type")
        return {'FINISHED'}


# ----------------------------------------------------------------------------
# More selection tools (mesh Edit Mode style)
# ----------------------------------------------------------------------------

def _any_selected(context):
    return any(k.any_sel for c in state.editable_caches(context.scene) for k in c.keys)


class EMP_OT_select_linked(bpy.types.Operator):
    """Select every key of the paths that have a selected key (Ctrl+L)"""
    bl_idname = "emp.select_linked"
    bl_label = "Select Linked Path Keys"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        if not _any_selected(context):
            return {'PASS_THROUGH'}
        return self.execute(context)

    def execute(self, context):
        for c in state.editable_caches(context.scene):
            pieces = {k.piece for k in c.keys if k.any_sel and not k.hidden}
            for k in c.keys:
                if k.piece in pieces:
                    pick.select_part(c, k, 'KEY', True)
        pick.tag_anim_editors()
        return {'FINISHED'}


class EMP_OT_select_linked_pick(bpy.types.Operator):
    """Select every key of the path under the mouse (L; Shift+L deselects)"""
    bl_idname = "emp.select_linked_pick"
    bl_label = "Pick Linked Path Keys"
    bl_options = {'REGISTER', 'UNDO'}

    deselect: BoolProperty(name="Deselect", default=False)

    def invoke(self, context, event):
        if context.region_data is None:
            return {'PASS_THROUGH'}
        mx, my = event.mouse_region_x, event.mouse_region_y
        hit = pick.hit_test(context, mx, my) or pick.line_hit(context, mx, my, 20.0 * compat.ui_scale())
        if hit is None:
            return {'PASS_THROUGH'}
        cache = hit.cache
        if not cache.owner_selected():
            if self.deselect or not pick.select_owner(context, cache, True):
                return {'PASS_THROUGH'}
            cache.refresh(force=True)
        if hit.key is not None:
            piece = hit.key.piece
        else:
            before = [k for k in cache.keys if k.t <= hit.t + 1e-6]
            piece = before[-1].piece if before else cache.keys[0].piece if cache.keys else 0
        for k in cache.keys:
            if k.piece == piece:
                pick.select_part(cache, k, 'KEY', not self.deselect)
        pick.tag_anim_editors()
        return {'FINISHED'}


def _ring(cache):
    return cache.cyclic or _is_loop(cache)


class EMP_OT_select_more_less(bpy.types.Operator):
    """Grow or shrink the key selection along the path (Ctrl+NumPad + / -)"""
    bl_idname = "emp.select_more_less"
    bl_label = "Select More / Less"
    bl_options = {'REGISTER', 'UNDO'}

    less: BoolProperty(name="Less", default=False)

    def invoke(self, context, event):
        if not _any_selected(context):
            return {'PASS_THROUGH'}
        return self.execute(context)

    def execute(self, context):
        for c in state.editable_caches(context.scene):
            sel = [k.sel for k in c.keys]
            n = len(sel)
            if not any(sel):
                continue
            ring = _ring(c) and n > 2

            def at(i):
                if ring:
                    return sel[i % n]
                return sel[i] if 0 <= i < n else None
            new = []
            for i in range(n):
                nb = [x for x in (at(i - 1), at(i + 1)) if x is not None]
                if self.less:
                    new.append(sel[i] and all(nb))
                else:
                    new.append(sel[i] or any(nb))
            for k, s in zip(c.keys, new):
                if s != k.sel:
                    pick.select_part(c, k, 'KEY', s)
        pick.tag_anim_editors()
        return {'FINISHED'}


class EMP_OT_select_side(bpy.types.Operator):
    """Select the keys before ([) or after (]) the current frame"""
    bl_idname = "emp.select_side"
    bl_label = "Select Before / After Current Frame"
    bl_options = {'REGISTER', 'UNDO'}

    side: EnumProperty(name="Side", items=[('LEFT', "Before Current Frame", ""),
                                           ('RIGHT', "After Current Frame", "")], default='LEFT')
    extend: BoolProperty(name="Extend", default=False)

    def invoke(self, context, event):
        if not state.editable_caches(context.scene):
            return {'PASS_THROUGH'}
        return self.execute(context)

    def execute(self, context):
        cur = context.scene.frame_current + context.scene.frame_subframe
        caches = state.editable_caches(context.scene)
        if not self.extend:
            pick.deselect_all(caches)
            for c in caches:
                c.refresh(force=True)
        for c in caches:
            for k in c.keys:
                if (k.t <= cur + 1e-4) if self.side == 'LEFT' else (k.t >= cur - 1e-4):
                    pick.select_part(c, k, 'KEY', True)
        pick.tag_anim_editors()
        return {'FINISHED'}


SIMILAR = [
    ('HANDLE_TYPE', "Handle Type", "Keys with the same handle type"),
    ('INTERPOLATION', "Interpolation", "Keys with the same interpolation"),
    ('KEY_TYPE', "Keyframe Type", "Keys of the same keyframe type (keyframe, breakdown...)"),
    ('PARTIAL', "Partial Keys", "Keys that are keyed on some channels only (dark center), or "
                                "fully keyed ones"),
    ('HOLD', "Holds", "Keys where the motion stops (or keys where it doesn't)"),
]


def _key_type(c, k):
    tg = c.target()
    ch = tg.channels(c.ptype)
    for ci, fc in enumerate(ch.fcurves):
        i = k.idx[ci] if ci < len(k.idx) else -1
        if fc is not None and i >= 0:
            return fc.keyframe_points[i].type
    return 'KEYFRAME'


def _is_hold(c, k):
    tg = c.target()
    if tg is None or not c.speeds:
        return False
    v = (c.point(tg, min(k.t + 0.5, c.fe)) - c.point(tg, max(k.t - 0.5, c.fs))).length
    vmax = max(c.speeds)
    return v < 0.03 * vmax


class EMP_OT_select_similar(bpy.types.Operator):
    """Select keys similar to the selected ones (Shift+G)"""
    bl_idname = "emp.select_similar"
    bl_label = "Select Similar Path Keys"
    bl_options = {'REGISTER', 'UNDO'}

    type: EnumProperty(name="Type", items=SIMILAR, default='HANDLE_TYPE')

    def invoke(self, context, event):
        if not _any_selected(context):
            return {'PASS_THROUGH'}
        bpy.ops.wm.call_menu(name="EMP_MT_select_similar")
        return {'FINISHED'}

    def execute(self, context):
        caches = state.editable_caches(context.scene)

        def attr(c, k):
            if self.type == 'HANDLE_TYPE':
                return (k.type_l, k.type_r)
            if self.type == 'INTERPOLATION':
                return k.interp
            if self.type == 'KEY_TYPE':
                return _key_type(c, k)
            if self.type == 'PARTIAL':
                return k.partial
            return _is_hold(c, k)
        want = {attr(c, k) for c in caches for k in c.keys if k.sel}
        if not want:
            return {'CANCELLED'}
        for c in caches:
            for k in c.keys:
                if not k.sel and attr(c, k) in want:
                    pick.select_part(c, k, 'KEY', True)
        pick.tag_anim_editors()
        return {'FINISHED'}


class EMP_MT_select_similar(bpy.types.Menu):
    bl_label = "Select Similar"
    bl_idname = "EMP_MT_select_similar"

    def draw(self, context):
        self.layout.operator_context = 'EXEC_DEFAULT'
        self.layout.operator_enum("emp.select_similar", "type")


class EMP_OT_hide(bpy.types.Operator):
    """Hide the selected keys (H) or the unselected ones (Shift+H). Hidden keys can't be """ \
        """selected or moved, and the path around them is left out, so it falls apart in pieces """ \
        """(proportional editing and Ctrl+L stay on the visible piece)"""
    bl_idname = "emp.hide"
    bl_label = "Hide Keys"
    bl_options = {'REGISTER', 'UNDO'}

    unselected: BoolProperty(name="Unselected", default=False)

    def invoke(self, context, event):
        if not _any_selected(context):
            return {'PASS_THROUGH'}
        return self.execute(context)

    def execute(self, context):
        n = 0
        for c in state.editable_caches(context.scene):
            if not any(k.any_sel for k in c.keys):
                continue
            e = state.entry_of(c, context.scene)
            if e is None:
                continue
            hide = [k for k in c.keys if not k.hidden and (k.any_sel != self.unselected)]
            if not hide:
                continue
            for k in hide:
                pick.select_part(c, k, 'KEY', False)
            frames = state.parse_frames(e.hidden_keys) | {round(k.t, 3) for k in hide}
            e.hidden_keys = state.format_frames(frames)
            n += len(hide)
        if not n:
            return {'CANCELLED'}
        state.sync_caches(context.scene)
        pick.tag_anim_editors()
        return {'FINISHED'}


class EMP_OT_reveal(bpy.types.Operator):
    """Show the hidden keys again, selected (Alt+H; also reveals hidden paths, bones and """ \
        """objects)"""
    bl_idname = "emp.reveal"
    bl_label = "Reveal Keys"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        self.execute(context)
        return {'PASS_THROUGH'}     # Blender reveals bones / objects too

    def execute(self, context):
        st = state.settings(context.scene)
        shown = {}
        for e in st.paths:
            e.show = True
            if e.hidden_keys and e.object is not None:
                shown[(e.object.name, e.bone_name)] = state.parse_frames(e.hidden_keys)
                e.hidden_keys = ""
        state.sync_caches(context.scene)
        for c in state.visible_caches(context.scene):
            frames = shown.get(c.key[:2])
            if frames and c.refresh(force=True):
                for k in c.keys:
                    if round(k.t, 3) in frames:
                        pick.select_part(c, k, 'KEY', True)
        pick.tag_anim_editors()
        state.tag_redraw()
        return {'FINISHED'}


class EMP_OT_circle_select(bpy.types.Operator):
    """Paint-select path keys with a circle (C). Wheel: radius, LMB: select, """ \
        """MMB / Shift+LMB: deselect, RMB / Enter: done"""
    bl_idname = "emp.circle_select"
    bl_label = "Circle Select Path Keys"
    bl_options = {'REGISTER', 'UNDO'}

    radius: bpy.props.IntProperty(name="Radius", default=25, min=1)

    def invoke(self, context, event):
        if context.region_data is None or not state.editable_caches(context.scene):
            return {'PASS_THROUGH'}
        self.painting = None
        self.pos = Vector((event.mouse_region_x, event.mouse_region_y))
        state.circle = (self.pos.x, self.pos.y, self.radius, context.area.as_pointer())
        context.window_manager.modal_handler_add(self)
        context.area.header_text_set("Circle select: LMB select, MMB / Shift+LMB deselect, "
                                     "wheel / +- radius, RMB / Enter done")
        state.tag_redraw()
        return {'RUNNING_MODAL'}

    def _paint(self, context, value):
        from bpy_extras.view3d_utils import location_3d_to_region_2d
        region, rv3d = context.region, context.region_data
        r2 = self.radius * self.radius
        changed = False
        for c in state.editable_caches(context.scene):
            for k in c.keys:
                s = location_3d_to_region_2d(region, rv3d, k.co)
                if s is not None and (s - self.pos).length_squared <= r2 and k.sel != value:
                    pick.select_part(c, k, 'KEY', value)
                    changed = True
        if changed:
            pick.tag_anim_editors()

    def modal(self, context, event):
        t, v = event.type, event.value
        if t in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            self.pos = Vector((event.mouse_region_x, event.mouse_region_y))
            if self.painting is not None:
                self._paint(context, self.painting)
        elif t in {'LEFTMOUSE', 'MIDDLEMOUSE'}:
            if v == 'PRESS':
                self.painting = not (t == 'MIDDLEMOUSE' or event.shift or event.ctrl)
                self._paint(context, self.painting)
            elif v == 'RELEASE':
                self.painting = None
        elif t in {'WHEELUPMOUSE', 'NUMPAD_PLUS', 'EQUAL'} and v == 'PRESS':
            self.radius = int(self.radius * 1.15) + 1
        elif t in {'WHEELDOWNMOUSE', 'NUMPAD_MINUS', 'MINUS'} and v == 'PRESS':
            self.radius = max(2, int(self.radius / 1.15))
        elif t in {'RIGHTMOUSE', 'ESC', 'RET', 'NUMPAD_ENTER', 'SPACE'} and v == 'PRESS':
            state.circle = None
            context.area.header_text_set(None)
            state.tag_redraw()
            return {'FINISHED'}
        state.circle = (self.pos.x, self.pos.y, self.radius, context.area.as_pointer())
        state.tag_redraw()
        return {'RUNNING_MODAL'}


classes = (EMP_OT_click, EMP_OT_select_all, EMP_OT_box_select, EMP_OT_jump_to_key,
           EMP_OT_select_linked, EMP_OT_select_linked_pick, EMP_OT_select_more_less,
           EMP_OT_select_side, EMP_OT_select_similar, EMP_MT_select_similar, EMP_OT_hide,
           EMP_OT_reveal, EMP_OT_circle_select,
           EMP_OT_context_menu, EMP_OT_handle_type_menu)
