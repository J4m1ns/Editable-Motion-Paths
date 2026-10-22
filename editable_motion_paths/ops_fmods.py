"""F-Curve modifiers as a modifier stack for the active path, like in modeling.

One entry of the stack is one modifier (same name) on the chosen channels (X / Y / Z, W X Y Z
for quaternions, the components of a value). Its settings are shown once and kept the same on
all its channels. Apply bakes it into keys (and cleans them up), copy / paste moves modifiers
between paths, and the Modifier Tools functions work on the whole stack."""

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty

from . import compat, core, pick, state

TYPES = [
    ('GENERATOR', "Generator", "Add a polynomial (e.g. a constant drift)", 'IPO_LINEAR'),
    ('FNGENERATOR', "Built-In Function", "Sine, cosine... (a wave on top of the motion)", 'IPO_SINE'),
    ('ENVELOPE', "Envelope", "Scale the motion between limits", 'MOD_SMOOTH'),
    ('CYCLES', "Cycles", "Repeat the motion (a loop)", 'FILE_REFRESH'),
    ('NOISE', "Noise", "Add noise (shake, jitter)", 'RNDCURVE'),
    ('LIMITS', "Limits", "Clamp the values", 'CON_DISTLIMIT'),
    ('STEPPED', "Stepped Interpolation", "Hold every n frames (on twos, on threes...)", 'IPO_CONSTANT'),
]
ICONS = {t[0]: t[3] for t in TYPES}
SKIP = {'rna_type', 'name', 'show_expanded', 'active', 'type', 'is_valid', 'control_points',
        'coefficients'}


# ----------------------------------------------------------------------------
# The stack of the active path
# ----------------------------------------------------------------------------

def active_cache(context):
    st = state.settings(context.scene)
    caches = state.editable_caches(context.scene)
    if st is not None and 0 <= st.active_index < len(st.paths):
        e = st.paths[st.active_index]
        for c in caches:
            if e.object is not None and c.key[:2] == (e.object.name, e.bone_name):
                return c
    return caches[0] if caches else None


def channels(cache):
    """[(label, fcurve or None)] of the active path's channels in the current mode."""
    tg = cache.target()
    if tg is None:
        return []
    if cache.ptype == core.VALUE:
        path = tg.value.path
        out = []
        for i in range(4):
            fc = tg.coll.find(path, index=i) if tg.coll is not None and path else None
            if fc is not None:
                out.append(("XYZW"[i], fc))
        if len(out) == 1:
            out = [("Value", out[0][1])]
        return out
    ch = tg.channels(cache.ptype)
    if cache.ptype == core.ROTATION and ch.count == 4:
        labels = "WXYZ"
    else:
        labels = "XYZ"
    return [(labels[i], fc) for i, fc in enumerate(ch.fcurves)]


def stack(cache):
    """[(name, type, [modifier or None per channel])] in stack order."""
    chans = channels(cache)
    order = []
    found = {}
    for ci, (_l, fc) in enumerate(chans):
        if fc is None:
            continue
        for i, m in enumerate(fc.modifiers):
            if m.name not in found:
                found[m.name] = [None] * len(chans)
                order.append((i, ci, m.name, m.type))
            found[m.name][ci] = m
    order.sort(key=lambda o: (o[0], o[1]))
    return [(name, typ, found[name]) for _i, _c, name, typ in order]


def copy_settings(src, dst):
    for p in src.bl_rna.properties:
        if p.is_readonly or p.identifier in SKIP:
            continue
        try:
            setattr(dst, p.identifier, getattr(src, p.identifier))
        except (AttributeError, TypeError, ValueError):
            pass
    if src.type == 'GENERATOR':
        dst.coefficients = tuple(src.coefficients)
    if src.type == 'ENVELOPE':
        while len(dst.control_points):
            dst.control_points.remove(dst.control_points[0])
        for cp in src.control_points:
            n = dst.control_points.add(cp.frame)
            n.min, n.max = cp.min, cp.max


def settings_of(m):
    data = {p.identifier: getattr(m, p.identifier) for p in m.bl_rna.properties
            if not p.is_readonly and p.identifier not in SKIP}
    for k, v in list(data.items()):
        try:
            data[k] = tuple(v) if not isinstance(v, str) else v
        except TypeError:
            pass
    if m.type == 'GENERATOR':
        data['coefficients'] = tuple(m.coefficients)
    if m.type == 'ENVELOPE':
        data['control_points'] = [(cp.frame, cp.min, cp.max) for cp in m.control_points]
    return data


def apply_settings(m, data):
    for k, v in data.items():
        if k == 'control_points':
            for f, lo, hi in v:
                n = m.control_points.add(f)
                n.min, n.max = lo, hi
            continue
        try:
            setattr(m, k, v)
        except (AttributeError, TypeError, ValueError):
            pass


def _unique_name(cache, base):
    names = {n for n, _t, _m in stack(cache)}
    if base not in names:
        return base
    i = 1
    while "%s.%03d" % (base, i) in names:
        i += 1
    return "%s.%03d" % (base, i)


def sync_stack(cache):
    """Keep every modifier of a stack entry like its first channel's one (the one the panel
    shows). Called after edits; changes only what differs."""
    for _name, _t, mods in stack(cache):
        master = next((m for m in mods if m is not None), None)
        if master is None:
            continue
        ref = settings_of(master)
        for m in mods:
            if m is None or m == master:
                continue
            if settings_of(m) != ref:
                copy_settings(master, m)


def _rebuild_order(fc, names):
    """Re-create the modifiers of fc in the order of `names`."""
    data = [(m.name, m.type, settings_of(m), m.show_expanded) for m in fc.modifiers]
    by_name = {d[0]: d for d in data}
    for m in list(fc.modifiers):
        fc.modifiers.remove(m)
    for n in names:
        d = by_name.get(n)
        if d is None:
            continue
        m = fc.modifiers.new(d[1])
        m.name = d[0]
        apply_settings(m, d[2])
        m.show_expanded = d[3]


def _done(cache):
    cache.dirty = True
    state.schedule_rebake(0.2, force=True)
    pick.tag_anim_editors()


# ----------------------------------------------------------------------------
# Operators
# ----------------------------------------------------------------------------

class _StackOp:
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return active_cache(context) is not None


class EMP_OT_fmod_add(_StackOp, bpy.types.Operator):
    """Add an F-Curve modifier to the active path's channels (in the current mode)"""
    bl_idname = "emp.fmod_add"
    bl_label = "Add Modifier"

    type: EnumProperty(name="Type", items=[t[:3] for t in TYPES])

    def execute(self, context):
        cache = active_cache(context)
        chans = [fc for _l, fc in channels(cache) if fc is not None]
        if not chans:
            self.report({'WARNING'}, "This path has no animated channels in this mode")
            return {'CANCELLED'}
        name = _unique_name(cache, dict((t[0], t[1]) for t in TYPES)[self.type])
        for fc in chans:
            m = fc.modifiers.new(self.type)
            m.name = name
            m.show_expanded = True
        _done(cache)
        return {'FINISHED'}


class EMP_OT_fmod_remove(_StackOp, bpy.types.Operator):
    """Remove this modifier from all channels"""
    bl_idname = "emp.fmod_remove"
    bl_label = "Remove Modifier"

    name: StringProperty()

    def execute(self, context):
        cache = active_cache(context)
        for _l, fc in channels(cache):
            if fc is not None:
                for m in [m for m in fc.modifiers if m.name == self.name]:
                    fc.modifiers.remove(m)
        _done(cache)
        return {'FINISHED'}


class EMP_OT_fmod_axis(_StackOp, bpy.types.Operator):
    """Use this modifier on this channel or not (one channel always keeps it)"""
    bl_idname = "emp.fmod_axis"
    bl_label = "Modifier Channel"

    name: StringProperty()
    index: IntProperty()

    def execute(self, context):
        cache = active_cache(context)
        chans = channels(cache)
        entry = next((e for e in stack(cache) if e[0] == self.name), None)
        if entry is None or not 0 <= self.index < len(chans):
            return {'CANCELLED'}
        _n, typ, mods = entry
        fc = chans[self.index][1]
        if fc is None:
            self.report({'WARNING'}, "This channel is not animated")
            return {'CANCELLED'}
        m = mods[self.index]
        if m is not None:
            if sum(1 for x in mods if x is not None) <= 1:
                return {'CANCELLED'}        # the last channel: use Remove instead
            fc.modifiers.remove(m)
        else:
            master = next(x for x in mods if x is not None)
            names = [n for n, _t, ms in stack(cache) if any(x is not None for x in ms)]
            new = fc.modifiers.new(typ)
            new.name = self.name
            copy_settings(master, new)
            _rebuild_order(fc, names)
        _done(cache)
        return {'FINISHED'}


class EMP_OT_fmod_move(_StackOp, bpy.types.Operator):
    """Move the modifier up or down the stack (it changes the result, like in modeling)"""
    bl_idname = "emp.fmod_move"
    bl_label = "Move Modifier"

    name: StringProperty()
    direction: EnumProperty(items=[('UP', "Up", ""), ('DOWN', "Down", "")])

    def execute(self, context):
        cache = active_cache(context)
        names = [n for n, _t, _m in stack(cache)]
        if self.name not in names:
            return {'CANCELLED'}
        i = names.index(self.name)
        j = i - 1 if self.direction == 'UP' else i + 1
        if not 0 <= j < len(names):
            return {'CANCELLED'}
        names[i], names[j] = names[j], names[i]
        for _l, fc in channels(cache):
            if fc is not None and len(fc.modifiers) > 1:
                _rebuild_order(fc, names)
        _done(cache)
        return {'FINISHED'}


def _bake(fc, names_to_apply):
    """Bake the listed modifiers of fc into keys (one per frame), the others stay."""
    keep = [m for m in fc.modifiers if m.name not in names_to_apply]
    for m in keep:
        m.mute = True
    scene = bpy.context.scene
    kps = fc.keyframe_points
    a = min(kps[0].co[0], scene.frame_start) if len(kps) else scene.frame_start
    b = max(kps[-1].co[0], scene.frame_end) if len(kps) else scene.frame_end
    vals = [(float(f), fc.evaluate(f)) for f in range(int(a), int(b) + 1)]
    for m in keep:
        m.mute = False
    for m in [m for m in fc.modifiers if m.name in names_to_apply]:
        fc.modifiers.remove(m)
    kps.clear()
    kps.add(len(vals))
    for kp, (f, v) in zip(kps, vals):
        kp.co = (f, v)
        kp.interpolation = 'BEZIER'
        kp.handle_left_type = kp.handle_right_type = 'AUTO_CLAMPED'
    fc.update()


class EMP_OT_fmod_apply(_StackOp, bpy.types.Operator):
    """Apply the modifier: bake its effect into keys (then clean them up to a few keys)"""
    bl_idname = "emp.fmod_apply"
    bl_label = "Apply Modifier"

    name: StringProperty(options={'SKIP_SAVE'})
    all: BoolProperty(options={'SKIP_SAVE'})
    simplify: BoolProperty(name="Clean Up", default=True,
                           description="Reduce the baked keys while keeping the motion (Clean Up)")

    def execute(self, context):
        cache = active_cache(context)
        names = {n for n, _t, _m in stack(cache)} if self.all else {self.name}
        if not names:
            return {'CANCELLED'}
        for _l, fc in channels(cache):
            if fc is not None and any(m.name in names for m in fc.modifiers):
                _bake(fc, names)
        cache.refresh(force=True)
        if self.simplify:
            pick.deselect_all([cache])
            cache.refresh(force=True)
            area = next((a for a in context.screen.areas if a.type == 'VIEW_3D'), None)
            if area is not None:
                with context.temp_override(area=area, region=next(r for r in area.regions if r.type == 'WINDOW')):
                    bpy.ops.emp.cleanup('EXEC_DEFAULT', method='CLEAN', tolerance=0.3)
        _done(cache)
        return {'FINISHED'}


_clipboard = []


class EMP_OT_fmod_copy(_StackOp, bpy.types.Operator):
    """Copy this modifier (all of them if none is given) to paste it on another path"""
    bl_idname = "emp.fmod_copy"
    bl_label = "Copy Modifiers"
    bl_options = {'REGISTER'}

    name: StringProperty(options={'SKIP_SAVE'})

    def execute(self, context):
        global _clipboard
        cache = active_cache(context)
        _clipboard = []
        for name, typ, mods in stack(cache):
            if self.name and name != self.name:
                continue
            master = next(m for m in mods if m is not None)
            _clipboard.append((name, typ, settings_of(master), [m is not None for m in mods]))
        self.report({'INFO'}, "Copied %d modifier%s" % (len(_clipboard), "" if len(_clipboard) == 1 else "s"))
        return {'FINISHED'}


class EMP_OT_fmod_paste(_StackOp, bpy.types.Operator):
    """Paste the copied modifiers on the active path's channels (same channels where possible)"""
    bl_idname = "emp.fmod_paste"
    bl_label = "Paste Modifiers"

    @classmethod
    def poll(cls, context):
        return bool(_clipboard) and active_cache(context) is not None

    def execute(self, context):
        cache = active_cache(context)
        chans = channels(cache)
        for name, typ, data, on in _clipboard:
            new_name = _unique_name(cache, name)
            for i, (_l, fc) in enumerate(chans):
                if fc is None or (i < len(on) and not on[i] and any(on)):
                    continue
                m = fc.modifiers.new(typ)
                m.name = new_name
                apply_settings(m, data)
        _done(cache)
        return {'FINISHED'}


class EMP_OT_fmod_tools(_StackOp, bpy.types.Operator):
    """Modifier Tools: work on the whole stack of the active path"""
    bl_idname = "emp.fmod_tools"
    bl_label = "Modifier Tools"

    action: EnumProperty(items=[
        ('REMOVE', "Remove All", "Remove every modifier", 'X', 0),
        ('MUTE', "Toggle Visibility", "Turn all modifiers off, or all on again", 'HIDE_OFF', 1),
        ('EXPAND', "Expand / Collapse", "Show or hide the settings of all modifiers", 'DISCLOSURE_TRI_DOWN', 2),
    ])

    def execute(self, context):
        cache = active_cache(context)
        mods = [m for _l, fc in channels(cache) if fc is not None for m in fc.modifiers]
        if not mods:
            return {'CANCELLED'}
        if self.action == 'REMOVE':
            for _l, fc in channels(cache):
                if fc is not None:
                    for m in list(fc.modifiers):
                        fc.modifiers.remove(m)
        elif self.action == 'MUTE':
            on = any(not m.mute for m in mods)
            for m in mods:
                m.mute = on
        else:
            show = not all(m.show_expanded for m in mods)
            for m in mods:
                m.show_expanded = show
        _done(cache)
        return {'FINISHED'}


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------

class EMP_MT_fmod_add(bpy.types.Menu):
    bl_label = "Add Modifier"
    bl_idname = "EMP_MT_fmod_add"

    def draw(self, context):
        for ident, name, _d, icon in TYPES:
            self.layout.operator("emp.fmod_add", text=name, icon=icon).type = ident


class EMP_MT_fmod_tools(bpy.types.Menu):
    bl_label = "Modifier Tools"
    bl_idname = "EMP_MT_fmod_tools"

    def draw(self, context):
        layout = self.layout
        op = layout.operator("emp.fmod_apply", text="Apply All", icon='CHECKMARK')
        op.all = True
        layout.operator_enum("emp.fmod_tools", "action")
        layout.separator()
        layout.operator("emp.fmod_copy", text="Copy All", icon='COPYDOWN')
        layout.operator("emp.fmod_paste", text="Paste", icon='PASTEDOWN')


def draw_stack(layout, context):
    cache = active_cache(context)
    if cache is None:
        layout.label(text="Select a bone / object with a path", icon='INFO')
        return
    row = layout.row(align=True)
    row.menu("EMP_MT_fmod_add", icon='ADD')
    row.menu("EMP_MT_fmod_tools", text="", icon='DOWNARROW_HLT')
    chans = channels(cache)
    st = stack(cache)
    layout.label(text="%s, %s" % (cache.label, cache.ptype.title()), icon='MODIFIER')
    if not st:
        layout.label(text="No modifiers (Alt+C adds Cycles)")
    for idx, (name, typ, mods) in enumerate(st):
        master = next(m for m in mods if m is not None)
        box = layout.box()
        row = box.row(align=True)
        row.prop(master, "show_expanded", text="", emboss=False,
                 icon='DISCLOSURE_TRI_DOWN' if master.show_expanded else 'DISCLOSURE_TRI_RIGHT')
        row.label(text=name, icon=ICONS.get(typ, 'MODIFIER'))
        sub = row.row(align=True)
        for i, (label, fc) in enumerate(chans):
            op = sub.operator("emp.fmod_axis", text=label, depress=mods[i] is not None)
            op.name, op.index = name, i
            sub.enabled = True
        row.prop(master, "mute", text="", icon='HIDE_ON' if master.mute else 'HIDE_OFF', emboss=False)
        sub = row.row(align=True)
        op = sub.operator("emp.fmod_move", text="", icon='TRIA_UP')
        op.name, op.direction = name, 'UP'
        op = sub.operator("emp.fmod_move", text="", icon='TRIA_DOWN')
        op.name, op.direction = name, 'DOWN'
        row.operator("emp.fmod_apply", text="", icon='CHECKMARK').name = name
        row.operator("emp.fmod_copy", text="", icon='COPYDOWN').name = name
        row.operator("emp.fmod_remove", text="", icon='X').name = name
        if master.show_expanded:
            col = box.column()
            col.use_property_split = True
            for p in master.bl_rna.properties:
                if p.is_readonly or p.identifier in SKIP or p.identifier in {'mute'}:
                    continue
                col.prop(master, p.identifier)
            if typ == 'GENERATOR':
                col.prop(master, "coefficients")
            if typ == 'ENVELOPE':
                for cp in master.control_points:
                    r = col.row(align=True)
                    r.prop(cp, "frame")
                    r.prop(cp, "min")
                    r.prop(cp, "max")


class EMP_PT_modifiers(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Motion Paths"
    bl_label = "Modifiers"
    bl_parent_id = "EMP_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        draw_stack(self.layout, context)


class EMP_PT_modifiers_popover(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'HEADER'
    bl_label = "Path Modifiers"
    bl_idname = "EMP_PT_modifiers_popover"
    bl_ui_units_x = 16

    def draw(self, context):
        draw_stack(self.layout, context)


classes = (EMP_OT_fmod_add, EMP_OT_fmod_remove, EMP_OT_fmod_axis, EMP_OT_fmod_move, EMP_OT_fmod_apply,
           EMP_OT_fmod_copy, EMP_OT_fmod_paste, EMP_OT_fmod_tools, EMP_MT_fmod_add, EMP_MT_fmod_tools)
# panels: registered after the main panel (ui.register)
ui_classes = (EMP_PT_modifiers, EMP_PT_modifiers_popover)


def sync_active(context):
    """Keep the stack entries' channels alike after an edit in the panel."""
    cache = active_cache(context)
    if cache is not None:
        sync_stack(cache)
