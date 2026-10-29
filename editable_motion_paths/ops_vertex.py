"""Track Motion Path: the path of one mesh vertex (Edit Mode, the active vertex; right-click menu).
It is only to look at - the vertex has no keys of its own to edit. Its positions are worked out by
stepping through the frames of a range (the mesh as Blender makes it, a little at a time: Esc
stops) and kept in the file (scene.emp.vertex_paths)."""

import time

import bpy
from bpy.props import IntProperty

from . import state

# modifiers that only move the vertices: the rest (subdivision, mirror...) are switched off while
# tracking, so the vertex is the same one in the mesh as made
_DEFORMS = {'ARMATURE', 'CAST', 'CLOTH', 'CORRECTIVE_SMOOTH', 'CURVE', 'DISPLACE', 'HOOK',
            'LAPLACIANDEFORM', 'LAPLACIANSMOOTH', 'LATTICE', 'MESH_CACHE', 'MESH_DEFORM',
            'MESH_SEQUENCE_CACHE', 'SHRINKWRAP', 'SIMPLE_DEFORM', 'SMOOTH', 'SOFT_BODY',
            'SURFACE_DEFORM', 'WARP', 'WAVE', 'VOLUME_DISPLACE', 'NORMAL_EDIT', 'WEIGHTED_NORMAL',
            'UV_PROJECT', 'UV_WARP', 'VERTEX_WEIGHT_EDIT', 'VERTEX_WEIGHT_MIX', 'VERTEX_WEIGHT_PROXIMITY',
            'DATA_TRANSFER'}


def active_vertex(obj):
    """The index of the mesh's active vertex in Edit Mode (or its only selected one), or None."""
    import bmesh
    if obj is None or obj.type != 'MESH' or obj.mode != 'EDIT':
        return None
    bm = bmesh.from_edit_mesh(obj.data)
    act = bm.select_history.active
    if isinstance(act, bmesh.types.BMVert):
        bm.verts.index_update()
        return act.index
    sel = [v for v in bm.verts if v.select]
    if len(sel) == 1:
        bm.verts.index_update()
        return sel[0].index
    return None


class EMP_OT_track_vertex(bpy.types.Operator):
    """Show the motion path of the active vertex (only to look at: the vertex has no keys to """ \
        """edit). Its positions are worked out over the range frame by frame - on a heavy mesh that """ \
        """takes a while (Esc stops)"""
    bl_idname = "emp.track_vertex"
    bl_label = "Track Motion Path"
    bl_options = {'REGISTER'}

    frame_start: IntProperty(name="Start", description="First frame of the path")
    frame_end: IntProperty(name="End", description="Last frame of the path")

    BUDGET = 0.04       # seconds of frame stepping per tick (Blender stays responsive)

    @classmethod
    def poll(cls, context):
        return active_vertex(context.edit_object) is not None

    def invoke(self, context, event):
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
        obj = context.edit_object
        idx = active_vertex(obj)
        if idx is None:
            self.report({'WARNING'}, "No active vertex")
            return {'CANCELLED'}
        a, b = sorted((self.frame_start, self.frame_end))
        sc = context.scene
        self.obj_name, self.idx, self.f0, self.f1 = obj.name, idx, a, b
        self.f = a
        self.pts = []
        self.cur, self.sub = sc.frame_current, sc.frame_subframe
        # (in Edit Mode the mesh isn't bent by its armature: tracked in Object Mode, back after)
        bpy.ops.object.mode_set(mode='OBJECT')
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
        for m in self.off:
            m.show_viewport = True
        if self.area is not None:
            self.area.header_text_set(None)
        obj = bpy.data.objects.get(self.obj_name)
        if obj is not None:
            if context.view_layer.objects.active != obj:
                context.view_layer.objects.active = obj
            bpy.ops.object.mode_set(mode='EDIT')
        if not ok:
            return {'CANCELLED'}
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
        state.tag_redraw()
        self.report({'INFO'}, "Motion path of vertex %d: %d frames (%.1f s)" % (
            self.idx, len(self.pts), time.time() - self.t_start))
        return {'FINISHED'}


class EMP_OT_vertex_path_remove(bpy.types.Operator):
    """Remove this vertex's motion path"""
    bl_idname = "emp.vertex_path_remove"
    bl_label = "Remove Vertex Path"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    index: IntProperty()

    def execute(self, context):
        st = state.settings(context.scene)
        if st is None or not 0 <= self.index < len(st.vertex_paths):
            return {'CANCELLED'}
        st.vertex_paths.remove(self.index)
        state.tag_redraw()
        return {'FINISHED'}


def context_menu_entry(self, context):
    """Edit Mode right-click menu, at the bottom."""
    self.layout.separator()
    self.layout.operator(EMP_OT_track_vertex.bl_idname, icon='VERTEXSEL')


classes = (EMP_OT_track_vertex, EMP_OT_vertex_path_remove)
