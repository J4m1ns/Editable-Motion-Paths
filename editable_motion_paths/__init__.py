bl_info = {
    "name": "Editable Motion Paths",
    "description": "Edit bone and object motion paths directly in the viewport: drag keys and "
                   "handles, smooth and exaggerate paths, edit location and rotation",
    "author": "Editable Motion Paths",
    "version": (1, 6, 2),
    "blender": (4, 4, 0),
    "location": "3D Viewport > Sidebar > Motion Paths, Toolbar > Edit Motion Path",
    "category": "Animation",
}

import sys

import bpy

from . import api, draw, ops_select, ops_slide, ops_tools, ops_transform, props, state, ui

_classes = ops_transform.classes + ops_slide.classes + ops_select.classes + ops_tools.classes


def register():
    props.register()
    for c in _classes:
        bpy.utils.register_class(c)
    ui.register()
    draw.register()
    state.register()
    # stable import name for scripts / AI assistants: import editable_motion_paths_api
    sys.modules["editable_motion_paths_api"] = api


def unregister():
    sys.modules.pop("editable_motion_paths_api", None)
    state.unregister()
    draw.unregister()
    ui.unregister()
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
    props.unregister()
