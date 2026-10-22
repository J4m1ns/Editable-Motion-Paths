import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty,
                       FloatVectorProperty, IntProperty, PointerProperty, StringProperty)

from . import state


def _redraw(self, context):
    state.tag_redraw()


def _rebake(self, context):
    for c in state.caches.values():
        c.B = []
    state.schedule_rebake(0.05, force=True)


def _mode_changed(self, context):
    state.sync_caches(context.scene)
    state.tag_redraw()


def _refresh(self, context):
    for c in state.caches.values():
        c.dirty = True
    state.tag_redraw()


class EMP_PathEntry(bpy.types.PropertyGroup):
    object: PointerProperty(type=bpy.types.Object)
    bone_name: StringProperty()
    # kept for files saved with 1.0 (paths were per type then); the type now comes from
    # the global Location / Rotation edit mode
    path_type: EnumProperty(items=[('LOCATION', "Location", ""), ('ROTATION', "Rotation", "")],
                            options={'HIDDEN'})
    show: BoolProperty(name="Show", default=True, update=_redraw)
    value_path: StringProperty(name="Value", description="F-Curve shown and edited in Value mode (4)",
                               update=_mode_changed)
    value_index: IntProperty(name="Index", update=_mode_changed)
    hidden_keys: StringProperty(options={'HIDDEN'}, update=_mode_changed,
                                description="Frames of hidden keys (H / Alt+H)")


class EMP_Settings(bpy.types.PropertyGroup):
    paths: CollectionProperty(type=EMP_PathEntry)
    active_index: IntProperty()

    edit_mode: EnumProperty(
        name="Edit",
        description="Which trajectory is shown and edited (1 / 2 with the Edit Motion Path tool)",
        items=[
            ('LOCATION', "Location", "Trajectory of the bone head / object origin. "
             "Editing it changes the location keys (1)", 'CON_LOCLIKE', 0),
            ('ROTATION', "Rotation", "Trajectory of the bone tip. Editing it changes the rotation "
             "keys: Euler, Quaternion or Axis Angle (2)", 'CON_ROTLIKE', 1),
            ('SCALE', "Scale", "The location path drawn as a ribbon as wide as the scale. S or "
             "Alt+S (Shrink / Fatten) changes the scale keys (3)", 'CON_SIZELIKE', 2),
            ('VALUE', "Value", "One animated value (custom property, IK/FK switch, constraint "
             "influence...) drawn as a track above the path; move its keys up and down (4)",
             'DRIVER', 3),
        ],
        default='LOCATION', update=_mode_changed,
    )

    proportional_size: FloatProperty(
        name="Proportional Size",
        description="Radius of proportional editing on paths (on/off, falloff and connected are "
                    "Blender's own settings: O, Shift+O, Alt+O)",
        default=0.3, min=1e-4, soft_max=10.0, subtype='DISTANCE', unit='LENGTH')
    range_mode: EnumProperty(
        name="Range",
        description="Frame range the paths are calculated for",
        items=[
            ('SCENE', "Scene", "Scene frame range (or preview range when enabled)"),
            ('ACTION', "Action", "Frame range of the animated action"),
            ('MANUAL', "Manual", "Custom frame range"),
        ],
        default='SCENE', update=_rebake,
    )
    frame_start: IntProperty(name="Start", default=1, update=_rebake)
    frame_end: IntProperty(name="End", default=100, update=_rebake)
    use_window: BoolProperty(
        name="Around Current Frame",
        description="Only draw the part of the path around the current frame",
        default=False, update=_redraw)
    window_before: IntProperty(name="Before", default=15, min=0, update=_redraw)
    window_after: IntProperty(name="After", default=15, min=0, update=_redraw)

    color_mode: EnumProperty(
        name="Color",
        items=[
            ('SPEED', "Speed", "Color the path by speed: blue is slow, red is fast"),
            ('TIME', "Before / After", "Darker before the current frame, brighter after"),
            ('SOLID', "Solid", "One color"),
            ('JITTER', "Jitter", "Heat map of roughness: smooth motion stays gray, hitches, pops "
                                 "and wobbles glow yellow to red (select them and smooth)"),
        ],
        default='SPEED', update=_redraw,
    )
    jitter_threshold: FloatProperty(
        name="Jitter Threshold",
        description="How rough the motion has to be to show full red (lower = more sensitive). "
                    "Roughness is the frame-to-frame change of acceleration relative to the "
                    "average speed",
        default=0.4, min=0.01, soft_max=2.0, update=_redraw)
    color: FloatVectorProperty(name="Path Color", subtype='COLOR', size=3,
                               default=(0.35, 0.75, 1.0), min=0.0, max=1.0, update=_redraw)
    show_frames: BoolProperty(name="Frame Dots", default=True,
                              description="Show a dot on every frame (spacing)", update=_redraw)
    show_frame_numbers: BoolProperty(name="Key Numbers", default=False,
                                     description="Show frame numbers next to keys", update=_redraw)
    show_all_handles: BoolProperty(name="All Handles", default=False,
                                   description="Show handles of all keys, not only selected ones",
                                   update=_redraw)
    show_ghost: BoolProperty(name="Ghost While Editing", default=True,
                             description="Show the original path while dragging", update=_redraw)
    show_spokes: BoolProperty(name="Bone Lines", default=True,
                              description="Rotation paths: draw lines from the bone head to selected keys",
                              update=_redraw)
    on_top: BoolProperty(name="In Front", default=True,
                         description="Draw paths in front of geometry", update=_redraw)
    line_width: FloatProperty(name="Line Width", default=2.0, min=1.0, max=10.0, update=_redraw)
    value_track: EnumProperty(
        name="Value Track",
        description="Where Value mode (4) draws the value",
        items=[('UP', "Above the Path", "Higher on the screen = bigger value"),
               ('PATH', "Beside the Path", "Follows the shape of the path: farther to the left of "
                                           "the motion = bigger value (works however the path turns)")],
        default='UP', update=_refresh)
    ribbon_width: FloatProperty(name="Ribbon Width", default=1.0, min=0.05, soft_max=5.0,
                                description="Scale paths: how wide the ribbons are drawn", update=_redraw)
    show_scale_box: BoolProperty(name="Scale Box", default=True,
                                 description="Scale paths: a box at the current frame, as big as the "
                                             "current scale", update=_redraw)
    scale_box_size: FloatProperty(name="Box Size", default=1.0, min=0.05, soft_max=5.0, update=_redraw)
    key_size: FloatProperty(name="Key Size", default=9.0, min=3.0, max=30.0, update=_redraw)
    rot_length: FloatProperty(
        name="Rotation Path Length",
        description="Rotation paths trace a point along the bone axis at this multiple of the bone "
                    "length (object: in meters). Larger values make small rotations easier to see",
        default=1.0, min=0.05, soft_max=5.0, update=_refresh)
    pin_partial: BoolProperty(
        name="Pin Partial Keys",
        description="Path keys that are keyed on some channels only (dark center) get keyed on "
                    "the other channels too when a neighbor is edited, so they stay in place "
                    "instead of following it",
        default=True)
    auto_update: BoolProperty(
        name="Auto Update",
        description="Recalculate paths automatically when other animation (e.g. parents) changes",
        default=True)


classes = (EMP_PathEntry, EMP_Settings)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.emp = PointerProperty(type=EMP_Settings)


def unregister():
    del bpy.types.Scene.emp
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
