"""Shape tools on path keys: 3D cursor snapping (Shift+S), mirror (Ctrl+M)."""

import bpy
from bpy.props import BoolProperty, EnumProperty
from mathutils import Vector

from . import core, edit, space, state


def _keys_selected(context):
    return any(k.sel for c in state.editable_caches(context.scene) for k in c.keys)


# ----------------------------------------------------------------------------
# 3D cursor
# ----------------------------------------------------------------------------

class EMP_OT_cursor_to_selected(bpy.types.Operator):
    """Put the 3D cursor on the selected path keys (their median)"""
    bl_idname = "emp.cursor_to_selected"
    bl_label = "Cursor to Selected Keys"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _keys_selected(context)

    def execute(self, context):
        pts = [k.co for _c, ks in edit.selected(context) for k in ks]
        context.scene.cursor.location = space.median(pts)
        state.tag_redraw()
        return {'FINISHED'}


class EMP_OT_cursor_to_active(bpy.types.Operator):
    """Put the 3D cursor on the active (last clicked) path key"""
    bl_idname = "emp.cursor_to_active"
    bl_label = "Cursor to Active Key"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        a = space.active_key(state.editable_caches(context.scene))
        if a is None:
            self.report({'WARNING'}, "No active key: click a key first")
            return {'CANCELLED'}
        context.scene.cursor.location = a[1].co
        state.tag_redraw()
        return {'FINISHED'}


class _MoveSelected:
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _keys_selected(context)

    def targets(self, context, pts):
        """World target per selected key position (same order)."""
        raise NotImplementedError

    def execute(self, context):
        groups = edit.selected(context)
        if not groups:
            return {'CANCELLED'}
        pts = [k.co.copy() for _c, ks in groups for k in ks]
        tgts = self.targets(context, pts)
        if tgts is None:
            return {'CANCELLED'}
        frames = [k.t for _c, ks in groups for k in ks]
        w = edit.Writer(frames)
        i = 0
        for cache, ks in groups:
            mine = tgts[i:i + len(ks)]
            i += len(ks)
            tg, fresh = edit.prepare(context, cache, [k.t for k in ks])
            by_t = {round(k.t, 3): W for k, W in zip(ks, mine)}
            for k in fresh:
                W = by_t.get(round(k.t, 3))
                if W is not None:
                    w.key(cache, tg, k, W)
        w.done([c for c, _ks in groups])
        return {'FINISHED'}


class EMP_OT_selection_to_cursor(_MoveSelected, bpy.types.Operator):
    """Move the selected path keys to the 3D cursor (rotation paths: aim at it)"""
    bl_idname = "emp.selection_to_cursor"
    bl_label = "Selection to Cursor"

    use_offset: BoolProperty(name="Keep Offset", default=False,
                             description="Move the keys together, their median onto the cursor")

    def targets(self, context, pts):
        cur = context.scene.cursor.location.copy()
        if self.use_offset:
            d = cur - space.median(pts)
            return [p + d for p in pts]
        return [cur.copy() for _p in pts]


class EMP_OT_selection_to_active(_MoveSelected, bpy.types.Operator):
    """Move the selected path keys onto the active (last clicked) key"""
    bl_idname = "emp.selection_to_active"
    bl_label = "Selection to Active Key"

    def targets(self, context, pts):
        a = space.active_key(state.editable_caches(context.scene))
        if a is None:
            self.report({'WARNING'}, "No active key: click a key first")
            return None
        return [a[1].co.copy() for _p in pts]


class EMP_OT_selection_to_grid(_MoveSelected, bpy.types.Operator):
    """Snap the selected path keys to the nearest grid point"""
    bl_idname = "emp.selection_to_grid"
    bl_label = "Selection to Grid"

    def targets(self, context, pts):
        g = 1.0
        sd = context.space_data
        if sd is not None and sd.type == 'VIEW_3D':
            g = sd.overlay.grid_scale or 1.0
        return [Vector([round(x / g) * g for x in p]) for p in pts]


class EMP_MT_snap_pie(bpy.types.Menu):
    bl_label = "Snap Path Keys"
    bl_idname = "EMP_MT_snap_pie"

    def draw(self, context):
        pie = self.layout.menu_pie()
        pie.operator("view3d.snap_cursor_to_grid", text="Cursor to Grid", icon='CURSOR')
        pie.operator("emp.selection_to_grid", text="Selection to Grid", icon='RESTRICT_SELECT_OFF')
        pie.operator("emp.cursor_to_selected", text="Cursor to Selected", icon='CURSOR')
        pie.operator("emp.selection_to_cursor", text="Selection to Cursor",
                     icon='RESTRICT_SELECT_OFF').use_offset = False
        pie.operator("emp.selection_to_cursor", text="Selection to Cursor (Keep Offset)",
                     icon='RESTRICT_SELECT_OFF').use_offset = True
        pie.operator("emp.selection_to_active", text="Selection to Active", icon='RESTRICT_SELECT_OFF')
        pie.operator("view3d.snap_cursor_to_center", text="Cursor to World Origin", icon='CURSOR')
        pie.operator("emp.cursor_to_active", text="Cursor to Active", icon='CURSOR')


class EMP_OT_snap_pie(bpy.types.Operator):
    """Snap menu for path keys (Blender's snap menu when no key is selected)"""
    bl_idname = "emp.snap_pie"
    bl_label = "Snap Path Keys"

    def invoke(self, context, event):
        if not _keys_selected(context):
            return {'PASS_THROUGH'}
        bpy.ops.wm.call_menu_pie(name="EMP_MT_snap_pie")
        return {'FINISHED'}


# ----------------------------------------------------------------------------
# Mirror
# ----------------------------------------------------------------------------

class EMP_OT_mirror(bpy.types.Operator):
    """Mirror the selected path keys around the pivot point (press X, Y or Z)"""
    bl_idname = "emp.mirror"
    bl_label = "Mirror Path Keys"
    bl_options = {'REGISTER', 'UNDO'}

    axis: EnumProperty(name="Axis", items=[('X', "X", ""), ('Y', "Y", ""), ('Z', "Z", "")], default='X')
    orient: EnumProperty(name="Orientation", items=space.ORIENTATIONS, default='GLOBAL')

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == 'VIEW_3D'

    def execute(self, context):
        v = [1.0, 1.0, 1.0]
        v["XYZ".index(self.axis)] = -1.0
        props = dict(mode='SCALE', value=v, orient=self.orient)
        if self.orient not in {'GLOBAL', 'LOCAL'}:
            groups = edit.selected(context)
            if groups:
                c, ks = groups[0]
                M = space.orientation_matrix(context, self.orient, c, ks[0])
                props["orient_matrix"] = [x for row in M for x in row]
        return bpy.ops.emp.transform('EXEC_DEFAULT', **props)

    def _restore(self):
        for sn in reversed(self.snaps):
            sn.restore()
        for c in state.caches.values():
            c.dirty = True
            c.refresh()

    def invoke(self, context, event):
        if not _keys_selected(context):
            return {'PASS_THROUGH'}
        self.orient = 'GLOBAL'
        self.presses = {}
        self.snaps = [core.FCurveSnapshot(c.target(), c.target().channels(c.ptype))
                      for c in state.editable_caches(context.scene)]
        context.area.header_text_set("Mirror: press X, Y or Z (twice: other orientation), "
                                     "Esc / RMB: cancel")
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.value != 'PRESS':
            return {'RUNNING_MODAL'}
        if event.type in {'X', 'Y', 'Z'}:
            header = space.orientation_type(context)
            n = self.presses.get(event.type, 0) + 1
            self.presses = {event.type: n}
            self.axis = event.type
            self.orient = header if n % 2 else ('LOCAL' if header == 'GLOBAL' else 'GLOBAL')
            context.area.header_text_set("Mirror along %s (%s): Enter to confirm, %s again to switch "
                                         "orientation" % (self.axis, self.orient.title(), self.axis))
            self._restore()
            self.execute(context)      # preview
            return {'RUNNING_MODAL'}
        if event.type in {'RET', 'NUMPAD_ENTER', 'SPACE', 'LEFTMOUSE'} and self.presses:
            context.area.header_text_set(None)
            return {'FINISHED'}
        if event.type in {'ESC', 'RIGHTMOUSE'}:
            context.area.header_text_set(None)
            self._restore()
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}


classes = (EMP_OT_cursor_to_selected, EMP_OT_cursor_to_active, EMP_OT_selection_to_cursor,
           EMP_OT_selection_to_active, EMP_OT_selection_to_grid, EMP_MT_snap_pie, EMP_OT_snap_pie,
           EMP_OT_mirror)
