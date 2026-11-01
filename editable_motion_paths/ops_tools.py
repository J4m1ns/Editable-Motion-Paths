"""Path management and key operations: Smooth, Exaggerate, handles, delete, insert."""

import math

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from mathutils import Quaternion, Vector

from . import compat, core, pick, state
from .ops_transform import NumInput


# ----------------------------------------------------------------------------
# Path management
# ----------------------------------------------------------------------------

def _varies(fc):
    if fc is None or len(fc.keyframe_points) < 2:
        return False
    ys = [kp.co[1] for kp in fc.keyframe_points]
    return max(ys) - min(ys) > 1e-5


def auto_type(tg):
    if tg.is_bone and tg.owner.bone.use_connect:
        return core.ROTATION
    if all(tg.loc_locks()):
        return core.ROTATION
    if any(_varies(fc) for fc in tg.loc.fcurves):
        return core.LOCATION
    if any(_varies(fc) for fc in tg.rot.fcurves):
        return core.ROTATION
    return core.LOCATION


def _targets(context):
    out = []
    if context.mode == 'POSE':
        for pb in context.selected_pose_bones or []:
            out.append((pb.id_data, pb.name, pb))
    else:
        for obj in context.selected_objects:
            out.append((obj, "", obj))
    return out


class EMP_OT_path_add(bpy.types.Operator):
    """Show an editable motion path for the selected bones (Pose Mode) or objects. What it """ \
        """shows (Location, Rotation, Scale or Value) is chosen with the edit mode (1 / 2 / 3 / 4)"""
    bl_idname = "emp.path_add"
    bl_label = "Add Motion Path"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode in {'POSE', 'OBJECT'}

    def execute(self, context):
        st = state.settings(context.scene)
        existing = {(e.object.name if e.object else None, e.bone_name) for e in st.paths}
        added = []
        for obj, bone_name, owner in _targets(context):
            if (obj.name, bone_name) in existing:
                continue
            e = st.paths.add()
            e.object = obj
            e.bone_name = bone_name
            st.active_index = len(st.paths) - 1
            added.append(core.Target(obj, owner, bool(bone_name)))
        if not added:
            self.report({'INFO'}, "Nothing new to add: select bones (Pose Mode) or objects")
            return {'CANCELLED'}
        # only rotation is animated (FK bones...): show what can actually be edited
        if st.edit_mode == 'LOCATION' and all(auto_type(tg) == core.ROTATION for tg in added):
            st.edit_mode = 'ROTATION'
            self.report({'INFO'}, "Switched to Rotation paths (1 / 2 switches Location / Rotation)")
        elif st.edit_mode == 'ROTATION' and all(not tg.rot.any() and tg.loc.any() for tg in added):
            st.edit_mode = 'LOCATION'
            self.report({'INFO'}, "Switched to Location paths (1 / 2 switches Location / Rotation)")
        state.sync_caches(context.scene)
        state.bake_all(context.scene)
        return {'FINISHED'}


class EMP_OT_path_remove(bpy.types.Operator):
    """Remove motion paths from the list"""
    bl_idname = "emp.path_remove"
    bl_label = "Remove Motion Paths"
    bl_options = {'REGISTER', 'UNDO'}

    TIPS = {'PICKED': "Remove the highlighted motion paths (the edited ones) from the list - or, with none, "
                      "the active one",
            'ACTIVE': "Remove the active motion path from the list",
            'SELECTED': "Remove the motion paths of the selected bones / objects from the list",
            'ALL': "Remove all motion paths from the list"}
    scope: EnumProperty(items=[
        ('PICKED', "Highlighted", TIPS['PICKED']),
        ('ACTIVE', "Active", TIPS['ACTIVE']),
        ('SELECTED', "Selected", TIPS['SELECTED']),
        ('ALL', "All", TIPS['ALL'])], default='ACTIVE')

    @classmethod
    def description(cls, context, properties):
        return cls.TIPS.get(properties.scope, cls.__doc__)

    def execute(self, context):
        st = state.settings(context.scene)
        picked = []
        if self.scope == 'PICKED':
            for i, e in enumerate(st.paths):
                c = state.caches.get(state.entry_key(e)) if e.object is not None else None
                if c is not None and c.owner_selected():
                    picked.append(i)
        if self.scope == 'ALL':
            st.paths.clear()
        elif picked:
            for i in reversed(picked):
                st.paths.remove(i)
            st.active_index = min(max(picked[0], 0), len(st.paths) - 1)
        elif self.scope in {'ACTIVE', 'PICKED'}:
            if 0 <= st.active_index < len(st.paths):
                st.paths.remove(st.active_index)
                st.active_index = min(st.active_index, len(st.paths) - 1)
        else:
            sel = {(o.name, b) for o, b, _ in _targets(context)}
            for i in reversed(range(len(st.paths))):
                e = st.paths[i]
                if e.object is None or (e.object.name, e.bone_name) in sel:
                    st.paths.remove(i)
            st.active_index = min(st.active_index, len(st.paths) - 1)
        state.sync_caches(context.scene)
        state.tag_redraw()
        return {'FINISHED'}


def graph_channels(entry):
    """(all F-Curves of the path's bone / object, {(data_path, index)} of the path's channels in
    the current mode) or None."""
    cache = state.caches.get(state.entry_key(entry)) if entry.object is not None else None
    tg = cache.target() if cache is not None else None
    if tg is None:
        return None
    fcs = compat.get_fcurves(entry.object)
    if fcs is None:
        return None
    if tg.prefix:
        fcs = [fc for fc in fcs if fc.data_path.startswith(tg.prefix)]
    want = {(fc.data_path, fc.array_index) for fc in tg.channels(cache.ptype).fcurves if fc is not None}
    return list(fcs), want


def graph_isolated(entry):
    """Does the Graph Editor show only this path's channels (the others hidden)?"""
    got = graph_channels(entry)
    if got is None:
        return False
    fcs, want = got
    if not want or len(fcs) <= len(want):
        return False
    return all(fc.hide == ((fc.data_path, fc.array_index) not in want) for fc in fcs)


class EMP_OT_graph_isolate(bpy.types.Operator):
    """Graph Editor: show only this path's curves of the current mode (Location, Rotation, """ \
        """Scale or the Value) and hide the bone's / object's other curves. Click again to show """ \
        """them all"""
    bl_idname = "emp.graph_isolate"
    bl_label = "Isolate in Graph Editor"
    bl_options = {'REGISTER', 'UNDO'}

    index: bpy.props.IntProperty(options={'HIDDEN'})

    def execute(self, context):
        st = state.settings(context.scene)
        if not 0 <= self.index < len(st.paths):
            return {'CANCELLED'}
        entry = st.paths[self.index]
        got = graph_channels(entry)
        if got is None:
            self.report({'WARNING'}, "This path has no animation curves")
            return {'CANCELLED'}
        fcs, want = got
        name = entry.bone_name or entry.object.name
        mode = state.edit_mode(context.scene).title()
        if graph_isolated(entry):
            for fc in fcs:
                fc.hide = False
            msg = "Graph Editor: all curves of %s shown again" % name
        else:
            for fc in fcs:
                mine = (fc.data_path, fc.array_index) in want
                fc.hide = not mine
                fc.select = mine
            msg = "Graph Editor: only the %s curves of %s" % (mode, name)
        graphs = [(w, a) for w in context.window_manager.windows for a in w.screen.areas
                  if a.type == 'GRAPH_EDITOR']
        for w, a in graphs:
            region = next((r for r in a.regions if r.type == 'WINDOW'), None)
            if region is not None:
                try:
                    with context.temp_override(window=w, area=a, region=region):
                        bpy.ops.graph.view_all()
                except RuntimeError:
                    pass
            a.tag_redraw()
        if not graphs:
            msg += " (open a Graph Editor to see them)"
        self.report({'INFO'}, msg)
        state.flash_message(msg)
        pick.tag_anim_editors()
        return {'FINISHED'}


class EMP_OT_lock_info(bpy.types.Operator):
    """This path can't be edited in the current mode"""
    bl_idname = "emp.lock_info"
    bl_label = "Locked Path"
    bl_options = {'INTERNAL'}

    reason: bpy.props.StringProperty(options={'HIDDEN'})

    @classmethod
    def description(cls, context, properties):
        return "Locked: %s" % (properties.reason or "this path can't be edited in the current mode")

    def execute(self, context):
        self.report({'INFO'}, "Locked: %s" % self.reason)
        return {'FINISHED'}


def reset_property(owner, name):
    """Set one property back to its default (and run its update), Python-defined or built in."""
    p = owner.bl_rna.properties.get(name)
    if p is None or p.is_readonly:
        return False
    if p.type in {'INT', 'FLOAT', 'BOOLEAN'}:
        setattr(owner, name, p.default_array if getattr(p, "is_array", False) else p.default)
    elif p.type == 'ENUM':
        if p.is_enum_flag:
            setattr(owner, name, set(p.default_flag))
        elif p.default:
            setattr(owner, name, p.default)
        else:
            owner.property_unset(name)      # (items made on the fly: the first one)
    elif p.type == 'STRING':
        setattr(owner, name, p.default)
    else:
        return False
    return True


class EMP_OT_reset_button(bpy.types.Operator):
    """Set this setting back to its default value. Blender's own Reset to Default Value doesn't """ \
        """reach the panels opened with a key under the mouse (U, J, P, the right click of the """ \
        """sculpt tool); this one does"""
    bl_idname = "emp.reset_button"
    bl_label = "Reset to Default Value"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return getattr(context, "button_prop", None) is not None and \
            getattr(context, "button_pointer", None) is not None

    def execute(self, context):
        if not reset_property(context.button_pointer, context.button_prop.identifier):
            return {'CANCELLED'}
        state.tag_redraw()
        return {'FINISHED'}


class EMP_OT_com_limb_reset(bpy.types.Operator):
    """Set this weight back to the one worked out from the meshes"""
    bl_idname = "emp.com_limb_reset"
    bl_label = "Worked Out Weight"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    part: EnumProperty(name="Part", items=[('zone', "Limb Weight", "The bone with what hangs on it"),
                                           ('own', "Bone Weight", "The bone alone")],
                       default='zone')

    def execute(self, context):
        from . import com
        from .props import _limb_target
        arm, pb = _limb_target(context)
        if arm is None or not com.reset_limb(arm, pb.name, self.part):
            return {'CANCELLED'}
        return {'FINISHED'}


class EMP_OT_com_reset_all(bpy.types.Operator):
    """Set every weight of this rig back to the ones worked out from the meshes"""
    bl_idname = "emp.com_reset_all"
    bl_label = "Reset All Weights"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        from . import com
        from .props import _limb_target
        arm, _pb = _limb_target(context)
        if arm is None:
            ob = context.active_object
            arm = ob if ob is not None and ob.type == 'ARMATURE' else None
        if arm is None:
            self.report({'WARNING'}, "Select a bone of the rig")
            return {'CANCELLED'}
        n = com.reset_all(arm)
        self.report({'INFO'}, "%d weight%s back to the worked out ones" % (n, "" if n == 1 else "s"))
        return {'FINISHED'}


class EMP_OT_com_set_weights(bpy.types.Operator):
    """Center of mass: set the weight of all the selected bones at once - the same for each, or """         """one weight split among them as they compare"""
    bl_idname = "emp.com_set_weights"
    bl_label = "Set Weights"
    bl_options = {'REGISTER', 'UNDO'}

    part: EnumProperty(name="Weight", items=[('zone', "Limb Weight", "Each bone with what hangs on it"),
                                             ('own', "Bone Weight", "Each bone alone")], default='zone')
    split: EnumProperty(name="Mode", items=[
        ('EACH', "Each", "Every selected bone gets this weight"),
        ('SPLIT', "Split", "This weight is shared among the selected bones as they compare now (a heavier "
                           "one gets more): together they are this much of the body. A limb inside another selected one (a hand "
                           "in an arm) is left out: the outer one holds it")], default='EACH')
    amount: FloatProperty(name="Weight", min=0.0, soft_max=100.0, default=5.0,
                          description="In the unit of the preferences (percent of the body, or kg / lb of "
                                      "the Body Mass)")

    @staticmethod
    def _bones(context):
        from .props import _limb_target
        arm, _pb = _limb_target(context)
        if arm is None:
            return None, []
        return arm, [pb.name for pb in (context.selected_pose_bones or ()) if pb.id_data == arm]

    @classmethod
    def poll(cls, context):
        return context.mode == 'POSE' and len(cls._bones(context)[1]) > 1

    def _unit(self, context):
        from .props import weight_unit
        return weight_unit()

    def _to_share(self, context):
        from .props import LB_PER_KG
        unit = self._unit(context)
        if unit == 'PERCENT':
            return self.amount / 100.0
        kg = self.amount / LB_PER_KG if unit == 'LB' else self.amount
        return kg / max(state.settings(context.scene).com_body_mass, 1e-6)

    def invoke(self, context, event):
        from . import com
        from .props import LB_PER_KG
        arm, bones = self._bones(context)
        apb = context.active_pose_bone
        share = com.limb_share(arm, apb.name, self.part) if apb is not None and apb.id_data == arm else None
        if share:
            unit = self._unit(context)
            body = state.settings(context.scene).com_body_mass
            self.amount = 100.0 * share if unit == 'PERCENT' else                 share * body * (LB_PER_KG if unit == 'LB' else 1.0)
        return context.window_manager.invoke_props_dialog(self, width=300)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, "part")
        layout.row().prop(self, "split", expand=True)
        unit = self._unit(context)
        layout.prop(self, "amount", text="Weight" + {'PERCENT': ", %", 'KG': ", kg", 'LB': ", lb"}[unit])
        n = len(self._bones(context)[1])
        layout.label(text=("%d bones, each" if self.split == 'EACH' else "%d bones, together") % n, icon='INFO')

    def execute(self, context):
        from . import com
        arm, bones = self._bones(context)
        if arm is None or len(bones) < 2:
            self.report({'WARNING'}, "Select several bones of the rig (Pose Mode)")
            return {'CANCELLED'}
        done = com.set_shares(arm, bones, self._to_share(context), self.part, self.split == 'SPLIT')
        if not done:
            self.report({'WARNING'}, "None of these bones moves a part with weight of its own")
            return {'CANCELLED'}
        left = len(bones) - len(done)
        self.report({'INFO'}, "%d weights set%s" % (len(done), "" if not left else
                                                     " (%d left out: the whole body, nothing with weight, or "
                                                     "inside another selected limb)" % left))
        state.tag_redraw()
        return {'FINISHED'}


class EMP_OT_edit_limb_weight(bpy.types.Operator):
    """Center of mass: how much of the body the limb the active controller moves is - set it """ \
        """in a small popup under the mouse (the limb lights up)"""
    bl_idname = "emp.edit_limb_weight"
    bl_label = "Edit Limb Weight"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        st = state.settings(context.scene)
        return st is not None and st.show_com and context.mode == 'POSE'

    def invoke(self, context, event):
        from . import com
        from .props import _limb_target
        arm, pb = _limb_target(context)
        if arm is not None and com.limb_share(arm, pb.name):
            com.flash_limb(arm, pb.name)
        return context.window_manager.invoke_popup(self, width=260)

    def draw(self, context):
        from .ui import draw_limb_weight
        draw_limb_weight(self.layout, state.settings(context.scene), body_mass=False)

    def execute(self, context):
        return {'FINISHED'}


class EMP_OT_reset_graph_look(bpy.types.Operator):
    """Set the look of the timing chart and the speed graph (place, size, colors, text) back to """ \
        """the defaults"""
    bl_idname = "emp.reset_graph_look"
    bl_label = "Reset the Charts' Look"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        from .props import GRAPH_LOOK, prefs
        p = prefs()
        if p is None:
            return {'CANCELLED'}
        for name in GRAPH_LOOK:
            reset_property(p, name)
        state.tag_redraw()
        return {'FINISHED'}


def button_context_entry(self, context):
    """Right click on a setting in a panel opened under the mouse: a Reset that works there."""
    region = getattr(context, "region", None)
    area = getattr(context, "area", None)
    if region is None or area is None or region.type != 'WINDOW' or area.type != 'VIEW_3D':
        return              # (sidebar, headers: Blender's own Reset works)
    if getattr(context, "button_prop", None) is None or getattr(context, "button_pointer", None) is None:
        return
    self.layout.separator()
    self.layout.operator(EMP_OT_reset_button.bl_idname, text="Reset to Default (Motion Paths)", icon='LOOP_BACK')


def _anim_curves(context):
    """The F-Curves of the selected bones / objects' paths (each once)."""
    seen, out = set(), []
    for cache in state.editable_caches(context.scene):
        tg = cache.target()
        if tg is None:
            continue
        for ptype in (core.LOCATION, core.ROTATION, core.SCALE):
            for fc in tg.channels(ptype).fcurves:
                if fc is not None and fc.as_pointer() not in seen:
                    seen.add(fc.as_pointer())
                    out.append(fc)
    return out


def cont_accel_curves(context):
    """How many of them use Blender's Continuous Acceleration smoothing."""
    return sum(1 for fc in _anim_curves(context) if getattr(fc, "auto_smoothing", 'NONE') != 'NONE')


class EMP_OT_onion_update(bpy.types.Operator):
    """Build the onion ghosts (and the center of mass) again, e.g. after editing a mesh's shape"""
    bl_idname = "emp.onion_update"
    bl_label = "Update Ghosts"

    def execute(self, context):
        from . import com, onion
        onion.clear_all()
        com.clear()
        state.tag_redraw()
        return {'FINISHED'}


class EMP_OT_set_view(bpy.types.Operator):
    """Change how the paths are shown / edited (a hotkey like 1-4, Ctrl+H): not an undo step, like
    Blender's overlays, so Ctrl+Z goes straight to the edits"""
    bl_idname = "emp.set_view"
    bl_label = "Path Display"
    bl_options = {'INTERNAL'}

    name: StringProperty(options={'SKIP_SAVE'})
    value: StringProperty(options={'SKIP_SAVE'})

    def execute(self, context):
        st = state.settings(context.scene)
        if st is None or self.name not in state.VIEW_SETTINGS:
            return {'CANCELLED'}
        if self.value:
            setattr(st, self.name, self.value)
        else:
            setattr(st, self.name, not getattr(st, self.name))
        state.tag_redraw()
        return {'FINISHED'}


class EMP_OT_onion_mode(bpy.types.Operator):
    """How the ghosts are drawn"""
    bl_idname = "emp.onion_mode"
    bl_label = "Onion Mode"
    bl_options = {'INTERNAL'}

    mode: EnumProperty(items=[('SILHOUETTE', "Silhouette", ""), ('WIRE', "Wire", ""),
                              ('POINTS', "Points", ""), ('BONES', "Bones", "")])

    @classmethod
    def description(cls, context, properties):
        st = state.settings(context.scene)
        return st.bl_rna.properties["onion_mode"].enum_items[properties.mode].description

    def execute(self, context):
        state.settings(context.scene).onion_mode = self.mode
        return {'FINISHED'}


class EMP_OT_path_space(bpy.types.Operator):
    """Draw (and edit) the path in this space; the highlighted paths all take it"""
    bl_idname = "emp.path_space"
    bl_label = "Path Space"
    bl_options = {'UNDO', 'INTERNAL'}

    index: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})
    space: EnumProperty(items=[('GLOBAL', "Global", ""), ('LOCAL', "Local", ""), ('CAMERA', "Camera", "")],
                        options={'SKIP_SAVE'})

    @classmethod
    def description(cls, context, props):
        from .props import EMP_PathEntry
        it = {i.identifier: i for i in EMP_PathEntry.bl_rna.properties["space"].enum_items}.get(props.space)
        return it.description if it is not None else ""

    def execute(self, context):
        st = state.settings(context.scene)
        if not 0 <= self.index < len(st.paths):
            return {'CANCELLED'}
        st.paths[self.index].space = self.space
        return {'FINISHED'}


class EMP_OT_path_space_menu(bpy.types.Operator):
    """Global / Local / Camera: the world, only the bone's / object's own motion, or as the active """ \
        """camera sees it. The highlighted paths all take it"""
    bl_idname = "emp.path_space_menu"
    bl_label = "Path Space"
    bl_options = {'INTERNAL'}

    index: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def invoke(self, context, event):
        index = self.index
        st = state.settings(context.scene)
        if not 0 <= index < len(st.paths):
            return {'CANCELLED'}
        cur = st.paths[index].space

        def draw(menu, _context):
            for sp, name, icon in (('GLOBAL', "Global", 'WORLD'), ('LOCAL', "Local", 'ORIENTATION_LOCAL'),
                                   ('CAMERA', "Camera", 'CAMERA_DATA')):
                op = menu.layout.operator("emp.path_space", text=name, icon=icon, depress=sp == cur)
                op.index, op.space = index, sp
        context.window_manager.popup_menu(draw, title="Space")
        return {'FINISHED'}


class EMP_OT_path_pick(bpy.types.Operator):
    """Edit this path: its bone / object gets selected (Shift: added to the selection - edit several """ \
        """paths at once; on one already selected it becomes the active one, on the active one it is """ \
        """taken out - like Blender's Shift+click)"""
    bl_idname = "emp.path_pick"
    bl_label = "Pick Path"
    bl_options = {'UNDO'}                  # (no redo panel: a click in a list)

    index: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})
    extend: BoolProperty(name="Extend", default=False, options={'HIDDEN', 'SKIP_SAVE'})

    def invoke(self, context, event):
        self.extend = event.shift
        return self.execute(context)

    def execute(self, context):
        st = state.settings(context.scene)
        if not 0 <= self.index < len(st.paths):
            return {'CANCELLED'}
        e = st.paths[self.index]
        ob = e.object
        if ob is None or not ob.visible_get():
            self.report({'WARNING'}, "Its object is hidden or gone")
            return {'CANCELLED'}
        st.active_index = self.index
        vl = context.view_layer
        if e.bone_name:
            pb = ob.pose.bones.get(e.bone_name) if ob.pose else None
            if pb is None:
                return {'CANCELLED'}
            if context.mode != 'POSE' or ob not in (context.objects_in_mode or []):
                # (its armature into Pose Mode; with Shift the other armatures already in it stay)
                if context.mode != 'OBJECT':
                    bpy.ops.object.mode_set(mode='OBJECT')
                if not self.extend:
                    for o in context.selected_objects:
                        o.select_set(False)
                ob.select_set(True)
                vl.objects.active = ob
                bpy.ops.object.mode_set(mode='POSE')
            if self.extend:
                # (like Blender: selected but not active -> active; active -> out of the selection)
                on = not (compat.bone_selected(pb) and ob.data.bones.active == pb.bone)
                compat.bone_select_set(pb, on)
                if on:
                    ob.data.bones.active = pb.bone
                    state.note_selected((ob.name, e.bone_name))      # (Offset Cascade: picked in this order)
                else:
                    state.forget_selected([(ob.name, e.bone_name)])
            else:
                for o in context.objects_in_mode or [ob]:
                    if o.pose:
                        for other in o.pose.bones:
                            if compat.bone_selected(other):
                                compat.bone_select_set(other, False)
                compat.bone_select_set(pb, True)
                ob.data.bones.active = pb.bone
                state.forget_selected(state.selection_order())
                state.note_selected((ob.name, e.bone_name))
        elif self.extend and context.mode == 'POSE' and ob.type != 'ARMATURE':
            # (Shift with bone paths being edited: the object joins them - its armature stays in
            # Pose Mode, so the bones' paths stay edited too; the active object stays the armature)
            on = not ob.select_get()
            ob.select_set(on)
            if on:
                state.note_selected((ob.name, ""))
            else:
                state.forget_selected([(ob.name, "")])
        else:
            if context.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            if self.extend:
                on = not (ob.select_get() and vl.objects.active == ob)
                ob.select_set(on)
                if on:
                    vl.objects.active = ob
                    state.note_selected((ob.name, ""))
                else:
                    state.forget_selected([(ob.name, "")])
            else:
                for o in context.selected_objects:
                    o.select_set(False)
                ob.select_set(True)
                vl.objects.active = ob
                state.forget_selected(state.selection_order())
                state.note_selected((ob.name, ""))
        state.tag_redraw()
        return {'FINISHED'}


class EMP_OT_smoothing_none(bpy.types.Operator):
    """Turn off Blender's Continuous Acceleration smoothing on every curve of the selected bones """ \
        """/ objects (Auto Handle Smoothing: None): editing a key then changes only its own """ \
        """segments, not the auto handles a few keys away. (The auto handles are solved once more """ \
        """the plain way, so the path may shift a little right now)"""
    bl_idname = "emp.smoothing_none"
    bl_label = "Smoothing: None"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        n = 0
        seen = set()
        for cache in state.editable_caches(context.scene):
            tg = cache.target()
            if tg is None:
                continue
            for ptype in (core.LOCATION, core.ROTATION, core.SCALE):
                for fc in tg.channels(ptype).fcurves:
                    if fc is None or fc.as_pointer() in seen:
                        continue
                    seen.add(fc.as_pointer())
                    if fc.auto_smoothing != 'NONE':
                        fc.auto_smoothing = 'NONE'
                        fc.update()
                        n += 1
        self.report({'INFO'}, "Auto Handle Smoothing set to None on %d curve%s" % (n, "" if n == 1 else "s"))
        pick.tag_anim_editors()
        return {'FINISHED'}


class EMP_OT_path_update(bpy.types.Operator):
    """Recalculate all motion paths (for changes they can't see by themselves, like """ \
        """constraints or drivers) and the onion ghosts (after editing a mesh)"""
    bl_idname = "emp.path_update"
    bl_label = "Update Motion Paths"

    def execute(self, context):
        from . import onion
        onion.clear_all()
        state.sync_caches(context.scene)
        for c in state.caches.values():
            c.B = []
        state.bake_all(context.scene)
        return {'FINISHED'}


class EMP_OT_edit_tool(bpy.types.Operator):
    """Turn the Edit Motion Path tool on or off (click keys to see their handles, drag them, """ \
        """G / R / S...)"""
    bl_idname = "emp.edit_tool"
    bl_label = "Edit Paths"

    _previous = {}      # mode -> tool that was active before the path tool

    def execute(self, context):
        tool = "emp.edit_path_pose" if context.mode == 'POSE' else "emp.edit_path_object"
        if context.mode not in {'POSE', 'OBJECT'}:
            self.report({'WARNING'}, "Switch to Pose or Object Mode")
            return {'CANCELLED'}
        area = next((a for a in context.screen.areas if a.type == 'VIEW_3D'), None)
        if context.area is not None and context.area.type == 'VIEW_3D':
            area = context.area
        if area is None:
            return {'CANCELLED'}
        cur = context.workspace.tools.from_space_view3d_mode(context.mode, create=False)
        cur = cur.idname if cur is not None else ""
        if cur == tool:
            # pressed again: back to the tool used before (like toggling a mode)
            new = EMP_OT_edit_tool._previous.get(context.mode) or "builtin.select_box"
        else:
            EMP_OT_edit_tool._previous[context.mode] = cur
            new = tool
        with context.temp_override(area=area, region=next(r for r in area.regions if r.type == 'WINDOW')):
            bpy.ops.wm.tool_set_by_id(name=new)
        return {'FINISHED'}


# ----------------------------------------------------------------------------
# Key operations
# ----------------------------------------------------------------------------

def _sel_keys(cache):
    return [k for k in cache.keys if k.sel]


def picked_channel(context):
    """Scale mode: the channel picked on the scale gizmo (0 / 1 / 2), else None."""
    if state.edit_mode(context.scene) != core.SCALE:
        return None
    return {'X': 0, 'Y': 1, 'Z': 2}.get(state.settings(context.scene).scale_channel)


class EMP_OT_handle_type(bpy.types.Operator):
    """Set the handle type of the selected path keys (V). In Scale mode: on the channel picked """ \
        """on the scale gizmo"""
    bl_idname = "emp.handle_type"
    bl_label = "Set Path Handle Type"
    bl_options = {'REGISTER', 'UNDO'}

    type: EnumProperty(name="Type", items=[
        ('AUTO_CLAMPED', "Auto Clamped", "Smooth, without overshoot", 'HANDLE_AUTOCLAMPED', 0),
        ('AUTO', "Automatic", "Smooth", 'HANDLE_AUTO', 1),
        ('ALIGNED', "Aligned", "Handles stay in line (smooth motion)", 'HANDLE_ALIGNED', 2),
        ('FREE', "Free (Break)", "Handles move independently (sharp corner possible)", 'HANDLE_FREE', 3),
        ('VECTOR', "Vector", "Handles point to the neighbors (sharp corner)", 'HANDLE_VECTOR', 4),
    ])

    def execute(self, context):
        touched = set()
        only = picked_channel(context)
        if only is not None:
            # Scale mode, a channel picked on the gizmo: its keys only (like a curve in the
            # Graph Editor)
            for cache in state.editable_caches(context.scene):
                fc = cache.target().channels(cache.ptype).fcurves[only]
                for k in _sel_keys(cache):
                    if fc is not None and only < len(k.idx) and k.idx[only] >= 0:
                        kp = fc.keyframe_points[k.idx[only]]
                        kp.handle_left_type = kp.handle_right_type = self.type
                        touched.add(fc)
                cache.dirty = True
            if not touched:
                return {'CANCELLED'}
            core.update_fcurves(touched)
            pick.tag_anim_editors()
            return {'FINISHED'}
        for cache in state.editable_caches(context.scene):
            tg = cache.target()
            ch = tg.channels(cache.ptype)
            for k in cache.keys:
                left = self.type if (k.sel or k.sel_l) else None
                right = self.type if (k.sel or k.sel_r) else None
                if left or right:
                    core.set_handle_types(ch, k, left, right, touched)
            cache.dirty = True
        if not touched:
            return {'CANCELLED'}
        core.update_fcurves(touched)
        pick.tag_anim_editors()
        return {'FINISHED'}


class EMP_OT_interpolation(bpy.types.Operator):
    """Set the interpolation of the selected path keys: the segment after each key (T). In """ \
        """Scale mode: on the channel picked on the scale gizmo"""
    bl_idname = "emp.interpolation"
    bl_label = "Set Path Interpolation"
    bl_options = {'REGISTER', 'UNDO'}

    type: EnumProperty(name="Interpolation", items=[
        ('BEZIER', "Bezier", "Smooth curves", 'IPO_BEZIER', 0),
        ('LINEAR', "Linear", "Straight lines between keys", 'IPO_LINEAR', 1),
        ('CONSTANT', "Constant", "Stepped (blocking)", 'IPO_CONSTANT', 2),
    ])

    def execute(self, context):
        touched = set()
        only = picked_channel(context)     # Scale mode: the channel picked on the gizmo
        for cache in state.editable_caches(context.scene):
            tg = cache.target()
            ch = tg.channels(cache.ptype)
            for k in _sel_keys(cache):
                for c, fc in enumerate(ch.fcurves):
                    if fc is not None and k.idx[c] >= 0 and (only is None or c == only):
                        fc.keyframe_points[k.idx[c]].interpolation = self.type
                        touched.add(fc)
            cache.dirty = True
        if not touched:
            return {'CANCELLED'}
        core.update_fcurves(touched)
        pick.tag_anim_editors()
        return {'FINISHED'}


# ----------------------------------------------------------------------------
# Smooth / Exaggerate
# ----------------------------------------------------------------------------

class _Job:
    """Keys of one path converted to vectors the algorithms work on.

    Location paths work on world positions, rotation paths on Euler values or on
    the quaternion log map relative to the path's first key.
    """

    def __init__(self, cache, tg):
        self.cache = cache
        self.keys = list(cache.keys)
        self.mask = [k.sel for k in self.keys]
        if not any(self.mask):
            self.mask = [not k.hidden for k in self.keys]
        self.times = [k.t for k in self.keys]
        # rotation and scale are smoothed in channel space, location / value in world space
        self.rot = cache.ptype in (core.ROTATION, core.SCALE)
        self.quat = cache.ptype == core.ROTATION and tg.rot.count == 4
        if self.quat and self.keys:
            self.q_ref = core.vals_to_quat(self.keys[0].vals, tg.rot_mode)
        self.vecs = [self.to_vec(tg, k.vals, k.co) for k in self.keys]

    def to_vec(self, tg, vals, world=None):
        if not self.rot:
            return world.copy() if world is not None else self.cache.point(tg, 0.0, vals)
        if not self.quat:
            return Vector(vals[:3])
        q = core.vals_to_quat(vals, tg.rot_mode)
        if q.dot(self.q_ref) < 0.0:
            q = -q
        return (self.q_ref.inverted() @ q).to_exponential_map()

    def to_vals(self, tg, k, vec, ref_vals):
        if not self.rot:
            return self.cache.solve(tg, k.t, vec, ref_vals)
        if not self.quat:
            vals = [vec[0], vec[1], vec[2]]
            locks = tg.locks(self.cache.ptype)
            return [ref_vals[i] if locks[i] else vals[i] for i in range(3)]
        q = self.q_ref @ Quaternion(vec)
        return self.cache.rot_vals(tg, q, ref_vals)


def _lin(vecs, times, i, a, b):
    t0, t1 = times[a], times[b]
    f = 0.5 if abs(t1 - t0) < 1e-9 else (times[i] - t0) / (t1 - t0)
    return vecs[a].lerp(vecs[b], f)


def smooth_vectors(vecs, times, mask, amount):
    n = len(vecs)
    if n < 3:
        return list(vecs)

    def step(x, lam=0.5):
        y = list(x)
        for i in range(1, n - 1):
            if mask[i]:
                y[i] = x[i] + (_lin(x, times, i, i - 1, i + 1) - x[i]) * lam
        return y

    if amount < 0.0:
        out = list(vecs)
        for i in range(1, n - 1):
            if mask[i]:
                out[i] = vecs[i] + (_lin(vecs, times, i, i - 1, i + 1) - vecs[i]) * amount
        return out
    amount = min(amount, 50.0)
    whole = int(math.floor(amount))
    frac = amount - whole
    x = list(vecs)
    for _ in range(whole):
        x = step(x)
    if frac > 1e-6:
        y = step(x)
        x = [a + (b - a) * frac for a, b in zip(x, y)]
    return x


def _lagrange(ss, xs, t):
    out = xs[0] * 0.0
    for j, (sj, xj) in enumerate(zip(ss, xs)):
        w = 1.0
        for m, sm in enumerate(ss):
            if m != j:
                w *= (t - sm) / (sj - sm)
        out = out + xj * w
    return out


def _shape_prediction(x, i):
    """Where key i would be on a smooth curve through its neighbors (2 on each side when
    available), using chord length along the keys as the curve parameter."""
    n = len(x)
    left = [j for j in (i - 2, i - 1) if j >= 0]
    right = [j for j in (i + 1, i + 2) if j < n]
    if not left or not right:
        return None
    s = {}
    acc = 0.0
    lo, hi = left[0], right[-1]
    for j in range(lo, hi + 1):
        if j > lo:
            acc += max((x[j] - x[j - 1]).length, 1e-9)
        s[j] = acc
    idx = left + right
    ss = [s[j] for j in idx]
    close = any(abs(ss[a] - ss[b]) < 1e-9 for a in range(len(ss)) for b in range(a + 1, len(ss)))
    if close or len(idx) == 2:
        a, b = i - 1, i + 1
        f = 0.5 if s[b] - s[a] < 1e-9 else (s[i] - s[a]) / (s[b] - s[a])
        return x[a].lerp(x[b], f)
    return _lagrange(ss, [x[j] for j in idx], s[i])


def _shape_displacements(x, mask, keep_timing=True):
    d = [None] * len(x)
    for i in range(1, len(x) - 1):
        if not mask[i]:
            continue
        p = _shape_prediction(x, i)
        if p is None:
            continue
        v = p - x[i]
        if keep_timing:
            # only move across the path, never along it: spacing / timing stay the same
            tng = x[i + 1] - x[i - 1]
            if tng.length > 1e-12:
                tng = tng.normalized()
                v = v - tng * v.dot(tng)
        d[i] = v
    return d


def shape_smooth_vectors(vecs, mask, amount, keep_timing=True):
    """Smooth the *shape*: bumps and corners go away, arcs keep their size (a cubic
    prediction from the neighbors is exact on smooth curves, so they barely move)."""
    n = len(vecs)
    if n < 3:
        return list(vecs)

    def step(x, lam=0.5):
        d = _shape_displacements(x, mask, keep_timing)
        return [xi + di * lam if di is not None else xi for xi, di in zip(x, d)]

    if amount < 0.0:
        d = _shape_displacements(vecs, mask, keep_timing)
        return [xi + di * amount if di is not None else xi for xi, di in zip(vecs, d)]
    amount = min(amount, 50.0)
    whole = int(math.floor(amount))
    frac = amount - whole
    x = list(vecs)
    for _ in range(whole * 2):
        x = step(x)
    if frac > 1e-6:
        y = step(step(x))
        x = [a + (b - a) * frac for a, b in zip(x, y)]
    return x


def exaggerate_vectors(vecs, times, mask, factor, pivot='AUTO', extra=None):
    """Scale the deviation of selected runs from a baseline by (1 + factor).

    `extra` is an optional list of (index, time, vector) samples (handles) that
    get the same mapping as the run their index belongs to.  Returns the new
    vectors and the mapped extras.
    """
    n = len(vecs)
    out = list(vecs)
    ex_out = {}
    i = 0
    runs = []
    while i < n:
        if mask[i]:
            j = i
            while j + 1 < n and mask[j + 1]:
                j += 1
            runs.append((i, j))
            i = j + 1
        else:
            i += 1
    s = 1.0 + factor
    for i0, i1 in runs:
        a = i0 - 1 if i0 > 0 else None
        b = i1 + 1 if i1 < n - 1 else None
        mode = pivot
        if mode == 'AUTO':
            mode = 'LINE' if (a is not None or b is not None) else 'CENTER'
        if mode == 'LINE':
            a = i0 if a is None else a
            b = i1 if b is None else b
            if a == b:
                mode = 'CENTER'
        if mode == 'CENTER':
            c = Vector((0.0, 0.0, 0.0))
            for k in range(i0, i1 + 1):
                c += vecs[k]
            c /= (i1 - i0 + 1)

            def base(t, c=c):
                return c
        else:
            ta, tb, va, vb = times[a], times[b], vecs[a], vecs[b]

            def base(t, ta=ta, tb=tb, va=va, vb=vb):
                f = 0.5 if abs(tb - ta) < 1e-9 else (t - ta) / (tb - ta)
                return va.lerp(vb, f)
        for k in range(i0, i1 + 1):
            bk = base(times[k])
            out[k] = bk + (vecs[k] - bk) * s
        if extra:
            for idx, t, v in extra:
                if i0 <= idx <= i1:
                    bt = base(t)
                    ex_out[(idx, t)] = bt + (v - bt) * s
    return out, ex_out


class _AdjustOp:
    """Shared modal "drag left/right to set the amount" behaviour."""
    bl_options = {'REGISTER', 'UNDO', 'BLOCKING'}
    label = ""
    pixels_per_unit = 250.0
    vertical = False        # drag up / down instead of left / right

    @classmethod
    def poll(cls, context):
        return context.mode in {'POSE', 'OBJECT'}

    def _jobs(self, context, snapshots=None, ensure=True):
        jobs = []
        for cache in state.editable_caches(context.scene):
            tg = cache.target()
            if tg is None or len(cache.keys) < 2:
                continue
            if snapshots is not None:
                snapshots.append(core.FCurveSnapshot(tg, tg.channels(cache.ptype)))
            frames = [k.t for k in cache.keys if k.sel] or [k.t for k in cache.keys]
            if ensure and core.ensure_keys(tg, cache.ptype, frames, None, state.settings(context.scene).pin_partial):
                cache.refresh(force=True)
                tg = cache.target()
            jobs.append(_Job(cache, tg))
        return jobs

    def execute(self, context):
        jobs = self._jobs(context)
        if not jobs:
            self.report({'WARNING'}, "No editable path: select a bone/object that has a path")
            return {'CANCELLED'}
        self._apply(context, jobs)
        if getattr(self, "result_text", None):
            self.report({'INFO'}, self.result_text)
        return {'FINISHED'}

    def invoke(self, context, event):
        self.snapshots = []
        self.jobs = self._jobs(context, self.snapshots, ensure=False)
        self.ensured = False
        if not self.jobs:
            self.report({'WARNING'}, "No editable path: select a bone/object that has a path")
            return {'CANCELLED'}
        m = event.mouse_y if self.vertical else event.mouse_x
        self.x0 = m
        self.x_virtual = float(m)
        self.x_last = m
        self.numinput = NumInput(1)
        self.factor = 0.0
        self.area = context.area
        for job in self.jobs:
            state.ghosts[job.cache.key] = list(job.cache.points)
        state.modal_running += 1
        state.hud_area = self._view_area(context)
        context.window_manager.modal_handler_add(self)
        self._update(context, event)
        return {'RUNNING_MODAL'}

    def _label(self):
        return self.label

    def _modal_key(self, context, event):
        """Extra keys while dragging; return True if handled."""
        return False

    def _hud_pos(self, context, event):
        """Mouse position in the 3D view's main region (the operator may run from the sidebar)."""
        area = context.area
        if area is None or area.type != 'VIEW_3D':
            return (60, 60)
        wr = next((r for r in area.regions if r.type == 'WINDOW'), None)
        if wr is None:
            return (60, 60)
        x = min(max(event.mouse_x - wr.x, 10), wr.width - 220)
        y = min(max(event.mouse_y - wr.y, 10), wr.height - 40)
        return (x, y)

    def _view_area(self, context):
        if context.area is not None and context.area.type == 'VIEW_3D':
            return context.area.as_pointer()
        for a in context.screen.areas:
            if a.type == 'VIEW_3D':
                return a.as_pointer()
        return 0

    def _set_amount(self, v):
        """What the mouse sets (the factor; a tool may switch it to another setting)."""
        self.factor = v

    def _amount(self):
        return self.factor

    def _update(self, context, event):
        if self.numinput.active:
            v = self.numinput.value(0)
            self._set_amount(v if v is not None else 0.0)
        else:
            f = (self.x_virtual - self.x0) / (self.pixels_per_unit * compat.ui_scale())
            if event.ctrl:
                inc = 0.01 if event.shift else 0.1
                f = round(f / inc) * inc
            self._set_amount(f)
        if abs(self.factor) > 1e-9 and not self.ensured:
            # first real change: only now add missing channel keys (an untouched curve
            # must not change just because the tool was started)
            self.ensured = True
            for c in [j.cache for j in self.jobs]:
                c.refresh(force=True)
            self.jobs = self._jobs(context, None, ensure=True)
        if self.ensured:
            self._apply(context, self.jobs)
        txt = "%s: %s" % (self._label(), self.numinput.text(0, self._amount(), "%.3f"))
        state.hud = (txt, self._hud_pos(context, event))
        if self.area is not None:
            self.area.header_text_set(txt + "   (drag %s, Ctrl: snap, Shift: precise, type a value, "
                                            "LMB/Enter: confirm, RMB/Esc: cancel)" % (
                                                "up/down" if self.vertical else "left/right"))
        state.tag_redraw()

    def _finish(self, context):
        state.modal_running = max(0, state.modal_running - 1)
        state.ghosts.clear()
        state.hud = None
        if self.area is not None:
            self.area.header_text_set(None)
        state.tag_redraw()

    def modal(self, context, event):
        t, v = event.type, event.value
        if t in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            m = event.mouse_y if self.vertical else event.mouse_x
            d = m - self.x_last
            self.x_last = m
            self.x_virtual += d * (0.1 if event.shift else 1.0)
            self._update(context, event)
        elif t in {'LEFT_CTRL', 'RIGHT_CTRL', 'LEFT_SHIFT', 'RIGHT_SHIFT'}:
            self._update(context, event)
        elif (t == 'LEFTMOUSE' and v == 'PRESS') or (t in {'RET', 'NUMPAD_ENTER', 'SPACE'} and v == 'PRESS'):
            self._finish(context)
            if getattr(self, "result_text", None):
                self.report({'INFO'}, self.result_text)
            return {'FINISHED'}
        elif t in {'RIGHTMOUSE', 'ESC'} and v == 'PRESS':
            for s in reversed(self.snapshots):
                s.restore()
            for c in state.caches.values():
                c.dirty = True
            self._finish(context)
            return {'CANCELLED'}
        elif v == 'PRESS' and self._modal_key(context, event):
            self._update(context, event)
        elif self.numinput.handle(event):
            self._update(context, event)
        return {'RUNNING_MODAL'}

    def _apply(self, context, jobs):
        touched = set()
        core.cycle_skip = {round(k.t, 3) for job in jobs for k, m in zip(job.keys, job.mask) if m}
        for job in jobs:
            tg = job.cache.target()
            if tg is None:
                continue
            self._apply_job(job, tg, touched)
            job.cache.dirty = True
        core.update_fcurves(touched)
        pick.tag_anim_editors()


class EMP_OT_smooth(_AdjustOp, bpy.types.Operator):
    """Smooth the selected path keys (all keys if none selected). Drag right to smooth more, """ \
        """left (negative) to sharpen"""
    bl_idname = "emp.smooth"
    bl_label = "Smooth Path"
    bl_options = {'REGISTER', 'UNDO', 'BLOCKING'}
    label = "Smooth"

    factor: FloatProperty(name="Factor", default=0.0, soft_min=-1.0, soft_max=4.0,
                          description="Positive values smooth (higher = stronger), negative values "
                                      "sharpen (anti-smooth)")
    method: EnumProperty(name="Method", items=[
        ('SHAPE', "Shape", "Smooth the form of the path: removes bumps and corners, keeps arcs "
                           "and timing (keys only move across the path)"),
        ('TIME', "Time", "Pull keys toward the in-between of their neighbors in time (also "
                         "evens out the spacing, but flattens arcs)"),
    ], default='SHAPE')
    fix_corners: BoolProperty(name="Smooth Handles", default=True,
                              description="Shape: also turn the handles of the smoothed keys along the "
                                          "path (from the key before to the key after), like the Smooth "
                                          "brush; auto handles do that by themselves")

    parents: BoolProperty(name="Smooth Parents", default=False,
                          description="Also smooth what the path shows but doesn't edit, over the same frames: "
                                      "the bone's other channels and the bones / objects it hangs from (mocap "
                                      "trembles on every bone). Their keys stay where they are in time")
    chain: IntProperty(name="Chain Length", default=0, min=0, soft_max=10,
                       description="Smooth Parents: how many bones / objects above it (0: all, like an IK "
                                   "constraint's Chain Length)")
    parent_frames: FloatProperty(name="Parents Smooth", default=2.0, min=0.1, soft_max=10.0,
                                 description="Smooth Parents: over about how many frames (the Factor, up to 1, "
                                             "says how much)")

    def _extra(self, job, tg):
        """The curves Smooth Parents smooths for this path, with their keys as they were."""
        from . import smoothing
        got = getattr(self, "extra_by", None)
        if got is None:
            got = self.extra_by = {}
        if job.cache.key not in got:
            got[job.cache.key] = [(key, fc, smoothing.key_values(fc))
                                  for key, fc in smoothing.chain_curves(tg, job.cache.ptype, True, self.chain)]
        return got[job.cache.key]

    def _label(self):
        return "Smooth (%s, T: %s)" % ("Shape" if self.method == 'SHAPE' else "Time",
                                       "Time" if self.method == 'SHAPE' else "Shape")

    def _modal_key(self, context, event):
        if event.type == 'T':
            self.method = 'TIME' if self.method == 'SHAPE' else 'SHAPE'
            return True
        return False

    def invoke(self, context, event):
        if state.edit_mode(context.scene) == core.SCALE:
            # Alt+S on scale paths: longer / shorter handles (whiskers) of the picked channel
            bpy.ops.emp.scale_handles('INVOKE_DEFAULT')
            return {'CANCELLED'}
        self.extra_by = None
        r = _AdjustOp.invoke(self, context, event)
        if self.parents and 'RUNNING_MODAL' in r:
            from . import smoothing
            for job in self.jobs:
                tg = job.cache.target()
                if tg is not None:
                    # (Esc / right click puts them back too)
                    self.snapshots += smoothing.snapshots(self._extra(job, tg))
        return r

    def _apply_job(self, job, tg, touched):
        cache = job.cache
        ch = tg.channels(cache.ptype)
        if self.parents:
            from . import smoothing
            ts = [t for t, m in zip(job.times, job.mask) if m]
            if ts:
                touched.update(smoothing.smooth_range(self._extra(job, tg), min(ts), max(ts), self.parent_frames,
                                                      min(max(self.factor, 0.0), 1.0)))
        if self.method == 'TIME':
            new = smooth_vectors(job.vecs, job.times, job.mask, self.factor)
        else:
            new = shape_smooth_vectors(job.vecs, job.mask, self.factor)
        n = len(job.keys)
        for i, (k, m, v) in enumerate(zip(job.keys, job.mask, new)):
            if not m:
                continue
            vals = job.to_vals(tg, k, v, k.vals)
            core.write_key(ch, k, vals, True, touched)
            if self.method != 'SHAPE' or not self.fix_corners or self.factor <= 0.0 or not (0 < i < n - 1):
                continue
            if k.type_l not in {'FREE', 'VECTOR', 'ALIGNED'} and k.type_r not in {'FREE', 'VECTOR', 'ALIGNED'}:
                continue
            if k.hl is None or k.hr is None:
                continue
            # turn the handles along the path (from the key before to the key after), keeping
            # their lengths
            vl, vr = k.hl - k.co, k.hr - k.co
            tng = job.keys[i + 1].co - job.keys[i - 1].co
            if tng.length < 1e-12:
                tng = vr - vl
            if tng.length < 1e-12:
                continue
            tng = tng.normalized()
            f = min(1.0, self.factor)
            co = cache.point(tg, k.t, vals)
            for side, vec in (('L', vl), ('R', vr)):
                aligned = tng * (vec.length if side == 'R' else -vec.length)
                target = vec.lerp(aligned, f)
                dt = k.dt_r if side == 'R' else k.dt_l
                if dt < core.EPS:
                    continue
                slopes = cache.slopes_from_point(tg, k, side, co + target, vals, dt)
                core.write_handle(ch, k, side, slopes, dt, vals, 'ALIGNED' if f >= 1.0 else 'FREE', touched)


class EMP_OT_exaggerate(_AdjustOp, bpy.types.Operator):
    """Exaggerate the shape of the selected part of the path (all keys if none selected). """ \
        """Negative values tone it down"""
    bl_idname = "emp.exaggerate"
    bl_label = "Exaggerate Path"
    bl_options = {'REGISTER', 'UNDO', 'BLOCKING'}
    label = "Exaggerate"

    factor: FloatProperty(name="Factor", default=0.0, soft_min=-1.0, soft_max=2.0, subtype='FACTOR',
                          description="0.5 = 50% bigger deviation from the baseline, -1 = flatten")
    pivot: EnumProperty(name="Relative To", items=[
        ('AUTO', "Auto", "Line between the unselected neighbor keys, or the center when "
                         "everything is selected"),
        ('LINE', "Neighbors", "Straight line between the keys around the selection"),
        ('CENTER', "Center", "Average position of the selected keys (makes the whole motion "
                             "bigger/smaller)"),
    ], default='AUTO')

    def _apply_job(self, job, tg, touched):
        cache = job.cache
        ch = tg.channels(cache.ptype)
        extra = []
        for i, k in enumerate(job.keys):
            if not job.mask[i]:
                continue
            for side, h in (('L', k.hl), ('R', k.hr)):
                dt = k.dt_r if side == 'R' else k.dt_l
                if dt < core.EPS:
                    continue
                th = k.t + (dt if side == 'R' else -dt)
                hv = cache.handle_vals(k, side)
                vec = h.copy() if (not job.rot and h is not None) else job.to_vec(tg, hv)
                extra.append((i, th, vec))
        new, ex = exaggerate_vectors(job.vecs, job.times, job.mask, self.factor, self.pivot, extra)
        for i, k in enumerate(job.keys):
            if not job.mask[i]:
                continue
            vals = job.to_vals(tg, k, new[i], k.vals)
            core.write_key(ch, k, vals, True, touched)
            for side in ('L', 'R'):
                dt = k.dt_r if side == 'R' else k.dt_l
                if dt < core.EPS:
                    continue
                th = k.t + (dt if side == 'R' else -dt)
                hvec = ex.get((i, th))
                if hvec is None:
                    continue
                sign = 1.0 if side == 'R' else -1.0
                if job.rot:
                    hv = job.to_vals(tg, k, hvec, cache.handle_vals(k, side))
                    slopes = [(h - v) * sign / dt for h, v in zip(hv, vals)]
                else:
                    slopes = cache.slopes_from_point(tg, k, side, hvec, vals, dt)
                core.write_handle(ch, k, side, slopes, dt, vals, None, touched)


class EMP_OT_fix_loop(bpy.types.Operator):
    """Close a broken loop: select the first or last key of the path (the one that is """ \
        """right) and the key at the other end of the loop is matched to it: same pose and """ \
        """same handles, so there is no jump or hitch at the seam"""
    bl_idname = "emp.fix_loop"
    bl_label = "Fix Loop"
    bl_options = {'REGISTER', 'UNDO'}

    whole_pose: BoolProperty(
        name="Whole Pose", default=True,
        description="Fix every channel of the bone/object (location, rotation, scale, custom "
                    "properties), not just the ones of the current path type")

    @staticmethod
    def _source_key(cache):
        if not cache.keys:
            return None
        first, last = cache.keys[0].t, cache.keys[-1].t
        for k in cache.keys:
            if k.sel and (abs(k.t - first) < 1e-3 or abs(k.t - last) < 1e-3):
                return k
        return None

    def invoke(self, context, event):
        caches = state.editable_caches(context.scene)
        if not any(self._source_key(c) for c in caches):
            return {'PASS_THROUGH'}
        return self.execute(context)

    def execute(self, context):
        fixed = 0
        for cache in state.editable_caches(context.scene):
            src = self._source_key(cache)
            if src is None:
                continue
            tg = cache.target()
            use_first = abs(src.t - cache.keys[0].t) < 1e-3
            if self.whole_pose and tg.coll is not None:
                fcs = [fc for fc in tg.coll if fc.data_path.startswith(tg.prefix)] if tg.prefix \
                        else list(tg.coll)
            else:
                fcs = [fc for fc in tg.channels(cache.ptype).fcurves if fc is not None]
            touched = set()
            for fc in fcs:
                kps = fc.keyframe_points
                n = len(kps)
                if n < 2:
                    continue
                a, b = (0, n - 1) if use_first else (n - 1, 0)
                s, t = kps[a], kps[b]
                sx, sy = s.co
                tx, ty = t.co
                offset = any(m.type == 'CYCLES' and not m.mute and
                             'OFFSET' in (m.mode_after + m.mode_before) for m in fc.modifiers)
                # with "Repeat with Offset" the values differ on purpose (e.g. walking forward):
                # only the handles (speed through the seam) are matched
                y = ty if offset else sy
                shl, shr = tuple(s.handle_left), tuple(s.handle_right)
                t.handle_left_type = s.handle_left_type
                t.handle_right_type = s.handle_right_type
                t.interpolation = s.interpolation
                t.co = (tx, y)
                t.handle_left = (tx - (sx - shl[0]), y - (sy - shl[1]))
                t.handle_right = (tx + (shr[0] - sx), y + (shr[1] - sy))
                touched.add(fc)
            core.update_fcurves(touched)
            cache.dirty = True
            fixed += 1
        if not fixed:
            self.report({'WARNING'}, "Select the first or last key of a path: the one the loop "
                                     "should match")
            return {'CANCELLED'}
        pick.tag_anim_editors()
        self.report({'INFO'}, "Loop fixed on %d path%s" % (fixed, "" if fixed == 1 else "s"))
        return {'FINISHED'}


class EMP_OT_cleanup(bpy.types.Operator):
    """Reduce the keys of the path while keeping its motion: where the bone is on every frame """ \
        """(selected range, or the whole path). Drag left / right to set how close it stays and """ \
        """watch the result. Clean: remove existing keys. Adaptive: rebuild with as few keys as """ \
        """possible, placed where the motion needs them"""
    bl_idname = "emp.cleanup"
    bl_label = "Clean Up Path"
    bl_options = {'REGISTER', 'UNDO'}

    method: EnumProperty(name="Method", items=[
        ('CLEAN', "Clean", "Remove keys that are not needed and refit the handles of the rest"),
        ('ADAPTIVE', "Adaptive", "Rebuild the keys from scratch: as few as possible, on the frames "
                                 "where the shape needs them"),
    ], default='CLEAN')
    tolerance: FloatProperty(
        name="Tolerance", subtype='PERCENTAGE', default=1.0, min=0.01, soft_max=10.0, precision=2,
        description="How far the cleaned path may move away from the original, in percent of "
                    "the path size (lower keeps more keys)")
    keep_timing: BoolProperty(
        name="Keep Timing", default=True,
        description="Where the bone is on every frame stays (within the tolerance). Off: only the shape "
                    "of the path counts - keys go as long as the path keeps its shape, and the timing "
                    "along it may change (fewer keys, a cleaner path)")
    keep_holds: BoolProperty(
        name="Keep Holds", default=True,
        description="The first and last key of a hold (the motion standing still) stay as they are")
    keep_poses: BoolProperty(
        name="Keep Poses", default=True,
        description="Keys where the motion stops for a moment or turns back (contacts, extremes) "
                    "stay, like an animator would keep them")

    @classmethod
    def poll(cls, context):
        return context.mode in {'POSE', 'OBJECT'}

    # -- live preview: drag to set the tolerance -----------------------------------

    def invoke(self, context, event):
        caches = [c for c in state.editable_caches(context.scene) if len(c.keys) > 2]
        if not caches:
            return self.execute(context)
        self.snaps = []
        for c in caches:
            tg = c.target()
            self.snaps.append(core.FCurveSnapshot(tg, tg.channels(c.ptype)))
            state.ghosts[c.key] = list(c.points)
        self.x0 = event.mouse_x
        self.tol0 = self.tolerance
        self.area = context.area
        state.modal_running += 1
        context.window_manager.modal_handler_add(self)
        self._preview(context, event)
        return {'RUNNING_MODAL'}

    def _restore(self):
        for s in self.snaps:
            s.restore()
        for c in state.caches.values():
            c.dirty = True
            c.refresh(force=True)

    def _preview(self, context, event):
        f = 2.0 ** ((event.mouse_x - self.x0) / (120.0 * compat.ui_scale()))
        self.tolerance = min(20.0, max(0.01, self.tol0 * f))
        self._restore()
        self.execute(context)
        txt = "Clean Up (%s): tolerance %.2f%%   %s   drag left / right: closer / fewer keys   " \
              "T: keep timing %s   M: method   H: keep holds %s   P: keep poses %s   LMB / Enter: done   "               "Esc: cancel" % (
                  "Clean" if self.method == 'CLEAN' else "Adaptive", self.tolerance,
                  getattr(self, "result_text", ""), "ON" if self.keep_timing else "off (shape only)",
                  "ON" if self.keep_holds else "off", "ON" if self.keep_poses else "off")
        if self.area is not None:
            self.area.header_text_set(txt)
        state.hud = ("%.2f%%  %s" % (self.tolerance, getattr(self, "result_text", "")),
                     (event.mouse_region_x, event.mouse_region_y))
        state.tag_redraw()

    def modal(self, context, event):
        t, v = event.type, event.value
        if t in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            self._preview(context, event)
        elif v == 'PRESS' and t in {'M', 'H', 'P', 'T'}:
            if t == 'T':
                self.keep_timing = not self.keep_timing
            elif t == 'M':
                self.method = 'ADAPTIVE' if self.method == 'CLEAN' else 'CLEAN'
            elif t == 'H':
                self.keep_holds = not self.keep_holds
            else:
                self.keep_poses = not self.keep_poses
            self._preview(context, event)
        elif (t == 'LEFTMOUSE' and v == 'PRESS') or (t in {'RET', 'NUMPAD_ENTER', 'SPACE'} and v == 'PRESS'):
            self._end(context)
            if getattr(self, "result_text", None):
                self.report({'INFO'}, self.result_text)
            return {'FINISHED'}
        elif t in {'RIGHTMOUSE', 'ESC'} and v == 'PRESS':
            self._restore()
            self._end(context)
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}

    def _end(self, context):
        state.modal_running = max(0, state.modal_running - 1)
        state.ghosts.clear()
        state.hud = None
        if self.area is not None:
            self.area.header_text_set(None)
        pick.tag_anim_editors()
        state.tag_redraw()

    def execute(self, context):
        from . import fit
        before = after = 0
        worst = 0.0
        skipped = []
        for cache in state.editable_caches(context.scene):
            tg = cache.target()
            keys = cache.keys
            if tg is None or len(keys) < 2:
                continue
            sel = [k.t for k in keys if k.sel]
            r0, r1 = (min(sel), max(sel)) if len(sel) >= 2 else (keys[0].t, keys[-1].t)
            ch = tg.channels(cache.ptype)
            fcs = [fc for fc in ch.fcurves if fc is not None and len(fc.keyframe_points)]
            if not fcs:
                continue
            # every refitted channel has to have keys over the whole range
            for fc in fcs:
                r0 = max(r0, fc.keyframe_points[0].co[0])
                r1 = min(r1, fc.keyframe_points[-1].co[0])
            if r1 - r0 < 2.0:
                continue
            if any(kp.interpolation == 'CONSTANT' and r0 - 1e-3 <= kp.co[0] < r1 - 1e-3
                   for fc in fcs for kp in fc.keyframe_points):
                skipped.append(cache.label)
                continue
            fitter = fit.Fitter(cache, tg, float(r0), float(r1))
            tol = fitter.size * self.tolerance / 100.0
            inside = sorted({float(k.t) for k in keys if r0 - 1e-3 <= k.t <= r1 + 1e-3} | {float(r0), float(r1)})
            fixed = {t for t in inside if fitter.is_corner(t)} | {float(r0), float(r1)}
            if self.keep_holds:
                fixed |= fitter.hold_keys([t for t in inside])
            if self.keep_poses:
                fixed |= fitter.pose_keys([t for t in inside])
            if not self.keep_timing:
                b, a, w = _clean_shape(cache, tg, inside, fixed, self.tolerance)
                before += b
                after += a
                worst = max(worst, w)
                cache.dirty = True
                continue
            if self.method == 'CLEAN':
                knots, slopes = fitter.prune(inside, fixed, tol)
            else:
                knots, slopes = fitter.adaptive(fixed, tol)
            err = fitter.max_error(knots, slopes)[0]
            worst = max(worst, err / fitter.size * 100.0)
            fit.write(fitter, knots, slopes)
            cache.dirty = True
            before += len(inside)
            after += len(knots)
        if skipped:
            self.report({'WARNING'}, "Stepped (Constant) keys are left alone: " + ", ".join(skipped))
        if not before:
            if not skipped:
                self.report({'WARNING'}, "Nothing to clean: select a bone/object with a path "
                                         "(and 2+ keys for a range)")
            return {'CANCELLED'}
        pick.tag_anim_editors()
        self.result_text = "%d -> %d keys, max deviation %.2f%%" % (before, after, worst)
        if not getattr(self, "snaps", None):        # (the live preview shows it in the header)
            self.report({'INFO'}, "Clean Up: %d -> %d keys, max deviation %.2f%% of the path size" % (
                before, after, worst))
        return {'FINISHED'}


def _poly_dist(p, poly):
    best = 1e18
    for a, b in zip(poly, poly[1:]):
        ab = b - a
        L = ab.length_squared
        f = 0.0 if L < 1e-12 else max(0.0, min(1.0, (p - a).dot(ab) / L))
        best = min(best, (a + ab * f - p).length_squared)
    return best ** 0.5


def _clean_shape(cache, tg, inside, fixed, tolerance):
    """Clean Up without Keep Timing: dissolve the keys the shape of the path doesn't need (the
    least needed first), each only if the path stays within the tolerance of its old shape (both
    ways: no point of either path farther than that from the other). The timing may change.
    A key only changes the path between its two neighbors, so only that part is measured, right
    from the curves (no rebuilding of the whole path per try).
    Returns (keys before, keys after, worst deviation in percent of the path size)."""
    from . import keys as _keys
    r0, r1 = inside[0], inside[-1]
    old_t = [t for t in cache.times if r0 - 1e-6 <= t <= r1 + 1e-6]
    old_by = {t: p.copy() for t, p in zip(cache.times, cache.points)}
    pts = [old_by[t] for t in old_t]
    lo = [min(p[i] for p in pts) for i in range(3)]
    hi = [max(p[i] for p in pts) for i in range(3)]
    size = max(sum((h - l) ** 2 for h, l in zip(hi, lo)) ** 0.5, 1e-6)
    ch = tg.channels(cache.ptype)
    alive = sorted(inside)

    def dissolve(t):
        n = 0
        for fc in ch.fcurves:
            if fc is not None and len(fc.keyframe_points) > 2:
                n += _keys.dissolve_channel(fc, [t])
        if n:
            core.update_fcurves([fc for fc in ch.fcurves if fc is not None])
        return n

    def deviation(t):
        """How far the path between t's neighbors is from its old shape there (percent)."""
        i = alive.index(t)
        a, b = alive[max(i - 1, 0)], alive[min(i + 1, len(alive) - 1)]
        ts = [x for x in old_t if a - 1e-6 <= x <= b + 1e-6]
        if len(ts) < 2:
            return 0.0
        tg2 = cache.target()
        new = [cache.point(tg2, x) for x in ts]
        old = [old_by[x] for x in ts]
        d1 = max(_poly_dist(p, old) for p in new)
        d2 = max(_poly_dist(q, new) for q in old)
        return max(d1, d2) / size * 100.0

    cands = [t for t in inside if t not in fixed]
    need = []
    for t in cands:
        snap = core.FCurveSnapshot(tg, ch)
        if dissolve(t):
            need.append((deviation(t), t))
        snap.restore()
    removed, worst = 0, 0.0
    for d, t in sorted(need):
        if d > tolerance:
            break
        snap = core.FCurveSnapshot(tg, ch)
        dev = deviation(t) if dissolve(t) else None
        if dev is None:
            continue
        if dev > tolerance:
            snap.restore()
        else:
            alive.remove(t)
            removed += 1
            worst = max(worst, dev)
    cache.dirty = True
    cache.refresh(force=True)
    return len(inside), len(inside) - removed, worst


class _EaseTimingJob:
    """Ease that keeps the path: the segments next to the selected keys of one path, each with
    its original motion (a SpaceJob), retimed along the path."""

    def __init__(self, cache, tg, sel):
        from .ops_timing import SpaceJob
        self.cache = cache
        self.keys, self.mask = [], []       # (for _AdjustOp)
        self.sel = sel
        self.snapshot = core.FCurveSnapshot(tg, tg.channels(cache.ptype))
        ks = [k.t for k in cache.keys]
        segs = set()
        for t in sel:
            i = ks.index(t)
            if i > 0:
                segs.add((ks[i - 1], t))
            if i + 1 < len(ks):
                segs.add((t, ks[i + 1]))
        self.segs = [((a, b), SpaceJob(cache, tg, float(a), float(b))) for a, b in sorted(segs)]


def ease_target(job, tg, slow_start, slow_end, e):
    """Target motion of one segment: the same path, travelled with a slow start and / or end
    (e > 0: up to the bone coming to rest at the key) or the other way (e < 0: faster at the
    key, the frames spread out there and gather in between)."""
    cache = job.cache
    total = job.arc[-1]
    r0, r1 = job.r0, job.r1
    p = 2.0 + e
    k = 0.9         # e < 0: at full strength, 1.9x the even speed at the key, 0.1x away from it

    def prof(u):
        if e < 0.0:
            if slow_start and slow_end:
                return u + k * math.sin(2.0 * math.pi * u) / (2.0 * math.pi)
            if slow_end:
                return u - k * math.sin(math.pi * u) / math.pi
            return u + k * math.sin(math.pi * u) / math.pi
        if slow_start and slow_end:
            a, b = u ** p, (1.0 - u) ** p
            return a / (a + b) if a + b > 0.0 else u
        if slow_end:
            return 1.0 - (1.0 - u) ** p
        return u ** p
    w = min(1.0, abs(e))

    def fn(t):
        u = min(1.0, max(0.0, (t - r0) / (r1 - r0)))
        s0 = job._at(job.arc, t)
        s = s0 + (total * prof(u) - s0) * w
        tt = job._time_at_arc(s)
        return cache.solve(tg, t, job._at(job.pts, tt), job._at(job.vals, tt))
    return fn


class EMP_OT_ease(_AdjustOp, bpy.types.Operator):
    """Ease (slow in / slow out) at the selected keys: drag right for a stronger ease (the """ \
        """frame dots bunch up at the key), left for the opposite: faster through the key. Keep the """ \
        """Path (default): only the timing changes, the path keeps its shape; Handles: the key's """ \
        """handles change (T while dragging). I / O / B: ease in, out or both"""
    bl_idname = "emp.ease"
    bl_label = "Ease"
    bl_options = {'REGISTER', 'UNDO', 'BLOCKING'}
    label = "Ease"
    pixels_per_unit = 300.0

    factor: FloatProperty(name="Factor", default=0.0, soft_min=-1.0, soft_max=1.0, min=-1.0, max=1.0,
                          description="1: full ease (the bone comes to rest at the key), "
                                      "-1: the opposite, faster through the key (Keep the Path: the frames spread out at "
                                      "the key; Handles: longer handles, a wider path around it)")
    side: EnumProperty(name="Side", items=[
        ('BOTH', "In & Out", "Slow in and slow out"),
        ('IN', "In", "Slow in: arrive gently (the part before the key)"),
        ('OUT', "Out", "Slow out: leave gently (the part after the key)"),
    ], default='BOTH')
    method: EnumProperty(name="Method", items=[
        ('TIMING', "Keep the Path", "Only the timing changes: the frames bunch up at the key along "
                                    "the same path (keys are added where needed)"),
        ('HANDLES', "Handles", "The key's handles get flatter and longer in time (the path near "
                               "the key changes a little too)"),
    ], default='TIMING')
    tolerance: FloatProperty(name="Tolerance", subtype='PERCENTAGE', default=0.4, min=0.01, soft_max=5.0,
                             description="Keep the Path: allowed distance from the exact new motion "
                                         "(percent of the path size); lower adds more keys")

    def _label(self):
        return {"BOTH": "Ease In & Out", "IN": "Ease In", "OUT": "Ease Out"}[self.side] + \
            "  (I / O / B)   %s (T)" % ("keeping the path" if self.method == 'TIMING' else "handles")

    def _modal_key(self, context, event):
        if event.type == 'T':
            # switch the method: start again from the original curves
            for s in reversed(self.snapshots):
                s.restore()
            for c in state.caches.values():
                c.dirty = True
                c.refresh(force=True)
            self.method = 'HANDLES' if self.method == 'TIMING' else 'TIMING'
            self.ensured = False
            self.jobs = self._jobs(context, None, ensure=False)
            return True
        m = {'I': 'IN', 'O': 'OUT', 'B': 'BOTH'}.get(event.type)
        if m is None:
            return False
        self.side = m
        return True

    def _jobs(self, context, snapshots=None, ensure=True):
        if self.method == 'HANDLES':
            return _AdjustOp._jobs(self, context, snapshots, ensure)
        jobs = []
        for cache in state.editable_caches(context.scene):
            tg = cache.target()
            if tg is None or len(cache.keys) < 2:
                continue
            sel = [k.t for k in cache.keys if k.sel] or [k.t for k in cache.keys if not k.hidden]
            ks = [k.t for k in cache.keys]
            frames = set(sel)
            for t in sel:
                i = ks.index(t)
                frames.update(ks[max(i - 1, 0):i + 2])
            if snapshots is not None:
                snapshots.append(core.FCurveSnapshot(tg, tg.channels(cache.ptype)))
            if ensure and core.ensure_keys(tg, cache.ptype, sorted(frames), None,
                                           state.settings(context.scene).pin_partial):
                cache.refresh(force=True)
                tg = cache.target()
            jobs.append(_EaseTimingJob(cache, tg, sel))
        return jobs

    def _apply_timing(self, job, tg, touched):
        from .ops_timing import refit
        job.snapshot.restore()
        cache = job.cache
        cache.refresh(force=True)
        tg = cache.target()
        e = self.factor
        sel = set(job.sel)
        for (a, b), sj in job.segs:
            if sj.arc[-1] < 1e-9:
                continue        # a hold: nothing to ease
            slow_start = a in sel and self.side in {'BOTH', 'OUT'}
            slow_end = b in sel and self.side in {'BOTH', 'IN'}
            if not (slow_start or slow_end) or abs(e) < 1e-9:
                continue
            touched.update(refit(sj, tg, ease_target(sj, tg, slow_start, slow_end, e), {round(t, 3) for t in sel},
                                 self.tolerance))
            tg = cache.target()

    def _originals(self, job, tg):
        """Handle data of the selected keys before easing (captured once per job)."""
        if getattr(job, "ease_orig", None) is not None:
            return job.ease_orig
        ch = tg.channels(job.cache.ptype)
        orig = []
        for k, m in zip(job.keys, job.mask):
            if not m:
                continue
            for c, fc in enumerate(ch.fcurves):
                i = k.idx[c] if c < len(k.idx) else -1
                if fc is None or i < 0:
                    continue
                kps = fc.keyframe_points
                kp = kps[i]
                prev = kps[i - 1] if i > 0 else None
                nxt = kps[i + 1] if i + 1 < len(kps) else None
                orig.append(dict(
                    path=fc.data_path, index=fc.array_index, i=i, x=kp.co[0], y=kp.co[1],
                    hl=tuple(kp.handle_left), hr=tuple(kp.handle_right),
                    tl=kp.handle_left_type, tr=kp.handle_right_type,
                    prev=(prev.co[0], prev.co[1], prev.handle_right[0] - prev.co[0]) if prev else None,
                    next=(nxt.co[0], nxt.co[1], nxt.co[0] - nxt.handle_left[0]) if nxt else None))
        job.ease_orig = orig
        return orig

    @staticmethod
    def _eased(e, x, y, h, nb, side):
        """New handle (x, y) for one side. nb = neighbor (x, y, its facing handle length)."""
        sign = 1.0 if side == 'R' else -1.0
        dt0 = (h[0] - x) * sign
        if nb is None or dt0 <= 1e-6:
            return h
        interval = abs(nb[0] - x)
        s0 = (h[1] - y) / (h[0] - x)
        cap = max(interval - max(nb[2], 0.0), interval / 3.0) * 0.98
        a = min(abs(e), 1.0)
        dt = dt0 + a * (max(cap, dt0) - dt0)
        # ease: flatter and longer (the bone comes to rest at the key); the other way: just
        # longer in the same direction, so the motion carries on through the key and the path
        # around it gets wider (a slope pulled toward the neighbor per channel turned the
        # handle sideways in 3D)
        s = s0 * (1.0 - a) if e >= 0.0 else s0
        return (x + sign * dt, y + s * sign * dt)

    def _apply_job(self, job, tg, touched):
        if self.method == 'TIMING':
            self._apply_timing(job, tg, touched)
            return
        coll = tg.coll
        e = self.factor
        for o in self._originals(job, tg):
            fc = coll.find(o['path'], index=o['index'])
            if fc is None:
                continue
            kp = fc.keyframe_points[o['i']]
            do_l = self.side in {'BOTH', 'IN'}
            do_r = self.side in {'BOTH', 'OUT'}
            hl = self._eased(e, o['x'], o['y'], o['hl'], o['prev'], 'L') if do_l else o['hl']
            hr = self._eased(e, o['x'], o['y'], o['hr'], o['next'], 'R') if do_r else o['hr']
            collinear = o['tl'] in {'AUTO', 'AUTO_CLAMPED', 'ALIGNED'} and \
                o['tr'] in {'AUTO', 'AUTO_CLAMPED', 'ALIGNED'}
            if do_l and do_r:
                tl = tr = 'ALIGNED' if collinear else 'FREE'
            else:
                # one side only: break the handles, keep the other side as it was
                tl = 'FREE' if do_l else ('FREE' if o['tl'] == 'ALIGNED' else o['tl'])
                tr = 'FREE' if do_r else ('FREE' if o['tr'] == 'ALIGNED' else o['tr'])
            if abs(e) < 1e-9:
                tl, tr, hl, hr = o['tl'], o['tr'], o['hl'], o['hr']
            kp.handle_left_type = tl
            kp.handle_right_type = tr
            kp.handle_left = hl
            kp.handle_right = hr
            touched.add(fc)


class _ScaleHandleOp(_AdjustOp):
    """Shared by the scale handle tools (Scale mode): which handles they work on."""

    @classmethod
    def poll(cls, context):
        return context.mode in {'POSE', 'OBJECT'} and state.edit_mode(context.scene) == core.SCALE

    def _channels(self, context):
        ch = self.channel
        if ch == 'ACTIVE':
            ch = state.settings(context.scene).scale_channel
        return {'X': {0}, 'Y': {1}, 'Z': {2}}.get(ch, {0, 1, 2})

    def _modal_key(self, context, event):
        m = {'I': 'IN', 'O': 'OUT', 'B': 'BOTH'}.get(event.type)
        if m is None:
            return False
        self.side = m
        return True

    def _originals(self, job, tg):
        if getattr(job, "handle_orig", None) is not None:
            return job.handle_orig
        picked = {(round(t, 3), c, s) for ck, t, c, s in state.whisker_sel(bpy.context.scene)
                  if ck == job.cache.key}
        use = {c for _t, c, _s in picked} if picked else self._channels(bpy.context)
        ch = tg.channels(job.cache.ptype)
        orig = []
        for k, m in zip(job.keys, job.mask):
            if not m and not picked:
                continue
            for c, fc in enumerate(ch.fcurves):
                i = k.idx[c] if c < len(k.idx) else -1
                if fc is None or i < 0 or c not in use:
                    continue
                sides = {s for t, cc, s in picked if cc == c and abs(t - k.t) < 1e-3}
                if picked and not sides:
                    continue        # picked handles: only those
                kps = fc.keyframe_points
                kp = kps[i]
                orig.append(dict(
                    path=fc.data_path, index=fc.array_index, i=i, x=kp.co[0], y=kp.co[1],
                    hl=tuple(kp.handle_left), hr=tuple(kp.handle_right),
                    tl=kp.handle_left_type, tr=kp.handle_right_type,
                    gap_l=kp.co[0] - kps[i - 1].co[0] if i > 0 else None,
                    gap_r=kps[i + 1].co[0] - kp.co[0] if i + 1 < len(kps) else None,
                    sides=sides or {'L', 'R'}))
        job.handle_orig = orig
        return orig


class EMP_OT_scale_handles(_ScaleHandleOp, bpy.types.Operator):
    """Make the picked scale handles (whiskers) longer or shorter; with none picked, the """ \
        """handles of the selected keys on the channel picked on the scale gizmo (all channels if """ \
        """none). Drag right for longer handles. I / O / B while dragging: the handle before the """ \
        """key, after it, or both"""
    bl_idname = "emp.scale_handles"
    bl_label = "Scale Handles"
    bl_options = {'REGISTER', 'UNDO', 'BLOCKING'}
    label = "Scale Handles"
    pixels_per_unit = 250.0

    factor: FloatProperty(name="Factor", default=0.0, soft_min=-1.0, soft_max=2.0, min=-1.0,
                          description="0.5: handles 50% longer, -1: no handles")
    side: EnumProperty(name="Side", items=[
        ('BOTH', "Both", "The handles on both sides of the key"),
        ('IN', "In", "The handle before the key"),
        ('OUT', "Out", "The handle after the key"),
    ], default='BOTH')
    channel: EnumProperty(name="Channel", items=[
        ('ACTIVE', "Picked", "The channel picked on the scale gizmo (all if none)"),
        ('X', "X", ""), ('Y', "Y", ""), ('Z', "Z", ""), ('ALL', "All", "")], default='ACTIVE')

    def _label(self):
        ch = self.channel
        if ch == 'ACTIVE':
            ch = state.settings(bpy.context.scene).scale_channel
        name = {'X': "X", 'Y': "Y", 'Z': "Z"}.get(ch, "all channels (click a gizmo handle to pick one)")
        return "Scale Handles %s, %s  (I / O / B)" % (
            name, {"BOTH": "both sides", "IN": "in", "OUT": "out"}[self.side])

    @staticmethod
    def _scaled(x, y, h, s, gap):
        dx, dy = h[0] - x, h[1] - y
        if gap is not None and abs(dx) * s > gap * 0.98 and abs(dx) > 1e-9:
            s = gap * 0.98 / abs(dx)   # the handle stays short of the next key
        return (x + dx * s, y + dy * s)

    def _apply_job(self, job, tg, touched):
        coll = tg.coll
        s = max(0.0, 1.0 + self.factor)
        for o in self._originals(job, tg):
            do_l = self.side in {'BOTH', 'IN'} and 'L' in o['sides']
            do_r = self.side in {'BOTH', 'OUT'} and 'R' in o['sides']
            fc = coll.find(o['path'], index=o['index'])
            if fc is None:
                continue
            kp = fc.keyframe_points[o['i']]
            if abs(self.factor) < 1e-9:
                tl, tr, hl, hr = o['tl'], o['tr'], o['hl'], o['hr']
            else:
                hl = self._scaled(o['x'], o['y'], o['hl'], s, o['gap_l']) if do_l else o['hl']
                hr = self._scaled(o['x'], o['y'], o['hr'], s, o['gap_r']) if do_r else o['hr']
                # auto handles would be set again by Blender: they keep their direction
                fixed = {'AUTO': 'ALIGNED', 'AUTO_CLAMPED': 'ALIGNED', 'VECTOR': 'FREE'}
                tl = fixed.get(o['tl'], o['tl'])
                tr = fixed.get(o['tr'], o['tr'])
            kp.handle_left_type = tl
            kp.handle_right_type = tr
            kp.handle_left = hl
            kp.handle_right = hr
            touched.add(fc)


class EMP_OT_scale_handle_values(_ScaleHandleOp, bpy.types.Operator):
    """Move the picked scale handles (whiskers) up or down: drag the mouse up / down. With both """ \
        """handles of a smooth key picked they tilt together (the key stays smooth). With none """ \
        """picked: the handles of the selected keys on the channel picked on the scale gizmo. """ \
        """I / O / B while dragging: the handle before the key, after it, or both"""
    bl_idname = "emp.scale_handle_values"
    bl_label = "Move Scale Handles"
    bl_options = {'REGISTER', 'UNDO', 'BLOCKING'}
    label = "Move Handles"
    pixels_per_unit = 200.0
    vertical = True

    factor: FloatProperty(name="Offset", default=0.0, soft_min=-2.0, soft_max=2.0,
                          description="How far the handle after the key goes up (scale value)")
    side: EnumProperty(name="Side", items=[
        ('BOTH', "Both", "The handles on both sides of the key"),
        ('IN', "In", "The handle before the key"),
        ('OUT', "Out", "The handle after the key"),
    ], default='BOTH')
    channel: EnumProperty(name="Channel", items=[
        ('ACTIVE', "Picked", "The channel picked on the scale gizmo (all if none)"),
        ('X', "X", ""), ('Y', "Y", ""), ('Z', "Z", ""), ('ALL', "All", "")], default='ACTIVE')

    def _label(self):
        ch = self.channel
        if ch == 'ACTIVE':
            ch = state.settings(bpy.context.scene).scale_channel
        name = {'X': "X", 'Y': "Y", 'Z': "Z"}.get(ch, "all channels")
        return "Move Handles %s, %s  (I / O / B)" % (
            name, {"BOTH": "both sides", "IN": "in", "OUT": "out"}[self.side])

    def _apply_job(self, job, tg, touched):
        coll = tg.coll
        dv = self.factor
        fixed = {'AUTO': 'ALIGNED', 'AUTO_CLAMPED': 'ALIGNED', 'VECTOR': 'FREE'}
        for o in self._originals(job, tg):
            do_l = self.side in {'BOTH', 'IN'} and 'L' in o['sides']
            do_r = self.side in {'BOTH', 'OUT'} and 'R' in o['sides']
            fc = coll.find(o['path'], index=o['index'])
            if fc is None:
                continue
            kp = fc.keyframe_points[o['i']]
            x, y = o['x'], o['y']
            hl, hr = o['hl'], o['hr']
            tl, tr = o['tl'], o['tr']
            if abs(dv) > 1e-9 and (do_l or do_r):
                tl, tr = fixed.get(tl, tl), fixed.get(tr, tr)
                smooth = tl == 'ALIGNED' and tr == 'ALIGNED'
                dl, dr = x - hl[0], hr[0] - x
                if do_l and do_r:
                    # both: they tilt around the key (up after it, down before it), so a smooth
                    # key stays smooth
                    hr = (hr[0], hr[1] + dv)
                    hl = (hl[0], hl[1] - dv * (dl / dr if dr > 1e-6 else 1.0))
                elif do_r:
                    hr = (hr[0], hr[1] + dv)
                    if smooth and dr > 1e-6:
                        # the other handle stays in line (keeping its length in time)
                        hl = (x - dl, y - (hr[1] - y) * dl / dr)
                else:
                    hl = (hl[0], hl[1] + dv)
                    if smooth and dl > 1e-6:
                        hr = (x + dr, y - (hl[1] - y) * dr / dl)
            kp.handle_left_type = tl
            kp.handle_right_type = tr
            kp.handle_left = hl
            kp.handle_right = hr
            touched.add(fc)


classes = (EMP_OT_com_reset_all, EMP_OT_com_set_weights, EMP_OT_path_pick, EMP_OT_path_space, EMP_OT_path_space_menu, EMP_OT_path_add, EMP_OT_path_remove, EMP_OT_graph_isolate, EMP_OT_lock_info, EMP_OT_reset_button,
           EMP_OT_reset_graph_look, EMP_OT_com_limb_reset, EMP_OT_edit_limb_weight,
           EMP_OT_smoothing_none, EMP_OT_set_view, EMP_OT_onion_mode, EMP_OT_onion_update, EMP_OT_path_update,
           EMP_OT_edit_tool,
           EMP_OT_handle_type, EMP_OT_interpolation,
           EMP_OT_smooth, EMP_OT_exaggerate, EMP_OT_ease, EMP_OT_scale_handles,
           EMP_OT_scale_handle_values, EMP_OT_fix_loop, EMP_OT_cleanup)
