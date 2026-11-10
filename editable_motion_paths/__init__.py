bl_info = {
    "name": "Editable Motion Paths",
    "description": "Edit bone and object motion paths directly in the viewport: drag keys and "
                   "handles, smooth and exaggerate paths, edit location and rotation",
    "author": "Editable Motion Paths",
    "version": (2, 0, 4),
    "blender": (4, 4, 0),
    "location": "3D Viewport > Sidebar > Motion Paths, Toolbar > Edit Motion Path",
    "category": "Animation",
}

import bpy

from . import api  # noqa: F401  (loaded for scripts: sys.modules["...editable_motion_paths.api"])
from . import (draw, ops_easing, ops_fmods, ops_io, ops_keys, ops_sculpt, ops_select, ops_shape,
               ops_slide, ops_timetools, ops_timing, ops_tools, ops_transform, ops_value, ops_vertex, props,
               state, ui)

_classes = (ops_transform.classes + ops_slide.classes + ops_keys.classes + ops_select.classes +
            ops_shape.classes + ops_tools.classes + ops_timing.classes + ops_io.classes +
            ops_value.classes + ops_timetools.classes + ops_fmods.classes + ops_easing.classes +
            ops_sculpt.classes + ops_vertex.classes)


def register():
    props.register()
    for c in _classes:
        bpy.utils.register_class(c)
    ops_keys.register_macros()
    ops_easing.register_icons()
    ui.register()
    ops_sculpt.register_tools()
    draw.register()
    state.register()


def unregister():
    state.unregister()
    draw.unregister()
    ops_sculpt.unregister_tools()
    ui.unregister()
    ops_easing.unregister_icons()
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
    props.unregister()
