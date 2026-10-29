import bpy
from bpy.props import (BoolProperty, BoolVectorProperty, CollectionProperty, EnumProperty, FloatProperty,
                       FloatVectorProperty, IntProperty, PointerProperty, StringProperty)

from . import state


def _onion_vgroups(self, context, edit_text):
    """The vertex groups of the meshes the onion ghosts (the Vertex Group field's list)."""
    from . import onion
    names = set()
    for o, _arm, how in onion._targets(context.scene):
        if how != 'LINES':
            names.update(g.name for g in getattr(o, "vertex_groups", ()))
    return sorted(n for n in names if edit_text.lower() in n.lower())


def _onion_vgroup_changed(self, context):
    from . import onion
    onion._schedule()       # (the timer works out which vertices are in it)
    _redraw(self, context)


def _redraw(self, context):
    state.tag_redraw()


def _limb_target(context=None):
    """(armature, active pose bone) for the Limb Weight, or (None, None)."""
    context = context or bpy.context
    ob = getattr(context, "active_object", None)
    if ob is None or ob.type != 'ARMATURE' or ob.mode != 'POSE':
        return None, None
    pb = getattr(context, "active_pose_bone", None) or ob.pose.bones.get(getattr(ob.data.bones.active, "name", ""))
    return (ob, pb) if pb is not None else (None, None)


def _share(part):
    from . import com
    arm, pb = _limb_target()
    share = com.limb_share(arm, pb.name, part) if arm is not None else None
    return 100.0 * share if share else 0.0


def _set_share(value, part):
    from . import com
    arm, pb = _limb_target()
    if arm is not None:
        com.set_limb_share(arm, pb.name, value / 100.0, part)


def _get_limb(self):
    return _share('zone')


def _set_limb(self, value):
    _set_share(value, 'zone')


def _get_own(self):
    return _share('own')


def _set_own(self, value):
    _set_share(value, 'own')


def _get_own_mass(self):
    m = _get_own(self) / 100.0 * self.com_body_mass
    return m * LB_PER_KG if weight_unit() == 'LB' else m


def _set_own_mass(self, value):
    kg = value / LB_PER_KG if weight_unit() == 'LB' else value
    _set_own(self, 100.0 * kg / max(self.com_body_mass, 1e-6))


LB_PER_KG = 2.2046226


def weight_unit():
    p = prefs()
    return p.com_weight_unit if p is not None else 'KG'


def _get_limb_mass(self):
    share = _get_limb(self) / 100.0
    m = share * self.com_body_mass
    return m * LB_PER_KG if weight_unit() == 'LB' else m


def _set_limb_mass(self, value):
    kg = value / LB_PER_KG if weight_unit() == 'LB' else value
    _set_limb(self, 100.0 * kg / max(self.com_body_mass, 1e-6))


def _com_range(self, context):
    from . import com
    com._schedule(0.05)
    state.tag_redraw()


def _onion_rebuild(self, context):
    from . import onion
    onion.clear_all()
    state.tag_redraw()


def _rebake(self, context):
    # (the old paths stay shown and editable until the new range is baked)
    state.schedule_rebake(0.05, force=True)


def _mode_changed(self, context):
    state.sync_caches(context.scene)
    state.tag_redraw()


def _refresh(self, context):
    for c in state.caches.values():
        c.dirty = True
    state.tag_redraw()


_local_busy = [False]


def _local_changed(self, context):
    """Global / Local of a path in the list: the paths being edited (highlighted) take it too, when
    this one is one of them - like setting a property with several objects selected."""
    if not _local_busy[0]:
        _local_busy[0] = True
        try:
            st = state.settings(context.scene if context else None)
            mine = state.caches.get(state.entry_key(self)) if self.object is not None else None
            if st is not None and mine is not None and mine.owner_selected():
                for e in st.paths:
                    if e != self and e.object is not None and e.local != self.local:
                        c = state.caches.get(state.entry_key(e))
                        if c is not None and c.owner_selected():
                            e.local = self.local
        finally:
            _local_busy[0] = False
    _refresh(self, context)


_last_channels = [True, True, True, True]


def _keep_one_channel(self, context):
    """Easing channels: like the modifier channels, at least one stays on."""
    global _last_channels
    from .ops_easing import channel_labels
    n = len(channel_labels(context)) or 4
    cur = list(self.easing_channels)
    if not any(cur[:n]):
        if cur != _last_channels:
            self.easing_channels = _last_channels
        return
    _last_channels = cur


def _brush_items(self, context):
    from .ops_sculpt import brush_items
    return brush_items(self, context)


class EMP_BrushSettings(bpy.types.PropertyGroup):
    """Sculpt Motion Path brush settings (on the window manager: undo doesn't change them)."""
    brush: EnumProperty(name="Brush", items=_brush_items,
                        description="Sculpt Motion Path: what the brush does")
    radius: IntProperty(name="Radius", default=50, min=5, soft_max=400, subtype='PIXEL',
                        description="Brush radius on the screen (F)")
    strength: FloatProperty(name="Strength", default=0.5, min=0.0, max=1.0, subtype='FACTOR',
                            description="How strong the brush is (Shift+F)")
    falloff: EnumProperty(
        name="Falloff",
        items=[('SMOOTH', "Smooth", "", 'SMOOTHCURVE', 0), ('SPHERE', "Sphere", "", 'SPHERECURVE', 1),
               ('ROOT', "Root", "", 'ROOTCURVE', 2), ('SHARP', "Sharp", "", 'SHARPCURVE', 3),
               ('LINEAR', "Linear", "", 'LINCURVE', 4), ('CONSTANT', "Constant", "", 'NOCURVE', 5)],
        default='SMOOTH', description="How the brush fades out toward its edge")
    insert_keys: BoolProperty(
        name="Insert Keys", default=True,
        description="Grab, Smooth, Pinch: where there are few keys under the brush, add keys there "
                    "first (the motion doesn't change), so the brush can shape the path")
    dissolve_tolerance: FloatProperty(
        name="Tolerance", subtype='PERCENTAGE', default=1.0, min=0.01, soft_max=10.0,
        description="Dissolve: a key goes only if the path then moves less than this (percent of the "
                    "path size)")
    dissolve_smooth: FloatProperty(
        name="Smooth", default=0.0, min=0.0, soft_max=10.0, precision=1,
        description="Dissolve: first smooth out the tremble of the keys under the brush (mocap), over "
                    "about this many frames around each key; then the keys the smooth motion doesn't need "
                    "go (the Tolerance counts from the smooth motion). 0: off - only keys go, the motion "
                    "stays exactly as it was")
    dissolve_parent_chain: IntProperty(
        name="Chain Length", default=0, min=0, soft_max=10,
        description="Smooth Parents: how many bones / objects above it are smoothed (0: all of them, like "
                    "an IK constraint's Chain Length)")
    dissolve_smooth_parents: BoolProperty(
        name="Smooth Parents", default=False,
        description="Smooth brush, Dissolve with Smooth: also smooth the bones / objects it hangs from (the "
                    "shoulder, the chest...) over the same frames - in mocap every bone trembles, and the path "
                    "shows them all. Their keys stay where they are in time")
    dissolve_keep_timing: BoolProperty(
        name="Keep Timing", default=True,
        description="Dissolve: the timing must stay too (where the bone is on every frame). Off: only "
                    "the shape of the path counts, so more keys can go")


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
    local: BoolProperty(name="Local", default=False, update=_local_changed,
                        description="Global: the motion in the world (the parents' motion is in the path). "
                                    "Local: only this bone's / object's own motion - the path is drawn in its "
                                    "parent's space as it is on the current frame, so it moves along with the "
                                    "parent and the parent's motion isn't in it. The paths being edited "
                                    "(highlighted) all take it; drag over the icons to set several")


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
            ('SCALE', "Scale", "The location path drawn as a ribbon as wide as the scale. S "
             "changes the scale keys, Alt+S their handles (3)", 'CON_SIZELIKE', 2),
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
            # (the numbers keep files saved with the old order)
            ('SPEED', "Speed", "Color the path by speed: blue is slow, red is fast", 'NONE', 0),
            ('JITTER', "Jitter", "Heat map of roughness: smooth motion stays gray, hitches, pops "
                                 "and wobbles glow yellow to red (select them and smooth)", 'NONE', 3),
            ('TIME', "Before / After", "Darker before the current frame, brighter after", 'NONE', 1),
            ('SOLID', "Solid", "One color", 'NONE', 2),
        ],
        default='SPEED', update=_redraw,
    )
    show_frames: BoolProperty(name="Frame Dots", default=True,
                              description="Show a dot on every frame: the timing between the keys "
                                          "(Ctrl+Shift+H)", update=_redraw)
    show_frame_numbers: BoolProperty(name="Key Numbers", default=False,
                                     description="Show frame numbers next to keys", update=_redraw)
    show_handles: BoolProperty(name="Handles", default=True, update=_redraw,
                               description="Show the handles of the selected keys (Ctrl+H)")
    show_all_handles: BoolProperty(name="All Handles", default=False,
                                   description="Show handles of all keys, not only selected ones",
                                   update=_redraw)
    show_rot_rings: BoolProperty(name="Axis Rings", default=True,
                                 description="Rotation paths: the bone's X, Y and Z rings at selected "
                                             "keys. Drag a ring to turn around that axis (Y: twist), "
                                             "drag the key to turn freely",
                                 update=_redraw)
    show_onion: BoolProperty(name="Onion Skin", default=False, update=_redraw,
                             description="Ghosts of the edited bones / objects on the frames around the "
                                         "current one (captured in the background, so the viewport stays "
                                         "fast)")
    onion_mode: EnumProperty(
        name="Onion Mode", update=_redraw,
        items=[('SILHOUETTE', "Silhouette", "The meshes in one flat color per ghost"),
               ('WIRE', "Wire", "The meshes' edges"),
               ('POINTS', "Points", "Points of the meshes: the lightest to draw"),
               ('BONES', "Bones", "The bones as sticks (no meshes needed)")],
        default='SILHOUETTE')
    onion_before: IntProperty(name="Before", default=2, min=0, soft_max=10, update=_redraw,
                              description="How many ghosts before the current frame")
    onion_after: IntProperty(name="After", default=2, min=0, soft_max=10, update=_redraw,
                             description="How many ghosts after the current frame")
    onion_step: IntProperty(name="Step", default=0, min=0, soft_max=20, update=_redraw,
                            description="Frames between the ghosts (0: on the keys)")
    onion_opacity: FloatProperty(name="Opacity", default=0.3, min=0.02, max=1.0, subtype='FACTOR',
                                 update=_redraw)
    onion_max_tris: IntProperty(name="Detail", default=20000, min=500, soft_max=200000, update=_redraw,
                                description="Fast ghosts: most triangles / edges / points per mesh (fewer: faster)")
    onion_fast: BoolProperty(
        name="Fast", default=True, update=_onion_rebuild,
        description="Fast ghosts: every bent mesh is simplified once (down to Detail) and bent by its bones "
                    "only - quick also on heavy characters, but shape keys (correctives, expressions), "
                    "lattices and other deformers don't show. Off: Exact - every ghost is the mesh as "
                    "Blender makes it on that frame, in full detail (slower on heavy characters)")
    onion_bones: EnumProperty(
        name="Onion Bones", update=_redraw,
        items=[('SELECTED', "Selected Bones", "Only the selected bones"),
               ('VISIBLE', "Visible Bones", "The bones you can see (hidden bones and hidden bone "
                                           "collections are left out)")],
        default='VISIBLE')
    show_com: BoolProperty(
        name="Center of Mass", default=False, update=_redraw,
        description="Show the character's center of mass (from its deform bones and their weights), a "
                    "plumb line to the floor, the support and where the body is going: to check that a "
                    "pose is believable")
    com_trail: BoolProperty(name="Trajectory", default=False, update=_redraw,
                            description="Center of mass: its path over the frames (a jump's arc should be a "
                                        "parabola)")
    com_velocity: BoolProperty(name="Velocity", default=True, update=_redraw,
                               description="Center of mass: an arrow where the body is going (about 8 frames "
                                           "ahead)")
    com_support: BoolProperty(name="Support", default=True, update=_redraw,
                              description="Center of mass: the outline of what touches the floor; the plumb "
                                          "point is green inside it (stable), red outside, gray in the air")
    com_velocity_mode: EnumProperty(
        name="Velocity Length", update=_redraw,
        items=[('ADAPTIVE', "Adaptive", "As long as the way the body goes in about 8 frames (slow: short)"),
               ('FIXED', "Fixed", "Always the length below: only the direction"),
               ('BOTH', "Adaptive + Length", "The way in about 8 frames, plus the length below (a slow motion "
                                             "still shows its direction)")],
        default='ADAPTIVE')
    com_velocity_length: FloatProperty(name="Length", default=0.3, min=0.0, soft_max=2.0, subtype='DISTANCE',
                                       unit='LENGTH', update=_redraw,
                                       description="Center of mass: the arrow's length (Fixed) or what is added "
                                                   "to it (Adaptive + Length)")
    com_floor: EnumProperty(
        name="Floor", update=_redraw,
        items=[('AUTO', "Auto", "The lowest point of the character over the whole animation"),
               ('HEIGHT', "Height", "A floor at this height (a rooftop, a table...)"),
               ('CURSOR', "3D Cursor", "A floor at the height of the 3D cursor")],
        default='AUTO', description="Center of mass: where the floor is (the plumb line ends there, the "
                                    "support is what touches it)")
    com_limb_share: FloatProperty(
        name="Limb Weight", subtype='PERCENTAGE', min=0.1, max=95.0, precision=1, get=_get_limb,
        set=_set_limb,
        description="Center of mass: how much of the body's mass the active bone and what hangs on it are (the "
                    "whole arm on the shoulder). Worked out from the meshes; set it where it's off and the other "
                    "bones make up for it - the part keeps this weight also when a smaller part inside it gets "
                    "its own. It lights up while it changes; the arrow: worked out again")
    com_own_share: FloatProperty(
        name="Bone Weight", subtype='PERCENTAGE', min=0.1, max=95.0, precision=1, get=_get_own, set=_set_own,
        description="Center of mass: how much of the body's mass the active bone alone is, without its child "
                    "bones (a hand without the fingers). The rest of the part it carries makes up for it")
    com_own_mass: FloatProperty(
        name="Bone Weight", min=0.0, soft_max=200.0, precision=2, get=_get_own_mass, set=_set_own_mass,
        description="Center of mass: the mass of the active bone alone (in the unit set in the preferences)")
    com_limb_mass: FloatProperty(
        name="Limb Weight", min=0.0, soft_max=200.0, precision=2, get=_get_limb_mass, set=_set_limb_mass,
        description="Center of mass: the mass of the limb this controller moves (its share of the Body Mass, in "
                    "the unit set in the preferences)")
    com_body_mass: FloatProperty(
        name="Body Mass", default=70.0, min=0.1, soft_max=500.0, precision=1, update=_redraw,
        description="Center of mass: the character's whole mass in kilograms (only for showing the limbs' "
                    "masses in kg / lb; the center itself only needs the shares)")
    com_range_mode: EnumProperty(
        name="Trajectory Range", update=_com_range,
        description="Center of mass: the frames its trajectory (and velocity) is worked out for",
        items=[('SCENE', "Scene", "The scene's frame range (or the preview range when it's on)"),
               ('ACTION', "Action", "The frame range of the armature's action"),
               ('PATHS', "Paths", "The frames of the motion paths shown"),
               ('MANUAL', "Manual", "The frames set below")],
        default='SCENE')
    com_frame_start: IntProperty(name="Start", default=1, update=_com_range)
    com_frame_end: IntProperty(name="End", default=100, update=_com_range)
    com_use_window: BoolProperty(name="Around Current Frame", default=False, update=_redraw,
                                 description="Center of mass: only draw the trajectory around the current frame")
    com_window_before: IntProperty(name="Before", default=15, min=0, update=_redraw)
    com_window_after: IntProperty(name="After", default=15, min=0, update=_redraw)
    com_floor_height: FloatProperty(name="Floor Height", default=0.0, subtype='DISTANCE', unit='LENGTH',
                                    update=_redraw)
    onion_show: EnumProperty(
        name="Show", update=_onion_vgroup_changed,
        items=[('WHOLE', "Whole", "The whole character (every mesh the armature moves, and what is on it)"),
               ('WEIGHT', "By Weight", "Only the part of the mesh the edited bones move (their weights, and "
                                       "of the bones that follow them: children, IK chains)"),
               ('OBJECT', "By Object", "Only the objects the edited bones move: for characters made of "
                                       "separate parts, a part with no weight from them isn't shown"),
               ('VGROUP', "Vertex Group", "Only one vertex group of the ghosted meshes (say the lower part "
                                          "of a handle); what has no such group isn't shown")],
        default='WHOLE')
    onion_vgroup: StringProperty(
        name="Vertex Group", default="", update=_onion_vgroup_changed, search=_onion_vgroups,
        description="Show: Vertex Group - the vertex group of the ghosted meshes shown (what has no such "
                    "group isn't shown; empty: all of them)")
    onion_size: FloatProperty(name="Size", default=3.0, min=1.0, soft_max=12.0, update=_redraw,
                              description="Points / lines of the ghosts: how thick they are")
    onion_preview_pose: BoolProperty(
        name="Preview Unkeyed Pose", default=False, update=_redraw,
        description="The ghosts show the neighboring frames as if the pose you are changing were keyed "
                    "on this frame (during G / R / S and after it, until you change the frame). With Auto "
                    "Keying on, this happens during G / R / S anyway")
    onion_in_front: BoolProperty(name="In Front", default=True, update=_redraw,
                                 description="Draw the ghosts over everything (X-ray)")
    show_rot_ribbon: BoolProperty(name="Rotation Ribbon", default=True, update=_redraw,
                                  description="Rotation paths: a ribbon along the path of the tip, "
                                              "turned like the bone's X axis, so the twist (around "
                                              "Y) shows")
    rot_ribbon_side: EnumProperty(
        name="Ribbon Side", update=_redraw,
        items=[('BOTH', "Both Sides", "Centered on the path of the tip"),
               ('POSITIVE', "+X", "On the side of the bone's +X axis"),
               ('NEGATIVE', "-X", "On the side of the bone's -X axis")],
        default='BOTH')
    show_reach: BoolProperty(name="Reach", default=True,
                             description="Rotation paths: a circle showing how far the tip can go "
                                         "(it stays on a sphere around the bone head)", update=_redraw)
    show_timing_chart: BoolProperty(
        name="Timing Chart", default=True, update=_redraw,
        description="A timing chart of the selected keys at the bottom of the viewport: where every "
                    "frame is between them. Drag a frame tick (or a frame dot on the path) to change it")
    easing_mode: EnumProperty(
        name="Easing Mode",
        items=[('TIMING', "Keep the Path", "Retime along the path: the shape stays (Location and "
                                           "Rotation paths)"),
               ('CHANNELS', "Channels", "Blender's easing interpolation on the chosen channels, like in "
                                        "the Graph Editor")],
        default='TIMING')
    easing_direction: EnumProperty(
        name="Easing Direction",
        items=[('EASE_IN', "In", "Slow start"), ('EASE_OUT', "Out", "Slow end"),
               ('EASE_IN_OUT', "In & Out", "Slow start and end")],
        default='EASE_IN_OUT')
    easing_channels: BoolVectorProperty(name="Easing Channels", size=4, default=(True, True, True, True),
                                        update=_keep_one_channel,
                                        description="Which channels get the easing (drag across to "
                                                    "switch several; one always stays on)")
    easing_strength: FloatProperty(name="Strength", default=1.0, min=0.0, max=1.0, subtype='FACTOR',
                                   description="How much of the easing (0: the timing as it was)")
    easing_type: EnumProperty(
        name="Easing", update=_redraw,
        items=[('LINEAR', "Linear", "")] + [(i, n, "") for i, n in (
            ('SINE', "Sine"), ('QUAD', "Quadratic"), ('CUBIC', "Cubic"), ('QUART', "Quartic"),
            ('QUINT', "Quintic"), ('EXPO', "Exponential"), ('CIRC', "Circular"), ('BACK', "Back"),
            ('BOUNCE', "Bounce"), ('ELASTIC', "Elastic"))],
        default='SINE', description="The easing picked in the gallery (P), put on with Apply")
    easing_blend: FloatProperty(name="Blend", default=0.0, min=0.0, max=0.5, subtype='FACTOR', update=_redraw,
                                description="Keep the Path: how softly the easing joins the timing left as it "
                                            "is before From and after To (share of the part)")
    easing_invert: BoolProperty(name="Invert", default=False, update=_redraw,
                                description="Keep the Path: the easing goes on outside From - To, the timing between "
                                            "them stays as it is")
    easing_from: FloatProperty(name="From", default=0.0, min=0.0, max=1.0, subtype='FACTOR', update=_redraw,
                               description="Keep the Path: where in the selected part the easing begins "
                                           "(before it the timing stays; e.g. Out from 60%: only the end "
                                           "slows down, the start keeps its speed)")
    easing_to: FloatProperty(name="To", default=1.0, min=0.0, max=1.0, subtype='FACTOR', update=_redraw,
                             description="Keep the Path: where in the selected part the easing ends "
                                         "(after it the timing stays)")
    show_speed_graph: BoolProperty(name="Speed Graph", default=True, update=_redraw,
                                   description="A graph of the speed of the active path, above the "
                                               "timing chart")
    speed_range: EnumProperty(
        name="Speed Graph Range",
        items=[('SEGMENT', "Segment", "The selected keys (like the timing chart), or the keys around "
                                      "the current frame"),
               ('RANGE', "Range", "The frames set below"),
               ('AUTO', "Timeline", "The scene's frame range (preview range if used)")],
        default='SEGMENT', update=_redraw)
    speed_start: IntProperty(name="Start", default=1, update=_redraw,
                             description="First frame of the speed graph")
    speed_end: IntProperty(name="End", default=100, update=_redraw,
                           description="Last frame of the speed graph")
    value_track: EnumProperty(
        name="Value Track",
        description="Where Value mode (4) draws the value",
        items=[('UP', "Above the Path", "Higher on the screen = bigger value"),
               ('PATH', "Beside the Path", "Follows the shape of the path: farther from it = "
                                           "bigger value, on the side set below (stays put when "
                                           "the view turns)")],
        default='UP', update=_refresh)
    value_side: EnumProperty(
        name="Side",
        description="The Value track / the Scale Y ribbon beside the path: on which side of the path "
                    "it is drawn (around a circle always outside)",
        items=[('OUTSIDE', "Outside", "Away from the middle of the motion (around the outside of "
                                      "its curves)"),
               ('INSIDE', "Inside", "Toward the middle of the motion")],
        default='OUTSIDE', update=_refresh)
    scale_channel: EnumProperty(
        name="Scale Handles",
        description="Scale paths: whose handles (whiskers) are shown on the selected keys. Click a "
                    "gizmo handle to pick it",
        items=[('NONE', "None", ""), ('X', "X", "On the X ribbon's edges"),
               ('Y', "Y", "On the path (Y is the ribbons' color)"), ('Z', "Z", "On the Z ribbon's edges")],
        default='NONE', update=_redraw)
    circle_radius: FloatProperty(name="Circle Radius", default=0.0, min=0.0, soft_max=10.0,
                                 subtype='DISTANCE', unit='LENGTH', update=_refresh,
                                 description="Size of the circle (0: automatic)")
    circle_use_range: BoolProperty(name="Custom Range", default=False, update=_refresh,
                                   description="Frames around the circle (off: the scene's frame range)")
    circle_start: IntProperty(name="Start", default=1, update=_refresh)
    circle_end: IntProperty(name="End", default=100, update=_refresh)
    layout: EnumProperty(
        name="Layout",
        description="Scale (3) and Value (4) paths: draw them along the motion path, or around a "
                    "circle like a clock face (for things that don't move)",
        items=[('AUTO', "Auto", "A circle when the bone / object stays in place, the path otherwise"),
               ('PATH', "Along the Path", "On the location path"),
               ('CIRCLE', "Circle", "The frame range goes once around a circle; the current frame "
                                    "runs around it like on a clock")],
        default='AUTO', update=_refresh)
    show_key_values: BoolProperty(name="Values at Keys", default=False, update=_redraw,
                                  description="Scale paths: the scale values next to the keys")
    show_scale_box_values: BoolProperty(name="Box Values", default=True, update=_redraw,
                                        description="Scale paths: the current scale next to the Scale Box")
    show_scale_box: BoolProperty(name="Scale Box", default=True,
                                 description="Scale paths: a box at the current frame, as big as the "
                                             "current scale", update=_redraw)
    picked_handles: StringProperty(
        options={'HIDDEN'},
        description="Scale handles picked on their whiskers (JSON). Saved in the scene so undo "
                    "brings them back")
    select_order: StringProperty(
        options={'HIDDEN'},
        description="The paths in the order they got selected keys (JSON), for Offset Cascade. Saved in "
                    "the scene so undo brings it back")
    select_history: StringProperty(
        options={'HIDDEN'},
        description="Clicked keys, oldest first (JSON); the last one is the active key. Saved in "
                    "the scene so undo brings it back")
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


def _hotkeys_changed(self, context):
    from . import ui
    ui.refresh_tool_keymaps()


class EMP_Preferences(bpy.types.AddonPreferences):
    """Settings you set once and forget (the same in every file); the ones you change while
    animating stay in the tool header and the sidebar"""
    bl_idname = __package__

    color: FloatVectorProperty(name="Path Color", subtype='COLOR', size=3,
                               default=(0.35, 0.75, 1.0), min=0.0, max=1.0, update=_redraw)
    jitter_threshold: FloatProperty(
        name="Jitter Threshold",
        description="How rough the motion has to be to show full red (lower = more sensitive). "
                    "Roughness is the frame-to-frame change of acceleration relative to the "
                    "average speed",
        default=0.4, min=0.01, soft_max=2.0, update=_redraw)
    show_ghost: BoolProperty(name="Ghost While Editing", default=True,
                             description="Show the original path while dragging", update=_redraw)
    show_spokes: BoolProperty(name="Bone Lines", default=True,
                              description="Rotation paths: draw lines from the bone head to selected keys",
                              update=_redraw)
    show_repeats: BoolProperty(name="Loop Repeats", default=True,
                               description="Looping motion: draw the path outside the loop's keys "
                                           "(the repeats) faintly", update=_redraw)
    on_top: BoolProperty(name="In Front", default=True,
                         description="Draw paths in front of geometry", update=_redraw)
    line_width: FloatProperty(name="Line Width", default=2.0, min=1.0, max=10.0, update=_redraw)
    key_size: FloatProperty(name="Key Size", default=9.0, min=3.0, max=30.0, update=_redraw)
    rot_length: FloatProperty(
        name="Rotation Path Length",
        description="Rotation paths trace a point along the bone axis at this multiple of the bone "
                    "length (object: in meters). Larger values make small rotations easier to see",
        default=1.0, min=0.05, soft_max=5.0, update=_refresh)
    ribbon_width: FloatProperty(name="Ribbon Width", default=1.0, min=0.05, soft_max=5.0,
                                description="Scale paths: how wide the ribbons are drawn", update=_redraw)
    rot_ribbon_width: FloatProperty(name="Ribbon Width", default=1.0, min=0.05, soft_max=4.0,
                                    update=_redraw, description="Rotation ribbon: how wide it is")
    scale_box_size: FloatProperty(name="Box Size", default=1.0, min=0.05, soft_max=5.0, update=_redraw)
    smooth_display: BoolProperty(
        name="Smooth Display", default=True, update=_redraw,
        description="Anti-aliased lines and dots (paths, keys, handles, ribbons, charts): softer edges, a "
                    "little more drawing work")
    scale_y_display: EnumProperty(
        name="Scale Y", update=_redraw,
        items=[('RIBBON', "Ribbon", "The Y scale is a third ribbon beside the other two, as wide as it; drag "
                                    "its edge like the others"),
               ('COLOR', "Color", "The Y scale is the color of the ribbons (blue below 1, red above)")],
        default='RIBBON')
    onion_color_before: FloatVectorProperty(name="Before", subtype='COLOR', size=3, min=0.0, max=1.0,
                                            default=(1.0, 0.35, 0.3), update=_redraw)
    onion_color_after: FloatVectorProperty(name="After", subtype='COLOR', size=3, min=0.0, max=1.0,
                                           default=(0.3, 0.75, 1.0), update=_redraw)
    onion_cache_range: IntProperty(
        name="Ghosts Ahead", default=10, min=0, soft_max=100, update=_redraw,
        description="Onion skin: the ghosts are captured ahead of time this many frames before and after the "
                    "current frame (the ones shown always are); more: smoother playback, more work")
    onion_other_display: EnumProperty(
        name="Other Objects", update=_redraw,
        description="Onion skin: how objects without a shape of their own (speakers, light probes, force "
                    "fields...) are drawn",
        items=[('PLAIN_AXES', "Plain Axes", ""), ('ARROWS', "Arrows", ""), ('SINGLE_ARROW', "Single Arrow", ""),
               ('CIRCLE', "Circle", ""), ('CUBE', "Cube", ""), ('SPHERE', "Sphere", ""), ('CONE', "Cone", "")],
        default='PLAIN_AXES')
    com_limb_color: FloatVectorProperty(
        name="Limb Color", subtype='COLOR', size=3, min=0.0, max=1.0, default=(1.0, 0.8, 0.25), update=_redraw,
        description="Center of mass: the color a limb lights up in while its weight changes (the bones it "
                    "carries)")
    com_bone_color: FloatVectorProperty(
        name="Bone Color", subtype='COLOR', size=3, min=0.0, max=1.0, default=(0.35, 0.75, 1.0), update=_redraw,
        description="Center of mass: the color of the bone itself in the lit up limb (and of the part "
                    "Bone Weight changes)")
    com_floor_detail: FloatProperty(
        name="Floor Detail", default=1.0, min=1.0, soft_max=8.0, max=20.0, update=_redraw,
        description="Center of mass: how closely the support area on the floor follows the feet (1: a "
                    "grid of 2% of the character's height; higher: finer, a little more work while "
                    "the pose changes). The ghosts' Detail sets how fine the meshes it's taken from are")
    com_weight_unit: EnumProperty(
        name="Limb Weight In", update=_redraw,
        description="Center of mass: how a limb's weight is shown and set",
        items=[('KG', "Kilograms", "Its mass, from the character's Body Mass"),
               ('LB', "Pounds", "Its mass, from the character's Body Mass"),
               ('PERCENT', "Percent", "Its share of the whole body")],
        default='KG')
    speed_select_new: BoolProperty(
        name="Select New Keys", default=True, update=_redraw,
        description="Speed graph: the keys a drag of the curve adds are selected (so the graph keeps "
                    "showing the same keys)")
    ca_notice: BoolProperty(
        name="Continuous Acceleration Note", default=False,
        description="When a drag starts on curves with Blender's Continuous Acceleration smoothing (the whole "
                    "curve re-solves, so the path moves beyond the edited key), say so in the view. The "
                    "sidebar's Curve Smoothing panel shows it either way")
    speed_keep_ends: BoolProperty(
        name="Keep the Graph's Keys", default=True, update=_redraw,
        description="Speed graph: after a drag, the keys at both ends of the graph are selected too (one "
                    "key selected: its neighbors), so the graph doesn't jump to other keys")
    graph_corner: EnumProperty(
        name="Corner", update=_redraw,
        description="Timing chart and speed graph: the corner of the 3D view they are in",
        items=[('BOTTOM_RIGHT', "Bottom Right", ""), ('BOTTOM_LEFT', "Bottom Left", ""),
               ('TOP_RIGHT', "Top Right", ""), ('TOP_LEFT', "Top Left", "")],
        default='BOTTOM_RIGHT')
    graph_margin_x: IntProperty(name="Margin X", default=40, min=0, soft_max=600, subtype='PIXEL', update=_redraw,
                                description="Distance from the side of the view")
    graph_margin_y: IntProperty(name="Margin Y", default=46, min=0, soft_max=600, subtype='PIXEL', update=_redraw,
                                description="Distance from the bottom / top of the view")
    graph_width: IntProperty(name="Width", default=560, min=200, soft_max=1600, subtype='PIXEL', update=_redraw)
    chart_height: IntProperty(name="Timing Chart Height", default=66, min=40, soft_max=200, subtype='PIXEL',
                              update=_redraw)
    speed_height: IntProperty(name="Speed Graph Height", default=60, min=40, soft_max=400, subtype='PIXEL',
                              update=_redraw)
    graph_text_size: FloatProperty(name="Text Size", default=1.0, min=0.6, max=2.5, update=_redraw,
                                   description="The labels and frame numbers (1: the usual size)")
    graph_bg: FloatVectorProperty(name="Background", subtype='COLOR', size=4, min=0.0, max=1.0,
                                  default=(0.08, 0.08, 0.1, 0.62), update=_redraw)
    speed_curve_color: FloatVectorProperty(name="Speed Curve", subtype='COLOR', size=3, min=0.0, max=1.0,
                                           default=(0.55, 0.85, 1.0), update=_redraw)
    speed_fill_opacity: FloatProperty(name="Fill Opacity", default=0.32, min=0.0, max=1.0, subtype='FACTOR',
                                      update=_redraw, description="The soft fill under the speed curve")
    graph_key_color: FloatVectorProperty(name="Keys", subtype='COLOR', size=3, min=0.0, max=1.0,
                                         default=(0.92, 0.92, 0.92), update=_redraw)
    graph_sel_color: FloatVectorProperty(name="Selected Keys", subtype='COLOR', size=3, min=0.0, max=1.0,
                                         default=(1.0, 0.62, 0.2), update=_redraw)
    graph_frame_color: FloatVectorProperty(name="Current Frame", subtype='COLOR', size=3, min=0.0, max=1.0,
                                           default=(0.35, 1.0, 0.5), update=_redraw)
    chart_tick_color: FloatVectorProperty(name="Frame Ticks", subtype='COLOR', size=4, min=0.0, max=1.0,
                                          default=(1.0, 1.0, 1.0, 0.75), update=_redraw)
    chart_key_color: FloatVectorProperty(name="Key Ticks", subtype='COLOR', size=3, min=0.0, max=1.0,
                                         default=(1.0, 0.6, 0.15), update=_redraw)
    paths_first: BoolProperty(
        name="Paths First", default=True,
        description="A click near a path goes to the path, not to a bone or object under it: keys, "
                    "handles win from farther away (2.5 times). Away from them, a click selects bones and "
                    "objects as usual")
    hotkey_priority: BoolProperty(
        name="Hotkey Priority", default=True, update=_hotkeys_changed,
        description="The path tools' keys win over other add-ons' keys in the 3D view. Off: where "
                    "another add-on uses the same key, its key works and the path tool's doesn't")

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop(self, "smooth_display")       # (all of the add-on's drawing)
        layout.separator()
        col = layout.column(heading="Keys")
        col.prop(self, "hotkey_priority")
        col.prop(self, "paths_first")
        col.prop(self, "ca_notice")
        layout.separator()
        col = layout.column()
        col.label(text="Paths")
        col.prop(self, "line_width")
        col.prop(self, "key_size")
        col.prop(self, "color", text="Path Color (Time / Solid)")
        col.prop(self, "jitter_threshold", text="Jitter Threshold")
        col.prop(self, "on_top")
        sub = col.column(heading="Show")
        sub.prop(self, "show_ghost")
        sub.prop(self, "show_repeats")
        sub.prop(self, "show_spokes")
        layout.separator()
        col = layout.column()
        col.label(text="Rotation and Scale")
        col.prop(self, "rot_length")
        col.prop(self, "rot_ribbon_width", text="Rotation Ribbon Width")
        col.prop(self, "ribbon_width", text="Scale Ribbon Width")
        col.prop(self, "scale_box_size", text="Scale Box Size")
        col.prop(self, "scale_y_display")
        layout.separator()
        col = layout.column()
        col.label(text="Onion Skin")
        col.prop(self, "onion_color_before", text="Before")
        col.prop(self, "onion_color_after", text="After")
        col.prop(self, "onion_other_display")
        col.prop(self, "onion_cache_range")
        layout.separator()
        col = layout.column()
        col.label(text="Center of Mass")
        col.prop(self, "com_weight_unit")
        col.prop(self, "com_limb_color")
        col.prop(self, "com_bone_color")
        col.prop(self, "com_floor_detail")
        layout.separator()
        col = layout.column()
        col.label(text="Timing Chart and Speed Graph (the 3D view shows changes right away)")
        sub = col.column(heading="After a Drag")
        sub.prop(self, "speed_select_new")
        sub.prop(self, "speed_keep_ends")
        col.prop(self, "graph_corner")
        sub = col.column(align=True)
        sub.prop(self, "graph_margin_x")
        sub.prop(self, "graph_margin_y")
        sub = col.column(align=True)
        sub.prop(self, "graph_width")
        sub.prop(self, "chart_height")
        sub.prop(self, "speed_height")
        col.prop(self, "graph_text_size")
        col.prop(self, "graph_bg")
        col.prop(self, "speed_curve_color")
        col.prop(self, "speed_fill_opacity")
        col.prop(self, "graph_key_color")
        col.prop(self, "graph_sel_color")
        col.prop(self, "graph_frame_color")
        col.prop(self, "chart_tick_color")
        col.prop(self, "chart_key_color")
        col.operator("emp.reset_graph_look", icon='LOOP_BACK')


def prefs():
    """The add-on's preferences (None while it isn't registered, e.g. in a background script)."""
    try:
        return bpy.context.preferences.addons[__package__].preferences
    except (KeyError, AttributeError):
        return None


def _pref_property(name):
    """A scene setting that lives in the preferences now: st.<name> still reads and writes it."""
    default = EMP_Preferences.__annotations__[name].keywords.get("default")

    def get(self):
        p = prefs()
        return getattr(p, name) if p is not None else default

    def set(self, value):
        p = prefs()
        if p is not None:
            setattr(p, name, value)
    return property(get, set)


PREF_NAMES = ("color", "jitter_threshold", "show_ghost", "show_spokes", "show_repeats", "on_top", "line_width",
              "key_size", "rot_length", "ribbon_width", "rot_ribbon_width", "scale_box_size", "scale_y_display",
              "smooth_display",
              "onion_color_before", "onion_color_after",
              "onion_other_display", "com_limb_color", "com_bone_color", "com_floor_detail", "graph_corner", "graph_margin_x", "graph_margin_y", "graph_width",
              "chart_height", "speed_height", "graph_text_size", "graph_bg", "speed_curve_color", "speed_fill_opacity",
              "graph_key_color", "graph_sel_color", "graph_frame_color", "chart_tick_color", "chart_key_color",
              "com_weight_unit", "speed_select_new", "speed_keep_ends")
GRAPH_LOOK = PREF_NAMES[-18:-3]
for _n in PREF_NAMES:
    setattr(EMP_Settings, _n, _pref_property(_n))

classes = (EMP_BrushSettings, EMP_PathEntry, EMP_Settings, EMP_Preferences)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.emp = PointerProperty(type=EMP_Settings)
    bpy.types.WindowManager.emp_brush = PointerProperty(type=EMP_BrushSettings)


def unregister():
    del bpy.types.WindowManager.emp_brush
    del bpy.types.Scene.emp
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
