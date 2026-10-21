"""Sidebar panel, menus and the Edit Motion Path tool."""

import bpy

from . import state


def _k(op, key, props=None, **mods):
    ev = {"type": key, "value": mods.pop("value", 'PRESS')}
    ev.update(mods)
    return (op, ev, {"properties": list(props.items())} if props else None)


TOOL_KEYMAP = (
    # select
    _k("emp.click", 'LEFTMOUSE', any=True),
    _k("emp.jump_to_key", 'LEFTMOUSE', value='DOUBLE_CLICK'),
    _k("emp.box_select", 'LEFTMOUSE', value='CLICK_DRAG', any=True),
    _k("emp.box_select", 'B', {"wait_for_input": True}),
    _k("emp.circle_select", 'C'),
    _k("emp.select_all", 'A', {"action": 'SELECT'}),
    _k("emp.select_all", 'A', {"action": 'DESELECT'}, alt=True),
    _k("emp.select_all", 'I', {"action": 'INVERT'}, ctrl=True),
    _k("emp.select_linked", 'L', ctrl=True),
    _k("emp.select_linked_pick", 'L'),
    _k("emp.select_linked_pick", 'L', {"deselect": True}, shift=True),
    _k("emp.select_more_less", 'NUMPAD_PLUS', ctrl=True),
    _k("emp.select_more_less", 'NUMPAD_MINUS', {"less": True}, ctrl=True),
    _k("emp.select_similar", 'G', shift=True),
    _k("emp.select_side", 'LEFT_BRACKET', {"side": 'LEFT'}),
    _k("emp.select_side", 'RIGHT_BRACKET', {"side": 'RIGHT'}),
    _k("emp.hide", 'H'),
    _k("emp.hide", 'H', {"unselected": True}, shift=True),
    _k("emp.reveal", 'H', alt=True),
    # transform
    _k("emp.transform", 'G', {"mode": 'TRANSLATE'}),
    _k("emp.transform", 'R', {"mode": 'ROTATE'}),
    _k("emp.transform", 'S', {"mode": 'SCALE'}),
    _k("emp.transform", 'T', {"mode": 'ROTATE', "twist": True}, ctrl=True),
    _k("emp.transform", 'S', {"mode": 'SHEAR'}, shift=True, ctrl=True, alt=True),
    _k("emp.slide", 'V', shift=True),
    _k("emp.mirror", 'M', ctrl=True),
    _k("emp.snap_pie", 'S', shift=True),
    # keys
    _k("emp.delete_menu", 'X'),
    _k("emp.delete_keys", 'DEL'),
    _k("emp.dissolve_keys", 'X', ctrl=True),
    _k("emp.extrude", 'E'),
    _k("emp.duplicate", 'D', shift=True),
    _k("emp.loopcut", 'R', ctrl=True),
    _k("emp.knife", 'K'),
    _k("emp.merge", 'M'),
    _k("emp.loop_menu", 'C', alt=True),
    _k("emp.handle_type_menu", 'V'),
    _k("emp.interpolation_menu", 'T'),
    # shape / timing
    _k("emp.smooth", 'S', alt=True),
    _k("emp.exaggerate", 'E', alt=True),
    _k("emp.ease", 'E', shift=True),
    _k("emp.context_menu", 'RIGHTMOUSE'),
    # modes, proportional editing (Blender's own settings)
    _k("wm.context_set_enum", 'ONE', {"data_path": "scene.emp.edit_mode", "value": 'LOCATION'}),
    _k("wm.context_set_enum", 'TWO', {"data_path": "scene.emp.edit_mode", "value": 'ROTATION'}),
    _k("wm.context_toggle", 'O', {"data_path": "tool_settings.use_proportional_edit"}),
    _k("wm.context_pie_enum", 'O', {"data_path": "tool_settings.proportional_edit_falloff"}, shift=True),
    _k("wm.context_toggle", 'O', {"data_path": "tool_settings.use_proportional_connected"}, alt=True),
)

HELP = (
    ("1 / 2", "edit Location / Rotation"),
    ("Click key", "select, shows handles"),
    ("Drag key / handle", "move it"),
    ("Shift+Click", "add to selection"),
    ("Ctrl+Click key", "select keys up to it (shortest way)"),
    ("Ctrl+Click path", "insert key"),
    ("Double-click key", "jump to its frame"),
    ("Drag empty / B", "box select"),
    ("G / R / S", "move / rotate / scale"),
    ("G G", "slide keys along the path"),
    ("  then T", "Timing / Spacing"),
    ("  then X Y Z", "axis (twice = local)"),
    ("R on one key", "rotate its handles"),
    ("S on one key", "handle length (speed)"),
    ("R R on rotation path", "twist the bone"),
    ("O", "proportional editing"),
    ("  Wheel, PgUp/PgDn", "radius (during G/R/S)"),
    ("Shift+O", "proportional falloff"),
    ("V", "handle type"),
    ("E", "ease (slow in / out)"),
    ("  then I / O / B", "in / out / both"),
    ("L on first/last key", "fix the loop seam"),
    ("Alt+S / Alt+E", "smooth / exaggerate"),
    ("X / Delete", "delete keys"),
    ("Ctrl+X", "dissolve key / clean up selection"),
    ("Right-click", "path menu"),
)


def _draw_tool_settings(context, layout, tool):
    st = state.settings(context.scene)
    if st is None:
        return
    row = layout.row(align=True)
    row.prop(st, "edit_mode", expand=True)
    layout.separator()
    _draw_proportional(layout, st, compact=True)
    layout.separator()
    layout.operator("emp.path_add", text="Add Path", icon='ADD')
    layout.prop(st, "show_all_handles")


def _draw_proportional(layout, st, compact=False):
    """Blender's proportional editing settings (shared with mesh editing) plus the path radius."""
    ts = bpy.context.scene.tool_settings
    row = layout.row(align=True)
    on = ts.use_proportional_edit
    row.prop(ts, "use_proportional_edit", text="" if compact else "Proportional",
             icon='PROP_CON' if on and ts.use_proportional_connected else ('PROP_ON' if on else 'PROP_OFF'))
    sub = row.row(align=True)
    sub.active = on
    sub.prop(ts, "proportional_edit_falloff", text="", icon_only=True)
    if not compact:
        sub.prop(st, "proportional_size", text="Size")
    sub.prop(ts, "use_proportional_connected", text="", icon='LINKED' if ts.use_proportional_connected
             else 'UNLINKED', toggle=True)


class EMP_PoseTool(bpy.types.WorkSpaceTool):
    bl_space_type = 'VIEW_3D'
    bl_context_mode = 'POSE'
    bl_idname = "emp.edit_path_pose"
    bl_label = "Edit Motion Path"
    bl_description = ("Edit motion paths: click a key to see its handles, drag keys and handles, "
                      "G/R/S with X/Y/Z, Ctrl+Click on a path to insert a key")
    bl_icon = "ops.curve.pen"
    bl_widget = None
    bl_keymap = TOOL_KEYMAP
    draw_settings = _draw_tool_settings


class EMP_ObjectTool(bpy.types.WorkSpaceTool):
    bl_space_type = 'VIEW_3D'
    bl_context_mode = 'OBJECT'
    bl_idname = "emp.edit_path_object"
    bl_label = "Edit Motion Path"
    bl_description = EMP_PoseTool.bl_description
    bl_icon = "ops.curve.pen"
    bl_widget = None
    bl_keymap = TOOL_KEYMAP
    draw_settings = _draw_tool_settings


# ----------------------------------------------------------------------------
# Menus
# ----------------------------------------------------------------------------

class EMP_MT_handle_type(bpy.types.Menu):
    bl_label = "Handle Type"
    bl_idname = "EMP_MT_handle_type"

    def draw(self, context):
        self.layout.operator_enum("emp.handle_type", "type")


class EMP_MT_interpolation(bpy.types.Menu):
    bl_label = "Interpolation"
    bl_idname = "EMP_MT_interpolation"

    def draw(self, context):
        self.layout.operator_enum("emp.interpolation", "type")


class EMP_MT_context(bpy.types.Menu):
    bl_label = "Motion Path"
    bl_idname = "EMP_MT_context"

    def draw(self, context):
        layout = self.layout
        layout.operator_context = 'INVOKE_DEFAULT'
        layout.operator("emp.smooth", icon='SMOOTHCURVE')
        layout.operator("emp.exaggerate", icon='SHARPCURVE')
        layout.operator("emp.ease", text="Ease (E)", icon='IPO_EASE_IN_OUT')
        layout.operator("emp.fix_loop", text="Fix Loop (L)", icon='LOOP_BACK')
        layout.operator_menu_enum("emp.cleanup", "method", text="Clean Up", icon='BRUSH_DATA')
        layout.operator("emp.slide", text="Slide Along Path (G G)", icon='TRACKING_FORWARDS')
        layout.separator()
        layout.menu("EMP_MT_handle_type", icon='HANDLE_ALIGNED')
        layout.menu("EMP_MT_interpolation", icon='IPO_BEZIER')
        layout.separator()
        layout.operator("emp.insert_key", text="Insert Key at Current Frame", icon='KEY_HLT')
        layout.operator("emp.delete_keys", text="Delete Keys", icon='X')
        layout.separator()
        layout.operator("emp.select_all", text="Select All").action = 'SELECT'
        layout.operator("emp.select_all", text="Invert Selection").action = 'INVERT'
        layout.separator()
        layout.operator("emp.path_update", icon='FILE_REFRESH')


def _context_menu_entry(self, context):
    st = state.settings(context.scene)
    if st is None or not st.paths:
        return
    layout = self.layout
    layout.separator()
    layout.menu("EMP_MT_context", icon='IPO_BEZIER')


# ----------------------------------------------------------------------------
# Panel
# ----------------------------------------------------------------------------

class EMP_UL_paths(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, "show", text="", emboss=False,
                 icon='HIDE_OFF' if item.show else 'HIDE_ON')
        name = item.bone_name or (item.object.name if item.object else "<missing>")
        row.label(text=name, icon='BONE_DATA' if item.bone_name else 'OBJECT_DATA')
        if item.object is not None:
            cache = state.caches.get(state.entry_key(item))
            if cache is not None and cache.locked:
                row.label(text="", icon='LOCKED')


class _Panel:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Motion Paths"

    @classmethod
    def poll(cls, context):
        return context.mode in {'POSE', 'OBJECT'}


class EMP_PT_main(_Panel, bpy.types.Panel):
    bl_label = "Editable Motion Paths"
    bl_idname = "EMP_PT_main"

    def draw(self, context):
        layout = self.layout
        st = state.settings(context.scene)

        row = layout.row(align=True)
        row.scale_y = 1.3
        row.prop(st, "edit_mode", expand=True)

        row = layout.row(align=True)
        row.operator("emp.path_add", text="Add", icon='ADD')
        row.operator("emp.path_remove", text="", icon='REMOVE').scope = 'SELECTED'
        row.operator("emp.path_update", text="", icon='FILE_REFRESH')

        row = layout.row()
        row.template_list("EMP_UL_paths", "", st, "paths", st, "active_index", rows=3)
        col = row.column(align=True)
        col.operator("emp.path_remove", text="", icon='X').scope = 'ACTIVE'
        col.operator("emp.path_remove", text="", icon='TRASH').scope = 'ALL'

        tool = context.workspace.tools.from_space_view3d_mode(context.mode, create=False)
        active = tool is not None and tool.idname in {"emp.edit_path_pose", "emp.edit_path_object"}
        row = layout.row()
        row.scale_y = 1.4
        row.operator("emp.edit_tool", text="Editing Paths" if active else "Edit Paths",
                     icon='CURVE_BEZCURVE', depress=active)

        _draw_proportional(layout, st)

        col = layout.column(align=True)
        col.label(text="Selected Keys (all if none):")
        row = col.row(align=True)
        op = row.operator("emp.smooth", icon='SMOOTHCURVE')
        op = row.operator("emp.exaggerate", icon='SHARPCURVE')
        row = col.row(align=True)
        row.operator("emp.ease", text="Ease", icon='IPO_EASE_IN_OUT')
        row.operator("emp.slide", text="Slide", icon='TRACKING_FORWARDS')
        row = col.row(align=True)
        row.operator("emp.fix_loop", text="Fix Loop", icon='LOOP_BACK')
        row.operator_menu_enum("emp.cleanup", "method", text="Clean Up", icon='BRUSH_DATA')
        row = col.row(align=True)
        row.menu("EMP_MT_handle_type", text="Handles", icon='HANDLE_ALIGNED')
        row.menu("EMP_MT_interpolation", text="Interpolation", icon='IPO_BEZIER')
        row = col.row(align=True)
        row.operator("emp.insert_key", text="Insert Key", icon='KEY_HLT')
        row.operator("emp.delete_keys", text="Delete", icon='X')


class EMP_PT_range(_Panel, bpy.types.Panel):
    bl_label = "Range"
    bl_parent_id = "EMP_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        st = state.settings(context.scene)
        layout.prop(st, "range_mode")
        if st.range_mode == 'MANUAL':
            col = layout.column(align=True)
            col.prop(st, "frame_start")
            col.prop(st, "frame_end")
        layout.prop(st, "use_window")
        if st.use_window:
            col = layout.column(align=True)
            col.prop(st, "window_before")
            col.prop(st, "window_after")
        layout.prop(st, "auto_update")


class EMP_PT_display(_Panel, bpy.types.Panel):
    bl_label = "Display"
    bl_parent_id = "EMP_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        st = state.settings(context.scene)
        layout.prop(st, "color_mode")
        if st.color_mode != 'SPEED':
            layout.prop(st, "color")
        col = layout.column(heading="Show")
        col.prop(st, "show_frames")
        col.prop(st, "show_frame_numbers")
        col.prop(st, "show_all_handles")
        col.prop(st, "show_ghost")
        col.prop(st, "show_spokes")
        col.prop(st, "on_top")
        layout.prop(st, "line_width")
        layout.prop(st, "key_size")
        layout.prop(st, "rot_length")
        layout.prop(st, "pin_partial")


class EMP_PT_help(_Panel, bpy.types.Panel):
    bl_label = "Shortcuts"
    bl_parent_id = "EMP_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        col = self.layout.column(align=True)
        col.label(text="With the Edit Paths tool active:")
        for a, b in HELP:
            row = col.row()
            split = row.split(factor=0.48)
            split.label(text=a)
            split.label(text=b)
        col.separator()
        col.label(text="Only paths of selected bones/objects", icon='INFO')
        col.label(text="are editable; click a gray path to pick it.")


classes = (EMP_MT_handle_type, EMP_MT_interpolation, EMP_MT_context, EMP_UL_paths,
           EMP_PT_main, EMP_PT_range, EMP_PT_display, EMP_PT_help)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.utils.register_tool(EMP_PoseTool, separator=True)
    bpy.utils.register_tool(EMP_ObjectTool, separator=True)
    bpy.types.VIEW3D_MT_pose_context_menu.append(_context_menu_entry)
    bpy.types.VIEW3D_MT_object_context_menu.append(_context_menu_entry)


def unregister():
    bpy.types.VIEW3D_MT_pose_context_menu.remove(_context_menu_entry)
    bpy.types.VIEW3D_MT_object_context_menu.remove(_context_menu_entry)
    bpy.utils.unregister_tool(EMP_ObjectTool)
    bpy.utils.unregister_tool(EMP_PoseTool)
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
