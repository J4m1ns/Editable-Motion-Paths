"""Onion skinning: ghosts of the edited bones / objects on the frames around the current one.

Heavy characters stay fast because their meshes are never evaluated for the ghosts:

* once (with a "Preparing ghosts" note), every mesh the armature deforms is melted down to
  the Detail limit (like Decimate, no holes) into a *proxy* that keeps the bone weights of the
  vertices it was made of, and its rest shape (worked back from the pose it was built in);
* for every ghost frame only the armature is evaluated (the deformed meshes are left out of
  the scene evaluation meanwhile) and the proxy is bent by its weights here (linear blend
  skinning, numpy);
* parts that don't bend (objects on a bone, props, cameras, lights, empties) are one shape
  drawn with a matrix per ghost: instanced.

The ghosts follow edits live (the nearest first, also while a key is dragged); the old one
stays drawn until its new one is ready, so nothing blinks. Show: the whole character, only
the part the edited bones move (by weight), or only the objects they move (by object).
Modes: Silhouette (one flat color per ghost), Wire, Points, Bones (sticks)."""

import time

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix

from . import state

_proxies = {}           # object name -> _Proxy
_frames = {}            # frame -> {"parts": [(proxy name, positions or None, matrix or None)], "bones": array}
_batches = {}           # (frame, kind, proxy name or "") -> GPU batch
_stale = set()          # captured frames the animation has changed since (still drawn until renewed)
_changed_at = 0.0       # time of the last animation change
_cost = 0.0             # how long one ghost frame takes to capture (running average, seconds)
_busy = False           # capturing right now (frame_set must not count as an edit)
_timer_on = False
_sig = None             # what the ghosts were captured for (objects, settings)
status = ""             # "Preparing ghosts..." while proxies are being built (shown in the viewport)


def busy():
    return _busy


# ---- what to ghost --------------------------------------------------------------------

def _edited(scene):
    """{armature name: {edited bone names}} and [edited objects that aren't armatures]."""
    arms, objs = {}, []
    for c in state.visible_caches(scene):
        if not c.owner_selected():
            continue
        obj = bpy.data.objects.get(c.obj_name)
        if obj is None:
            continue
        if obj.type == 'ARMATURE':
            arms.setdefault(obj.name, set())
            if c.bone_name:
                arms[obj.name].add(c.bone_name)
        elif obj not in objs:
            objs.append(obj)
    if not arms and not objs:
        # no path shown: the selection (the ghosts don't need a path)
        from . import compat
        vl = bpy.context.view_layer
        ob = vl.objects.active if vl else None
        if ob is not None and ob.type == 'ARMATURE' and ob.mode == 'POSE':
            arms[ob.name] = {pb.name for pb in ob.pose.bones if compat.bone_selected(pb)}
        else:
            for o in getattr(bpy.context, "selected_objects", None) or []:
                if o.type == 'ARMATURE':
                    arms.setdefault(o.name, set())
                elif o not in objs:
                    objs.append(o)
    return arms, objs


def _key_frames(scene):
    """The key frames of what is ghosted: the paths' keys, or (no path) the keys of the selected
    bones / objects."""
    from . import compat
    cs = state.editable_caches(scene)
    if cs:
        return sorted({round(k.t) for c in cs for k in c.keys})
    arms, objs = _edited(scene)

    def collect(only_selected):
        frames = set()
        for name, bones in arms.items():
            ob = bpy.data.objects.get(name)
            for fc in (compat.get_fcurves(ob) or []) if ob is not None else []:
                if only_selected and bones and not any(('pose.bones["%s"]' % b) in fc.data_path for b in bones):
                    continue
                frames.update(round(kp.co[0]) for kp in fc.keyframe_points)
        for ob in objs:
            # its own keys, or (no keys: a child of something animated) its parents'
            o = ob
            while o is not None:
                for fc in compat.get_fcurves(o) or []:
                    frames.update(round(kp.co[0]) for kp in fc.keyframe_points)
                if frames:
                    break
                o = o.parent
        return frames
    # the selected bones' keys; with fewer than two (a mechanism bone), the whole rig's
    sel = collect(True)
    return sorted(sel if len(sel) >= 2 else collect(False))


def affected_bones(arm, edited):
    """The bones the edited ones move: their children, and bones constrained to them (an IK
    chain to its controller, Copy Transforms...) with their own children. Constraints that are
    off now (muted, no influence) don't count."""
    bones = arm.data.bones
    out = set()
    for b in bones:
        if b.name in edited or any(p.name in edited for p in b.parent_recursive):
            out.add(b.name)
    for _round in range(4):
        grow = set()
        for pb in arm.pose.bones:
            if pb.name in out:
                continue
            for con in pb.constraints:
                if con.mute or not getattr(con, "enabled", True) or con.influence < 1e-3:
                    continue        # (a space switch that is off now: the head doesn't follow the hand)
                if getattr(con, "target", None) == arm and getattr(con, "subtarget", "") in out:
                    chain = [pb.name]
                    if con.type == 'IK':
                        p, n = pb.parent, con.chain_count
                        while p is not None and (n == 0 or len(chain) < n):
                            chain.append(p.name)
                            p = p.parent
                    grow.update(chain)
        if not grow:
            break
        for b in bones:
            if b.name in grow or any(p.name in grow for p in b.parent_recursive):
                out.add(b.name)
    return out


def _targets(scene):
    """[(object, armature or None, how)]: the objects to ghost. how: 'DEFORM' (bent by the
    armature), 'RIGID' (moves as one piece), 'LINES' (no mesh: camera, light, empty...)."""
    arms, objs = _edited(scene)
    out = []
    seen = set()

    def add(o, arm, how):
        if o.name not in seen:
            seen.add(o.name)
            out.append((o, arm, how))
    for name in arms:
        arm = bpy.data.objects.get(name)
        if arm is None:
            continue
        deform = []
        for o in scene.objects:
            if not o.visible_get():
                continue
            if o.type == 'MESH' and any(m.type == 'ARMATURE' and m.object == arm and m.show_viewport
                                        for m in o.modifiers):
                add(o, arm, 'DEFORM')
                deform.append(o)
            elif o.parent == arm and _how(o):
                add(o, arm, _how(o))
        # what hangs on them further down: a prop on an empty on a bone, eyes parented to a
        # vertex of the head mesh...
        for top in [arm] + deform:
            for o in top.children_recursive:
                if o.visible_get() and _how(o):
                    add(o, arm, _how(o))
    for obj in objs:
        # (a mesh bent by an armature is ghosted bent - not as one stiff piece)
        for o in [obj] + [o for o in obj.children_recursive if o.visible_get()]:
            arm = deformed_by(o)
            if arm is not None:
                add(o, arm, 'DEFORM')
            elif _how(o):
                add(o, None, _how(o))
    return out


def deformed_by(o):
    """The armature bending a mesh (its first Armature modifier shown in the viewport), or None."""
    if o.type != 'MESH':
        return None
    for m in o.modifiers:
        if m.type == 'ARMATURE' and m.show_viewport and m.object is not None and m.object.type == 'ARMATURE':
            return m.object
    return None


def _how(o):
    """How an object is ghosted: 'RIGID' (its mesh, or a curve / text / surface turned into one),
    'LINES' (a shape of lines: camera, light, empty...), None (metaballs: their shape changes
    all the time)."""
    if o.type in {'MESH', 'CURVE', 'SURFACE', 'FONT'}:
        return 'RIGID'
    if o.type == 'META':
        return None
    return 'LINES'


def _has_faces(o):
    """A ghost of it can be a silhouette: a mesh with faces, a curve with thickness, a text..."""
    if o.type == 'MESH':
        return len(o.data.polygons) > 0
    if o.type == 'CURVE':
        cu = o.data
        return cu.bevel_depth > 0.0 or cu.extrude > 0.0 or \
            (cu.bevel_mode == 'OBJECT' and cu.bevel_object is not None) or \
            (cu.dimensions == '2D' and cu.fill_mode != 'NONE' and any(sp.use_cyclic_u for sp in cu.splines))
    return o.type in {'SURFACE', 'FONT'}


_modes_cache = [None, 0.0, None]


def _hiding():
    """True while meshes are hidden for a moment (the ghosts', the center's, the paths' frame
    stepping)."""
    from . import com
    return _busy or com.busy() or state.baking


def modes(scene):
    """The onion modes that show something for what is ghosted now: Silhouette (something with
    faces), Wire and Points (meshes), Bones (armatures); only shapes of lines (cameras, lights,
    empties): Wire. (Worked out again when the edited set changes, else twice a second.)"""
    arms, objs = _edited(scene)
    key = (tuple(sorted(arms)), tuple(o.name for o in objs))
    now = time.time()
    if _modes_cache[0] == key and (now - _modes_cache[1] < 0.5 or _hiding()):
        return _modes_cache[2]      # (not while the meshes are hidden for stepping frames: none would show)
    targets = _targets(scene)
    out = []
    if any(how != 'LINES' and _has_faces(o) for o, _a, how in targets):
        out.append('SILHOUETTE')
    if targets or not arms:
        out.append('WIRE')
    if any(how != 'LINES' for _o, _a, how in targets):
        out.append('POINTS')
    if arms:
        out.append('BONES')
    _modes_cache[:] = [key, now, out]
    return out


def mode_used(scene, st):
    """The onion mode drawn: the chosen one, or (it shows nothing here) the first that does."""
    ms = modes(scene)
    return st.onion_mode if st.onion_mode in ms else ms[0]


def _bone_shown(pb, st):
    from . import compat
    b = pb.bone
    if st.onion_bones == 'SELECTED':
        return compat.bone_selected(pb)
    if b.hide:
        return False
    cols = list(getattr(b, "collections", ()))
    return not cols or any(getattr(col, "is_visible_effectively", getattr(col, "is_visible", True)) for col in cols)


def _bone_state(scene, st):
    """Which bones the Bones mode shows (it changes with the selection / visibility)."""
    if st.onion_mode != 'BONES':
        return None     # (only the chosen mode: the fallback one may flicker while meshes are hidden)
    out = []
    for name in _edited(scene)[0]:
        obj = bpy.data.objects.get(name)
        if obj is not None:
            out.append(tuple(pb.name for pb in obj.pose.bones if _bone_shown(pb, st)))
    return tuple(out)


def _signature(scene, st):
    """What the captured ghosts belong to: the edited paths and the settings that change them."""
    arms, objs = _edited(scene)
    return (tuple(sorted((k, tuple(sorted(v))) for k, v in arms.items())), tuple(o.name for o in objs),
            st.onion_max_tris, st.onion_bones, _bone_state(scene, st))   # (not Show: it only filters what is drawn)


# ---- proxies: a mesh melted down once, with its bone weights ------------------------------

def _decimate(co, tri, limit, edges=None):
    """Fewer triangles without holes, like Decimate: vertices that fall into the same cell of a
    grid melt into one, triangles that collapse go away; the grid gets coarser until the mesh
    fits the limit. Returns (positions, triangles[, edges], cluster of every vertex)."""
    import numpy as np
    lo, hi = co.min(axis=0), co.max(axis=0)
    diag = float(np.linalg.norm(hi - lo)) or 1.0
    cells = max(8.0, (limit * 0.5) ** 0.5 * 2.0)
    best = (co, tri, np.arange(len(co)))
    for _i in range(12):
        q = np.floor((co - lo) / (diag / cells)).astype(np.int64)
        # one number per cell (cheaper than unique rows)
        q -= q.min(axis=0)
        span = q.max(axis=0) + 1
        key = (q[:, 0] * span[1] + q[:, 1]) * span[2] + q[:, 2]
        _u, inv = np.unique(key, return_inverse=True)
        inv = inv.reshape(-1)
        cnt = np.bincount(inv)
        newco = np.stack([np.bincount(inv, weights=co[:, i]) for i in range(3)], axis=1) / cnt[:, None]
        newco = newco.astype(np.float32)
        t2 = inv[tri]
        keep = (t2[:, 0] != t2[:, 1]) & (t2[:, 1] != t2[:, 2]) & (t2[:, 0] != t2[:, 2])
        t2 = t2[keep]
        if len(t2):
            _u2, first = np.unique(np.sort(t2, axis=1), axis=0, return_index=True)
            t2 = t2[np.sort(first)]     # the same triangle twice: once (its winding kept for culling)
        best = (newco, t2, inv)
        if len(t2) <= limit or cells <= 8.0:
            break
        cells *= 0.75
    newco, t2, inv = best
    if edges is None:
        return newco, t2, inv
    e2 = inv[edges]
    e2 = e2[e2[:, 0] != e2[:, 1]]
    if len(e2):
        e2 = np.unique(np.sort(e2, axis=1), axis=0)
    return newco, t2, e2, inv


class _Proxy:
    """One object's ghost shape: positions (world, as built), triangles, edges; for a bent mesh
    also the bone weights (K per vertex) and the rest shape they bend."""

    def __init__(self, name, how):
        self.name = name
        self.how = how
        self.sig = None
        self.co = None          # (P, 3): world positions when built (RIGID / LINES: object space)
        self.tri = None
        self.edges = None
        self.bones = []         # DEFORM: names of the bones used
        self.idx = None         # (P, K) bone index
        self.w = None           # (P, K) weights (rows sum to 1, or 0: follows the object)
        self.rest = None        # (P, 4) rest positions
        self.free = None        # (P,) no weight: follows the object
        self.keep = None        # (P,) shown in "by weight"
        self.vg = None          # (P,) in the onion's Vertex Group (None: the object has no such group)
        self.vg_name = None     # (the group vg is for: worked out by the timer, not while drawing)
        self.src_inv = None     # (proxy vertex of every vertex of the mesh as made, and their count)
        self.src_n = 0
        self.arm = None
        self.M0 = None          # object matrix when built
        self.rest_inv = None    # DEFORM: (B, 4, 4) inverse rest matrices of the bones
        self.exact = False      # DEFORM: shaped from the mesh Blender makes on every ghost frame
        self.inv = None         # exact: proxy vertex of every vertex of the mesh as made
        self.counts = None      # exact: how many vertices of the mesh make each proxy vertex


def needs_exact(o):
    """Exact ghosts (Fast off): the bent mesh is taken as Blender makes it on every ghost frame, in
    full detail - shape keys, lattices and every other deformer show."""
    st = state.settings()
    return o.type == 'MESH' and st is not None and not st.onion_fast


def _mesh_sig(o):
    mods = tuple((m.type, m.show_viewport, getattr(m, "levels", None)) for m in o.modifiers)
    n = len(o.data.vertices) if getattr(o.data, "vertices", None) is not None else 0
    return (o.type, n, mods, needs_exact(o))


def _circle(r, n=24, axis=2, z=0.0):
    import math
    out = []
    for i in range(n):
        p = [0.0, 0.0, 0.0]
        u, v = [(1, 2), (0, 2), (0, 1)][axis]
        p[u], p[v], p[axis] = math.cos(i * 2 * math.pi / n) * r, math.sin(i * 2 * math.pi / n) * r, z
        out.append(tuple(p))
    return out


def _empty_shape(kind, s, seg):
    """The lines of an empty of this display type, size s."""
    import math
    if kind == 'ARROWS':
        for i in range(3):
            b = [0.0, 0.0, 0.0]
            b[i] = s
            seg((0.0, 0.0, 0.0), tuple(b))
            # a little letter-like tick at the end of each axis
            t = [0.0, 0.0, 0.0]
            t[i] = s * 0.85
            t[(i + 1) % 3] = s * 0.08
            seg(tuple(b), tuple(t))
    elif kind == 'SINGLE_ARROW':
        seg((0.0, 0.0, 0.0), (0.0, 0.0, s))
        for d in ((0.06, 0.0), (-0.06, 0.0), (0.0, 0.06), (0.0, -0.06)):
            seg((0.0, 0.0, s), (d[0] * s, d[1] * s, s * 0.82))
    elif kind == 'CIRCLE':
        ring = _circle(s, 32, axis=2)
        for i in range(32):
            seg(ring[i], ring[(i + 1) % 32])
    elif kind == 'CUBE':
        c = [(x, y, z) for x in (-s, s) for y in (-s, s) for z in (-s, s)]
        for i, p in enumerate(c):
            for q in c[i + 1:]:
                if sum(1 for a, b in zip(p, q) if a != b) == 1:
                    seg(p, q)
    elif kind == 'SPHERE':
        for axis in range(3):
            ring = _circle(s, 32, axis=axis)
            for i in range(32):
                seg(ring[i], ring[(i + 1) % 32])
    elif kind == 'CONE':
        ring = _circle(s, 16, axis=1)
        for i in range(16):
            seg(ring[i], ring[(i + 1) % 16])
            if i % 4 == 0:
                seg(ring[i], (0.0, 2.0 * s, 0.0))
    else:   # PLAIN_AXES (and images: their frame isn't worth it)
        for i in range(3):
            a = [0.0, 0.0, 0.0]
            b = [0.0, 0.0, 0.0]
            a[i], b[i] = -s, s
            seg(tuple(a), tuple(b))


def _lines_shape(o):
    """Object-space line pairs for things without a mesh: a camera's frame, a light like Blender
    draws it, an empty of its own display type; anything else as the empty type chosen in the
    preferences."""
    import math
    import numpy as np
    pts = []

    def seg(a, b):
        pts.extend((a, b))

    def loop(ring):
        for i in range(len(ring)):
            seg(ring[i], ring[(i + 1) % len(ring)])
    if o.type == 'CAMERA':
        try:
            frame = [tuple(v) for v in o.data.view_frame(scene=bpy.context.scene)]
        except Exception:
            frame = [(0.5, 0.3, -1.0), (0.5, -0.3, -1.0), (-0.5, -0.3, -1.0), (-0.5, 0.3, -1.0)]
        for i in range(4):
            seg(frame[i], frame[(i + 1) % 4])
            seg((0.0, 0.0, 0.0), frame[i])
        top = (0.0, frame[0][1] * 1.35, frame[0][2])
        seg((frame[0][0] * 0.5, frame[0][1] * 1.1, frame[0][2]), top)
        seg((frame[3][0] * 0.5, frame[3][1] * 1.1, frame[3][2]), top)
    elif o.type == 'LIGHT':
        kind = o.data.type
        # the light itself: two circles (like Blender's), seen from any side as rings
        for axis in range(3):
            loop(_circle(0.05, 16, axis))
        loop(_circle(0.12, 24, 2))
        if kind == 'POINT':
            r = max(getattr(o.data, "shadow_soft_size", 0.25), 0.05)
            dashes = _circle(r, 32, 2)
            for i in range(0, 32, 2):
                seg(dashes[i], dashes[i + 1])
        elif kind == 'SUN':
            for a in range(8):
                x, y = math.cos(a * math.pi / 4), math.sin(a * math.pi / 4)
                seg((x * 0.16, y * 0.16, 0.0), (x * 0.26, y * 0.26, 0.0))
            seg((0.0, 0.0, 0.0), (0.0, 0.0, -1.0))
        elif kind == 'SPOT':
            ang = min(getattr(o.data, "spot_size", 0.8), 3.0) * 0.5
            d = 1.0
            rr = math.tan(ang) * d
            cone = [(math.cos(a * math.pi / 8) * rr, math.sin(a * math.pi / 8) * rr, -d) for a in range(16)]
            loop(cone)
            for i in range(0, 16, 4):
                seg((0.0, 0.0, 0.0), cone[i])
        elif kind == 'AREA':
            sx = getattr(o.data, "size", 1.0) * 0.5
            shape = getattr(o.data, "shape", 'SQUARE')
            sy = getattr(o.data, "size_y", o.data.size) * 0.5 if shape in {'RECTANGLE', 'ELLIPSE'} else sx
            if shape in {'DISK', 'ELLIPSE'}:
                loop([(math.cos(a * math.pi / 16) * sx, math.sin(a * math.pi / 16) * sy, 0.0) for a in range(32)])
            else:
                loop([(-sx, -sy, 0.0), (sx, -sy, 0.0), (sx, sy, 0.0), (-sx, sy, 0.0)])
            seg((0.0, 0.0, 0.0), (0.0, 0.0, -0.5))
    elif o.type == 'EMPTY':
        _empty_shape(o.empty_display_type, getattr(o, "empty_display_size", 1.0) or 1.0, seg)
    else:
        from .props import prefs
        p = prefs()
        _empty_shape(p.onion_other_display if p is not None else 'PLAIN_AXES', 0.5, seg)
    co = np.array(pts, dtype=np.float32).reshape(-1, 3)
    return co, np.arange(len(co)).reshape(-1, 2)


def _build(o, arm, how, st, dg):
    """Make (or keep) the proxy of one object. Returns True when it was (re)built."""
    import numpy as np
    sig = (_mesh_sig(o), how, arm.name if arm else None, st.onion_max_tris)
    p = _proxies.get(o.name)
    if p is not None and p.sig == sig:
        return False
    p = _Proxy(o.name, how)
    p.sig = sig
    p.arm = arm.name if arm else None
    if hasattr(getattr(o, "data", None), "vertices"):
        _content[o.data.name] = _mesh_content(o.data)    # (what an edit of it is told by)
    if how == 'LINES':
        p.co, p.edges = _lines_shape(o)
        p.tri = np.zeros((0, 3), dtype=np.int64)
        _proxies[o.name] = p
        return True
    ev = o.evaluated_get(dg)
    try:
        me = ev.to_mesh()
    except RuntimeError:
        me = None
    if me is None:
        p.co = np.zeros((0, 3), dtype=np.float32)
        p.tri = np.zeros((0, 3), dtype=np.int64)
        p.edges = np.zeros((0, 2), dtype=np.int64)
        _proxies[o.name] = p
        return True
    n = len(me.vertices)
    co = np.empty(n * 3, dtype=np.float32)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    M = np.array(ev.matrix_world, dtype=np.float64)
    if how == 'DEFORM':
        co = (co @ M[:3, :3].T + M[:3, 3]).astype(np.float32)       # world, as posed now
    me.calc_loop_triangles()
    tri = np.empty(len(me.loop_triangles) * 3, dtype=np.int64)
    me.loop_triangles.foreach_get("vertices", tri)
    tri = tri.reshape(-1, 3)
    ed = np.empty(len(me.edges) * 2, dtype=np.int64)
    me.edges.foreach_get("vertices", ed)
    ed = ed.reshape(-1, 2)
    limit = max(500, st.onion_max_tris)
    inv = np.arange(n)
    exact = how == 'DEFORM' and needs_exact(o)
    if n and not exact and (len(tri) > limit or len(ed) > limit * 1.5 or n > limit):
        co, tri, ed, inv = _decimate(co, tri, limit, ed)
    p.co, p.tri, p.edges = co, tri, ed
    p.src_inv, p.src_n = inv, n
    p.M0 = Matrix(ev.matrix_world)
    if how == 'DEFORM':
        _weights(p, o, arm, me, inv)
        if exact:
            p.exact = True
            p.inv = inv
            p.counts = np.maximum(np.bincount(inv, minlength=len(co)), 1)
    ev.to_mesh_clear()
    _proxies[o.name] = p
    return True


def _in_group(me, gi, inv, n):
    """(n,) the proxy vertices (inv: the proxy vertex of every vertex of the mesh) mostly made of
    vertices in vertex group gi."""
    import numpy as np
    src = np.zeros(len(me.vertices), dtype=np.float64)
    for v in me.vertices:
        for g in v.groups:
            if g.group == gi and g.weight > 0.0:
                src[v.index] = 1.0
                break
    if not len(src):
        return np.zeros(n, dtype=bool)
    got = np.bincount(inv, weights=src, minlength=n)
    tot = np.maximum(np.bincount(inv, minlength=n), 1)
    return got / tot >= 0.5


def vgroup(st):
    """The vertex group the ghosts show only (Show: Vertex Group), or ""."""
    return st.onion_vgroup if st.onion_show == 'VGROUP' else ""


def _group_masks(st, dg):
    """Which proxy vertices are in the onion's Vertex Group (Show: Vertex Group), for the proxies not
    worked out for it yet. From the mesh itself when the modifiers keep its vertices, else from the
    mesh as made. Called by the timer (never while drawing). True when anything changed."""
    name = vgroup(st)
    done = False
    for p in list(_proxies.values()):
        if p.vg_name == name:
            continue
        p.vg_name, p.vg = name, None
        done = True
        o = bpy.data.objects.get(p.name)
        if not name or o is None or p.how == 'LINES' or p.src_inv is None or                 name not in getattr(o, "vertex_groups", {}):
            continue
        gi = o.vertex_groups[name].index
        if o.type == 'MESH' and len(o.data.vertices) == p.src_n:
            p.vg = _in_group(o.data, gi, p.src_inv, len(p.co))
            continue
        ev = o.evaluated_get(dg)
        try:
            me = ev.to_mesh()
        except RuntimeError:
            me = None
        if me is not None and len(me.vertices) == p.src_n:
            p.vg = _in_group(me, gi, p.src_inv, len(p.co))
        if me is not None:
            ev.to_mesh_clear()
    return done


def _weights(p, o, arm, me, inv, K=4):
    """Bone weights of the proxy (a few vertices of each melted group, averaged) and its rest
    shape: the built pose worked back through the bones."""
    import numpy as np
    P = len(p.co)
    names = {g.index: g.name for g in o.vertex_groups}
    bones = {b.name: i for i, b in enumerate(arm.data.bones) if b.use_deform}
    # a few vertices per proxy vertex (the first ones met)
    order = np.argsort(inv, kind='stable')
    starts = np.searchsorted(inv[order], np.arange(P))
    acc = [dict() for _ in range(P)]
    verts = me.vertices
    for pi in range(P):
        s = starts[pi]
        for j in range(s, min(s + 3, len(order))):
            vi = int(order[j])
            if inv[vi] != pi:
                break
            for g in verts[vi].groups:
                b = bones.get(names.get(g.group, ""))
                if b is not None and g.weight > 0.0:
                    acc[pi][b] = acc[pi].get(b, 0.0) + g.weight
    idx = np.zeros((P, K), dtype=np.int64)
    w = np.zeros((P, K), dtype=np.float64)
    for pi, d in enumerate(acc):
        top = sorted(d.items(), key=lambda kv: -kv[1])[:K]
        tot = sum(v for _b, v in top)
        for k, (b, v) in enumerate(top):
            idx[pi, k] = b
            w[pi, k] = v / tot if tot > 0 else 0.0
    # only the bones with weight (a big rig: far fewer matrices per ghost)
    names = [b.name for b in arm.data.bones]
    used = np.unique(idx[w > 0]) if (w > 0).any() else np.array([0])
    remap = np.zeros(max(len(names), 1), dtype=np.int64)
    remap[used] = np.arange(len(used))
    p.bones = [names[int(u)] for u in used]
    p.idx, p.w = remap[idx], w
    p.free = w.sum(axis=1) < 1e-6
    # rest shape, in the armature's space: r = (sum w Ma B(f0))^-1 v(f0) (no weight: Ma^-1 v),
    # so the proxy also follows an armature object that moves
    p.rest_inv = rest_inverses(arm, p.bones)
    Ma, Bs = bone_mats(arm, p.bones, p.rest_inv)
    A = np.einsum('pk,pkij->pij', w, Ma @ Bs[p.idx])        # (P, 4, 4)
    A[p.free] = Ma
    v = np.concatenate([p.co.astype(np.float64), np.ones((P, 1))], axis=1)
    try:
        p.rest = np.einsum('pij,pj->pi', np.linalg.inv(A), v)
    except np.linalg.LinAlgError:
        p.rest = v @ np.linalg.inv(Ma).T


def rest_inverses(arm, names):
    """(B, 4, 4): the inverse rest matrix of every bone (armature space)."""
    import numpy as np
    out = np.empty((len(names), 4, 4))
    for i, name in enumerate(names):
        b = arm.data.bones.get(name)
        out[i] = np.linalg.inv(np.array(b.matrix_local, dtype=np.float64)) if b is not None else np.eye(4)
    return out


_pose_token = [0]       # bumped whenever the pose may have changed (a ghost frame stepped to)
_memo_on = [False]      # only while capturing: anywhere else the pose may change any time
_all_mats = {}          # armature name -> (token, (N, 4, 4) every pose bone's matrix)
_bone_index = {}        # (armature name, id of the names list) -> (names, index of each in pose.bones; -1: gone)
_bone_count = {}        # armature name -> its bone count when its indices were worked out


class pose_memo:
    """with pose_memo(): the bone matrices are read once for everything inside (the pose doesn't
    change meanwhile). Nested uses keep the outer one."""

    def __enter__(self):
        self.outer = _memo_on[0]
        if not self.outer:
            _pose_token[0] += 1
            _memo_on[0] = True
        return self

    def __exit__(self, *exc):
        if not self.outer:
            _memo_on[0] = False
            _all_mats.clear()
        return False


def pose_changed():
    """The pose may be different now (a frame stepped to): bone matrices are read again."""
    _pose_token[0] += 1


def _pose_matrices(arm):
    """(N, 4, 4) every pose bone's matrix, read at once (one foreach_get, not a Python loop over the
    bones for every part of the character); kept until the pose changes."""
    import numpy as np
    got = _all_mats.get(arm.name) if _memo_on[0] else None
    if got is not None and got[0] == _pose_token[0]:
        return got[1]
    bones = arm.pose.bones
    n = len(bones)
    if _bone_count.get(arm.name) != n:
        # (bones added / removed: the bone indices worked out are wrong now)
        _bone_count[arm.name] = n
        for k in [k for k in _bone_index if k[0] == arm.name]:
            del _bone_index[k]
    buf = np.empty(n * 16, dtype=np.float32)
    bones.foreach_get("matrix", buf)
    mats = buf.reshape(-1, 4, 4).transpose(0, 2, 1).astype(np.float64)    # (stored column by column)
    _all_mats[arm.name] = (_pose_token[0], mats)
    return mats


def bone_mats(arm, names, rest_inv):
    """(armature matrix, (B, 4, 4) how every bone bends now in the armature's space: pose @ rest^-1)."""
    import numpy as np
    Ma = np.array(arm.matrix_world, dtype=np.float64)
    mats = _pose_matrices(arm)
    key = (arm.name, id(names))
    got = _bone_index.get(key)
    if got is None or got[0] is not names:
        # (the names list of a proxy / the masses: kept by it, looked up once)
        order = {pb.name: i for i, pb in enumerate(arm.pose.bones)}
        got = _bone_index[key] = (names, np.array([order.get(n, -1) for n in names], dtype=np.int64))
        if len(_bone_index) > 256:
            _bone_index.clear()
            _bone_index[key] = got
    ix = got[1]
    out = np.empty((len(names), 4, 4))
    ok = ix >= 0
    out[ok] = mats[ix[ok]] @ rest_inv[ok]
    out[~ok] = np.eye(4)
    return Ma, out


def rows_of(p, rows):
    """(weights, bone indices, rest positions, no weight) of some vertices of a bent proxy - taken out
    once when the same vertices are bent again and again."""
    return p.w[rows], p.idx[rows], p.rest[rows], p.free[rows]


def pose_rows(p, rows, taken=None):
    """World positions of some vertices of a bent proxy (rows: their indices) for the pose of now
    (taken: rows_of(p, rows), kept by the caller)."""
    import numpy as np
    arm = bpy.data.objects.get(p.arm)
    if arm is None or p.rest is None:
        return p.co[rows]
    Ma, Bs = bone_mats(arm, p.bones, p.rest_inv)
    w, idx, rest, free = taken if taken is not None else rows_of(p, rows)
    v = np.einsum('pij,pj->pi', np.einsum('pk,pkij->pij', w, Bs[idx]), rest)
    if free.any():
        v[free] = rest[free]
    v = v @ Ma.T
    return v[:, :3].astype(np.float32)


def _pose(p):
    """World positions of a bent proxy for the pose of now."""
    import numpy as np
    arm = bpy.data.objects.get(p.arm)
    if arm is None or p.rest is None:
        return p.co
    Ma, Bs = bone_mats(arm, p.bones, p.rest_inv)
    # (in two steps: the blended matrix of every vertex, then the vertex - 3x faster than at once)
    v = np.einsum('pij,pj->pi', np.einsum('pk,pkij->pij', p.w, Bs[p.idx]), p.rest)
    if p.free.any():
        v[p.free] = p.rest[p.free]          # (no weight: it goes with the armature object)
    v = v @ Ma.T
    return v[:, :3].astype(np.float32)


def _exact_shape(o, p, dg):
    """World positions of a proxy from the mesh Blender makes now (shape keys, lattices...), its
    vertices melted together like the proxy's; None if the mesh changed its vertex count."""
    import numpy as np
    ev = o.evaluated_get(dg)
    try:
        me = ev.to_mesh()
    except RuntimeError:
        return None
    try:
        n = len(me.vertices)
        if p.inv is None or n != len(p.inv):
            return None
        co = np.empty(n * 3, dtype=np.float64)
        me.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3)
        M = np.array(ev.matrix_world, dtype=np.float64)
        co = co @ M[:3, :3].T + M[:3, 3]
        out = np.stack([np.bincount(p.inv, weights=co[:, i], minlength=len(p.counts)) for i in range(3)], axis=1)
        return (out / p.counts[:, None]).astype(np.float32)
    finally:
        ev.to_mesh_clear()


def _shown_by_weight(p, aff):
    """By weight: the proxy vertices the affected bones move (half their weight or more)."""
    import numpy as np
    if p.idx is None:
        return None
    mask = np.array([b in aff for b in p.bones] + [False], dtype=bool)
    return (p.w * mask[p.idx]).sum(axis=1) >= 0.5


# ---- capture -----------------------------------------------------------------------------

def clear():
    _curve_memo.clear()
    _frames.clear()
    _batches.clear()
    _stale.clear()
    _previewed.clear()


def clear_all():
    clear()
    _proxies.clear()
    if state.settings() is not None and state.settings().show_onion:
        _schedule(0.05)         # (made again right away, not only at the next redraw)


_EDITING = {'EDIT', 'WEIGHT_PAINT', 'SCULPT', 'VERTEX_PAINT', 'TEXTURE_PAINT'}
_content = {}           # mesh name -> its own shape as last seen (see _mesh_content)


def _mesh_content(me):
    """The mesh's own shape (not what shape keys / modifiers make of it): vertex and face count and
    the vertices' places."""
    import numpy as np
    n = len(me.vertices)
    co = np.empty(n * 3, dtype=np.float32)
    me.vertices.foreach_get("co", co)
    return (n, len(me.polygons), hash(co.tobytes()))


def meshes_changed(mesh_names, keys_moved=()):
    """These meshes were edited (their shape, weights, normals...): their proxies are made again.
    Blender says a mesh's geometry changed also when only its shape keys move (driven by the rig,
    on every frame): that isn't an edit - only a mesh being edited / painted, whose own shape
    changed, or that changed without its shape keys moving (weights set by a script) counts.
    keys_moved: the shape keys (Key datablocks) updated along with it."""
    gone = []
    for name in list(_proxies):
        o = bpy.data.objects.get(name)
        me = getattr(o, "data", None) if o is not None else None
        if me is None or getattr(me, "name", None) not in mesh_names or not hasattr(me, "vertices"):
            continue
        now = _mesh_content(me)
        before = _content.get(me.name)
        _content[me.name] = now
        sk = getattr(me, "shape_keys", None)
        driven = sk is not None and sk.name in keys_moved
        if o.mode in _EDITING or (before is not None and before != now) or not driven:
            gone.append(name)
            del _proxies[name]
    if gone:
        clear()
        _schedule(0.05)
        from . import com
        com.meshes_changed()
    return gone


_curve_memo = {}        # (object name, data path, index) -> (keys (n, 7), modifiers) as last seen


def _watched_curves(scene):
    """[(object, F-Curve)] of what is being edited (the paths' bones / objects), or None when that
    can't be told (no bone, NLA, a looping action): their changes are told apart by frame."""
    from . import compat
    arms, objs = _edited(scene)
    out = []
    for ob in [bpy.data.objects.get(n) for n in arms] + list(objs):
        if ob is None:
            continue
        ad = ob.animation_data
        if ad is None or ad.action is None:
            continue
        if getattr(ad, "use_tweak_mode", False) or any(not t.mute for t in ad.nla_tracks) or                 getattr(ad.action, "use_cyclic", False):
            return None         # (the action's frames aren't the scene's frames)
        fcs = compat.get_fcurves(ob) or ()
        if ob.type == 'ARMATURE':
            bones = arms.get(ob.name)
            if not bones:
                return None
            pre = tuple('pose.bones["%s"]' % bpy.utils.escape_identifier(b) for b in bones)
            # (and the rig object's own curves: its location, rotation, scale)
            out += [(ob, fc) for fc in fcs if fc.data_path.startswith(pre) or not fc.data_path.startswith("pose.")]
        else:
            out += [(ob, fc) for fc in fcs]
            # (a mesh bent by an armature: the armature's curves bend its ghosts too)
            arm = deformed_by(ob)
            if arm is not None and arm.name not in arms:
                ad = arm.animation_data
                if ad is not None and ad.action is not None:
                    if getattr(ad, "use_tweak_mode", False) or any(not t.mute for t in ad.nla_tracks) or \
                            getattr(ad.action, "use_cyclic", False):
                        return None
                    out += [(arm, fc) for fc in compat.get_fcurves(arm) or ()]
    return out


def _key_rows(fc):
    """(n, 7): every key's frame, value, handles and interpolation."""
    import numpy as np
    kps = fc.keyframe_points
    n = len(kps)
    parts = []
    for name in ("co", "handle_left", "handle_right"):
        a = np.empty(n * 2, dtype=np.float32)
        kps.foreach_get(name, a)
        parts.append(a.reshape(n, 2))
    ip = np.empty(n, dtype=np.int32)
    kps.foreach_get("interpolation", ip)
    parts.append(ip.reshape(n, 1).astype(np.float32))
    return np.concatenate(parts, axis=1)


def _changed_span(old, new):
    """(from, to) scene frames whose values can differ between two versions of a curve's keys
    (from the key before the first change to the key after the last one), or None."""
    import numpy as np
    inf = float("inf")
    if len(old) == len(new):
        diff = np.nonzero(np.any(np.abs(old - new) > 1e-6, axis=1))[0]
        if not len(diff):
            return None
        i0, j_old, j_new = int(diff[0]), int(diff[-1]), int(diff[-1])
    else:
        n = min(len(old), len(new))
        same = np.all(np.abs(old[:n] - new[:n]) <= 1e-6, axis=1)
        i0 = int(np.argmin(same)) if not same.all() else n
        tail = 0
        while tail < n - i0 and np.all(np.abs(old[len(old) - 1 - tail] - new[len(new) - 1 - tail]) <= 1e-6):
            tail += 1
        j_old, j_new = len(old) - 1 - tail, len(new) - 1 - tail
    lo = min(rows[i0 - 1, 0] if 0 < i0 <= len(rows) else -inf for rows in (old, new))
    hi = max(rows[j + 1, 0] if 0 <= j + 1 < len(rows) else inf for rows, j in ((old, j_old), (new, j_new)))
    return float(lo), float(hi)


def _remember_curves(watched):
    from .core import _fmod_signature
    for ob, fc in watched:
        _curve_memo[(ob.name, fc.data_path, fc.array_index)] = (
            _key_rows(fc), tuple(_fmod_signature(m) for m in fc.modifiers))


why = [""]          # (what the last edit_spans went by: for tests)
ALL = None          # (edit_spans: every frame)


def edit_spans(scene, moved=True):
    """[(from, to)] scene frames whose poses an edit of the curves may have changed (around the
    keys that changed), [] for none (only what is selected changed), ALL when that can't be told.
    Asked once per change: the ghosts and the center of mass's track both go by it."""
    from .core import _fmod_signature, cycle_info
    try:
        watched = _watched_curves(scene)
    except (ReferenceError, AttributeError):
        watched = None
    if not watched:
        why[0] = "not watched"
        return ALL
    spans = []
    unknown = False
    for ob, fc in watched:
        key = (ob.name, fc.data_path, fc.array_index)
        rows = _key_rows(fc)
        mods = tuple(_fmod_signature(m) for m in fc.modifiers)
        got = _curve_memo.get(key)
        _curve_memo[key] = (rows, mods)
        if got is None:
            unknown = True          # (not seen before: can't tell what changed - all the memo is taken still)
            why[0] = "new curve " + fc.data_path
            continue
        if got[1] != mods:
            unknown = True
            why[0] = "modifiers"
            continue
        span = _changed_span(got[0], rows)
        if span is None:
            continue
        if cycle_info(fc) is not None:
            unknown = True          # (a loop: a change shows on every repeat)
            why[0] = "loop"
            continue
        spans.append(span)
    if unknown:
        return ALL
    if not spans:
        if moved:
            why[0] = "another curve changed"
            return ALL              # (an object moved, but none of these curves: another one did)
        why[0] = "nothing moved"
        return []                   # (only what is selected changed - a click on a key)
    why[0] = "spans %s" % spans
    return spans


def frames_in(frames, spans):
    """Those of the frames inside the spans (all of them for ALL)."""
    if spans is ALL:
        return set(frames)
    return {f for f in frames if any(lo - 1e-3 <= f <= hi + 1e-3 for lo, hi in spans)}


_UNSET = object()


def changed(pose=False, moved=True, spans=_UNSET):
    """The animation changed: the ghosts are captured again right away, the nearest first,
    also while a tool drags (the old ones stay drawn until each is renewed) - only the frames
    around the keys that changed (spans, see edit_spans; all of them for a change of the pose
    itself: pose=True)."""
    global _changed_at
    if spans is _UNSET:
        try:
            spans = ALL if pose else edit_spans(bpy.context.scene, moved)
        except Exception:
            spans = ALL
    if not _frames:
        return
    _stale.update(frames_in(_frames, ALL if pose else spans))
    _changed_at = time.time()
    _schedule(0.01)


def all_frames(scene, st, limit=250):
    """Every frame a ghost can be on (to capture ahead of time): the keys (spaced like the ghosts),
    or every step frames, within the scene's (preview) range - and only so many frames around the
    current one (the preferences: Ghosts Ahead), not the whole timeline."""
    if scene.use_preview_range:
        f0, f1 = scene.frame_preview_start, scene.frame_preview_end
    else:
        f0, f1 = scene.frame_start, scene.frame_end
    from .props import prefs
    p = prefs()
    ahead = p.onion_cache_range if p is not None else 10
    cur = scene.frame_current
    f0, f1 = max(f0, cur - ahead), min(f1, cur + ahead)
    if st.onion_step > 0:
        return [f for f in range(f0, f1 + 1)][:limit]
    keys = _key_frames(scene)
    if len(keys) > 1 and keys[-1] > keys[0] and _loops(scene):
        p = keys[-1] - keys[0]
        keys = sorted({k + n * p for n in range((f0 - keys[-1]) // p, (f1 - keys[0]) // p + 1) for k in keys})
    keys = [k for k in keys if f0 <= k <= f1]
    return keys[:limit]


def ready(scene, st):
    """(frames shown that are ready, frames shown in all), for the note in the timeline."""
    shows = [f for f, _i in frames_to_show(scene, st)]
    return [f for f in shows if f in _frames and f not in _stale], shows


_loop_cache = [None, 0.0, False]


def _loops(scene):
    """The ghosted curves loop (a Cycles modifier: the path's Looping, or set in the Graph
    Editor). Worked out again when the edited set changes, else twice a second."""
    from . import compat
    arms, objs = _edited(scene)
    key = (tuple(sorted(arms)), tuple(o.name for o in objs))
    now = time.time()
    if _loop_cache[0] == key and now - _loop_cache[1] < 0.5:
        return _loop_cache[2]
    out = False
    for ob in [bpy.data.objects.get(n) for n in arms] + list(objs):
        if ob is not None and any(m.type == 'CYCLES' for fc in compat.get_fcurves(ob) or [] for m in fc.modifiers):
            out = True
            break
    _loop_cache[:] = [key, now, out]
    return out


def frames_to_show(scene, st):
    """Ghost frames around the current one: every `onion_step` frames, or the keys (looping
    curves: the keys go on in both directions, one loop after another)."""
    cur = scene.frame_current
    if st.onion_step > 0:
        before = [cur - st.onion_step * i for i in range(1, st.onion_before + 1)]
        after = [cur + st.onion_step * i for i in range(1, st.onion_after + 1)]
    else:
        keys = _key_frames(scene)
        if len(keys) > 1 and keys[-1] > keys[0] and _loops(scene):
            p = keys[-1] - keys[0]
            n0 = (cur - keys[0]) // p
            reps = max(st.onion_before, st.onion_after) // max(len(keys) - 1, 1) + 2
            keys = sorted({k + n * p for n in range(n0 - reps, n0 + reps + 1) for k in keys})
        if not keys:
            # nothing keyed: every 5 frames
            step = 5
            return [(cur - step * (i + 1), -(i + 1)) for i in range(st.onion_before)] + \
                [(cur + step * (i + 1), i + 1) for i in range(st.onion_after)]
        # baked motion (a key on every frame): ghosts on neighboring frames would overlap, so
        # keep the keys at least 4 frames apart
        gaps = sorted(b - a for a, b in zip(keys, keys[1:]))
        gap = 4 if gaps and gaps[len(gaps) // 2] <= 1.5 else 1

        def spaced(ts):
            out, last = [], cur
            for t in ts:
                if abs(t - last) >= gap:
                    out.append(t)
                    last = t
            return out
        before = spaced([t for t in reversed(keys) if t < cur])[:st.onion_before]
        after = spaced([t for t in keys if t > cur])[:st.onion_after]
    return [(f, -(i + 1)) for i, f in enumerate(before)] + [(f, i + 1) for i, f in enumerate(after)]


def _capture(scene, frames, targets, st):
    """The ghosts on these frames. The bent meshes are left out of the evaluation (only their
    armatures are evaluated); their proxies are bent here."""
    import numpy as np
    hidden = []
    # a bent mesh something is parented to (eyes on a vertex of the head) has to be evaluated
    # for real, or that child wouldn't follow it
    parents = {o.parent.name for o, _a, _h in targets if o.parent is not None}
    try:
        exact = {o.name for o, _arm, how in targets if how == 'DEFORM' and
                 getattr(_proxies.get(o.name), "exact", False)}
        for o, _arm, how in targets:
            # (exact meshes are made by Blender on every ghost frame: they stay in)
            if how == 'DEFORM' and not o.hide_viewport and o.name not in parents and o.name not in exact and \
                    state.can_hide(o, len(frames)):
                o.hide_viewport = True
                hidden.append(o)
        arms = {a.name: a for _o, a, _h in targets if a is not None}
        for name in _edited(scene)[0]:
            arms.setdefault(name, bpy.data.objects[name])
        # other characters' bent meshes (nothing of the ghosts depends on them) are left out of the
        # evaluation too - like the paths' bake: only what the ghosts need is worked out per frame
        from .core import _meshes_not_needed
        needed = {o.name: o for o, _a, _h in targets}
        needed.update(arms)
        for o in _meshes_not_needed(scene, needed):
            if o.name not in needed and o.name not in parents and state.can_hide(o, len(frames)):
                o.hide_viewport = True
                hidden.append(o)
        dg = bpy.context.evaluated_depsgraph_get() if exact else None
        # the bones (Bones mode only: switching to it captures the ghosts again)
        want_bones = mode_used(scene, st) == 'BONES'
        shown = {name: np.array([_bone_shown(pb, st) for pb in a.pose.bones], dtype=bool)
                 for name, a in arms.items()} if want_bones else {}
        _memo_on[0] = True
        for f in frames:
            scene.frame_set(int(f))
            pose_changed()
            parts = []
            for o, _arm, how in targets:
                p = _proxies.get(o.name)
                if p is None:
                    continue
                if how == 'DEFORM':
                    pos = _exact_shape(o, p, dg) if o.name in exact else None
                    if pos is not None or p.rest is None:
                        parts.append((p.name, pos if pos is not None else _pose(p), None, None))
                    else:
                        # (bent on the GPU when drawn: only how every bone bends is kept)
                        parts.append((p.name, None, None, _skin_mats(p)))
                else:
                    parts.append((p.name, None, o.matrix_world.copy(), None))
            bones = []
            for arm in (arms.values() if want_bones else ()):
                # (all heads and tails at once; which bones show was worked out once above)
                n = len(arm.pose.bones)
                ht = np.empty((2, n * 3), dtype=np.float32)
                arm.pose.bones.foreach_get("head", ht[0])
                arm.pose.bones.foreach_get("tail", ht[1])
                Mw = np.array(arm.matrix_world, dtype=np.float32)
                seg = np.stack([ht[0].reshape(n, 3), ht[1].reshape(n, 3)], axis=1)[shown[arm.name]]
                bones.append((seg.reshape(-1, 3) @ Mw[:3, :3].T + Mw[:3, 3]).astype(np.float32))
            _frames[f] = {"parts": parts, "bones": np.concatenate(bones) if bones else None}
            try:
                from . import com
                if not _previewing[0]:          # (a pose not keyed, put on for a moment: not the track's)
                    com.visit(scene, int(f))    # (the center of mass's track takes this frame too)
            except Exception as ex:
                print("Editable Motion Paths: center of mass on a ghost frame:", ex)
            for key in [k for k in _batches if k[0] == f]:
                del _batches[key]
    finally:
        _memo_on[0] = False
        _all_mats.clear()
        for o in hidden:
            o.hide_viewport = False
        _shown_again[:] = hidden


_previewed = set()          # frames captured with an unkeyed pose put on the curves
_previewing = [False]       # capturing with that pose on right now
_previewed_at = [None]      # on which frame that pose was


_TF = {"location", "rotation_euler", "rotation_quaternion", "rotation_axis_angle", "scale"}


def _pending_keys(scene):
    """Auto keying during Blender's own G / R / S: [(object, F-Curve, value)] that the drag will
    key on the current frame once confirmed - the channels of the selected bones / objects whose
    pose differs from their curve now."""
    from . import compat
    f = scene.frame_current
    arms, objs = _edited(scene)
    owners = [bpy.data.objects.get(n) for n in arms] + [o for o in objs if o.select_get()]
    out = []
    for ob in owners:
        if ob is None:
            continue
        ad = ob.animation_data
        if ad is None or ad.action is None or getattr(ad, "use_tweak_mode", False) or \
                ad.action_blend_type != 'REPLACE' or ad.action_influence < 1.0 - 1e-6 or \
                any(not t.mute for t in ad.nla_tracks):
            continue            # (the pose is more than the action's curves: NLA, blending)
        driven = {(d.data_path, d.array_index) for d in ad.drivers}
        sel = ob.select_get()
        for fc in compat.get_fcurves(ob) or []:
            if (fc.data_path, fc.array_index) in driven:
                continue        # (a driver sets that channel, not the curve)
            dp = fc.data_path
            prop = dp.rsplit(".", 1)[-1]
            if prop not in _TF or fc.mute:
                continue
            if dp.startswith('pose.bones["'):
                m = state._BONE_PATH.match(dp)
                name = m.group(1).replace('\\"', '"').replace('\\\\', '\\') if m else None
                pb = ob.pose.bones.get(name) if ob.pose and name is not None else None
                if pb is None or not compat.bone_selected(pb):
                    continue
                owner = pb
            elif "." in dp or not sel:
                continue
            else:
                owner = ob
            try:
                v = getattr(owner, prop)[fc.array_index]
            except (AttributeError, IndexError, TypeError):
                continue
            if abs(v - fc.evaluate(f)) > 1e-5:
                out.append((ob, fc, v))
    return out


def _put_keys(pending, f, undo):
    """The pending keys put on the curves for a moment (to capture what the neighbors will look
    like) - on a COPY of each action, assigned for the capture: the real curves are never touched,
    so a transform running elsewhere (keys dragged in the Graph Editor holds pointers into them)
    is safe. How to put the real actions back goes into `undo` as soon as a copy is made (also if
    something fails half way: the caller takes them all off again)."""
    from . import compat
    by_ob = {}
    for ob, fc, v in pending:
        by_ob.setdefault(ob.name, (ob, []))[1].append((fc.data_path, fc.array_index, v))
    for ob, items in by_ob.values():
        ad = ob.animation_data
        orig = ad.action if ad is not None else None
        if orig is None or getattr(ad, "use_tweak_mode", False):
            continue
        slot = getattr(ad, "action_slot", None)
        slot_id = slot.identifier if slot is not None else None
        tmp = orig.copy()
        undo.append((ad, orig, slot_id, tmp))
        ad.action = tmp
        if slot_id is not None and hasattr(tmp, "slots"):
            s = tmp.slots.get(slot_id)
            if s is not None and ad.action_slot != s:
                ad.action_slot = s
        coll = compat.get_fcurves(ob)
        for path, index, v in items:
            fc = coll.find(path, index=index) if coll is not None else None
            if fc is None:
                continue
            kp = next((k for k in fc.keyframe_points if abs(k.co[0] - f) < 1e-3), None)
            if kp is None:
                fc.keyframe_points.insert(f, v, options={'FAST'})
            else:
                dv = v - kp.co[1]
                kp.co[1] = v
                kp.handle_left[1] += dv
                kp.handle_right[1] += dv
            fc.update()


def _take_keys_off(undo, f):
    """The real actions back; the copies go (each one on its own: one failing never leaves
    another object on its copy)."""
    for ad, orig, slot_id, tmp in reversed(undo):
        try:
            if ad.action != orig:
                ad.action = orig
            if slot_id is not None and hasattr(orig, "slots"):
                s = orig.slots.get(slot_id)
                if s is not None and ad.action_slot != s:
                    ad.action_slot = s
        except Exception as ex:
            print("Editable Motion Paths: couldn't put the action back:", ex)
        try:
            bpy.data.actions.remove(tmp)
        except Exception:
            pass
    undo.clear()


_shown_again = []      # the meshes the last capture hid and showed again


def _schedule(delay=0.05):
    global _timer_on
    if not _timer_on:
        _timer_on = True
        bpy.app.timers.register(_tick, first_interval=delay)


def _tick():
    """Build missing proxies (one object per tick, with a note), then capture missing or
    changed ghost frames, the nearest first, about 40 ms per tick."""
    global _timer_on, _busy, _sig, _cost, status
    batch = []
    try:
        scene = bpy.context.scene
        st = state.settings(scene)
        if st is None or not st.show_onion:
            status = ""
            _timer_on = False
            return None
        if state.render_running():
            return 0.5              # (a render reads the scene: no frame stepping meanwhile)
        screen = bpy.context.screen
        playing = screen is not None and screen.is_animation_playing
        # Blender's own G / R / S: the dragged pose isn't keyed yet. With auto keying the ghosts
        # show what the neighbors will look like once it is (the pose put on the curves for a
        # moment); without it nothing changes on the other frames, so they wait
        editing = state.blender_edit_running()
        # (Preview Unkeyed Pose: also without auto keying, during the drag and after it)
        preview = st.onion_preview_pose or (editing and scene.tool_settings.use_keyframe_insert_auto)
        if editing and not preview:
            return 0.1
        if (state.modal_running or editing) and _cost > 0.03 and time.time() - _changed_at < 0.15:
            return 0.05
        # a change made a moment ago that Blender hasn't worked in yet would be worked in by the
        # frame stepping below - where it counts as ours: work it in now, so it is seen as a change
        state.flush_changes()
        sig = _signature(scene, st)
        if sig != _sig:
            clear()
            _sig = sig
        targets = _targets(scene)
        # proxies first: one object per tick, so Blender stays responsive
        todo = [t for t in targets if t[0].name not in _proxies or _proxies[t[0].name].sig !=
                (_mesh_sig(t[0]), t[2], t[1].name if t[1] else None, st.onion_max_tris)]
        if todo:
            status = "Preparing ghosts: %d / %d" % (len(targets) - len(todo) + 1, len(targets))
            state.tag_redraw()
            _busy = True
            try:
                _build(todo[0][0], todo[0][1], todo[0][2], st, bpy.context.evaluated_depsgraph_get())
            finally:
                _busy = False
            clear()
            return 0.01
        status = ""
        if _group_masks(st, bpy.context.evaluated_depsgraph_get()):
            state.tag_redraw()
        shows = sorted(frames_to_show(scene, st), key=lambda x: abs(x[1]))
        want = [f for f, _i in shows if f not in _frames] + [f for f, _i in shows if f in _stale]
        if not want and not state.modal_running and not editing:
            # ahead of time: every frame a ghost can be on (playback then finds them ready),
            # the nearest first
            cur_f = scene.frame_current
            want = sorted((f for f in all_frames(scene, st) if f not in _frames or f in _stale),
                          key=lambda f: abs(f - cur_f))
        if not want:
            _timer_on = False
            return None
        if playing:
            want = want[:1]         # (while playing: a frame per tick, the fps drops a little)
        cur, sub = scene.frame_current, scene.frame_subframe
        t0 = time.time()
        _busy = True
        held = state.keep_scene_pose(scene, [o for o, _a, _h in targets] +
                                     [bpy.data.objects.get(n) for n in _edited(scene)[0]])
        undo = []
        try:
            if preview:
                _put_keys(_pending_keys(scene), cur, undo)
            _previewing[0] = bool(undo)
            done = []
            # as many frames as fit in ~40 ms (a heavy character: one a tick, so Blender keeps
            # answering; a light one: up to 4)
            most = max(1, min(4, int(0.04 / _cost))) if _cost > 0 else 1
            for f in want:
                batch.append(f)
                if len(batch) >= most:
                    break
            t1 = time.time()
            for f in batch:
                _stale.discard(f)
            _capture(scene, batch, targets, st)
            done += batch
            if undo:
                # captured with the pose put on the curves: captured again once the frame changes
                # (the pose that isn't keyed is gone then)
                _previewed.update(batch)
                _previewed_at[0] = cur
            else:
                _previewed.difference_update(batch)
            per = (time.time() - t1) / max(len(batch), 1)
            _cost = per if not _cost else _cost * 0.7 + per * 0.3
        finally:
            _previewing[0] = False
            _take_keys_off(undo, cur)
            t_back = time.time()
            scene.frame_set(cur, subframe=sub)
            state.unhide_cost(_shown_again, time.time() - t_back)
            _shown_again.clear()
            if state.put_pose_back(held):     # (what isn't keyed stays as it was)
                bpy.context.view_layer.update()     # (now, so it doesn't count as an edit)
            _busy = False
        state.tag_redraw()
        try:
            w = _watched_curves(scene)
            if w:
                _remember_curves([(o, fc) for o, fc in w if (o.name, fc.data_path, fc.array_index) not in _curve_memo])
        except (ReferenceError, AttributeError):
            pass
        if len(done) < len(want) or time.time() - t0 > 0.04:
            return 0.01
        if not state.modal_running and not editing and any(f not in _frames or f in _stale for f in all_frames(scene, st)):
            return 0.02             # the ghosts shown are ready: on with the rest, ahead of time
        _timer_on = False
        return None
    except Exception as ex:   # never let the timer die with a traceback storm
        _busy = False
        status = ""
        # the frames of this step weren't captured: they stay to do (else they'd stay drawn as
        # they were, marked ready); tried again a bit later
        _stale.update(f for f in batch if f in _frames)
        key = (type(ex).__name__, str(ex)[:80])
        if key not in _errors:
            _errors.add(key)
            import traceback
            traceback.print_exc()
            print("Editable Motion Paths: onion skin failed:", ex)
            state.flash_message("Onion skin: a ghost couldn't be captured (%s) - see the console" %
                                type(ex).__name__, 5.0)
        return 1.0


_errors = set()     # (kind, text) of the capture errors reported (each once)


def ghost(f, kind):
    """World positions of a captured ghost (for scripts and tests): "tris" (3 per triangle),
    "edges" (2 per line), "points" or "bones" (2 per bone). None if not captured."""
    import numpy as np
    data = _frames.get(f)
    if data is None:
        return None
    if kind == "bones":
        return data.get("bones")
    out = []
    for name, pos, M, skin in data["parts"]:
        p = _proxies.get(name)
        if p is None:
            continue
        co = pos if skin is None else _skin_positions(p, skin)
        if co is None:
            m = np.array(M, dtype=np.float64)
            co = (p.co @ m[:3, :3].T + m[:3, 3]).astype(np.float32) if len(p.co) else p.co
        if kind == "tris" and len(p.tri):
            out.append(co[p.tri.ravel()])
        elif kind == "edges" and p.edges is not None and len(p.edges):
            out.append(co[p.edges.ravel()])
        elif kind == "points" and len(co):
            out.append(co)
    return np.concatenate(out) if out else None


# ---- drawing -----------------------------------------------------------------------------

# ---- GPU skinning: a bent proxy is drawn from its rest shape, bent in the vertex shader -------
# (per ghost frame only how its bones bend is kept: no positions worked out on the CPU, no new
# vertex buffer for every frame)

_SKIN_VS = """
mat4 bone(int i)
{
  return mat4(texelFetch(bones, ivec2(i * 4, 0), 0), texelFetch(bones, ivec2(i * 4 + 1, 0), 0),
              texelFetch(bones, ivec2(i * 4 + 2, 0), 0), texelFetch(bones, ivec2(i * 4 + 3, 0), 0));
}

void main()
{
  vec4 r = vec4(rest, 1.0);
  vec4 p = bw.x * (bone(int(bidx.x)) * r) + bw.y * (bone(int(bidx.y)) * r) +
           bw.z * (bone(int(bidx.z)) * r) + bw.w * (bone(int(bidx.w)) * r);
  gl_Position = viewProjectionMatrix * vec4(p.xyz, 1.0);
}
"""

_SKIN_FS = """
void main()
{
  /* the 3D view draws into an sRGB target: the color goes in linear (like Blender's own shaders) */
  vec3 c = color.rgb;
  vec3 lin = mix(c / 12.92, pow((c + 0.055) / 1.055, vec3(2.4)), step(vec3(0.04045), c));
  fragColor = vec4(mix(c, lin, srgbTarget), color.a);
}
"""

_skin_sh = [None]       # the shader, or False where it can't be made (then the CPU bends)
SRGB_TARGET = [1.0]     # 1: the target stores sRGB (the 3D view) - the color is made linear first


def _skin_shader():
    if _skin_sh[0] is None:
        try:
            info = gpu.types.GPUShaderCreateInfo()
            info.push_constant('MAT4', "viewProjectionMatrix")
            info.push_constant('VEC4', "color")
            info.push_constant('FLOAT', "srgbTarget")
            info.sampler(0, 'FLOAT_2D', "bones")
            info.vertex_in(0, 'VEC3', "rest")
            info.vertex_in(1, 'VEC4', "bidx")
            info.vertex_in(2, 'VEC4', "bw")
            info.fragment_out(0, 'VEC4', "fragColor")
            info.vertex_source(_SKIN_VS)
            info.fragment_source(_SKIN_FS)
            _skin_sh[0] = gpu.shader.create_from_info(info)
        except Exception as ex:      # (an old or odd GPU / backend: bent on the CPU as before)
            print("Editable Motion Paths: ghosts bent on the CPU (%s)" % str(ex).splitlines()[0][:120])
            _skin_sh[0] = False
    return _skin_sh[0] or None


def _skin_mats(p):
    """(B + 1, 4, 4) float32: how every bone of a bent proxy bends now, in world space; the last
    one is the armature object (vertices without weight go with it)."""
    import numpy as np
    arm = bpy.data.objects.get(p.arm)
    Ma, Bs = bone_mats(arm, p.bones, p.rest_inv)
    return np.concatenate([Ma[None] @ Bs, Ma[None]]).astype(np.float32)


def _skin_layout(p):
    """(bone index, weight) per vertex as 4 floats each (vertices without weight: the armature)."""
    import numpy as np
    got = getattr(p, "_skin", None)
    if got is not None:
        return got
    P, K = p.idx.shape
    idx = np.zeros((P, 4), dtype=np.float32)
    w = np.zeros((P, 4), dtype=np.float32)
    k = min(K, 4)
    idx[:, :k] = p.idx[:, :k]
    w[:, :k] = p.w[:, :k]
    if K > 4:
        w /= np.maximum(w.sum(axis=1, keepdims=True), 1e-12)
    free = p.free if p.free is not None else np.zeros(P, dtype=bool)
    idx[free] = 0.0
    w[free] = 0.0
    idx[free, 0] = len(p.bones)
    w[free, 0] = 1.0
    p._skin = (idx, w)
    return p._skin


def _skin_positions(p, skin):
    """World positions of a bent proxy from its bones' matrices (CPU: scripts, and where the GPU
    can't bend it)."""
    import numpy as np
    idx, w = _skin_layout(p)
    ii = idx.astype(np.int64)
    M = np.einsum('pk,pkij->pij', w.astype(np.float64), skin[ii].astype(np.float64))
    v = np.einsum('pij,pj->pi', M, p.rest)
    return v[:, :3].astype(np.float32)


def _skin_batch(kind, name, shader, keep):
    """The rest shape of a bent proxy with its weights, made once (every frame draws it)."""
    import numpy as np
    key = (None, "skin-" + kind, name)
    b = _batches.get(key)
    if b is not None:
        return b
    p = _proxies.get(name)
    if p is None or p.rest is None:
        return None
    idx, w = _skin_layout(p)
    attrs = {"rest": p.rest[:, :3].astype(np.float32), "bidx": idx, "bw": w}
    if kind == "tris":
        el = p.tri
    else:
        el = p.edges
    if el is None or not len(el):
        return None
    if keep is not None:
        el = el[keep[el].all(axis=1)]
        if not len(el):
            return None
    b = batch_for_shader(shader, 'TRIS' if kind == "tris" else 'LINES', attrs, indices=el.astype(np.int32))
    _batches[key] = b
    return b


def _skin_texture(f, name, skin):
    """This ghost frame's bone matrices as a texture (4 texels a bone, column by column)."""
    key = (f, "skin-bones", name)
    t = _batches.get(key)
    if t is not None:
        return t
    try:
        cols = skin.transpose(0, 2, 1).ravel()
        buf = gpu.types.Buffer('FLOAT', len(cols), cols.tolist())
        t = gpu.types.GPUTexture((len(skin) * 4, 1), format='RGBA32F', data=buf)
    except Exception as ex:
        print("Editable Motion Paths: ghost bones texture failed:", ex)
        return None
    _batches[key] = t
    return t


def _point_shader():
    """Points need a shader that sets their size (the plain one ignores point_size_set on
    Vulkan / Metal)."""
    try:
        return gpu.shader.from_builtin('POINT_UNIFORM_COLOR')
    except (ValueError, TypeError):
        return gpu.shader.from_builtin('UNIFORM_COLOR')


def _part_batch(f, kind, name, pos, shader, keep):
    """A ghost part's batch: bent parts per frame, instanced ones once (key frame None)."""
    import numpy as np
    key = (f, kind, name)
    b = _batches.get(key)
    if b is not None:
        return b
    p = _proxies.get(name)
    if p is None:
        return None
    co = pos if pos is not None else p.co
    if co is None or not len(co):
        return None
    if kind == "tris":
        tri = p.tri
        if keep is not None and len(tri):
            tri = tri[keep[tri].all(axis=1)]
        if not len(tri):
            return None
        b = batch_for_shader(shader, 'TRIS', {"pos": co}, indices=tri.astype(np.int32))
    elif kind == "edges":
        ed = p.edges
        if keep is not None and len(ed):
            ed = ed[keep[ed].all(axis=1)]
        if ed is None or not len(ed):
            return None
        b = batch_for_shader(shader, 'LINES', {"pos": co}, indices=ed.astype(np.int32))
    else:
        pts = co if keep is None else co[keep]
        if not len(pts):
            return None
        b = batch_for_shader(shader, 'POINTS', {"pos": pts})
    _batches[key] = b
    return b


_filter_cache = [None, None]


def _filters(scene, st):
    """{proxy name: kept vertices or None}, and the names left out (by object); worked out once
    per selection / setting (the signature) and proxy set."""
    vg = vgroup(st)
    if st.onion_show == 'WHOLE' or (st.onion_show == 'VGROUP' and not vg):
        key = None
    else:
        key = (_sig, st.onion_show, vg,
               tuple(sorted((n, id(p), p.vg_name if vg else None) for n, p in _proxies.items())))
    if _filter_cache[0] == key and _filter_cache[1] is not None:
        return _filter_cache[1]
    if _filter_cache[1] is not None:
        # (other vertices kept: the batches made with the old ones go - the ghosts themselves stay)
        for k in [k for k in _batches if k[1] != "bones"]:
            del _batches[k]
    _filter_cache[1] = ({}, set()) if key is None else _filters_now(scene, st)
    _filter_cache[0] = key
    return _filter_cache[1]


def _filters_now(scene, st):
    arms, objs = _edited(scene)
    aff = {}
    for name, edited in arms.items():
        arm = bpy.data.objects.get(name)
        if arm is not None:
            aff[name] = affected_bones(arm, edited)
    keep, out = {}, set()
    for p in _proxies.values():
        if st.onion_show == 'VGROUP':
            pass
        elif p.how == 'DEFORM' and p.arm in aff:
            k = _shown_by_weight(p, aff[p.arm])
            if st.onion_show == 'WEIGHT':
                keep[p.name] = k
            elif k is None or not k.any():
                out.add(p.name)
        elif p.how != 'DEFORM' and st.onion_show == 'OBJECT':
            o = bpy.data.objects.get(p.name)
            if o is not None and o.parent is not None and o.parent.type == 'ARMATURE' and \
                    o.parent.name in aff and o.parent_type == 'BONE' and o.parent_bone not in aff[o.parent.name]:
                out.add(p.name)
    if vgroup(st):
        # only the chosen vertex group (what has none isn't shown; not worked out yet: whole)
        for p in _proxies.values():
            if p.vg_name != vgroup(st):
                continue
            if p.vg is None or not p.vg.any():
                out.add(p.name)
            elif p.name not in out:
                k = keep.get(p.name)
                keep[p.name] = p.vg if k is None else (p.vg & k)
    return keep, out


def draw(scene, st, us):
    """Draw the ghosts (called from the 3D view's draw handler)."""
    global _sig
    if not st.show_onion or _busy:
        return
    sig = _signature(scene, st)
    if sig != _sig:
        # other bones / objects are edited now: their ghosts are captured anew
        clear()
        _sig = sig
    if _previewed and scene.frame_current != _previewed_at[0]:
        # ghosts of a pose that wasn't keyed: another frame now, that pose is gone - they go at
        # once (not drawn on until each is captured again)
        for f in _previewed:
            _frames.pop(f, None)
            _stale.discard(f)
        for key in [k for k in _batches if k[0] in _previewed]:
            del _batches[key]
        _previewed.clear()
        _schedule()
    shows = frames_to_show(scene, st)
    if any(f not in _frames or f in _stale for f, _i in shows) or status:
        _schedule()
    mode = mode_used(scene, st)
    kind = {'SILHOUETTE': "tris", 'WIRE': "edges", 'POINTS': "points", 'BONES': "bones"}[mode]
    sh = _point_shader() if kind == "points" else gpu.shader.from_builtin('UNIFORM_COLOR')
    keep, left_out = _filters(scene, st) if kind != "bones" else ({}, set())
    prev_test = gpu.state.depth_test_get()
    # (in front: the depth still has to be written for the one-layer silhouette, and with the
    # depth test off nothing is written - so "always" passes instead)
    test = 'ALWAYS' if st.onion_in_front else 'LESS_EQUAL'
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set(test)
    n_b = max(1, st.onion_before)
    n_a = max(1, st.onion_after)
    for f, i in sorted(shows, key=lambda x: -abs(x[1])):     # the farthest first
        data = _frames.get(f)
        if data is None:
            continue
        n = n_b if i < 0 else n_a
        fade = (n - abs(i) + 1) / float(n)                  # the farther, the fainter
        col = tuple(st.onion_color_before if i < 0 else st.onion_color_after)
        a = st.onion_opacity * max(0.08, fade ** 1.5)
        if kind == "bones":
            pts = data.get("bones")
            if pts is None or not len(pts):
                continue
            b = _batches.get((f, "bones", ""))
            if b is None:
                b = _batches[(f, "bones", "")] = batch_for_shader(sh, 'LINES', {"pos": pts})
            gpu.state.line_width_set(st.onion_size * us)
            sh.uniform_float("color", col + (min(1.0, a * 1.6),))
            b.draw(sh)
            gpu.state.line_width_set(1.0)
            continue
        draws = []
        skin_sh = _skin_shader() if kind in ("tris", "edges") else None
        for name, pos, M, skin in data["parts"]:
            if name in left_out:
                continue
            p = _proxies.get(name)
            if p is None:
                continue
            pkind = "edges" if p.how == 'LINES' or (kind == "tris" and not len(p.tri)) else kind
            if skin is not None and skin_sh is not None:
                b = _skin_batch(pkind, name, skin_sh, keep.get(name))
                tex = _skin_texture(f, name, skin)
                if b is not None and tex is not None:
                    draws.append((b, tex, pkind))
                continue
            if skin is not None:
                pos = _skin_positions(p, skin)          # (no GPU skinning here: bent on the CPU)
            b = _part_batch(f if pos is not None else None, pkind, name, pos, sh if pkind == kind else
                            gpu.shader.from_builtin('UNIFORM_COLOR'), keep.get(name))
            if b is not None:
                draws.append((b, M, pkind))
        state_col = [None]

        def each(fn):
            for b, M, pkind in draws:
                if isinstance(M, gpu.types.GPUTexture):
                    # bent on the GPU: the same shape every frame, this frame's bones
                    skin_sh.bind()
                    skin_sh.uniform_float("viewProjectionMatrix",
                                          gpu.matrix.get_projection_matrix() @ gpu.matrix.get_model_view_matrix())
                    skin_sh.uniform_float("color", state_col[0])
                    skin_sh.uniform_float("srgbTarget", SRGB_TARGET[0])
                    skin_sh.uniform_sampler("bones", M)
                    b.draw(skin_sh)
                    continue
                if M is not None:
                    gpu.matrix.push()
                    gpu.matrix.multiply_matrix(M)       # instanced: one shape, a matrix per ghost
                fn(b, pkind)
                if M is not None:
                    gpu.matrix.pop()
        if kind == "tris":
            # one flat layer per ghost: depth first, then the color only where it is in front
            # (back faces are skipped: half the work, and nothing changes in a silhouette)
            gpu.state.face_culling_set('BACK')
            gpu.state.depth_mask_set(True)
            gpu.state.color_mask_set(False, False, False, False)
            sh.uniform_float("color", (0, 0, 0, 0))
            state_col[0] = (0.0, 0.0, 0.0, 0.0)
            draws_all = draws
            draws = [d for d in draws_all if d[2] == "tris"]
            each(lambda b, k: b.draw(sh))
            gpu.state.color_mask_set(True, True, True, True)
            gpu.state.depth_test_set('EQUAL')
            sh.uniform_float("color", col + (a,))
            state_col[0] = col + (a,)
            each(lambda b, k: b.draw(sh))
            draws = [d for d in draws_all if d[2] == "edges"]
            state_col[0] = col + (min(1.0, a * 1.6),)
            gpu.state.depth_test_set(test)
            gpu.state.depth_mask_set(False)
            gpu.state.face_culling_set('NONE')
            lines = gpu.shader.from_builtin('UNIFORM_COLOR')
            gpu.state.line_width_set(max(1.0, st.onion_size * us * 0.5))
            lines.uniform_float("color", col + (min(1.0, a * 1.6),))
            each(lambda b, k: b.draw(lines) if k == "edges" else None)
            gpu.state.line_width_set(1.0)
        else:
            if kind == "edges":
                gpu.state.line_width_set(st.onion_size * us * 0.4)
            else:
                gpu.state.point_size_set(st.onion_size * us)
            sh.uniform_float("color", col + (min(1.0, a * 1.6),))
            lines = gpu.shader.from_builtin('UNIFORM_COLOR')
            lines.uniform_float("color", col + (min(1.0, a * 1.6),))
            state_col[0] = col + (min(1.0, a * 1.6),)
            each(lambda b, k: b.draw(sh if k == kind else lines))
            gpu.state.line_width_set(1.0)
            gpu.state.point_size_set(1.0)
    gpu.state.depth_mask_set(False)
    gpu.state.depth_test_set(prev_test)
