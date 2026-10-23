"""Sidebar panel, menus and the Edit Motion Path tool."""

import bpy

from . import core, ops_fmods, state


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
    _k("emp.duplicate_move", 'D', shift=True),
    _k("emp.loopcut", 'R', ctrl=True),
    _k("emp.bevel", 'B', ctrl=True),
    _k("emp.knife", 'K'),
    _k("emp.merge", 'M'),
    _k("emp.copy_keys", 'C', ctrl=True),
    _k("emp.paste_keys", 'V', ctrl=True),
    _k("wm.call_menu", 'C', {"name": "EMP_MT_loop"}, alt=True),
    # menu calls (not operators), so buttons and menus show the shortcut; when a menu can't
    # be used (nothing selected) the key goes on to Blender (T: toolbar)
    _k("wm.call_menu", 'V', {"name": "EMP_MT_handle_type"}),
    _k("wm.call_menu", 'T', {"name": "EMP_MT_interpolation"}),
    # shape / timing
    _k("emp.smooth", 'S', alt=True),
    _k("emp.exaggerate", 'E', alt=True),
    _k("emp.ease", 'E', shift=True),
    _k("emp.context_menu", 'RIGHTMOUSE'),
    _k("wm.call_menu", 'T', {"name": "EMP_MT_timing"}, shift=True),
    _k("wm.call_panel", 'U', {"name": "EMP_PT_display_popover", "keep_open": True}),
    # modes, proportional editing (Blender's own settings)
    _k("wm.context_set_enum", 'ONE', {"data_path": "scene.emp.edit_mode", "value": 'LOCATION'}),
    _k("wm.context_set_enum", 'TWO', {"data_path": "scene.emp.edit_mode", "value": 'ROTATION'}),
    _k("wm.context_set_enum", 'THREE', {"data_path": "scene.emp.edit_mode", "value": 'SCALE'}),
    _k("wm.context_set_enum", 'FOUR', {"data_path": "scene.emp.edit_mode", "value": 'VALUE'}),
    _k("wm.context_toggle", 'O', {"data_path": "tool_settings.use_proportional_edit"}),
    _k("wm.context_pie_enum", 'O', {"data_path": "tool_settings.proportional_edit_falloff"}, shift=True),
    _k("wm.context_toggle", 'O', {"data_path": "tool_settings.use_proportional_connected"}, alt=True),
)

HELP = (
    ("Modes", (
        ("1 2 3 4", "Location / Rotation / Scale / Value"),
    )),
    ("Select", (
        ("Click / Shift+Click", "select a key or handle"),
        ("Ctrl+Click key", "select the keys up to it"),
        ("Drag empty, B, C", "box / circle select"),
        ("A, Alt+A, Ctrl+I", "all / none / invert"),
        ("Ctrl+L, L", "whole path / path under mouse"),
        ("Ctrl+NumPad + / -", "grow / shrink"),
        ("Shift+G", "select similar"),
        ("[  ]", "before / after current frame"),
        ("H, Shift+H, Alt+H", "hide keys / hide the others / reveal"),
        ("Double-click key", "jump to its frame"),
    )),
    ("Transform", (
        ("G R S", "move / rotate / scale (X Y Z: axis)"),
        ("R R", "trackball"),
        ("Ctrl+T", "twist (rotation paths)"),
        ("G G, Shift+V", "slide in time (left / right)"),
        ("R / S on one key", "turn / stretch its handles"),
        ("Ctrl+M", "mirror"),
        ("Shift+Ctrl+Alt+S", "shear"),
        ("Shift+S", "snap / 3D cursor pie"),
        ("O, Shift+O, Alt+O", "proportional: on, falloff, connected"),
        ("Ctrl (while moving)", "snap (Blender's snap settings)"),
    )),
    ("Keys", (
        ("Ctrl+Click path", "insert a key"),
        ("E", "extrude: continue the motion (wheel: frames)"),
        ("Shift+D", "duplicate and move the copies"),
        ("Ctrl+R, K", "loop cut / knife"),
        ("Ctrl+B", "bevel (A width, S segments, P profile)"),
        ("M", "merge (at the last selected key)"),
        ("Ctrl+C, Ctrl+V", "copy the active key's pose / paste on the current frame"),
        ("X, Delete, Ctrl+X", "delete menu / delete / dissolve"),
        ("V, T", "handle type / interpolation"),
        ("Alt+C", "loop: cycles, fix seam"),
    )),
    ("Shape & Timing", (
        ("Alt+S", "smooth (T: shape / time; Scale mode: handles of the picked channel)"),
        ("Alt+E", "exaggerate"),
        ("Shift+E", "ease (I / O / B)"),
        ("Shift+T", "timing menu: breakdown, scale time, add frames, hold..."),
        ("Drag a frame dot", "move that frame along the path (spacing)"),
        ("Right-click", "path menu (Edit Value, Space, LoopTools...)"),
        ("U", "display settings under the mouse"),
    )),
)


def _draw_tool_settings(context, layout, tool):
    st = state.settings(context.scene)
    if st is None:
        return
    row = layout.row(align=True)
    row.prop(st, "edit_mode", expand=True, icon_only=True)
    if st.edit_mode == 'VALUE':
        _draw_value_picker(layout, context)
        layout.prop(st, "value_track", text="")
    layout.separator()
    _draw_proportional(layout, st, compact=True)
    layout.separator()
    layout.operator("emp.path_add", text="Add Path", icon='ADD')
    layout.prop(st, "show_all_handles")
    layout.popover("EMP_PT_display_popover", text="", icon='COLOR')
    layout.popover("EMP_PT_modifiers_popover", text="", icon='MODIFIER')


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


def _active_entry(context):
    st = state.settings(context.scene)
    if st is None or not st.paths:
        return None
    if 0 <= st.active_index < len(st.paths):
        return st.paths[st.active_index]
    return st.paths[0]


def _draw_value_picker(layout, context):
    e = _active_entry(context)
    if e is None:
        return
    label = "Pick a value"
    for dp, idx, name in core.value_candidates(e.object, e.bone_name):
        if dp == e.value_path and idx == e.value_index:
            label = name
            break
    layout.menu("EMP_MT_value_channel", text=label, icon='DRIVER')


class EMP_PoseTool(bpy.types.WorkSpaceTool):
    bl_space_type = 'VIEW_3D'
    bl_context_mode = 'POSE'
    bl_idname = "emp.edit_path_pose"
    bl_label = "Edit Motion Path"
    bl_description = ("Edit motion paths like curves: click keys, drag keys and handles, G R S, "
                      "E extrude, Ctrl+Click on a path to insert a key")
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

def _any_key_selected(context):
    return any(k.sel or k.sel_l or k.sel_r for c in state.editable_caches(context.scene) for k in c.keys)


class EMP_MT_handle_type(bpy.types.Menu):
    bl_label = "Handle Type"
    bl_idname = "EMP_MT_handle_type"

    @classmethod
    def poll(cls, context):
        return _any_key_selected(context)

    def draw(self, context):
        self.layout.operator_enum("emp.handle_type", "type")


class EMP_MT_interpolation(bpy.types.Menu):
    bl_label = "Interpolation"
    bl_idname = "EMP_MT_interpolation"

    @classmethod
    def poll(cls, context):
        return _any_key_selected(context)

    def draw(self, context):
        self.layout.operator_enum("emp.interpolation", "type")


class EMP_MT_value_channel(bpy.types.Menu):
    bl_label = "Value"
    bl_idname = "EMP_MT_value_channel"

    def draw(self, context):
        layout = self.layout
        e = _active_entry(context)
        cands = core.value_candidates(e.object, e.bone_name) if e is not None else []
        if not cands:
            layout.label(text="No animated values (custom properties, influences...)")
        for dp, idx, name in cands:
            op = layout.operator("emp.set_value_channel", text=name)
            op.data_path = dp
            op.index = idx


class EMP_OT_set_value_channel(bpy.types.Operator):
    """Show and edit this animated value in Value mode (4)"""
    bl_idname = "emp.set_value_channel"
    bl_label = "Set Value Channel"
    bl_options = {'REGISTER', 'UNDO'}

    data_path: bpy.props.StringProperty()
    index: bpy.props.IntProperty()

    def execute(self, context):
        e = _active_entry(context)
        if e is None:
            return {'CANCELLED'}
        e.value_path = self.data_path
        e.value_index = self.index
        state.sync_caches(context.scene)
        state.tag_redraw()
        return {'FINISHED'}


class EMP_MT_space(bpy.types.Menu):
    bl_label = "Space"
    bl_idname = "EMP_MT_space"

    def draw(self, context):
        layout = self.layout
        layout.operator_context = 'INVOKE_REGION_WIN'
        for ident, name, _d in (('EVEN', "Even (constant speed)", ""), ('SLOW_IN_OUT', "Slow In & Out", ""),
                                ('SLOW_IN', "Slow In (decelerate)", ""), ('SLOW_OUT', "Slow Out (accelerate)", "")):
            layout.operator("emp.space", text=name).profile = ident


class EMP_MT_looptools(bpy.types.Menu):
    bl_label = "LoopTools"
    bl_idname = "EMP_MT_looptools"

    def draw(self, context):
        layout = self.layout
        layout.operator("emp.circle", icon='MESH_CIRCLE')
        layout.operator("emp.flatten", icon='MESH_PLANE')
        layout.operator("emp.straighten", icon='IPO_LINEAR')
        layout.operator("emp.gstretch", icon='GREASEPENCIL')
        layout.operator_context = 'INVOKE_REGION_WIN'
        layout.operator("emp.smooth", text="Relax", icon='SMOOTHCURVE')


class EMP_MT_snap(bpy.types.Menu):
    bl_label = "Snap"
    bl_idname = "EMP_MT_snap"

    def draw(self, context):
        layout = self.layout
        layout.operator("emp.selection_to_cursor", text="Selection to Cursor").use_offset = False
        layout.operator("emp.selection_to_cursor", text="Selection to Cursor (Keep Offset)").use_offset = True
        layout.operator("emp.selection_to_active", text="Selection to Active")
        layout.operator("emp.selection_to_grid", text="Selection to Grid")
        layout.separator()
        layout.operator("emp.cursor_to_selected", text="Cursor to Selected")
        layout.operator("emp.cursor_to_active", text="Cursor to Active")


class EMP_MT_curves(bpy.types.Menu):
    bl_label = "Curves"
    bl_idname = "EMP_MT_curves"

    def draw(self, context):
        layout = self.layout
        layout.operator_menu_enum("emp.export_curve", "kind", text="Path to Curve", icon='CURVE_BEZCURVE')
        layout.operator_context = 'INVOKE_REGION_WIN'
        layout.operator("emp.import_curve", text="Follow Curve...", icon='CURVE_PATH')


class EMP_MT_context(bpy.types.Menu):
    """Right-click menu, laid out like the mesh vertex context menu."""
    bl_label = "Motion Path"
    bl_idname = "EMP_MT_context"

    def draw(self, context):
        layout = self.layout
        layout.operator_context = 'INVOKE_REGION_WIN'
        layout.operator("emp.edit_value", text="Edit Value...", icon='GREASEPENCIL')
        layout.separator()
        layout.operator("emp.subdivide", icon='SNAP_EDGE')
        layout.operator("emp.extrude", text="Extrude", icon='EXPORT')
        layout.operator("emp.duplicate_move", text="Duplicate", icon='DUPLICATE')
        layout.operator("emp.duplicate", text="Duplicate in Time", icon='TIME')
        layout.operator("emp.bevel", text="Bevel", icon='MOD_BEVEL')
        layout.operator("emp.spin", text="Spin", icon='LOOP_FORWARDS')
        layout.menu("EMP_MT_merge", icon='AUTOMERGE_OFF')
        layout.separator()
        layout.operator("emp.smooth", text="Smooth", icon='SMOOTHCURVE')
        layout.operator("emp.exaggerate", text="Exaggerate", icon='SHARPCURVE')
        layout.operator("emp.ease", text="Ease", icon='IPO_EASE_IN_OUT')
        layout.menu("EMP_MT_space", icon='ALIGN_JUSTIFY')
        layout.menu("EMP_MT_looptools", icon='MESH_CIRCLE')
        layout.menu("EMP_MT_timing", icon='TIME')
        layout.separator()
        layout.menu("EMP_MT_handle_type", icon='HANDLE_ALIGNED')
        layout.menu("EMP_MT_interpolation", icon='IPO_BEZIER')
        layout.menu("EMP_MT_loop", icon='FILE_REFRESH')
        layout.menu("EMP_MT_snap", icon='SNAP_ON')
        layout.menu("EMP_MT_curves", icon='CURVE_DATA')
        layout.separator()
        layout.menu("EMP_MT_delete", icon='X')


def _context_menu_entry(self, context):
    st = state.settings(context.scene)
    if st is None or not st.paths:
        return
    layout = self.layout
    layout.separator()
    layout.menu("EMP_MT_context", icon='IPO_BEZIER')


# ----------------------------------------------------------------------------
# Panels
# ----------------------------------------------------------------------------

def _object_icon(ob):
    if ob is None:
        return 'ERROR'
    return {'MESH': 'OUTLINER_OB_MESH', 'CURVE': 'OUTLINER_OB_CURVE', 'SURFACE': 'OUTLINER_OB_SURFACE',
            'META': 'OUTLINER_OB_META', 'FONT': 'OUTLINER_OB_FONT', 'ARMATURE': 'OUTLINER_OB_ARMATURE',
            'LATTICE': 'OUTLINER_OB_LATTICE', 'EMPTY': 'OUTLINER_OB_EMPTY', 'LIGHT': 'OUTLINER_OB_LIGHT',
            'CAMERA': 'OUTLINER_OB_CAMERA', 'SPEAKER': 'OUTLINER_OB_SPEAKER',
            'LIGHT_PROBE': 'OUTLINER_OB_LIGHTPROBE', 'GREASEPENCIL': 'OUTLINER_OB_GREASEPENCIL',
            'CURVES': 'OUTLINER_OB_CURVES', 'POINTCLOUD': 'OUTLINER_OB_POINTCLOUD',
            'VOLUME': 'OUTLINER_OB_VOLUME'}.get(ob.type, 'OBJECT_DATA')


class EMP_UL_paths(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, "show", text="", emboss=False,
                 icon='HIDE_OFF' if item.show else 'HIDE_ON')
        name = item.bone_name or (item.object.name if item.object else "<missing>")
        row.label(text=name, icon='BONE_DATA' if item.bone_name else _object_icon(item.object))
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

        col = layout.column(align=True)
        col.scale_y = 1.2
        for pair in (('LOCATION', 'ROTATION'), ('SCALE', 'VALUE')):
            row = col.row(align=True)
            for m in pair:
                row.prop_enum(st, "edit_mode", m)
        if st.edit_mode == 'VALUE':
            row = layout.row(align=True)
            _draw_value_picker(row, context)
            row.prop(st, "value_track", text="")

        row = layout.row()
        row.template_list("EMP_UL_paths", "", st, "paths", st, "active_index", rows=3)
        col = row.column(align=True)
        col.operator("emp.path_add", text="", icon='ADD')
        col.operator("emp.path_remove", text="", icon='REMOVE').scope = 'ACTIVE'
        col.separator()
        col.operator("emp.path_update", text="", icon='FILE_REFRESH')
        col.operator("emp.path_remove", text="", icon='TRASH').scope = 'ALL'

        tool = context.workspace.tools.from_space_view3d_mode(context.mode, create=False)
        active = tool is not None and tool.idname in {"emp.edit_path_pose", "emp.edit_path_object"}
        row = layout.row()
        row.scale_y = 1.4
        row.operator("emp.edit_tool", text="Editing Paths" if active else "Edit Paths",
                     icon='CURVE_BEZCURVE', depress=active)
        _draw_proportional(layout, st)


class EMP_PT_shape(_Panel, bpy.types.Panel):
    bl_label = "Shape"
    bl_parent_id = "EMP_PT_main"

    def draw(self, context):
        layout = self.layout
        layout.operator_context = 'INVOKE_REGION_WIN'
        col = layout.column(align=True)
        row = col.row(align=True)
        row.operator("emp.smooth", text="Smooth", icon='SMOOTHCURVE')
        row.operator("emp.exaggerate", text="Exaggerate", icon='SHARPCURVE')
        row = col.row(align=True)
        row.operator("emp.bevel", text="Bevel", icon='MOD_BEVEL')
        row.operator("emp.spin", text="Spin", icon='LOOP_FORWARDS')
        row = col.row(align=True)
        row.menu("EMP_MT_looptools", text="LoopTools", icon='MESH_CIRCLE')
        row.menu("EMP_MT_curves", text="Curves", icon='CURVE_DATA')


class EMP_PT_timing(_Panel, bpy.types.Panel):
    bl_label = "Timing & Speed"
    bl_parent_id = "EMP_PT_main"

    def draw(self, context):
        layout = self.layout
        layout.operator_context = 'INVOKE_REGION_WIN'
        col = layout.column(align=True)
        row = col.row(align=True)
        row.operator("emp.space", text="Even Spacing", icon='ALIGN_JUSTIFY').profile = 'EVEN'
        row.menu("EMP_MT_space", text="Profile", icon='IPO_EASE_IN_OUT')
        row = col.row(align=True)
        row.operator("emp.ease", text="Ease", icon='IPO_SINE')
        row.operator("emp.slide", text="Slide", icon='TRACKING_FORWARDS')
        col.menu("EMP_MT_timing", text="Timing Tools", icon='TIME')


class EMP_PT_keys(_Panel, bpy.types.Panel):
    bl_label = "Keys"
    bl_parent_id = "EMP_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        layout.operator_context = 'INVOKE_REGION_WIN'
        col = layout.column(align=True)
        row = col.row(align=True)
        row.operator("emp.insert_key", text="Insert", icon='KEY_HLT')
        row.operator("emp.subdivide", text="Subdivide", icon='SNAP_EDGE')
        row = col.row(align=True)
        row.operator("emp.dissolve_keys", text="Dissolve", icon='X')
        row.operator_menu_enum("emp.cleanup", "method", text="Clean Up", icon='BRUSH_DATA')
        row = col.row(align=True)
        row.menu("EMP_MT_handle_type", text="Handles", icon='HANDLE_ALIGNED')
        row.menu("EMP_MT_interpolation", text="Interpolation", icon='IPO_BEZIER')
        row = col.row(align=True)
        row.operator("emp.toggle_cycles", text="Cycles", icon='FILE_REFRESH')
        row.operator("emp.fix_loop", text="Fix Loop", icon='LOOP_BACK')


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
        _draw_display(self.layout, state.settings(context.scene))


def _draw_display(layout, st):
    layout.use_property_split = True
    layout.use_property_decorate = False
    layout.prop(st, "color_mode")
    if st.color_mode in {'TIME', 'SOLID'}:
        layout.prop(st, "color")
    elif st.color_mode == 'JITTER':
        layout.prop(st, "jitter_threshold", text="Threshold")
    col = layout.column(heading="Show")
    col.prop(st, "show_frames")
    col.prop(st, "show_frame_numbers")
    col.prop(st, "show_all_handles")
    col.prop(st, "show_ghost")
    col.prop(st, "show_repeats")
    col.prop(st, "show_spokes")
    col.prop(st, "show_rot_rings")
    col.prop(st, "show_reach")
    col.prop(st, "show_timing_chart")
    col.prop(st, "show_speed_graph")
    if st.show_speed_graph:
        col.prop(st, "speed_range", text="Range")
        if st.speed_range == 'RANGE':
            sub = col.column(align=True)
            sub.prop(st, "speed_start")
            sub.prop(st, "speed_end")
    col.prop(st, "on_top")
    layout.prop(st, "line_width")
    layout.prop(st, "key_size")
    layout.prop(st, "rot_length")
    layout.prop(st, "ribbon_width")
    row = layout.row(heading="Scale Box")
    row.prop(st, "show_scale_box", text="")
    sub = row.row()
    sub.active = st.show_scale_box
    sub.prop(st, "scale_box_size", text="Size")
    layout.prop(st, "value_track")
    layout.prop(st, "layout", text="Scale / Value Layout")
    if st.layout != 'PATH':
        col = layout.column(align=True)
        col.prop(st, "circle_radius", text="Radius")
        col.prop(st, "circle_use_range")
        sub = col.column(align=True)
        sub.active = st.circle_use_range
        sub.prop(st, "circle_start")
        sub.prop(st, "circle_end")
    layout.prop(st, "pin_partial")


class EMP_PT_display_popover(bpy.types.Panel):
    """Path colors and display, from the tool header"""
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'HEADER'
    bl_label = "Path Display"
    bl_idname = "EMP_PT_display_popover"

    def draw(self, context):
        _draw_display(self.layout, state.settings(context.scene))


class EMP_PT_help(_Panel, bpy.types.Panel):
    bl_label = "Shortcuts"
    bl_parent_id = "EMP_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        col = self.layout.column(align=True)
        col.label(text="With the Edit Motion Path tool:")
        for title, rows in HELP:
            col.separator()
            col.label(text=title, icon='DOT')
            for a, b in rows:
                split = col.row().split(factor=0.45)
                split.label(text=a)
                split.label(text=b)
        col.separator()
        col.label(text="Only paths of selected bones/objects", icon='INFO')
        col.label(text="are editable; click a gray path to pick it.")


classes = (EMP_MT_handle_type, EMP_MT_interpolation, EMP_MT_value_channel, EMP_OT_set_value_channel,
           EMP_MT_space, EMP_MT_looptools, EMP_MT_snap, EMP_MT_curves, EMP_MT_context, EMP_UL_paths,
           EMP_PT_main, EMP_PT_shape, EMP_PT_timing, EMP_PT_keys, ops_fmods.EMP_PT_modifiers,
           EMP_PT_range, EMP_PT_display, EMP_PT_display_popover, ops_fmods.EMP_PT_modifiers_popover,
           EMP_PT_help)

_addon_keymaps = []


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.utils.register_tool(EMP_PoseTool, separator=True)
    bpy.utils.register_tool(EMP_ObjectTool, separator=True)
    bpy.types.VIEW3D_MT_pose_context_menu.append(_context_menu_entry)
    bpy.types.VIEW3D_MT_object_context_menu.append(_context_menu_entry)
    # Ctrl+T picks the tool in Pose Mode (also shown in the Shift+Space tool menu); in Object
    # Mode Ctrl+T stays Blender's Track menu
    kc = bpy.context.window_manager.keyconfigs.addon
    if kc is not None:
        km = kc.keymaps.new("Pose", space_type='EMPTY')
        kmi = km.keymap_items.new("wm.tool_set_by_id", 'T', 'PRESS', ctrl=True)
        kmi.properties.name = EMP_PoseTool.bl_idname
        _addon_keymaps.append((km, kmi))


def unregister():
    for km, kmi in _addon_keymaps:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    _addon_keymaps.clear()
    bpy.types.VIEW3D_MT_pose_context_menu.remove(_context_menu_entry)
    bpy.types.VIEW3D_MT_object_context_menu.remove(_context_menu_entry)
    bpy.utils.unregister_tool(EMP_ObjectTool)
    bpy.utils.unregister_tool(EMP_PoseTool)
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
