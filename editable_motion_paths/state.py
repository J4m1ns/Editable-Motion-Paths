"""Runtime state (not saved in the .blend) and change tracking."""

import re
import time

import bpy
from bpy.app.handlers import persistent

from . import compat

caches = {}          # (obj_name, bone_name, type) -> core.PathCache
baking = False       # True while frame-stepping for a bake
modal_running = 0    # >0 while one of our modal operators is editing
hud = None           # (text, (x, y)) shown next to the cursor
hud_area = 0         # as_pointer() of the area showing the HUD
box = None           # (x0, y0, x1, y1, mode) box-select rectangle in region space
box_area = 0
circle = None        # (x, y, radius, area pointer) circle select brush
chart = None         # timing chart geometry of the last draw (see draw._draw_chart)
speed_graph = None   # where the speed graph is and what it shows (for dragging it), see draw
speed_hold = None    # while the speed graph is dragged: {cache, lo, hi, vmax} it keeps
speed_hot = None     # (path key, 'KEY' / 'EASE', frame / segment) under the mouse on the graph
speed_reach = None   # while the graph's speed is dragged: (path key, frame, spread before, after) - its bar
sculpt_cursor = None   # (x, y, radius, area pointer, painting) the sculpt brush circle
sculpt_preview = None  # ([world points], area pointer) frame dots while a timing brush paints
sculpt_hide = set()    # paths whose own frame dots are hidden meanwhile
sculpt_strip_hover = None   # (brush, area pointer) under the mouse on the brush strip
sculpt_radial = None   # F / Shift+F: what the radial control shows
scale_box_label = None  # (box corners, text, area) the Scale Box's values, for the 2D pass
gallery = None        # the easing gallery under the mouse (P): its place, what the mouse is over
knife = None         # ([cut lines of world points], area pointer, cursor or None, snap) knife
markers = None       # ([world points], area pointer[, labels]) previews of keys about to be added
constraint = None    # (pivot, axis, color) axis line shown while transforming
snap_point = None    # world point the transform snapped to
prop_circle = None   # (center, radius) proportional editing circle while transforming
prop_weights = {}    # (path key, frame) -> proportional influence, for drawing
slide_marker = None  # (world point, label) target of a key slide
slide_extension = None  # (before, after) world polylines: where a slide can go past the ends
_history_cache = (None, [])  # parsed settings.select_history
flash = None         # (text, until_time) short message shown at the bottom of the viewport
force_draw = False   # draw even with overlays off (render_view for scripts)
_watch_last = None
force_numbers = False  # show key frame numbers (while sliding)
ghosts = {}          # path key -> list of world points shown while dragging
_action_hashes = {}  # action name -> {(data_path, index): hash}
_bake_due = 0.0
_bake_timer_on = False
_force_bake = False


def settings(scene=None):
    scene = scene or bpy.context.scene
    return getattr(scene, "emp", None) if scene else None


def overlays_hidden():
    """True when the 3D view we are drawing / working in has Show Overlays turned off."""
    if force_draw:
        return False
    sd = getattr(bpy.context, "space_data", None)
    return sd is not None and sd.type == 'VIEW_3D' and not sd.overlay.show_overlays


def _viewport_input():
    """True for hotkeys / clicks in the 3D view itself (not buttons in the sidebar)."""
    region = getattr(bpy.context, "region", None)
    return region is not None and region.type == 'WINDOW'


def tag_redraw():
    wm = bpy.context.window_manager
    if wm is None:
        return
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type in {'VIEW_3D', 'DOPESHEET_EDITOR'}:     # (the timeline: the ghosts' frames)
                area.tag_redraw()


# ----------------------------------------------------------------------------
# Cache management
# ----------------------------------------------------------------------------

def edit_mode(scene=None):
    st = settings(scene)
    return st.edit_mode if st is not None else 'LOCATION'


def entry_key(entry, mode=None):
    if entry.object is None:
        return None
    return (entry.object.name, entry.bone_name, mode or edit_mode(entry.id_data))


_bone_names = {}      # armature object name -> its bone names (in order) when last seen


def _follow_renamed_bones(st):
    """A bone of a path was renamed: the path follows it (the bones keep their order when one is
    renamed, so its old place tells the new name)."""
    for e in st.paths:
        ob = e.object
        if ob is None or ob.type != 'ARMATURE' or not e.bone_name:
            continue
        names = tuple(b.name for b in ob.data.bones)
        if e.bone_name not in names:
            old = _bone_names.get(ob.name)
            if old is not None and len(old) == len(names) and e.bone_name in old:
                was = e.bone_name
                e.bone_name = names[old.index(was)]
                prefix = 'pose.bones["%s"]' % bpy.utils.escape_identifier(was)
                if e.value_path.startswith(prefix):
                    e.value_path = 'pose.bones["%s"]' % bpy.utils.escape_identifier(e.bone_name) + \
                        e.value_path[len(prefix):]
    for e in st.paths:
        if e.object is not None and e.object.type == 'ARMATURE':
            _bone_names[e.object.name] = tuple(b.name for b in e.object.data.bones)


def sync_caches(scene=None):
    """Make the cache dict match the scene's path list and edit mode.

    Caches of the other edit mode are kept (switching back is instant) and a new
    cache reuses the baked parent space of its sibling. Returns caches needing a bake.
    """
    from . import core
    st = settings(scene)
    if st is None:
        return []
    _follow_renamed_bones(st)
    mode = st.edit_mode
    targets = set()
    wanted = []
    for e in st.paths:
        k = entry_key(e, mode)
        if k is not None:
            targets.add(k[:2])
            wanted.append(k)
    for k in list(caches):
        if k[:2] not in targets:
            del caches[k]
    need = []
    refs = {entry_key(e, mode): (e.value_path, e.value_index) for e in st.paths if e.object is not None}
    hidden = {entry_key(e, mode): parse_frames(e.hidden_keys) for e in st.paths if e.object is not None}
    for k in wanted:
        c = caches.get(k)
        if c is None:
            c = caches[k] = core.PathCache(*k)
            other = next((o for o in caches.values() if o is not c and o.key[:2] == k[:2] and o.B), None)
            if other is not None:
                c.fs, c.B, c.bake_id = other.fs, other.B, other.bake_id
        h = hidden.get(k, set())
        if h != c.hidden:
            c.hidden = h
            c.dirty = True
        if mode == core.VALUE:
            ref = refs.get(k)
            if not (ref and ref[0]):
                # nothing picked yet: the first animated value of the bone / object
                e = next((e for e in st.paths if entry_key(e, mode) == k), None)
                cands = core.value_candidates(e.object, e.bone_name) if e is not None else []
                if cands and not baking:
                    e.value_path, e.value_index = cands[0][0], cands[0][1]
                    ref = (cands[0][0], cands[0][1])
            ref = ref if ref and ref[0] else None
            if ref != c.value_ref:
                c.value_ref = ref
                c.vgain_id = None
                c.dirty = True
        if not c.B:
            need.append(c)
    return need


def parse_frames(text):
    out = set()
    for part in (text or "").split(","):
        try:
            out.add(round(float(part), 3))
        except ValueError:
            pass
    return out


def format_frames(frames):
    return ",".join("%g" % f for f in sorted(frames))


def entry_of(cache, scene=None):
    st = settings(scene)
    for e in st.paths if st is not None else ():
        if e.object is not None and e.object.name == cache.obj_name and e.bone_name == cache.bone_name:
            return e
    return None


def visible_caches(scene=None):
    st = settings(scene)
    if st is None:
        return []
    out = []
    seen = set()
    for e in st.paths:
        if not e.show:
            continue
        k = entry_key(e, st.edit_mode)
        if k in seen:
            continue
        seen.add(k)
        c = caches.get(k)
        if c is not None and c.B:
            out.append(c)
        elif c is None or not c.failed:
            # new / renamed entry: calculate it soon (never from inside drawing)
            schedule_rebake(0.1)
    return out


draw_pass = None     # set while drawing (see draw.draw_view): results can be kept for that pass
_editable_memo = [None, []]


def editable_caches(scene=None):
    if overlays_hidden() and _viewport_input():
        return []       # paths are hidden with the overlays: viewport hotkeys go to Blender
    if draw_pass is not None:
        key = (draw_pass, id(scene or bpy.context.scene))
        if _editable_memo[0] == key:
            return list(_editable_memo[1])
    out = []
    for c in visible_caches(scene):
        if c.owner_selected() and c.refresh() and not c.locked:
            out.append(c)
    if draw_pass is not None:
        _editable_memo[:] = [key, list(out)]
    return out


def bake_all(scene=None, only=None):
    from . import core
    scene = scene or bpy.context.scene
    need = sync_caches(scene)
    targets = list(caches.values()) if only is None else only
    if only == 'MISSING':
        targets = need
    core.bake(scene, targets)
    tag_redraw()


# ----------------------------------------------------------------------------
# Auto update: re-bake when animation other than a path's own channels changed
# ----------------------------------------------------------------------------

def _fcurve_hashes(obj, curves=True):
    """{(data path, index): hash} of what moves obj on the frames: its F-Curves (curves=False:
    left out, they are what costs), and under special keys ('#...') its NLA, drivers (with the
    values they read now) and constraints."""
    out = {}
    coll = compat.get_fcurves(obj) if curves else None
    from .core import _fmod_signature, fc_shape
    for fc in coll if coll is not None else ():
        # (the interpolation and the modifiers' settings too: they change the curve as much)
        out[(fc.data_path, fc.array_index)] = hash((fc_shape(fc), fc.mute,
                                                    tuple(_fmod_signature(m) for m in fc.modifiers)))
    ad = obj.animation_data
    if ad is not None:
        out[("#nla", 0)] = hash((ad.action_blend_type, round(ad.action_influence, 6), ad.action_extrapolation,
                                 ad.use_nla, tuple((t.mute, t.is_solo, tuple(_strip_sig(s) for s in t.strips))
                                                   for t in ad.nla_tracks)))
    drv = []
    for idb in (obj, getattr(obj, "data", None)):
        dad = getattr(idb, "animation_data", None)
        for d in dad.drivers if dad is not None else ():
            drv.append((d.data_path, d.array_index, d.mute, d.driver.type, d.driver.expression, fc_shape(d),
                        tuple(_fmod_signature(m) for m in d.modifiers),
                        tuple((v.type, tuple((t.id.name if t.id else None, t.data_path, t.bone_target,
                                              t.transform_type, t.transform_space, _target_value(t))
                                             for t in v.targets)) for v in d.driver.variables)))
    if drv:
        out[("#drivers", 0)] = hash(tuple(drv))
    owners = [obj] + (list(obj.pose.bones) if obj.type == 'ARMATURE' and obj.pose is not None else [])
    cons = tuple((ow.name, tuple(_constraint_sig(c) for c in ow.constraints)) for ow in owners if len(ow.constraints))
    if cons:
        out[("#constraints", 0)] = hash(cons)
    return out


def _strip_sig(s):
    return (s.action.name if s.action else None, s.frame_start, s.frame_end, s.action_frame_start,
            s.action_frame_end, s.scale, s.repeat, s.mute, s.influence, s.blend_type, s.extrapolation,
            s.use_reverse, s.blend_in, s.blend_out)


def _target_value(t):
    """What a driver's single-property variable reads now (an unkeyed slider moves the rig)."""
    if t.id is None or not t.data_path:
        return None
    try:
        v = t.id.path_resolve(t.data_path)
    except (ValueError, TypeError, AttributeError):
        return None
    if isinstance(v, (bool, int, float)):
        return round(float(v), 6)
    if isinstance(v, str):
        return v            # (a text property: 'L' / 'R'...)
    try:
        if len(v) <= 32:
            return tuple(round(float(x), 6) for x in v)
    except (TypeError, ValueError):
        pass
    return getattr(v, "name", None) or type(v).__name__     # (a datablock, a collection...: by its name)


_con_props = {}     # constraint type -> [(identifier, kind)] of the settings read for its signature


def _constraint_props(con):
    props = _con_props.get(con.type)
    if props is None:
        props = []
        for p in con.bl_rna.properties:
            if p.identifier in {'rna_type', 'show_expanded', 'active', 'error_location', 'error_rotation',
                                'name'}:
                continue
            if p.type == 'POINTER':
                props.append((p.identifier, 'POINTER'))
            elif p.type in {'BOOLEAN', 'INT', 'FLOAT', 'ENUM', 'STRING'}:
                props.append((p.identifier, 'ARRAY' if getattr(p, "is_array", False) else
                              'SET' if getattr(p, "is_enum_flag", False) else p.type))
        _con_props[con.type] = props
    return props


def _constraint_sig(con):
    """A constraint's settings (its limits, targets, influence...)."""
    out = [con.type]
    for ident, kind in _constraint_props(con):
        try:
            v = getattr(con, ident)
        except AttributeError:
            continue
        if kind == 'POINTER':
            out.append(getattr(v, "name", None))
        elif kind == 'SET':
            out.append(tuple(sorted(v)))
        elif kind == 'ARRAY':
            flat = []
            for x in v:
                if hasattr(x, "__len__"):
                    flat += [round(float(y), 6) for y in x]
                else:
                    flat.append(round(float(x), 6))
            out.append(tuple(flat))
        elif kind == 'FLOAT':
            out.append(round(v, 6))
        else:
            out.append(v)
    return tuple(out)


def _depends_on(obj):
    """The objects whose animation can move obj: obj itself, its parents, its (and its bones')
    constraints' targets, its drivers' targets - and theirs, all the way up."""
    out = {}
    todo = [obj]
    while todo:
        o = todo.pop()
        if o is None or o.name in out:
            continue
        out[o.name] = o
        refs = [o.parent]
        owners = [o] + (list(o.pose.bones) if o.type == 'ARMATURE' and o.pose is not None else [])
        for ow in owners:
            for con in ow.constraints:
                refs.append(getattr(con, "target", None))
                refs.append(getattr(con, "pole_target", None))
                for t in getattr(con, "targets", ()):
                    refs.append(getattr(t, "target", None))
        for idb in (o, getattr(o, "data", None)):
            ad = getattr(idb, "animation_data", None)
            for d in ad.drivers if ad is not None else ():
                for v in d.driver.variables:
                    refs += [t.id for t in v.targets]
        todo += [r for r in refs if isinstance(r, bpy.types.Object)]
    return out


def _tracked_objects():
    objs = {}
    for name in {c.obj_name for c in caches.values()}:
        obj = bpy.data.objects.get(name)
        if obj is not None:
            objs.update(_depends_on(obj))
    return objs


def remember_action_hashes():
    _action_hashes.clear()
    for name, obj in _tracked_objects().items():
        _action_hashes[name] = _fcurve_hashes(obj)


_TRANSFORM_PROPS = {"location", "rotation_quaternion", "rotation_euler", "rotation_axis_angle", "scale"}
_BONE_PATH = re.compile(r'^pose\.bones\["(.*)"\]\.(\w+)$')
_LINEAR_CONSTRAINTS = {'CHILD_OF', 'ARMATURE'}   # (they only put a parent space in front)


def _constrained(owner):
    """The bone / object has a constraint that works on its own transform (Limit Rotation, Copy
    Location...): its parent space as baked depends on its own channels."""
    return any(c.type not in _LINEAR_CONSTRAINTS and not c.mute and getattr(c, "enabled", True) and
               c.influence > 0.0 for c in owner.constraints)


def _harmless(obj, data_path, memo=None):
    """True if changing this channel can't move any path's parent space.

    That is the case for transform channels of a path target that moves no other path target
    (by the hierarchy or by a constraint) and has no constraint working on its own transform.
    """
    targets = [c for c in caches.values() if c.obj_name == obj.name]
    if data_path.endswith('"]') and any(c.value_ref and c.value_ref[0] == data_path for c in targets) \
            and not _driven_by(obj, data_path):
        return True     # a custom property shown on a value track that moves nothing else
    # paths of other objects that hang on this one (a parent, Child Of on a bone, a driver...)
    memo = {} if memo is None else memo
    for c in caches.values():
        other = bpy.data.objects.get(c.obj_name) if c.obj_name != obj.name else None
        if other is None:
            continue
        deps = memo.get(("deps", other.name))
        if deps is None:
            deps = memo[("deps", other.name)] = _depends_on(other)
        if obj.name in deps:
            return False
    if obj.type == 'ARMATURE' and obj.pose is not None:
        m = _BONE_PATH.match(data_path)
        if not m or m.group(2) not in _TRANSFORM_PROPS:
            return False
        bname = m.group(1).replace('\\"', '"').replace('\\\\', '\\')
        if not any(c.bone_name == bname for c in targets):
            return False
        pb = obj.pose.bones.get(bname)
        if pb is None or _constrained(pb):
            return False
        from .onion import affected_bones
        memo = {} if memo is None else memo
        moved = memo.get((obj.name, bname))
        if moved is None:
            moved = memo[(obj.name, bname)] = affected_bones(obj, {bname})
        if any(p.name in moved for p in pb.parent_recursive):
            return False    # (it moves a bone it hangs from: a neck sharing the head's turn...)
        return not any(c.bone_name and c.bone_name != bname and c.bone_name in moved for c in targets)
    if data_path not in _TRANSFORM_PROPS or not any(not c.bone_name for c in targets):
        return False
    return not _constrained(obj)


def _driven_by(obj, data_path):
    """True if a driver anywhere reads this property of obj."""
    name = data_path.rsplit('["', 1)[-1].rstrip('"]')
    for owner in list(bpy.data.objects) + list(bpy.data.shape_keys) + list(bpy.data.armatures) + \
            list(bpy.data.meshes):
        ad = getattr(owner, "animation_data", None)
        if ad is None:
            continue
        for d in ad.drivers:
            for v in d.driver.variables:
                for t in v.targets:
                    if t.id == obj and name in (t.data_path or ""):
                        return True
    return False


def _range_changed():
    """True if the frame range to calculate changed (e.g. a key was slid past the end)."""
    from . import core
    st = settings()
    cs = [c for c in caches.values() if c.B]
    if st is None or not cs:
        return False
    fs, fe = core.bake_range(bpy.context.scene, st, cs)
    return any(c.fs != fs or c.fe != fe for c in cs)


def _needs_rebake(curves=True):
    """True if something that moves a path's parent space changed since the bake: an F-Curve
    that is not a path's own channel, an NLA strip, a driver (or what it reads), a constraint.
    curves=False: only the latter (no curve changed, an object did)."""
    memo = {}
    for name, obj in _tracked_objects().items():
        new = _fcurve_hashes(obj, curves)
        old = _action_hashes.get(name)
        if old is None:
            return True
        keys = set(new) | set(old) if curves else {k for k in set(new) | set(old) if k[0].startswith("#")}
        for k in keys:
            if new.get(k) != old.get(k) and not _harmless(obj, k[0], memo):
                return True
        # only own channels changed: keep hashes current so later comparisons stay precise
        if curves:
            _action_hashes[name] = new
        else:
            old.update(new)
    return False


_check_curves = True     # the next check compares the curves too (an action changed)


def schedule_rebake(delay=0.35, force=False, curves=True):
    global _bake_due, _bake_timer_on, _force_bake, _check_curves
    _force_bake = _force_bake or force
    _check_curves = _check_curves or curves
    _bake_due = time.time() + delay
    if not _bake_timer_on:
        _bake_timer_on = True
        bpy.app.timers.register(_bake_timer, first_interval=delay)


def _any_modal(skip=()):
    """True while an interactive edit (Blender's transform, scrubbing, graph editing...)
    is running. Other add-ons' permanent modal operators are ignored."""
    wm = bpy.context.window_manager
    if wm is None:
        return False
    for win in wm.windows:
        for op in getattr(win, "modal_operators", ()):
            pre = op.bl_idname.split("_OT_")[0]
            if pre in _BLOCKING_MODALS and pre not in skip:
                return True
    return False


_unhide_cost = {}       # mesh name -> seconds showing it again took (it is rebuilt then)


def can_hide(o, frames, quick=False):
    """Hiding a bent mesh while stepping through `frames` frames saves evaluating it on each of
    them - worth it unless showing it again costs more: a heavy subdivided mesh is rebuilt from
    scratch then (seconds), while a frame of it costs some 20-60 ms."""
    cost = _unhide_cost.get(o.name)
    if quick:
        # (a step of the background bake: showing it again must not stall that step)
        return cost is not None and cost < 0.1
    return cost is None or cost < 0.02 * frames


def unhide_cost(objs, seconds):
    """Showing these again (and the frame after) took that long."""
    for o in objs:
        _unhide_cost[o.name] = seconds


def render_running():
    """True while Blender renders (F12, an animation) or bakes: nothing may step the scene's
    frames then - the render reads the scene, and an animation render would get wrong, doubled
    or missing frames."""
    is_running = getattr(bpy.app, "is_job_running", None)
    if is_running is None:
        return False
    for job in ('RENDER', 'OBJECT_BAKE', 'COMPOSITE'):
        try:
            if is_running(job):
                return True
        except (TypeError, ValueError):
            pass
    return False


def blender_edit_running():
    """True while one of Blender's own interactive edits of a pose runs (G / R / S, the pose
    sliders): the pose being dragged isn't keyed yet, so frames may change under it only with
    the pose put back. Other modal operators (scrubbing, other add-ons' permanent ones) don't
    count."""
    wm = bpy.context.window_manager
    if wm is None:
        return False
    for win in wm.windows:
        for op in getattr(win, "modal_operators", ()):
            if op.bl_idname.split("_OT_")[0] in {"TRANSFORM", "POSE"}:
                return True
    return False


def keep_pose(objs):
    """The poses of these objects and their parents, as they are now (also what isn't keyed),
    to put back after stepping through frames."""
    from . import core
    seen = {}
    for o in objs:
        while o is not None and o.name not in seen:
            seen[o.name] = o
            o = o.parent
    return core._pose_state(seen.values())


def keep_scene_pose(scene, objs=()):
    """keep_pose for everything stepping through frames can change: every animated object of
    the scene (its unkeyed changes - a prop moved by hand - would be lost otherwise) and `objs`."""
    animated = [o for o in scene.objects if is_animated(o)]
    return keep_pose(animated + [o for o in objs if o is not None])


def is_animated(o):
    """The object, its data or its shape keys have animation (a frame change sets their values)."""
    data = getattr(o, "data", None)
    return any(getattr(idb, "animation_data", None) is not None
               for idb in (o, data, getattr(data, "shape_keys", None)))


def flush_changes():
    """Work in the changes Blender hasn't evaluated yet (a value typed a moment ago...) before our
    timers step through frames: evaluated in there, they'd count as ours and be missed (the ghosts
    / the center of mass wouldn't follow them)."""
    try:
        bpy.context.view_layer.update()
    except Exception:
        pass


def put_pose_back(saved):
    """Put the kept poses back; True if anything had changed."""
    from . import core
    return core._restore_pose_state(saved)


_BLOCKING_MODALS = {"TRANSFORM", "EMP", "ANIM", "GRAPH", "ACTION", "VIEW3D", "POSE", "OBJECT", "WM"}


_job = None        # the automatic re-bake running in steps (core.BakeJob), or None
STEP = 0.08        # how long a step of the background re-bake goes on (seconds)
QUICK_BAKE = 0.15  # a whole re-bake quicker than this is done at once (else in steps: no hitch)
bake_seconds = 0.0  # how long the last whole bake took (a quick one is done at once)


def bake_running():
    return _job is not None


def _bake_timer():
    """The automatic re-bake: when something else than a path's own channels changed, the paths
    are baked again - in steps of ~40 ms (a heavy scene doesn't freeze Blender meanwhile; the old
    paths stay drawn until the new ones are ready)."""
    global _bake_timer_on, _force_bake, _job, _check_curves
    remaining = _bake_due - time.time()
    if remaining > 0.01 and _job is None:
        return remaining
    reconcile_modal()
    ctx = bpy.context
    screen = getattr(ctx, "screen", None)
    if modal_running or baking or (screen is not None and screen.is_animation_playing) or _any_modal() or \
            render_running():
        return 0.3
    from . import core
    try:
        if _job is not None:
            # (a step: ~0.08 s with putting the frame back, which costs about one frame)
            if not _job.step(max(0.02, STEP - 2.0 * getattr(_job, "pace", 0.0))):
                return 0.01
            _job.finish()
            _job = None
            tag_redraw()
            # (what changed meanwhile is seen right away below: the job kept the hashes of its start)
        force = _force_bake
        _force_bake = False
        curves = _check_curves
        _check_curves = False
        st = settings()
        if st is not None and st.paths:
            if force or any(not c.B for c in caches.values()) or sync_caches() or _range_changed() \
                    or _needs_rebake(curves):
                sync_caches()
                _job = core.BakeJob(bpy.context.scene, list(caches.values()))
                # quick scenes at once (as before); heavy ones in steps, without a freeze - the
                # first of them on the next tick (this one checked what changed: on a big rig that
                # takes a moment already)
                if bake_seconds >= QUICK_BAKE:
                    return 0.01
                if not _job.step(None):
                    return 0.01
                _job.finish()
                _job = None
            tag_redraw()
    except Exception as ex:  # never let a timer die silently with a traceback storm
        print("Editable Motion Paths: auto update failed:", ex)
        _job = None
    _bake_timer_on = False
    return None


_dg_frame = None
_dg_frame_all = None


@persistent
def _on_depsgraph(scene, depsgraph):
    from . import onion
    from . import com
    if baking or onion.busy() or com.busy():
        return
    st = settings(scene)
    if st is None:
        return
    if st.show_onion or st.show_com:
        global _dg_frame
        # a mesh edited (Edit Mode, weight paint, normals flipped...): its ghost shape is made again
        edited = {u.id.original.name for u in depsgraph.updates
                  if isinstance(u.id, bpy.types.Mesh) and u.is_updated_geometry}
        if edited:
            onion.meshes_changed(edited)
        f = scene.frame_current
        playing = bpy.context.screen is not None and bpy.context.screen.is_animation_playing
        if not playing and f == _dg_frame and not modal_running:
            # (a frame change also updates the objects: only changes on the same frame count)
            if any(isinstance(u.id, bpy.types.Object) and (u.is_updated_transform or u.is_updated_geometry)
                   for u in depsgraph.updates):
                onion.changed()
                com.changed()
        _dg_frame = f
    global _dg_frame_all
    same_frame = scene.frame_current == _dg_frame_all
    _dg_frame_all = scene.frame_current
    if not caches or not st.auto_update:
        # no path: the ghosts and the center of mass still follow the animation
        if st.show_onion or st.show_com:
            if any(isinstance(u.id, bpy.types.Action) for u in depsgraph.updates):
                onion.changed()
                com.changed()
        return
    for u in depsgraph.updates:
        if isinstance(u.id, bpy.types.Action):
            try:
                from . import ops_fmods
                ops_fmods.sync_active(bpy.context)     # modifier stack entries stay alike
            except Exception:
                pass
            tag_redraw()        # the path follows right away (e.g. a modifier's settings)
            schedule_rebake()
            onion.changed()
            com.changed()
            return
    # no curve changed, but an object did (on the same frame, not while dragging or playing): a
    # value a driver reads, an NLA strip, a constraint's setting may have changed - checked
    # without the curves (cheap)
    if same_frame and not modal_running and not (bpy.context.screen is not None and
                                                 bpy.context.screen.is_animation_playing) and \
            any(isinstance(u.id, bpy.types.Object) for u in depsgraph.updates):
        schedule_rebake(curves=False)


# Settings that only say how the paths are shown / edited (like Blender's overlays): Ctrl+Z
# doesn't change them (the ghosts don't go off, the edit mode doesn't jump back), it goes to the
# edits. What is data stays with undo: the path list, the selection, the ranges, the masses.
_UNDONE_SETTINGS = {"rna_type", "name", "paths", "picked_handles", "select_history", "select_order", "com_limb_share",
                    "com_limb_mass", "com_own_share", "com_own_mass", "com_body_mass", "range_mode", "frame_start", "frame_end"}
VIEW_SETTINGS = set()       # filled in register(): every other setting of scene.emp
_view_before = {}


def _view_now(st):
    out = {}
    for name in VIEW_SETTINGS:
        try:
            v = getattr(st, name)
        except AttributeError:
            continue
        out[name] = tuple(v) if hasattr(v, "__len__") and not isinstance(v, str) else v
    return out


@persistent
def _before_undo(*_args):
    st = settings()
    _view_before.clear()
    if st is not None:
        _view_before.update(_view_now(st))


def _keep_view():
    """The view settings back as they were before the undo / redo."""
    st = settings()
    if st is None or not _view_before:
        return
    now = _view_now(st)
    for name, v in _view_before.items():
        if now.get(name) != v:
            try:
                setattr(st, name, v)
            except (AttributeError, TypeError, ValueError):
                pass
    _view_before.clear()


@persistent
def _on_undo(*_args):
    global _job
    from . import com, onion
    try:
        _keep_view()
    except Exception:
        pass
    onion.changed()
    com.changed()
    # (no need to mark the paths dirty: a path rebuilds when its curves differ from what it was
    # built from; hidden keys and value channels are taken over below)
    try:
        sync_caches()
    except Exception:
        pass
    tag_redraw()
    st = settings()
    if st is not None and st.auto_update and caches:
        # a re-bake half done was for the animation before the undo: it starts over
        restart = _job is not None
        _job = None
        schedule_rebake(0.2, force=restart)


@persistent
def _on_load(*_args):
    global _bake_timer_on, _job, modal_running
    from . import onion
    onion.clear_all()
    from . import com
    com.clear()
    # (loading a file drops the timers that aren't persistent: their flags start over)
    _bake_timer_on = False
    _job = None
    modal_running = 0
    onion._timer_on = False
    com._timer_on = False
    com._flash[2] = False
    caches.clear()
    ghosts.clear()
    _action_hashes.clear()
    st = settings()
    if st is not None and st.paths:
        bpy.app.timers.register(_load_bake, first_interval=0.2)


def _load_bake():
    # (in steps, like the automatic re-bake: a heavy file opens without a freeze)
    schedule_rebake(0.01, force=True)
    return None


def select_history(scene=None):
    """[(path key, frame)] of clicked keys, oldest first (Merge At First / Last). It lives in the
    scene (undo restores it)."""
    global _history_cache
    st = settings(scene)
    text = st.select_history if st is not None else ""
    if text != _history_cache[0]:
        import json
        try:
            items = [((o, b, m), float(t)) for o, b, m, t in json.loads(text or "[]")]
        except (ValueError, TypeError):
            items = []
        _history_cache = (text, items)
    return _history_cache[1]


def get_active(scene=None):
    """(path key, frame) of the active (last clicked) key, or None."""
    h = select_history(scene)
    return h[-1] if h else None


def set_active(path_key, t, scene=None):
    """The active key: drawn with a white ring, used by Merge, Copy, pivot Active Element. Its
    path becomes the active one in the path list (the modifier stack, the charts show it), like
    a clicked vertex makes its object active in Blender's multi-object Edit Mode."""
    import json
    st = settings(scene)
    if st is None:
        return
    item = (tuple(path_key), round(t, 3))
    items = [i for i in select_history(scene) if i != item] + [item]
    st.select_history = json.dumps([[k[0], k[1], k[2], f] for k, f in items[-100:]])
    for i, e in enumerate(st.paths):
        if e.object is not None and (e.object.name, e.bone_name) == tuple(path_key[:2]):
            if st.active_index != i:
                st.active_index = i
            break


_order_cache = (None, [])


def selection_order(scene=None):
    """[path key] in the order the paths got selected keys (clicked, boxed, A...), oldest first."""
    global _order_cache
    st = settings(scene)
    text = st.select_order if st is not None else ""
    if text != _order_cache[0]:
        import json
        try:
            items = [tuple(k) for k in json.loads(text or "[]")]
        except (ValueError, TypeError):
            items = []
        _order_cache = (text, items)
    return _order_cache[1]


def note_selected(path_key, scene=None):
    """A key of this path got selected: the path joins the selection order (if it isn't in it)."""
    order = selection_order(scene)
    if tuple(path_key) in order:
        return
    import json
    st = settings(scene)
    if st is not None:
        st.select_order = json.dumps([list(k) for k in order + [tuple(path_key)]][-64:])


def forget_selected(path_keys, scene=None):
    """These paths have no selected keys any more: out of the selection order."""
    order = selection_order(scene)
    drop = {tuple(k) for k in path_keys}
    if not any(k in drop for k in order):
        return
    import json
    st = settings(scene)
    if st is not None:
        st.select_order = json.dumps([list(k) for k in order if k not in drop])


def whisker_sel(scene=None):
    """Scale handles picked by clicking their whiskers: {(path key, frame, channel, 'L' / 'R')}.
    They live in the scene, so undo brings the picking back."""
    import json
    st = settings(scene)
    try:
        return {((o, b, m), round(float(t), 3), c, s)
                for o, b, m, t, c, s in json.loads((st.picked_handles if st is not None else "") or "[]")}
    except (ValueError, TypeError):
        return set()


def set_whisker_sel(sel, scene=None):
    import json
    st = settings(scene)
    if st is not None:
        st.picked_handles = json.dumps(sorted([k[0], k[1], k[2], t, c, s] for k, t, c, s in sel))


def flash_message(text, seconds=3.0):
    global flash
    flash = (text, time.time() + seconds)
    tag_redraw()


_isolated_last = None


def _watch_isolated(st):
    """The path list's Graph Editor buttons show whether curves are isolated: redraw the
    sidebar when that changes elsewhere (e.g. curves shown again in the Graph Editor)."""
    global _isolated_last
    from .ops_tools import graph_isolated
    now = tuple(graph_isolated(e) for e in st.paths)
    if now != _isolated_last:
        _isolated_last = now
        for w in bpy.context.window_manager.windows:
            for a in w.screen.areas:
                if a.type == 'VIEW_3D':
                    for r in a.regions:
                        if r.type == 'UI':
                            r.tag_redraw()


def reconcile_modal():
    """The 'editing' counter says one of our modal tools runs, but none does (it stopped on an
    error): set it back, or the paths would never re-bake again."""
    global modal_running, hud, constraint, prop_circle, snap_point
    if not modal_running:
        return
    wm = bpy.context.window_manager
    if wm is None:
        return
    for win in wm.windows:
        if any(op.bl_idname.startswith("EMP_OT_") for op in getattr(win, "modal_operators", ())):
            return
    modal_running = 0
    ghosts.clear()
    hud = constraint = prop_circle = snap_point = None
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == 'VIEW_3D':
                area.header_text_set(None)
    tag_redraw()


def _watch_active():
    """Auto edit mode: when the active bone/object has a path but nothing to edit in the
    current mode (e.g. only location is animated while editing Rotation), switch modes."""
    global _watch_last
    try:
        reconcile_modal()
        st = settings()
        if st is None or not st.paths or modal_running:
            return 0.25
        try:
            _watch_isolated(st)
        except Exception:
            pass
        vl = bpy.context.view_layer
        obj = vl.objects.active if vl is not None else None
        if obj is None:
            return 0.25
        bone = ""
        if obj.type == 'ARMATURE' and obj.mode == 'POSE' and obj.data.bones.active is not None:
            bone = obj.data.bones.active.name
        key = (obj.name, bone)
        if key == _watch_last:
            return 0.25
        _watch_last = key
        if not any(e.object == obj and e.bone_name == bone for e in st.paths):
            return 0.25
        from . import core
        _o, owner = core.resolve(obj.name, bone)
        if owner is None:
            return 0.25
        tg = core.Target(obj, owner, bool(bone))
        has_loc = tg.loc.any() and not core._lock_state(tg, core.LOCATION)[0]
        has_rot = tg.rot.any() and not core._lock_state(tg, core.ROTATION)[0]
        mode = st.edit_mode
        new = None
        if mode == 'ROTATION' and has_loc and not has_rot:
            new = 'LOCATION'
        elif mode == 'LOCATION' and has_rot and not has_loc:
            new = 'ROTATION'
        if new:
            st.edit_mode = new
            flash_message("Switched to %s: %s has no %s keys" % (
                new.title(), bone or obj.name, mode.lower()))
    except Exception:
        pass
    return 0.25


def register():
    global _bake_timer_on, modal_running, _job
    # (enabled again in the same session: the module is the same, its timers are gone)
    _bake_timer_on = False
    _job = None
    modal_running = 0
    from . import com, onion
    onion._timer_on = False
    com._timer_on = False
    com._flash[2] = False
    from .props import EMP_Settings
    VIEW_SETTINGS.clear()
    VIEW_SETTINGS.update(p.identifier for p in EMP_Settings.bl_rna.properties
                         if p.identifier not in _UNDONE_SETTINGS and not p.is_readonly)
    bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph)
    bpy.app.handlers.undo_pre.append(_before_undo)
    bpy.app.handlers.redo_pre.append(_before_undo)
    bpy.app.handlers.undo_post.append(_on_undo)
    bpy.app.handlers.redo_post.append(_on_undo)
    bpy.app.handlers.load_post.append(_on_load)
    bpy.app.timers.register(_load_bake, first_interval=0.5)
    bpy.app.timers.register(_watch_active, first_interval=1.0, persistent=True)


def unregister():
    for lst, fn in ((bpy.app.handlers.depsgraph_update_post, _on_depsgraph),
                    (bpy.app.handlers.undo_post, _on_undo),
                    (bpy.app.handlers.undo_pre, _before_undo),
                    (bpy.app.handlers.redo_pre, _before_undo),
                    (bpy.app.handlers.redo_post, _on_undo),
                    (bpy.app.handlers.load_post, _on_load)):
        if fn in lst:
            lst.remove(fn)
    if bpy.app.timers.is_registered(_bake_timer):
        bpy.app.timers.unregister(_bake_timer)
    if bpy.app.timers.is_registered(_load_bake):
        bpy.app.timers.unregister(_load_bake)
    if bpy.app.timers.is_registered(_watch_active):
        bpy.app.timers.unregister(_watch_active)
    from . import com, onion
    for fn in (onion._tick, com._tick):
        if bpy.app.timers.is_registered(fn):
            bpy.app.timers.unregister(fn)
    caches.clear()
