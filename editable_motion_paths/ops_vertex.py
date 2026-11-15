"""Track Motion Path: the path of one mesh vertex (Edit Mode, the active vertex; right-click menu).
It is only to look at - the vertex has no keys of its own to edit. Its positions are worked out by
stepping through the frames of a range (the mesh as Blender makes it, a little at a time: Esc
stops) and kept in the file (scene.emp.vertex_paths)."""

import time

import bpy
from bpy.props import BoolProperty, IntProperty

from . import state

# modifiers that only move the vertices: the rest (subdivision, mirror...) are switched off while
# tracking, so the vertex is the same one in the mesh as made
_DEFORMS = {'ARMATURE', 'CAST', 'CLOTH', 'CORRECTIVE_SMOOTH', 'CURVE', 'DISPLACE', 'HOOK',
            'LAPLACIANDEFORM', 'LAPLACIANSMOOTH', 'LATTICE', 'MESH_CACHE', 'MESH_DEFORM',
            'MESH_SEQUENCE_CACHE', 'SHRINKWRAP', 'SIMPLE_DEFORM', 'SMOOTH', 'SOFT_BODY',
            'SURFACE_DEFORM', 'WARP', 'WAVE', 'VOLUME_DISPLACE', 'NORMAL_EDIT', 'WEIGHTED_NORMAL',
            'UV_PROJECT', 'UV_WARP', 'VERTEX_WEIGHT_EDIT', 'VERTEX_WEIGHT_MIX', 'VERTEX_WEIGHT_PROXIMITY',
            'DATA_TRANSFER'}


# ----------------------------------------------------------------------------
# Following the animation by itself: a vertex moved by an Armature modifier is where its bones put it -
# skinning (the bones' matrices on each frame, weighted by its vertex groups). The bones' matrices are
# their parent spaces (baked like any path's: hidden paths of their own - state.helper_caches) times
# their own channels now: an edit of a bone shows at once, one of its parents once baked again (or at
# once: live.py, for parents with paths of their own).
# ----------------------------------------------------------------------------

# modifiers that move the vertices of the mesh itself (the others, generating geometry, were off while
# tracking anyway)
_MOVERS = {'ARMATURE', 'CAST', 'CLOTH', 'CORRECTIVE_SMOOTH', 'CURVE', 'DISPLACE', 'HOOK', 'LAPLACIANDEFORM',
           'LAPLACIANSMOOTH', 'LATTICE', 'MESH_CACHE', 'MESH_DEFORM', 'MESH_SEQUENCE_CACHE', 'SHRINKWRAP',
           'SIMPLE_DEFORM', 'SMOOTH', 'SOFT_BODY', 'SURFACE_DEFORM', 'WARP', 'WAVE', 'VOLUME_DISPLACE', 'NODES'}


def lbs_setup(e):
    """How the tracked vertex follows its armature: ({"arm", "bones": [[bone, weight, rest^-1]], "v", "K", "M"},
    "") - or (None, why) when it isn't only an Armature modifier's doing (a lattice, cloth, shape keys...: its
    path is updated by tracking it again)."""
    obj = e.object
    if obj is None or obj.type != 'MESH':
        return None, "not a mesh"
    me = obj.data
    if e.index >= len(me.vertices):
        return None, "the mesh has fewer vertices now"
    if me.shape_keys is not None:
        return None, "shape keys move it too"
    movers = [m for m in obj.modifiers if m.show_viewport and m.type in _MOVERS]
    if len(movers) != 1 or movers[0].type != 'ARMATURE':
        return None, "not only an Armature modifier moves it"
    md = movers[0]
    arm = md.object
    if arm is None or arm.type != 'ARMATURE' or not md.use_vertex_groups or md.use_bone_envelopes or \
            md.use_deform_preserve_volume or getattr(md, "use_multi_modifier", False) or md.vertex_group:
        return None, "the Armature modifier's options (envelopes, Preserve Volume, a mask)"
    ad = obj.animation_data
    if any(not c.mute and c.influence > 0.0 for c in obj.constraints) or \
            (ad is not None and (ad.action is not None or len(ad.drivers))):
        return None, "the mesh object moves by itself"
    setup = {"arm": arm.name, "v": me.vertices[e.index].co.copy(), "K": None, "M": None}
    if obj.parent == arm and obj.parent_type in {'OBJECT', 'ARMATURE'}:
        setup["K"] = obj.matrix_parent_inverse @ obj.matrix_basis      # (the mesh in the armature's space)
    elif obj.parent is None:
        setup["M"] = obj.matrix_world.copy()
    else:
        return None, "the mesh hangs on something else"
    bones = []
    total = 0.0
    for g in me.vertices[e.index].groups:
        if g.weight <= 0.0 or g.group >= len(obj.vertex_groups):
            continue
        b = arm.data.bones.get(obj.vertex_groups[g.group].name)
        if b is not None and b.use_deform:
            bones.append([b.name, g.weight, b.matrix_local.inverted_safe()])
            total += g.weight
    for x in bones:
        x[1] /= total
    setup["bones"] = bones
    return setup, ""


def _keys_of(setup):
    keys = [(setup["arm"], b) for b, _w, _r in setup["bones"]]
    if setup["M"] is not None or not setup["bones"]:
        keys.append((setup["arm"], ""))
    return keys


def helper_keys(st):
    """The bones (and armature objects) the tracked vertices following the animation need the matrices of:
    {(object name, bone name)}."""
    out = set()
    for e in (st.vertex_paths if st is not None else ()):
        if not e.show or not getattr(e, "auto_update", True):
            continue
        setup, _why = lbs_setup(e)
        if setup is not None:
            out.update(_keys_of(setup))
    return out


def _world_cache(obj_name, bone):
    """A baked cache whose parent space is the whole one (world = B @ basis) for this bone / object: a path's, or
    the hidden one made for the tracked vertices."""
    from . import core
    for mode in (core.ROTATION, core.SCALE, core.LOCATION):
        c = state.caches.get((obj_name, bone, mode))
        # (its B as baked - Local / Camera / Relative To only change how it is drawn)
        if c is not None and c.B and (mode != core.LOCATION or not state._own_loc_space(obj_name, bone)):
            return c
    c = state.helper_caches.get((obj_name, bone, core.ROTATION))
    return c if c is not None and c.B else None


_memo = {}      # (object name, index) -> (signature, points)


def current_points(e):
    """The tracked vertex's path following the animation as it is now - None when it can't (then the points kept
    when it was tracked are drawn)."""
    if not getattr(e, "auto_update", True) or e.object is None:
        return None
    setup, _why = lbs_setup(e)
    if setup is None:
        return None
    caches = []
    for k in _keys_of(setup):
        c = _world_cache(*k)
        if c is None:
            return None
        c.refresh()
        caches.append(c)
    need = setup["bones"]
    sig = (tuple((id(c.B), c.fs, len(c.B), c.sig) for c in caches), e.frame_start, len(e.points),
           tuple(setup["v"]), tuple(tuple(r) for r in (setup["K"] if setup["K"] is not None else setup["M"])),
           tuple((b, round(w, 6)) for b, w, _r in need))
    got = _memo.get((e.object.name, e.index))
    if got is not None and got[0] == sig:
        return got[1]
    tgs = [c.target() for c in caches]
    if any(tg is None for tg in tgs):
        return None
    n = max(len(e.points), 2)
    f0 = e.frame_start
    v = setup["v"].to_4d()
    pts = []
    for i in range(n):
        t = float(f0 + i)
        W = []
        for c, tg in zip(caches, tgs):
            j = min(max(int(round(t)) - c.fs, 0), len(c.B) - 1)
            W.append(c.B[j] @ tg.basis(tg.loc.values(t), tg.rot.values(t), tg.scale.values(t)))
        K = setup["K"]
        if setup["M"] is not None:
            K = W[-1].inverted_safe() @ setup["M"]      # (a mesh that stays: seen from the armature moving)
        if not need:
            p = (W[-1] @ K @ v) if setup["K"] is not None else (setup["M"] @ v)
        else:
            p = None
            for (_b, w, Rinv), Wi in zip(need, W):
                q = (Wi @ Rinv @ K @ v) * w
                p = q if p is None else p + q
        pts.append(tuple(p.to_3d()))
    _memo[(e.object.name, e.index)] = (sig, pts)
    if len(_memo) > 64:
        _memo.clear()
    return pts


def store_points(st):
    """The tracked vertices' paths as they follow the animation now, kept in the file (after a bake - drawing
    can't write)."""
    for e in (st.vertex_paths if st is not None else ()):
        try:
            pts = current_points(e)
        except Exception:
            pts = None
        if pts is None or len(pts) != len(e.points):
            continue
        if all(abs(a - b) < 1e-6 for p, q in zip(pts, e.points) for a, b in zip(p, q.co)):
            continue
        for p, q in zip(pts, e.points):
            q.co = p


def active_vertex(obj):
    """The index of the mesh's active vertex in Edit Mode - or the vertex selected last, or its only
    selected one - or None."""
    import bmesh
    if obj is None or obj.type != 'MESH' or obj.mode != 'EDIT':
        return None
    bm = bmesh.from_edit_mesh(obj.data)
    hist = [e for e in bm.select_history if isinstance(e, bmesh.types.BMVert) and e.select]
    v = hist[-1] if hist else None
    if v is None:
        sel = [x for x in bm.verts if x.select]
        v = sel[0] if len(sel) == 1 else None
    if v is None:
        return None
    bm.verts.index_update()
    return v.index


def tracked_vertex(context):
    """(object, vertex index) to track: the active object's active vertex, else another object's in
    Edit Mode (several meshes edited at once); None when there is none."""
    objs = [context.edit_object] + [o for o in (context.objects_in_mode or []) if o != context.edit_object]
    for o in objs:
        i = active_vertex(o)
        if i is not None:
            return o, i
    return None


class EMP_OT_track_vertex(bpy.types.Operator):
    """Show the motion path of the active vertex (only to look at: the vertex has no keys to """ \
        """edit). Its positions are worked out over the range frame by frame - on a heavy mesh that """ \
        """takes a while (Esc stops)"""
    bl_idname = "emp.track_vertex"
    bl_label = "Track Motion Path"
    bl_options = {'REGISTER'}

    # (never remembered from the last time: no range given means the scene's)
    frame_start: IntProperty(name="Start", description="First frame of the path", options={'SKIP_SAVE'})
    frame_end: IntProperty(name="End", description="Last frame of the path", options={'SKIP_SAVE'})
    entry: IntProperty(default=-1, options={'HIDDEN', 'SKIP_SAVE'})     # (Update: a vertex tracked before)

    @classmethod
    def description(cls, context, props):
        if props.entry >= 0:
            return ("Update Static Path: its positions worked out again over its range, with the mesh as Blender "
                    "makes it (from any mode)")
        return cls.__doc__

    BUDGET = 0.04       # seconds of frame stepping per tick (Blender stays responsive)

    @classmethod
    def poll(cls, context):
        if context.mode != 'EDIT_MESH':
            st = state.settings(context.scene)
            return st is not None and len(st.vertex_paths) > 0     # (Update of a tracked one)
        if tracked_vertex(context) is None:
            cls.poll_message_set("Click a vertex first (Vertex select mode): the active vertex is tracked")
            return False
        return True

    def invoke(self, context, event):
        if self.entry >= 0:
            return self.execute(context)
        sc = context.scene
        self.frame_start, self.frame_end = sc.frame_start, sc.frame_end
        return context.window_manager.invoke_props_dialog(self, title="Track Motion Path",
                                                          confirm_text="Create Motion Path")

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.label(text="Range", icon='VERTEXSEL')
        row = col.row(align=True)
        row.prop(self, "frame_start")
        row.prop(self, "frame_end")

    def execute(self, context):
        sc = context.scene
        st = state.settings(sc)
        if self.entry >= 0:
            # Update: that vertex again, over its own range
            if st is None or not 0 <= self.entry < len(st.vertex_paths) or st.vertex_paths[self.entry].object is None:
                return {'CANCELLED'}
            e = st.vertex_paths[self.entry]
            obj, idx = e.object, e.index
            self.frame_start, self.frame_end = e.frame_start, e.frame_start + max(len(e.points) - 1, 1)
        else:
            got = tracked_vertex(context)
            if got is None:
                self.report({'WARNING'}, "No active vertex: click a vertex first")
                return {'CANCELLED'}
            obj, idx = got
            if not (self.properties.is_property_set("frame_start") or self.properties.is_property_set("frame_end")):
                self.frame_start, self.frame_end = sc.frame_start, sc.frame_end     # (no range given: the scene's)
        a, b = sorted((self.frame_start, self.frame_end))
        if b - a < 1:
            self.report({'WARNING'}, "The range needs at least two frames")
            return {'CANCELLED'}
        self.obj_name, self.idx, self.f0, self.f1 = obj.name, idx, a, b
        self.f = a
        self.pts = []
        self.cur, self.sub = sc.frame_current, sc.frame_subframe
        # (in Edit Mode the mesh isn't bent by its armature: tracked in Object Mode, back after - an Update from
        # another mode leaves the modes alone)
        self.edit = obj.mode == 'EDIT'
        if self.edit:
            if context.view_layer.objects.active != obj:
                context.view_layer.objects.active = obj
            bpy.ops.object.mode_set(mode='OBJECT')
        self.held = state.keep_scene_pose(sc, [obj])
        self.off = [m for m in obj.modifiers if m.show_viewport and m.type not in _DEFORMS]
        for m in self.off:
            m.show_viewport = False
        state.modal_running += 1
        self.t_start = time.time()
        self.area = context.area
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.01, window=context.window)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == 'ESC' and event.value == 'PRESS':
            return self._end(context, False)
        if event.type != 'TIMER':
            return {'RUNNING_MODAL'} if event.type not in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE'} \
                else {'PASS_THROUGH'}
        sc = context.scene
        obj = bpy.data.objects.get(self.obj_name)
        if obj is None:
            return self._end(context, False)
        dg = context.evaluated_depsgraph_get()
        t0 = time.time()
        while self.f <= self.f1 and time.time() - t0 < self.BUDGET:
            sc.frame_set(self.f)
            ev = obj.evaluated_get(dg)
            vs = ev.data.vertices
            if self.idx >= len(vs):
                self.report({'WARNING'}, "The mesh as made has other vertices: can't follow this one")
                return self._end(context, False)
            self.pts.append(tuple(ev.matrix_world @ vs[self.idx].co))
            self.f += 1
        if self.area is not None:
            self.area.header_text_set("Track Motion Path: frame %d / %d   (Esc: stop)" % (
                min(self.f, self.f1), self.f1))
        if self.f > self.f1:
            return self._end(context, True)
        return {'RUNNING_MODAL'}

    def _end(self, context, ok):
        wm = context.window_manager
        if self._timer is not None:
            wm.event_timer_remove(self._timer)
            self._timer = None
        state.modal_running = max(0, state.modal_running - 1)
        sc = context.scene
        sc.frame_set(self.cur, subframe=self.sub)
        try:
            state.put_pose_back(self.held)
        except Exception:
            pass
        for m in self.off:
            m.show_viewport = True
        if self.area is not None:
            self.area.header_text_set(None)
        obj = bpy.data.objects.get(self.obj_name)
        if ok:
            st = state.settings(sc)
            e = next((e for e in st.vertex_paths if e.object == obj and e.index == self.idx), None)
            if e is None:
                e = st.vertex_paths.add()
                e.object, e.index = obj, self.idx
            e.frame_start = self.f0
            e.points.clear()
            for p in self.pts:
                e.points.add().co = p
            e.show = True
            try:
                bpy.ops.ed.undo_push(message="Track Motion Path")     # (Ctrl+Z takes the path away again)
            except RuntimeError:
                pass
        if obj is not None and self.edit:
            if context.view_layer.objects.active != obj:
                context.view_layer.objects.active = obj
            bpy.ops.object.mode_set(mode='EDIT')
        if not ok:
            return {'CANCELLED'}
        state.tag_redraw()
        self.report({'INFO'}, "Motion path of vertex %d: frames %d - %d (%.1f s)" % (
            self.idx, self.f0, self.f0 + len(self.pts) - 1, time.time() - self.t_start))
        return {'FINISHED'}


def is_live(e):
    return getattr(e, "auto_update", True) and e.object is not None and lbs_setup(e)[0] is not None


class EMP_OT_vertex_path_mode(bpy.types.Operator):
    """Live: the path follows the animation by itself. Static: kept as it was tracked"""
    bl_idname = "emp.vertex_path_mode"
    bl_label = "Vertex Path Mode"
    bl_options = {'UNDO', 'INTERNAL'}

    index: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})
    live: BoolProperty(options={'SKIP_SAVE'})

    @classmethod
    def description(cls, context, props):
        st = state.settings(context.scene)
        e = st.vertex_paths[props.index] if st is not None and 0 <= props.index < len(st.vertex_paths) else None
        if props.live:
            why = lbs_setup(e)[1] if e is not None else ""
            return ("Live: the path follows the animation by itself - worked out from the bones (an edit shows at "
                    "once, while dragging where it can)" + ("" if not why else ". Not for this vertex: " + why +
                                                            " - use Static and Update"))
        return "Static: the path is kept as it was tracked; Update Static Path tracks it again over its range"

    def execute(self, context):
        st = state.settings(context.scene)
        if st is None or not 0 <= self.index < len(st.vertex_paths):
            return {'CANCELLED'}
        e = st.vertex_paths[self.index]
        if self.live and lbs_setup(e)[0] is None:
            self.report({'INFO'}, "This vertex can't follow by itself (%s): Static, with Update Static Path" %
                        lbs_setup(e)[1])
            return {'CANCELLED'}
        e.auto_update = self.live
        state.tag_redraw()
        return {'FINISHED'}


class EMP_OT_vertex_path_mode_menu(bpy.types.Operator):
    """Live (follows the animation by itself) or Static (kept as tracked, Update Static Path tracks it again)"""
    bl_idname = "emp.vertex_path_mode_menu"
    bl_label = "Vertex Path Mode"
    bl_options = {'INTERNAL'}

    index: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def invoke(self, context, event):
        st = state.settings(context.scene)
        if st is None or not 0 <= self.index < len(st.vertex_paths):
            return {'CANCELLED'}
        index = self.index
        e = st.vertex_paths[index]
        live = is_live(e)
        can = lbs_setup(e)[0] is not None

        def draw(menu, _context):
            lay = menu.layout
            lay.operator_context = 'INVOKE_DEFAULT'
            sub = lay.row()
            sub.enabled = can
            op = sub.operator("emp.vertex_path_mode", text="Live", icon='PLAY', depress=live)
            op.index, op.live = index, True
            op = lay.operator("emp.vertex_path_mode", text="Static", icon='FREEZE', depress=not live)
            op.index, op.live = index, False
            lay.separator()
            lay.operator("emp.track_vertex", text="Update Static Path", icon='FILE_REFRESH').entry = index
        context.window_manager.popup_menu(draw, title="Vertex Path")
        return {'FINISHED'}


class EMP_OT_vertex_path_remove(bpy.types.Operator):
    """Remove this vertex's motion path (-1: the one picked in the list)"""
    bl_idname = "emp.vertex_path_remove"
    bl_label = "Remove Vertex Path"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    index: IntProperty(default=-1)

    def execute(self, context):
        st = state.settings(context.scene)
        i = st.vertex_active_index if self.index < 0 else self.index
        if st is None or not 0 <= i < len(st.vertex_paths):
            return {'CANCELLED'}
        st.vertex_paths.remove(i)
        st.vertex_active_index = min(st.vertex_active_index, len(st.vertex_paths) - 1)
        state.tag_redraw()
        return {'FINISHED'}


def context_menu_entry(self, context):
    """Edit Mode right-click menu, at the bottom."""
    self.layout.separator()
    # (the menu may run its items without their popups - this one asks for the range first)
    self.layout.operator_context = 'INVOKE_DEFAULT'
    self.layout.operator(EMP_OT_track_vertex.bl_idname, icon='VERTEXSEL')


class EMP_UL_vertex_paths(bpy.types.UIList):
    """The tracked vertices: their paths are only to look at."""

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, "show", text="", emboss=False, icon='HIDE_OFF' if item.show else 'HIDE_ON')
        name = "%s  ·  %d" % (item.object.name if item.object else "?", item.index)
        row.label(text=name, icon='VERTEXSEL')
        sub = row.row()
        sub.alignment = 'RIGHT'
        sub.label(text="%d - %d" % (item.frame_start, item.frame_start + max(len(item.points) - 1, 0)))
        # Live (follows the animation by itself) / Static (as tracked): the icon opens a menu, like the paths'
        # spaces; Static: its Update button next to it
        live = is_live(item)
        sub = row.row(align=True)
        sub.operator("emp.vertex_path_mode_menu", text="", emboss=False,
                     icon='PLAY' if live else 'FREEZE').index = index
        if not live:
            sub.operator("emp.track_vertex", text="", icon='FILE_REFRESH', emboss=False).entry = index


classes = (EMP_OT_track_vertex, EMP_OT_vertex_path_mode, EMP_OT_vertex_path_mode_menu, EMP_OT_vertex_path_remove,
           EMP_UL_vertex_paths)
