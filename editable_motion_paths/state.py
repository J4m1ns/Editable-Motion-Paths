"""Runtime state (not saved in the .blend) and change tracking."""

import re
import time

import bpy
from bpy.app.handlers import persistent

from . import compat

caches = {}          # (obj_name, bone_name, type) -> core.PathCache
baking = False       # True while frame-stepping for a bake
modal_running = 0    # >0 while one of our modal operators is editing
hud = None           # (text, (x, y)[, 'GRAPH': above the speed graph instead]) shown next to the cursor
hud_area = 0         # as_pointer() of the area showing the HUD
box = None           # (x0, y0, x1, y1, mode) box-select rectangle in region space
box_area = 0
circle = None        # (x, y, radius, area pointer) circle select brush
chart = None         # timing chart geometry of the last draw (see draw._draw_chart)
speed_graph = None   # where the speed graph is and what it shows (for dragging it), see draw
speed_hold = None    # while the speed graph is dragged: {cache, lo, hi, vmax} it keeps
speed_hot = None     # (path key, 'KEY' / 'EASE', frame / segment) under the mouse on the graph
path_hot = None      # (path key, frame, part) of the key / handle / gizmo part under the mouse (lit up)
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
# what every path's parent spaces were last baked from in full: {object name: (its hashes, {(data_path, index):
# (fc_parts, mute, modifiers)})} - an edit of curves is baked again only on the frames it changed (None: in full)
partial_base = [None]
_bake_memo = []      # [(animation signature, {path key: its parent spaces...})] - the last bakes
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


mode_force = [None]     # (the Python API's mode for one call: every path in it, as before paths had their own)
helper_caches = {}      # (object, bone, ROTATION) -> PathCache: hidden ones, the bones tracked vertices follow


def all_caches():
    """The paths' caches and the hidden ones of the tracked vertices' bones (what a bake bakes)."""
    return list(caches.values()) + list(helper_caches.values())


def entry_mode(entry):
    """The mode a path is shown and edited in: its own (pinned in the path list) or the one picked at the top."""
    if mode_force[0]:
        return mode_force[0]
    pin = getattr(entry, "mode_pin", 'HEADER')
    return pin if pin != 'HEADER' else edit_mode(entry.id_data)


def mode_in_use(mode, scene=None):
    """Is this mode the one at the top, or the own mode of a path being edited?"""
    if edit_mode(scene) == mode:
        return True
    return any(c.ptype == mode for c in editable_caches(scene))


def entry_key(entry, mode=None):
    if entry.object is None:
        return None
    return (entry.object.name, entry.bone_name, mode or entry_mode(entry))


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
    targets = set()
    wanted = []
    for e in st.paths:
        k = entry_key(e)        # (in its own mode, or the one at the top)
        if k is not None:
            targets.add(k[:2])
            wanted.append(k)
    for k in list(caches):
        if k[:2] not in targets:
            del caches[k]
    need = []
    # (the bones the tracked vertices follow: hidden caches - no path of their own needed)
    try:
        from . import ops_vertex
        want = {(o, b, core.ROTATION) for o, b in ops_vertex.helper_keys(st)}
    except Exception:
        want = set()
    # (and the animated bones / objects the paths hang on rigidly, without a path of their own: an edit of them -
    # in the Graph Editor too - moves the paths below at once, see live.py)
    try:
        from . import live
        wanted_set = set(wanted)
        for o, b in live.helper_keys(st):
            if (o, b, core.ROTATION) in wanted_set or (o, b, core.SCALE) in wanted_set or \
                    ((o, b, core.LOCATION) in wanted_set and not _own_loc_space(o, b)):
                continue
            want.add((o, b, core.ROTATION))
    except Exception:
        pass
    for k in list(helper_caches):
        if k not in want:
            del helper_caches[k]
    for k in want:
        if k not in helper_caches and k not in caches:
            hc = helper_caches[k] = core.PathCache(*k)
            need.append(hc)
    refs = {entry_key(e): (e.value_path, e.value_index) for e in st.paths if e.object is not None}
    hidden = {entry_key(e): parse_frames(e.hidden_keys) for e in st.paths if e.object is not None}
    for k in wanted:
        c = caches.get(k)
        if c is None:
            c = caches[k] = core.PathCache(*k)
            # (a bone whose location has its own axes, a bone / object turned by its constraints: its location
            # path's parent space is another)
            own = _own_loc_space(k[0], k[1])
            other = next((o for o in caches.values() if o is not c and o.key[:2] == k[:2] and o.B
                          and (not own or (o.ptype == core.LOCATION) == (k[2] == core.LOCATION))), None)
            if other is not None:
                c.fs, c.B, c.bake_id = other.fs, other.B, other.bake_id
                c.Cam, c.cam_fs, c.cam_obj = other.Cam, other.cam_fs, other.cam_obj
                c.Ref, c.ref_fs, c.ref_key = other.Ref, other.ref_fs, other.ref_key
        h = hidden.get(k, set())
        if h != c.hidden:
            c.hidden = h
            c.dirty = True
        if k[2] == core.VALUE:
            ref = refs.get(k)
            if not (ref and ref[0]):
                # nothing picked yet: the first animated value of the bone / object
                e = next((e for e in st.paths if entry_key(e) == k), None)
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
    global _hidden_changes
    out = []
    seen = set()
    for e in st.paths:
        if not e.show or (st.only_selected and not _entry_selected(e)):
            continue
        k = entry_key(e)
        if k is None or k in seen:
            continue        # (no object: its object was deleted - nothing to calculate)
        seen.add(k)
        c = caches.get(k)
        if c is not None and c.B:
            out.append(c)
        elif c is None or not c.failed:
            # new / renamed entry: calculate it soon (never from inside drawing)
            schedule_rebake(0.1, keep=True)
    if out and _hidden_changes:
        # (something changed while no path was shown: what was left out is caught up with now)
        _hidden_changes = False
        schedule_rebake(0.1, keep=True)
    return out


_hidden_changes = False     # the scene changed while no path was shown (not tracked then)


def _entry_selected(e):
    """Is the path's bone / object selected (Only Selected)?"""
    obj = e.object
    if obj is None:
        return False
    try:
        if e.bone_name:
            pb = obj.pose.bones.get(e.bone_name) if obj.pose is not None else None
            return pb is not None and compat.bone_selected(pb)
        return obj.select_get()
    except (RuntimeError, ReferenceError, AttributeError):
        return False


def paths_shown(scene=None):
    """Is any path drawn now? (None shown: changes aren't tracked - Blender isn't slowed down by
    paths nobody sees; they are brought up to date when one shows again.)"""
    st = settings(scene)
    if st is None or overlays_hidden():
        return False
    for e in st.paths:
        if e.show and e.object is not None and (not st.only_selected or _entry_selected(e)):
            try:
                if e.object.visible_get():
                    return True
            except (RuntimeError, ReferenceError):
                pass
    if helper_caches and any(e.show and getattr(e, "auto_update", True) for e in st.vertex_paths):
        return True     # (a tracked vertex that follows the animation)
    return False


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
    global _job
    from . import core
    scene = scene or bpy.context.scene
    need = sync_caches(scene)
    targets = list(caches.values()) if only is None else only
    if only == 'MISSING':
        targets = need
    if only is None:
        _job = None     # (a bake going on in the background: this one is newer)
    core.bake(scene, targets)
    if _job is None:
        _aux_drop(_BAKE_SCENE)
    tag_redraw()


# ----------------------------------------------------------------------------
# Auto update: re-bake when animation other than a path's own channels changed
# ----------------------------------------------------------------------------

def _fcurve_hashes(obj, curves=True, parts=None):
    """{(data path, index): hash} of what moves obj on the frames: its F-Curves (curves=False:
    left out, they are what costs), and under special keys ('#...') its NLA, drivers (with the
    values they read now) and constraints. parts: a dict filled with each curve's keys (see
    changed_spans)."""
    out = {}
    # an armature's bones the paths don't depend on are left out: on a big rig with a dense animation
    # going through all of them took a tenth of a second after every click
    near = _pose_owners(obj)
    names = {pb.name for pb in near} if near else (set() if near is not None else None)

    def relevant(dp):
        if names is None:
            return True
        b = _bone_of(dp)
        return b is None or b in names
    coll = compat.get_fcurves(obj) if curves else None
    from .core import _fmod_signature, fc_parts, fc_shape
    for fc in coll if coll is not None else ():
        if not relevant(fc.data_path):
            continue
        # (the interpolation and the modifiers' settings too: they change the curve as much)
        p = fc_parts(fc)
        mods = tuple(_fmod_signature(m) for m in fc.modifiers)
        out[(fc.data_path, fc.array_index)] = hash((b"|".join(p), fc.mute, mods))
        if parts is not None:
            parts[(fc.data_path, fc.array_index)] = (p, fc.mute, mods)
    ad = obj.animation_data
    if ad is not None:
        out[("#nla", 0)] = hash((ad.action_blend_type, round(ad.action_influence, 6), ad.action_extrapolation,
                                 ad.use_nla, tuple((t.mute, t.is_solo, tuple(_strip_sig(s) for s in t.strips))
                                                   for t in ad.nla_tracks)))
    drv = []
    for idb in (obj, getattr(obj, "data", None)):
        dad = getattr(idb, "animation_data", None)
        for d in dad.drivers if dad is not None else ():
            if idb is obj and not relevant(d.data_path):
                continue
            drv.append((d.data_path, d.array_index, d.mute, d.driver.type, d.driver.expression, fc_shape(d),
                        tuple(_fmod_signature(m) for m in d.modifiers),
                        tuple((v.type, tuple((t.id.name if t.id else None, t.data_path, t.bone_target,
                                              t.transform_type, t.transform_space, _target_value(t))
                                             for t in v.targets)) for v in d.driver.variables)))
    if drv:
        out[("#drivers", 0)] = hash(tuple(drv))
    owners = [obj] + (list(obj.pose.bones) if obj.type == 'ARMATURE' and obj.pose is not None else [])
    # transforms with no key and no driver stay as they are on every frame: moving one (a parent
    # whose keys were removed) moves the paths hanging on it for the whole animation
    # (keyed / driven ones are left out: {owner name ('' = the object): {(prop, index)}})
    fixed = {}
    paths = [(fc.data_path, fc.array_index) for fc in (compat.get_fcurves(obj) or ()) if relevant(fc.data_path)]
    if obj.animation_data is not None:
        paths += [(d.data_path, d.array_index) for d in obj.animation_data.drivers if relevant(d.data_path)]
    for dp, i in paths:
        m = _BONE_PATH.match(dp)
        name, prop = (m.group(1).replace('\\"', '"').replace('\\\\', '\\'), m.group(2)) if m else ("", dp)
        if prop in _TRANSFORM_PROPS:
            fixed.setdefault(name, set()).add((prop, i))
    free = []
    if near is not None:
        owners = [obj] + near
    for ow in owners:
        skip = fixed.get(ow.name if ow is not obj else "", ())
        for prop in _TRANSFORM_PROPS:
            v = getattr(ow, prop)
            free.append(tuple(v) if not skip else tuple(x for i, x in enumerate(v) if (prop, i) not in skip))
    out[("#pose", 0)] = hash(tuple(free))
    # (a constraint setting a driver or a key sets is the rig's output, not a setting: its driver /
    # curve is above already - its value can differ from one evaluation of the scene to the next)
    driven = set()
    for dp, i in paths:
        m = _CON_PATH.match(dp)
        if m:
            driven.add((_unescape(m.group(1) or ""), _unescape(m.group(2)), m.group(3)))
    cons = tuple((ow.name, tuple(_constraint_sig(c, driven, ow.name if ow is not obj else "") for c in ow.constraints))
                 for ow in owners if len(ow.constraints))
    if cons:
        out[("#constraints", 0)] = hash(cons)
    return out


def _bone_of(dp):
    """The bone a data path is on ('pose.bones["X"]...' -> 'X'), or None (no regular expression:
    it runs for every curve of a rig)."""
    if not dp.startswith('pose.bones["'):
        return None
    j = dp.find('"]', 12)
    while j > 12 and dp[j - 1] == '\\' and dp[j - 2] != '\\':     # (an escaped quote in the name)
        j = dp.find('"]', j + 1)
    return _unescape(dp[12:j]) if j >= 12 else None


def _pose_owners(obj):
    """The bones of an armature whose animation can move its paths: the paths' bones, what they
    hang from, what their constraints follow (an Armature constraint's every target too) and what
    the drivers of their transforms / constraints read (a bone's transform, a property on a bone) -
    with all of that's own. A control moved by hand elsewhere on the rig moves no path. None: all
    of them (no path is on one of its bones, but something else hangs on it)."""
    if obj.type != 'ARMATURE' or obj.pose is None:
        return []
    names = {c.bone_name for c in all_caches() if c.obj_name == obj.name and c.bone_name}
    # (a bone some path is drawn Relative To: its motion is in that path)
    names |= {b for o, b in relative_refs() if o == obj and b}
    if not names:
        return None
    # (and the bones other paths' objects hang on: an object with a path on a bone of a rig that has a path of its
    # own - before only that one's bones were looked at, an edit of the object's bone went unseen)
    more = _bones_others_use(obj)
    if more is None:
        return None
    names |= more
    bones = obj.pose.bones
    reads = {}      # bone -> the bones its drivers read
    ad = obj.animation_data
    for d in (ad.drivers if ad is not None else ()):
        b = _bone_of(d.data_path)
        if b is None:
            continue
        got = reads.setdefault(b, set())
        for v in d.driver.variables:
            for t in v.targets:
                if t.id != obj:
                    continue        # (another object: tracked as an object of its own)
                if t.bone_target:
                    got.add(t.bone_target)
                bt = _bone_of(t.data_path or "")
                if bt is not None:
                    got.add(bt)
    out = {}
    todo = [bones.get(n) for n in names]
    while todo:
        pb = todo.pop()
        if pb is None or pb.name in out:
            continue
        out[pb.name] = pb
        todo += list(pb.parent_recursive)
        for con in pb.constraints:
            subs = [(getattr(con, "target", None), getattr(con, "subtarget", "")),
                    (getattr(con, "pole_target", None), getattr(con, "pole_subtarget", ""))]
            subs += [(t.target, t.subtarget) for t in getattr(con, "targets", ())]     # (Armature)
            for target, name in subs:
                if name and target == obj:
                    todo.append(bones.get(name))
        todo += [bones.get(n) for n in reads.get(pb.name, ())]
    return list(out.values())


def _bones_others_use(arm):
    """The bones of this armature that the other paths' objects (their parents, constraints' targets, drivers'
    variables, all the way up), a Relative To reference object or the camera of Camera space hang on - None: it
    may be any of them (a mesh it bends, an object it parents as a whole by its bones)."""
    starts = [bpy.data.objects.get(n) for n in {c.obj_name for c in all_caches()} if n != arm.name]
    starts += [o for o, _b in relative_refs() if o != arm]
    cam = bpy.context.scene.camera if bpy.context.scene is not None else None
    if cam is not None and any(c.cam is not None for c in caches.values()):
        starts.append(cam)
    out, seen = set(), set()
    todo = [o for o in starts if o is not None]
    while todo:
        o = todo.pop()
        if o is None or o == arm or o.name in seen:
            continue
        seen.add(o.name)
        if o.parent is not None:
            if o.parent == arm:
                if o.parent_type == 'BONE':
                    out.add(o.parent_bone)
                elif o.parent_type == 'ARMATURE':
                    return None
            else:
                todo.append(o.parent)
        owners = [o] + (list(o.pose.bones) if o.type == 'ARMATURE' and o.pose is not None else [])
        for ow in owners:
            for con in ow.constraints:
                pairs = [(getattr(con, "target", None), getattr(con, "subtarget", "")),
                         (getattr(con, "pole_target", None), getattr(con, "pole_subtarget", ""))]
                pairs += [(t.target, t.subtarget) for t in getattr(con, "targets", ())]
                for t, sub in pairs:
                    if t == arm:
                        if sub:
                            out.add(sub)
                    elif isinstance(t, bpy.types.Object):
                        todo.append(t)
        for m in getattr(o, "modifiers", ()):
            if m.type == 'ARMATURE' and m.object == arm:
                return None
            if m.type == 'HOOK' and m.object == arm and m.subtarget:
                out.add(m.subtarget)
        for idb in (o, getattr(o, "data", None)):
            ad = getattr(idb, "animation_data", None)
            for d in ad.drivers if ad is not None else ():
                for v in d.driver.variables:
                    for t in v.targets:
                        if t.id == arm:
                            if t.bone_target:
                                out.add(t.bone_target)
                            b = _bone_of(t.data_path or "")
                            if b is not None:
                                out.add(b)
                        elif isinstance(t.id, bpy.types.Object):
                            todo.append(t.id)
    return {b for b in out if b in arm.pose.bones}


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


_CON_PATH = re.compile(r'^(?:pose\.bones\["((?:[^"\\]|\\.)*)"\]\.)?constraints\["((?:[^"\\]|\\.)*)"\]\.(\w+)$')


def _unescape(name):
    return name.replace('\\"', '"').replace('\\\\', '\\')


def _constraint_sig(con, driven=(), owner=""):
    """A constraint's settings (its limits, targets, influence...) - but those driven or keyed
    (driven: {(owner name, constraint name, setting)}; owner '' is the object)."""
    out = [con.type]
    for ident, kind in _constraint_props(con):
        if driven and (owner, con.name, ident) in driven:
            continue
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
        # (an armature with paths: only the bones they depend on - see _pose_owners)
        near = _pose_owners(o) if o.type == 'ARMATURE' else []
        names = None if near is None else {pb.name for pb in near}
        owners = [o] + (list(o.pose.bones) if near is None and o.pose is not None else near)
        for ow in owners:
            for con in ow.constraints:
                refs.append(getattr(con, "target", None))
                refs.append(getattr(con, "pole_target", None))
                for t in getattr(con, "targets", ()):
                    refs.append(getattr(t, "target", None))
        for idb in (o, getattr(o, "data", None)):
            ad = getattr(idb, "animation_data", None)
            for d in ad.drivers if ad is not None else ():
                if idb is o and names is not None:
                    b = _bone_of(d.data_path)
                    if b is not None and b not in names:
                        continue
                for v in d.driver.variables:
                    refs += [t.id for t in v.targets]
        todo += [r for r in refs if isinstance(r, bpy.types.Object)]
    return out


def _tracked_objects():
    objs = {}
    for name in {c.obj_name for c in all_caches()}:
        obj = bpy.data.objects.get(name)
        if obj is not None:
            objs.update(_depends_on(obj))
    cam = bpy.context.scene.camera if bpy.context.scene is not None else None
    if cam is not None and any(c.cam is not None for c in caches.values()):
        objs.update(_depends_on(cam))       # (Camera space: the camera's motion is in the paths)
    for o, _b in relative_refs():
        objs.update(_depends_on(o))         # (Relative To: the reference's motion is in the paths)
    return objs


def relative_refs(scene=None):
    """[(object, bone name)] the paths are drawn Relative To."""
    st = settings(scene)
    out = []
    for e in (st.paths if st is not None else ()):
        if getattr(e, "relative", False) and e.rel_object is not None and e.object is not None:
            k = (e.rel_object, e.rel_bone)
            if k not in out:
                out.append(k)
    return out


def anim_signature(scene, hashes=None):
    """What the paths' parent spaces are baked from, as one number: every curve, NLA, driver and
    constraint that moves them (hashes: {object name: hashes} already worked out), and the range."""
    from . import core
    st = settings(scene)
    if hashes is None:
        hashes = {name: _fcurve_hashes(obj) for name, obj in _tracked_objects().items()}
    cs = [c for c in caches.values()]
    rng = core.bake_range(scene, st, cs) if st is not None and cs else None
    return hash((tuple(sorted((n, tuple(sorted(h.items()))) for n, h in hashes.items())), rng,
                 scene.camera.name if scene.camera is not None else ""))


def remember_bake(sig, baked):
    """A bake done: kept (the last few) by what it was baked from - Ctrl+Z back to that animation
    takes them again at once, nothing baked anew."""
    global _bake_memo
    _bake_memo = [m for m in _bake_memo if m[0] != sig][-7:] + [(sig, baked)]


def _bake_from_memo(scene):
    """After an undo: the parent spaces baked for this very animation before, if they were.
    True when every path got them."""
    if not _bake_memo or not caches:
        return False
    sig = anim_signature(scene)
    got = next((b for s, b in _bake_memo if s == sig), None)
    if got is None:
        return False
    done = True
    from . import live
    live.clear()
    for k, c in list(caches.items()) + list(helper_caches.items()):
        m = got.get(k)
        if m is None:
            done = False
            continue
        if c.B is m[1] and c.fs == m[0]:
            continue            # (the very bake it has: nothing to take again)
        c.fs, c.B, c.Cam, c.cam_fs, c.cam_obj = m
        c.bake_id += 1
        c.dirty = True
        c.failed = False
    if done:
        base = remember_action_hashes(parts=True)
        # (the paths are this animation's bake again - but the memo has no Relative To references: those
        # could be another one's)
        partial_base[0] = base if not relative_refs() else None
        live.snapshot()
    else:
        partial_base[0] = None
    return done


def remember_action_hashes(parts=False):
    """The hashes of what moves the paths now (see _needs_rebake). parts: also returned with each curve's keys,
    by object - what the bake starting now is made from (see changed_spans)."""
    _action_hashes.clear()
    base = {} if parts else None
    for name, obj in _tracked_objects().items():
        p = {} if parts else None
        _action_hashes[name] = _fcurve_hashes(obj, parts=p)
        if parts:
            base[name] = (_action_hashes[name], p)
    return base


_PHYSICS_MODS = {'CLOTH', 'SOFT_BODY', 'DYNAMIC_PAINT', 'FLUID', 'COLLISION', 'PARTICLE_SYSTEM'}


def changed_spans(scene=None, owners=None, only=None, memo=None):
    """The frames an edit of the curves changed since the last full bake: [(start, end)] (None ends: up to the
    range's own), [] when nothing changed but the last digits - or None when only a full bake will do: something
    else changed too (NLA, drivers, constraints, a pose without keys, the objects followed), a curve's
    modifiers or extrapolation, a curve came or went, an action is edited in the NLA (its time isn't the
    scene's) or a simulation reads the frames before (a change goes on to every frame after it).
    owners: a set filled with the (object name, bone name or '') whose curves changed. only: such a set - only
    their curves looked at (a drag going on: the rest is as its first look found it, see _drag_tick)."""
    from . import core
    base = partial_base[0]
    if base is None:
        return None
    objs = _tracked_objects()
    if set(objs) != set(base):
        return None
    if scene is not None and getattr(scene, "rigidbody_world", None) is not None and \
            any(getattr(o, "rigid_body", None) is not None for o in objs.values()):
        return None
    spans = []
    memo = {} if memo is None else memo
    for name, obj in objs.items():
        ad = obj.animation_data
        if ad is not None and ad.use_tweak_mode:
            return None
        if any(m.type in _PHYSICS_MODS for m in getattr(obj, "modifiers", ())):
            return None
        old_h, old_p = base[name]
        new_p = {}
        if only is not None:
            bones = {b for o, b in only if o == name}
            if not bones:
                continue
            new_h = _owner_hashes(obj, bones, new_p, memo)
            keys = [k for k in new_h if k in old_h]     # (one not looked at before: the bake after the drag)
        else:
            new_h = _fcurve_hashes(obj, parts=new_p)
            keys = set(old_h) | set(new_h)
        for k in keys:
            if old_h.get(k) == new_h.get(k):
                continue
            if owners is not None and not k[0].startswith("#"):
                owners.add((name, _bone_of(k[0]) or ""))
            if not k[0].startswith("#") and ((name, k) in _ignored or _harmless(obj, k[0], memo)):
                continue        # (a path's own channel - its parent space doesn't change - or one not checked)
            if k[0].startswith("#") or k not in old_p or k not in new_p:
                return None
            (op, om, omods), (np_, nm, nmods) = old_p[k], new_p[k]
            if om != nm or omods != nmods or nmods:
                return None
            span = core.curve_change_span(op, np_)
            if span is None:
                return None
            if span:
                spans.append(span)
    return spans


def _owner_hashes(obj, bones, parts, memo):
    """_fcurve_hashes of these bones' curves only ('': the object's own), without the '#' ones."""
    from .core import _fmod_signature, fc_parts
    coll = compat.get_fcurves(obj)
    if coll is None:
        return {}
    key = ("curves of", obj.name, frozenset(bones))
    paths = memo.get(key)
    if paths is None:
        paths = memo[key] = [(fc.data_path, fc.array_index) for fc in coll if (_bone_of(fc.data_path) or "") in bones]
    out = {}
    for dp, i in paths:
        fc = coll.find(dp, index=i)
        if fc is None:
            continue
        p = fc_parts(fc)
        mods = tuple(_fmod_signature(m) for m in fc.modifiers)
        out[(dp, i)] = hash((b"|".join(p), fc.mute, mods))
        parts[(dp, i)] = (p, fc.mute, mods)
    return out


_TRANSFORM_PROPS = {"location", "rotation_quaternion", "rotation_euler", "rotation_axis_angle", "scale"}
_BONE_PATH = re.compile(r'^pose\.bones\["(.*)"\]\.(\w+)$')
_LINEAR_CONSTRAINTS = {'CHILD_OF', 'ARMATURE'}   # (they only put a parent space in front)


def _own_loc_space(obj_name, bone_name):
    """The bone / object moves by its location along other axes than its rotation turns it (see
    core.own_loc_map): Local Location off and the like, or constraints that turn it (Track To)."""
    from . import core
    obj = bpy.data.objects.get(obj_name)
    if obj is None:
        return False
    if not bone_name:
        return core.own_loc_map(obj, "")
    pb = obj.pose.bones.get(bone_name) if obj.type == 'ARMATURE' and obj.pose is not None else None
    return pb is not None and core.own_loc_map(pb, bone_name)


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
    targets = [c for c in all_caches() if c.obj_name == obj.name]     # (the tracked vertices' bones too)
    if data_path.endswith('"]') and any(c.value_ref and c.value_ref[0] == data_path for c in targets) \
            and not _driven_by(obj, data_path):
        return True     # a custom property shown on a value track that moves nothing else
    # paths of other objects that hang on this one (a parent, Child Of on a bone, a driver...)
    memo = {} if memo is None else memo
    # what a path is drawn against - a Relative To reference, the camera of Camera space - or what moves it: its
    # own path edited (it is a path target too) moves the other path (it stayed until an Update)
    seen_from = memo.get("seen_from")
    if seen_from is None:
        movers, selves, ref_bones = set(), set(), set()
        st = settings()
        refs = list(relative_refs())
        if st is not None and bpy.context.scene.camera is not None and any(e.camera for e in st.paths):
            refs.append((bpy.context.scene.camera, ""))
        for o, b in refs:
            movers.update(n for n in _depends_on(o) if n != o.name)    # (what moves it from outside)
            if b:
                ref_bones.add((o.name, b))
            else:
                selves.add(o.name)
        seen_from = memo["seen_from"] = (movers, selves, ref_bones)
    movers, selves, ref_bones = seen_from
    bone_channel = data_path.startswith("pose.bones[")
    if obj.name in movers or (obj.name in selves and not bone_channel):
        return False
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
        from . import core
        if m.group(2) == "location" and core.own_loc_space(pb.bone) and \
                any(c.bone_name == bname and c.ptype != core.LOCATION for c in targets):
            # (its location moves it by other axes than the rotation / scale paths' parent space
            # holds: those are baked again - its location path takes it at once)
            return False
        from .onion import affected_bones
        memo = {} if memo is None else memo
        moved = memo.get((obj.name, bname))
        if moved is None:
            moved = memo[(obj.name, bname)] = affected_bones(obj, {bname})
        if any(p.name in moved for p in pb.parent_recursive):
            return False    # (it moves a bone it hangs from: a neck sharing the head's turn...)
        if any(o == obj.name and (rb == bname or rb in moved) for o, rb in ref_bones):
            return False    # (a reference bone of a Relative To path, or one it moves)
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
            _why[0] = (name, None)
            return True
        keys = set(new) | set(old) if curves else {k for k in set(new) | set(old) if k[0].startswith("#")}
        for k in keys:
            if new.get(k) != old.get(k) and (name, k) not in _ignored and not _harmless(obj, k[0], memo):
                _why[0] = (name, k)
                return True
        # only own channels changed: keep hashes current so later comparisons stay precise
        if curves:
            _action_hashes[name] = new
        else:
            old.update(new)
    return False


_check_curves = True     # the next check compares the curves too (an action changed)

# Endless re-bakes: something the checks compare "changes" by itself (a value the rig writes as
# it is evaluated...) - each re-bake would set off the next one and keep Blender busy for good.
# A re-bake that moved no path counts; when the same thing set off LOOP_LIMIT of them in a row,
# each one right after the one before (a loop doesn't wait - an animator's edits come with
# pauses: an IK/FK switch flipped a few times isn't one), it isn't checked any more (until a file
# is loaded) and the console says what it was.
LOOP_LIMIT = 3
LOOP_GAP = 1.0      # seconds from one re-bake's end to the next one's start that still count as "in a row"
_why = [None]       # what the last check found changed: (object name, hash key) / 'range' / 'new' / 'force'
_ignored = set()    # what isn't checked any more
_useless = []       # the reasons of the automatic re-bakes in a row that moved no path
_last_done = [0.0]  # when the last bake ended


def _unbaked():
    """A path still to be calculated: a new one, or one that couldn't be (its bone / object was
    gone) whose bone / object is there again. One that can't be isn't tried on every check: that
    baked every path again on each click."""
    from . import core
    sync_caches()
    for c in all_caches():
        if not c.B and (not c.failed or core.resolve(c.obj_name, c.bone_name)[1] is not None):
            return True
    return False


def _note_bake(job):
    why = getattr(job, "why", None)
    if why in (None, 'new', 'force') or getattr(job, "moved", True):
        _useless.clear()
        return
    if getattr(job, "gap", 0.0) > LOOP_GAP:
        _useless.clear()        # (after a pause: an edit, not a loop - a new count starts)
    _useless.append(why)
    if len(_useless) >= LOOP_LIMIT and all(w == why for w in _useless[-LOOP_LIMIT:]):
        _useless.clear()
        _ignored.add(why)
        what = "the frame range" if why == 'range' else "%s of %s" % (
            (why[1][0].lstrip("#") if why[1] else "its animation"), why[0])
        print("Editable Motion Paths: %s kept changing by itself without moving the paths (%d updates "
              "in a row) - not checked any more, so the paths aren't calculated again and again. If "
              "it should move them, press Update next to the path list." % (what, LOOP_LIMIT))


def schedule_rebake(delay=0.35, force=False, curves=True, keep=False):
    """keep: when one is due already, it stays when it was (drawing asks for it on every redraw: it
    would never come while something redraws all the time)."""
    global _bake_due, _bake_timer_on, _force_bake, _check_curves
    if keep and _bake_timer_on:
        return
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
    scratch then (seconds), while a frame of it costs some 20-60 ms. Never one that is selected or
    active: hiding takes it out of the selection (and it is what is being worked on)."""
    try:
        vl = bpy.context.view_layer
        if o.select_get() or (vl is not None and vl.objects.active == o):
            return False
    except (RuntimeError, ReferenceError):
        pass
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
STEP_AUX = 0.04    # the same in the bake scene (nothing to put back after it: shorter steps)
QUICK_BAKE = 0.15  # a whole re-bake quicker than this is done at once (else in steps: no hitch)
QUICK_PART = 0.3   # the frames an edit changed: baked again at once when that takes no longer than this
bake_seconds = 0.0  # how long the last whole bake took (a quick one is done at once)
_job_started = 0.0  # when the job began (a long one shows how far it got)
SHOW_PROGRESS = 1.0  # a first bake going on longer than this says how far it got
_progress_shown = 0.0


def bake_running():
    return _job is not None


def _job_step(job):
    """One step of a background bake; returns how long Blender gets before the next one (as long
    as the step took: the interface keeps up while a long range is baked), or None when done."""
    t = time.time()
    budget = STEP_AUX if job.aux else max(0.02, STEP - 2.0 * getattr(job, "pace", 0.0))
    if job.window is not None and job.i < getattr(job, "wlen", 0):
        budget = max(budget, QUICK_BAKE)    # (the frames around the current one in one go: shown at once)
    if job.step(budget):
        return None
    global _progress_shown
    spent = time.time() - t
    now = time.time()
    if job.first and now - _job_started > SHOW_PROGRESS and now - _progress_shown > 0.3:
        # (only while a path isn't drawn yet - a path being updated is there already)
        _progress_shown = now
        flash_message("Calculating motion paths... %d%%" % int(100 * job.progress), 1.0)
    return max(0.02, spent)


after_blender_edit = [False]    # (Blender's own G / R / S ran since the last bake: its shift kept until re-baked)


def _job_done(job):
    global flash
    job.finish()
    # (every path now baked from the curves of the job's start: the next edit is baked on its frames only)
    partial_base[0] = job.base if job.ok_start and job.done else None
    after_blender_edit[0] = False
    try:
        from . import ops_vertex
        ops_vertex.store_points(settings())     # (the tracked vertices as they follow the animation: in the file)
    except Exception:
        pass
    try:
        from . import live
        live.snapshot()         # (what the bake was made from: children follow an edited parent at once)
    except Exception:
        pass
    _aux_drop(_BAKE_SCENE)
    if flash is not None and flash[0].startswith("Calculating motion paths"):
        flash = None
    _note_bake(job)
    _last_done[0] = time.time()
    tag_redraw()


_BAKE_SCENE = ".Motion Paths Bake"     # (core.AUX_NAME: the paths' bake scene - the snapshots have one of their own)


def _aux_drop(name=None, later=True):
    """The bake scene removed (core.drop_aux); while the playhead is dragged, once it's let go."""
    from . import core
    try:
        if core.drop_aux(name) is False and later:
            _drop_wait.add(name)
            if not bpy.app.timers.is_registered(_aux_drop_later):
                bpy.app.timers.register(_aux_drop_later, first_interval=0.3)
    except Exception:
        pass


_drop_wait = set()      # (the bake scenes put off by _aux_drop: their names, None for all)


def _aux_drop_later():
    from . import core
    if core.scrubbing():
        return 0.3
    names = list(_drop_wait)
    _drop_wait.clear()
    if _job is not None:
        return None     # (a bake started meanwhile: its end removes the scene)
    for name in (None,) if None in names else names:
        try:
            core.drop_aux(name)
        except Exception:
            pass
    return None


def bake_soon(scene=None, budget=0.15):
    """Bake every path, the first part now: on a light scene they are done before this returns
    (as with bake_all); a heavy rig or a long range goes on in steps in the background (Blender
    stays responsive), the frames around the current one first - a new path shows there at once."""
    global _job, _job_started
    from . import core
    scene = scene or bpy.context.scene
    sync_caches(scene)
    _job = core.BakeJob(scene, all_caches(), near=scene.frame_current)
    _job_started = time.time()
    if _job.step(budget):
        job, _job = _job, None
        _job_done(job)
        return True
    tag_redraw()
    schedule_rebake(0.02)       # (the rest in the background)
    return False


def _bake_timer():
    """The automatic re-bake: when something else than a path's own channels changed, the paths
    are baked again - in steps of ~40 ms (a heavy scene doesn't freeze Blender meanwhile; the old
    paths stay drawn until the new ones are ready)."""
    global _bake_timer_on, _force_bake, _job, _check_curves, _job_started, _drag_job, _drag_dirty
    remaining = _bake_due - time.time()
    # (a drag going on: not put off by each move of it - its frames are baked as it goes)
    if remaining > 0.01 and _job is None and not (_drag_dirty or _drag_job is not None):
        return remaining
    reconcile_modal()
    ctx = bpy.context
    screen = getattr(ctx, "screen", None)
    if baking or (screen is not None and screen.is_animation_playing) or render_running():
        return 0.3
    if modal_running or _any_modal():
        try:
            wait = _drag_tick()
        except Exception as ex:
            print("Editable Motion Paths: update during a drag failed:", ex)
            _drag_job, _drag_dirty, wait = None, False, None
        return 0.3 if wait is None else wait
    from . import core, live
    global _drag_used
    # (a drag over: the bake after it sets its paths right - or back)
    was_drag, _drag_job, _drag_dirty, _drag_used = _drag_used, None, False, False
    _drag_look.clear()
    try:
        if _job is not None:
            wait = _job_step(_job)
            if wait is not None:
                return wait
            job, _job = _job, None
            _job_done(job)
            # (what changed meanwhile is seen right away below: the job kept the hashes of its start)
        force = _force_bake
        _force_bake = False
        curves = _check_curves
        _check_curves = False
        st = settings()
        if st is not None and (st.paths or helper_caches or len(st.vertex_paths)):
            why = 'force' if force else 'new' if _unbaked() else \
                'range' if 'range' not in _ignored and _range_changed() else \
                _why[0] if _needs_rebake(curves) else None
            if why is not None:
                sync_caches()
                scene = bpy.context.scene
                # an edit of curves: only the frames it changed (none - the last digits only: nothing to bake)
                spans = changed_spans(scene) if isinstance(why, tuple) and why[1] is not None and \
                    not why[1][0].startswith("#") else None
                if spans == []:
                    partial_base[0] = remember_action_hashes(parts=True)
                    why = None
            if why is not None:
                _job = core.BakeJob(scene, all_caches(), near=scene.frame_current, spans=spans)
                _job.why = why
                _job_started = time.time()
                _job.gap = _job_started - _last_done[0]
                # quick scenes at once (as before); heavy ones and long ranges in steps, without a
                # freeze - the first of them on the next tick (this one checked what changed: on a
                # big rig that takes a moment already). A few frames baked again: at once too
                if (bake_seconds >= QUICK_BAKE and not _job.partial) or \
                        not _job.step(QUICK_PART if _job.partial else QUICK_BAKE):
                    return 0.02
                job, _job = _job, None
                _job_done(job)
            # (nothing redrawn when nothing was baked: drawing asks for a check when a path isn't
            # calculated yet - a redraw after every check kept that going for good)
    except Exception as ex:  # never let a timer die silently with a traceback storm
        print("Editable Motion Paths: auto update failed:", ex)
        _job = None
        _aux_drop(_BAKE_SCENE)
    if _job is None and (was_drag or live._drag):
        # (a drag's frames shown and nothing baked after it - cancelled, or back as it was: the paths as baked)
        if live.drop_drag():
            tag_redraw()
        _aux_drop(_BAKE_SCENE)
    _bake_timer_on = False
    return None


# A drag (the add-on's own, the Graph Editor's...) that changes curves: the frames it changed are baked again as it
# goes, in the bake scene, a few at a time - the paths that don't follow an edit at once (live.py: an IK chain
# under a dragged control, a constraint mixing in another bone...) follow it during the drag, not only after it
DRAG_REBAKE = True
DRAG_PACE = 0.03        # seconds between the steps meanwhile (at least as long as a step took: Blender keeps up)
DRAG_STEP = 0.015       # how long a step goes on
DRAG_LOOK = 1.0         # every so often all the curves are looked at again (else only those the drag edits)
_drag_job = None        # the bake of the drag's frames going on (core.BakeJob, drag=True)
_drag_dirty = False     # the curves changed during a drag since that job began
_drag_skip = set()      # the owners whose curves the drag changes (their own paths are left as they are)
_drag_used = False      # a drag baked frames (its end: the bake scene removed, the paths set right)
# the drag's look at the curves: {"owners": what its edit changes, "at": when all were looked at, "off": nothing
# to do in this drag (only a full bake will do), "memo"}
_drag_look = {}


def _drag_spans(scene):
    """The frames the drag changed so far (see changed_spans), and the owners of the curves it edits. The first look
    (and one every DRAG_LOOK) at every curve; the others only at the curves of what it edits - on a heavy rig the
    whole look takes a few dozen milliseconds."""
    look = _drag_look
    if look.get("off"):
        return None, set()
    memo = look.setdefault("memo", {})
    owners = look.get("owners")
    if not owners or time.time() - look.get("at", 0.0) > DRAG_LOOK:
        owners = set()
        spans = changed_spans(scene, owners, memo=memo)
        look["at"] = time.time()
        if spans is None:
            look["off"] = True      # (something else changed too: the bake after the drag, in full)
            return None, set()
        look["owners"] = owners
        return spans, owners
    return changed_spans(scene, only=owners, memo=memo), owners


def _drag_tick():
    """One step of the bake during a drag; how long until the next one (None: nothing to do - the usual wait)."""
    global _drag_job, _drag_dirty, _drag_skip, _drag_used
    from . import core, live
    if not DRAG_REBAKE or _job is not None or live._pv["tmp"] or not paths_shown():
        return None     # (a bake of all of them going on / Blender's G / R / S: no curve is edited)
    t0 = time.time()
    scene = bpy.context.scene
    if _drag_dirty:
        _drag_dirty = False
        _drag_job = None
        spans, owners = _drag_spans(scene)
        if spans:
            job = core.BakeJob(scene, all_caches(), near=scene.frame_current, spans=spans, drag=True)
            if job.ok_start:
                _drag_job, _drag_skip, _drag_used = job, owners, True
    job = _drag_job
    if job is None:
        # (the drag's bake done: looked at often still - its end is baked right after it)
        return DRAG_PACE if _drag_used else None
    spent = time.time() - t0
    if spent > DRAG_STEP:
        return max(DRAG_PACE, spent)    # (a look at every curve took long: its frames on the next step)
    done = job.step(DRAG_STEP)
    if job.show_drag(_drag_skip):
        tag_redraw()
    if done:
        _drag_job = None
    return max(DRAG_PACE, time.time() - t0)


def _drag_changed():
    """The curves changed during a drag: its frames baked again soon (see _drag_tick)."""
    global _drag_dirty, _bake_timer_on
    fast = _drag_dirty or _drag_job is not None
    _drag_dirty = True
    schedule_rebake(DRAG_PACE)
    if not fast:
        # (the timer may wait for longer - a drag only just started: it comes at once)
        if bpy.app.timers.is_registered(_bake_timer):
            bpy.app.timers.unregister(_bake_timer)
        _bake_timer_on = True
        bpy.app.timers.register(_bake_timer, first_interval=DRAG_PACE)


_dg_frame = None
_dg_frame_all = None


def _edit_spans(scene, depsgraph):
    """The frames an edit of the curves changed (see onion.edit_spans), once for the ghosts and the
    center of mass alike; every frame when it can't be told."""
    from . import onion
    st = settings(scene)
    if st is None or not (st.show_onion or st.show_com):
        return onion.ALL
    try:
        return onion.edit_spans(scene, _moved(depsgraph))
    except Exception:
        return onion.ALL


def _moved(depsgraph):
    """An object was moved by this update (not only keys selected: that changes an action, but no
    object moves)."""
    return any(isinstance(u.id, bpy.types.Object) and (u.is_updated_transform or u.is_updated_geometry)
               for u in depsgraph.updates)     # (a pose change is the armature's geometry)


@persistent
def _on_frame(scene, *_args):
    """The frame changed. In the interface this comes after the frame's update (which is told
    apart by the frame it is on); a script's frame_set sends no update of its own - an edit after
    it comes on the new frame and is an edit (not a frame change)."""
    global _dg_frame_all
    from . import onion
    if baking or onion.busy():
        return
    from . import com
    if not com.busy():
        from . import snapshots
        snapshots.frame_changed(scene)      # (a Static one previewing a frame goes back to its own)
    _dg_frame_all = scene.frame_current
    if settings(scene) is not None and (settings(scene).show_onion or settings(scene).show_com):
        global _dg_frame
        _dg_frame = scene.frame_current


@persistent
def _on_depsgraph(scene, depsgraph):
    from . import onion
    from . import com
    if baking or onion.busy() or com.busy():
        return
    st = settings(scene)
    if st is None:
        return
    if st.paths and scene == bpy.context.scene and not modal_running:
        try:
            sync_list_active(st)        # (a bone / object clicked in the view: its row at once)
        except Exception:
            pass
    if (st.paths or path_tool_on()) and scene == bpy.context.scene and not modal_running:
        try:
            _watch_order(st)            # (and its place in the selection order, click by click)
        except Exception:
            pass
    live_done = False       # (the children's paths already moved in this update: not worked out twice)
    if st.paths and scene == bpy.context.scene and not modal_running:
        try:
            from . import live
            if blender_edit_running():
                after_blender_edit[0] = True
                live_done = True
                live.preview(scene)     # (Blender's G / R / S with auto keying: the paths as they will be)
            else:
                live.end_preview()
        except Exception:
            pass
    if st.show_onion or st.show_com:
        global _dg_frame
        f = scene.frame_current
        playing = bpy.context.screen is not None and bpy.context.screen.is_animation_playing
        # a mesh edited (Edit Mode, weight paint, normals flipped...): its ghost shape is made again
        # (on the same frame only: a frame change bends meshes by their shape keys, no edit)
        edited = {u.id.original.name for u in depsgraph.updates
                  if isinstance(u.id, bpy.types.Mesh) and u.is_updated_geometry}             if f == _dg_frame and not playing else set()
        if edited:
            # (shape keys updated along with their mesh: the rig moved them - no edit)
            keys = {u.id.original.name for u in depsgraph.updates if isinstance(u.id, bpy.types.Key)}
            onion.meshes_changed(edited, keys)
        if not playing and f == _dg_frame and not modal_running:
            # (a frame change also updates the objects: only changes on the same frame count)
            if any(isinstance(u.id, bpy.types.Object) and (u.is_updated_transform or u.is_updated_geometry)
                   for u in depsgraph.updates):
                # (a curve changed too: that is told below, once - the ghosts then renew only the
                # frames around it)
                if not any(isinstance(u.id, bpy.types.Action) for u in depsgraph.updates):
                    onion.changed(pose=True)
                    # (the center of mass: a pose changed without keys changes only this frame - its point is
                    # live; the whole track is worked out again only when a character itself moved - every
                    # frame of its own track with it; a pose dragged on a heavy rig re-ran every frame)
                    if _rigs_moved(scene):
                        com.changed()
        _dg_frame = f
    global _dg_frame_all
    same_frame = scene.frame_current == _dg_frame_all
    _dg_frame_all = scene.frame_current
    if not same_frame:
        return      # (a frame change reports the actions as updated too: no curve changed)
    if st.show_snapshots and len(st.snapshots) and             any(isinstance(u.id, bpy.types.Action) for u in depsgraph.updates):
        from . import snapshots
        snapshots.anim_changed()        # (the Live ones follow the animation)
    if not (caches or helper_caches) or not st.auto_update:
        # no path: the ghosts and the center of mass still follow the animation
        if st.show_onion or st.show_com:
            if any(isinstance(u.id, bpy.types.Action) for u in depsgraph.updates):
                spans = _edit_spans(scene, depsgraph)
                onion.changed(spans=spans)
                com.changed(spans)
        return
    for u in depsgraph.updates:
        if isinstance(u.id, bpy.types.Action):
            try:
                from . import ops_fmods
                ops_fmods.sync_active(bpy.context)     # modifier stack entries stay alike
            except Exception:
                pass
            try:
                from . import live
                if not live_done:
                    live.update()   # (the paths of the children of an edited bone / object follow it at once)
            except Exception:
                pass
            tag_redraw()        # the path follows right away (e.g. a modifier's settings)
            _rebake_if_shown(scene)
            spans = _edit_spans(scene, depsgraph)
            onion.changed(spans=spans)
            com.changed(spans)
            return
    # no curve changed, but an object did (on the same frame, not while dragging or playing): a
    # value a driver reads, an NLA strip, a constraint's setting may have changed - checked
    # without the curves (cheap)
    if same_frame and not modal_running and not (bpy.context.screen is not None and
                                                 bpy.context.screen.is_animation_playing) and \
            any(isinstance(u.id, bpy.types.Object) for u in depsgraph.updates):
        try:
            from . import live
            if live.update():
                tag_redraw()    # (an object without animation moved by hand: its paths follow at once)
        except Exception:
            pass
        _rebake_if_shown(scene, curves=False)


def _rebake_if_shown(scene, curves=True):
    """Something changed: the paths are checked (and baked again if it moves them) - only while one
    is shown; else that is noted and done when one shows."""
    global _hidden_changes
    if paths_shown(scene):
        if curves and DRAG_REBAKE and (modal_running or _any_modal()):
            _drag_changed()
        else:
            schedule_rebake(curves=curves)
    else:
        _hidden_changes = True


# Settings that only say how the paths are shown / edited (like Blender's overlays): Ctrl+Z
# doesn't change them (the ghosts don't go off, the edit mode doesn't jump back), it goes to the
# edits. What is data stays with undo: the path list (and its active row), the selection, the
# ranges, the masses.
_UNDONE_SETTINGS = {"rna_type", "name", "paths", "snapshots", "snapshot_index", "active_index", "picked_handles", "select_history", "select_order", "com_limb_share",
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


_rig_mats = {}      # armature -> its world matrix last seen (see _rigs_moved)


def _rigs_moved(scene):
    """Has any character's armature object moved (its own transform, not its pose) since last asked?"""
    from . import com
    moved = False
    for arm in com.armatures(scene):
        m = tuple(round(v, 7) for row in arm.matrix_world for v in row)
        if _rig_mats.get(arm.name) != m:
            moved = moved or arm.name in _rig_mats
            _rig_mats[arm.name] = m
    return moved


@persistent
def _on_undo(*_args):
    global _job
    from . import com, onion
    try:
        _keep_view()
    except Exception:
        pass
    onion.changed(pose=True)
    com.changed()
    from . import snapshots
    snapshots.after_undo()      # (what is drawn stays until it is made again: no blinking)
    try:
        _reconcile_order()      # (the selection order as the selection the undo brought back)
    except Exception:
        pass
    # (no need to mark the paths dirty: a path rebuilds when its curves differ from what it was
    # built from; hidden keys and value channels are taken over below)
    try:
        sync_caches()
    except Exception:
        pass
    # (the animation as it was before: its parent spaces were baked then - taken again at once,
    # the paths show it right away instead of after a new bake)
    try:
        memo = _bake_from_memo(bpy.context.scene)
        if memo:
            _job = None         # (a bake half done was for the animation undone)
        else:
            from . import live
            live.update()       # (an object moved by hand and undone: its paths back with it at once)
    except Exception:
        memo = False
    tag_redraw()
    st = settings()
    if st is not None and st.auto_update and caches and not memo:
        # a re-bake half done was for the animation before the undo: it starts over
        restart = _job is not None
        _job = None
        schedule_rebake(0.2, force=restart)
    if _job is None:
        _aux_drop()         # (the bake scene an undo step brought back: nothing bakes in it)


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
    partial_base[0] = None
    _bake_memo.clear()
    _ignored.clear()
    _useless.clear()
    from . import snapshots
    snapshots.clear_runtime()
    _aux_drop()     # (one saved by an auto save, or by the add-on of before)
    st = settings()
    if st is not None and st.paths:
        bpy.app.timers.register(_load_bake, first_interval=0.2)


@persistent
def _before_save(*_args):
    # the bake scene isn't saved (a bake going on makes it again)
    _aux_drop()


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
    """[(object, bone)] in the order the paths got selected - keys clicked, boxed, A..., bones
    clicked in the view, paths picked in the list - oldest first."""
    global _order_cache
    st = settings(scene)
    text = st.select_order if st is not None else ""
    if text != _order_cache[0]:
        import json
        try:
            items = [tuple(k[:2]) for k in json.loads(text or "[]")]
        except (ValueError, TypeError):
            items = []
        _order_cache = (text, items)
    return _order_cache[1]


_keys_off = set()       # paths whose keys were all deselected while their bone / object stayed selected


def note_selected(path_key, scene=None):
    """A key of this path got selected (or its bone / object): the path joins the selection order (if it
    isn't in it). One that stayed in it only for its selected bone (its keys were deselected) goes to its
    end: its keys clicked again are clicked after the others."""
    order = selection_order(scene)
    key = tuple(path_key[:2])
    if key in order:
        if key not in _keys_off:
            return
        order = [k for k in order if k != key]
    _keys_off.discard(key)
    import json
    st = settings(scene)
    if st is not None:
        st.select_order = json.dumps([list(k) for k in order + [key]][-64:])


def forget_selected(path_keys, scene=None, keys_only=False):
    """These paths have no selected keys any more: out of the selection order. keys_only: only their keys
    were deselected - one whose bone / object is still selected stays (Offset Cascade goes by the bones
    then: their order is still the one they were clicked in)."""
    order = selection_order(scene)
    drop = {tuple(k[:2]) for k in path_keys}
    if keys_only:
        kept = {k for k in drop if k in order and _owner_selected(*k)}
        _keys_off.update(kept)
        drop -= kept
    if not any(k in drop for k in order):
        return
    _keys_off.difference_update(drop)
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
_isolated_at = 0.0
_vtx_store_at = 0.0


def _isolation_shown():
    """A Graph Editor and a 3D view's sidebar (the path list's Graph Editor buttons) are open."""
    graph = side = False
    for win in bpy.context.window_manager.windows:
        for a in win.screen.areas:
            if a.type == 'GRAPH_EDITOR':
                graph = True
            elif a.type == 'VIEW_3D' and any(r.type == 'UI' and r.width > 1 for r in a.regions):
                side = True
    return graph and side


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


def _owner_selected(obj_name, bone_name):
    from . import core
    obj, owner = core.resolve(obj_name, bone_name)
    if owner is None:
        return False
    if bone_name:
        return obj.mode == 'POSE' and compat.bone_selected(owner)
    return obj.select_get()


_order_active = [None]     # the active bone / object the selection order last saw


def _view_active_key():
    vl = bpy.context.view_layer
    obj = vl.objects.active if vl is not None else None
    if obj is None:
        return None
    bone = obj.data.bones.active.name if obj.type == 'ARMATURE' and obj.mode == 'POSE' and \
        obj.data.bones.active is not None else ""
    return (obj.name, bone)


def selected_owners_any():
    """[(object name, bone name)] of every bone selected in Pose Mode (shown: not hidden) / object selected in
    Object Mode - with a path or not."""
    out = []
    vl = bpy.context.view_layer
    if vl is None:
        return out
    for o in vl.objects:
        try:
            if o.mode == 'POSE' and o.type == 'ARMATURE' and o.pose is not None:
                for pb in o.pose.bones:
                    if not pb.bone.hide and compat.bone_selected(pb):
                        out.append((o.name, pb.name))
            elif o.mode == 'OBJECT' and o.select_get() and bpy.context.mode == 'OBJECT':
                out.append((o.name, ""))
        except (RuntimeError, ReferenceError, AttributeError):
            continue
    return out


def _selected_owners(st):
    """The bones / objects selected now: with paths first, in the path list's order, then the others (Offset
    Cascade / Shift Phase move animated bones without paths too)."""
    out = []
    for e in st.paths:
        k = (e.object.name, e.bone_name) if e.object is not None else None
        if k is not None and k not in out and _owner_selected(*k):
            out.append(k)
    for k in selected_owners_any():
        if k not in out:
            out.append(k)
    return out


def _watch_order(st):
    """The selection order follows the bones / objects clicked in the view (Offset Cascade): the ones
    deselected leave it; the ones selected that aren't in it join it (the active one last: clicked last) - all
    of them, not only the active one: a bone deselected and clicked again that was the active one all along
    never joined, and went last; one that becomes the active one (Shift+click on one already selected) goes
    last. It looks after every update (each click) and on a timer."""
    now = _selected_owners(st)
    order = selection_order()
    # (what isn't selected now leaves it - also an armature object noted in Object Mode: in Pose Mode it stays
    # "selected", but only its bones count there)
    gone = [k for k in order if k not in now]
    if gone:
        forget_selected(gone)
        order = selection_order()
    # (selected and not in it yet: last, the active one the very last - the ones noted already, by a click on a
    # key or a row, keep their place)
    active = _view_active_key()
    new = [k for k in now if k not in order]
    if new:
        new.sort(key=lambda k: k == active)
        for k in new:
            note_selected(k)
    if active != _order_active[0]:
        _order_active[0] = active
        if active in now and active not in new and selection_order()[-1:] != [active]:
            _keys_off.add(active)       # (in it already: to its end)
            note_selected(active)


_list_active = [None]      # the active bone / object the path list's active row last followed


def _view_active():
    """(object name, bone name, its path editable now) of the active bone / object in the view, or None."""
    vl = bpy.context.view_layer
    obj = vl.objects.active if vl is not None else None
    if obj is None:
        return None
    if obj.type == 'ARMATURE' and obj.mode == 'POSE':
        b = obj.data.bones.active
        if b is None:
            return (obj.name, "", False)
        pb = obj.pose.bones.get(b.name)
        return (obj.name, b.name, pb is not None and compat.bone_selected(pb))
    try:
        return (obj.name, "", obj.mode == 'OBJECT' and obj.select_get())
    except RuntimeError:
        return None


def sync_list_active(st=None):
    """The path list's active row follows the bone / object made active in the view (clicked, Shift+clicked
    last), like the Outliner follows the view: its row becomes the active one - the list's highlight, the
    charts, the modifier stack go with it. Only when the active one changes (and it is selected and has a
    path): a key clicked on another path's line makes that path's row the active one, and it stays so."""
    st = st if st is not None else settings()
    if st is None:
        return
    key = _view_active()
    if key == _list_active[0]:
        return
    _list_active[0] = key
    if key is None or not key[2]:
        return
    for i, e in enumerate(st.paths):
        if e.object is not None and e.object.name == key[0] and e.bone_name == key[1]:
            if st.active_index != i:
                st.active_index = i
                tag_redraw()
            return


def path_tool_on():
    """The Edit Paths / Sculpt Paths tool is the active one (the selection order is kept then even without
    paths: Offset Cascade on bones without paths)."""
    ctx = bpy.context
    try:
        ws = ctx.workspace
        tool = ws.tools.from_space_view3d_mode(ctx.mode, create=False) if ws is not None else None
    except (AttributeError, TypeError, RuntimeError):
        return False
    return tool is not None and tool.idname.startswith("emp.")


def _reconcile_order():
    """After an undo / redo: the selection is the undo step's, but the order the step kept lags behind it -
    a click's undo step is made before the watcher notes the click (see _watch_order): the bone clicked
    last was missing from it, and the cascade put it after every bone clicked later. The bones / objects
    with paths selected now that the order lacks join it, the active one last (its click was the latest)."""
    st = settings()
    if st is None:
        return
    order = selection_order()
    vl = bpy.context.view_layer
    obj = vl.objects.active if vl is not None else None
    active = None
    if obj is not None:
        bone = obj.data.bones.active.name if obj.type == 'ARMATURE' and obj.mode == 'POSE' and \
            obj.data.bones.active is not None else ""
        active = (obj.name, bone)
    missing = [k for k in _selected_owners(st) if k not in order]
    missing.sort(key=lambda k: k == active)
    for k in missing:
        note_selected(k)
    _order_active[0] = _view_active_key()
    _keys_off.clear()
    _list_active[0] = _view_active()     # (the active row as the undo step kept it)


def _watch_active():
    """Auto edit mode: when the active bone/object has a path but nothing to edit in the
    current mode (e.g. only location is animated while editing Rotation), switch modes."""
    global _watch_last
    try:
        reconcile_modal()
        st = settings()
        if st is not None and not st.paths and not modal_running and path_tool_on():
            try:
                _watch_order(st)        # (no path: the selection order still kept - Offset Cascade on bones)
            except Exception:
                pass
        global _vtx_store_at
        if st is not None and helper_caches and not modal_running and not blender_edit_running() and \
                time.time() - _vtx_store_at > 1.0:
            _vtx_store_at = time.time()
            try:
                from . import ops_vertex
                ops_vertex.store_points(st)     # (the tracked vertices as they follow the animation: in the file)
            except Exception:
                pass
        if st is None or not st.paths or modal_running:
            return 0.25
        global _isolated_at
        now = time.time()
        if now - _isolated_at > 1.0:
            _isolated_at = now
            try:
                if _isolation_shown():
                    _watch_isolated(st)
            except Exception:
                pass
        vl = bpy.context.view_layer
        obj = vl.objects.active if vl is not None else None
        try:
            _watch_order(st)
        except Exception:
            pass
        try:
            sync_list_active(st)
        except Exception:
            pass
        try:
            from . import live
            if not blender_edit_running():
                live.end_preview()      # (a drag that ended without an update after it)
        except Exception:
            pass
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
        if any(e.object == obj and e.bone_name == bone and e.mode_pin != 'HEADER' for e in st.paths):
            return 0.25     # (its path has its own mode: nothing to switch at the top for it)
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
    bpy.app.handlers.frame_change_post.append(_on_frame)
    bpy.app.handlers.undo_pre.append(_before_undo)
    bpy.app.handlers.redo_pre.append(_before_undo)
    bpy.app.handlers.undo_post.append(_on_undo)
    bpy.app.handlers.redo_post.append(_on_undo)
    bpy.app.handlers.load_post.append(_on_load)
    bpy.app.handlers.save_pre.append(_before_save)
    bpy.app.timers.register(_load_bake, first_interval=0.5)
    bpy.app.timers.register(_watch_active, first_interval=1.0, persistent=True)


def unregister():
    for lst, fn in ((bpy.app.handlers.depsgraph_update_post, _on_depsgraph),
                    (bpy.app.handlers.frame_change_post, _on_frame),
                    (bpy.app.handlers.undo_post, _on_undo),
                    (bpy.app.handlers.undo_pre, _before_undo),
                    (bpy.app.handlers.redo_pre, _before_undo),
                    (bpy.app.handlers.redo_post, _on_undo),
                    (bpy.app.handlers.load_post, _on_load),
                    (bpy.app.handlers.save_pre, _before_save)):
        if fn in lst:
            lst.remove(fn)
    if bpy.app.timers.is_registered(_bake_timer):
        bpy.app.timers.unregister(_bake_timer)
    if bpy.app.timers.is_registered(_load_bake):
        bpy.app.timers.unregister(_load_bake)
    if bpy.app.timers.is_registered(_watch_active):
        bpy.app.timers.unregister(_watch_active)
    from . import com, onion
    from . import snapshots
    for fn in (onion._tick, com._tick, snapshots._tick):
        if bpy.app.timers.is_registered(fn):
            bpy.app.timers.unregister(fn)
    snapshots._timer[0] = False
    if bpy.app.timers.is_registered(_aux_drop_later):
        bpy.app.timers.unregister(_aux_drop_later)
    _aux_drop(later=False)
    caches.clear()


def seen_from_itself(cache, context=None):
    """The 3D view looks through the camera this path is of (its points would sit on the lens,
    flickering): not drawn, not picked there."""
    ctx = context or bpy.context
    rv3d = getattr(ctx, "region_data", None)
    scene = getattr(ctx, "scene", None)
    cam = scene.camera if scene is not None else None
    return rv3d is not None and cam is not None and getattr(rv3d, "view_perspective", "") == 'CAMERA' and \
        cache.obj_name == cam.name and not cache.bone_name
