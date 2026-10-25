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
    chain to its controller, Copy Transforms...) with their own children."""
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
        arm = bpy.data.objects[name]
        for o in scene.objects:
            if not o.visible_get():
                continue
            if o.type == 'MESH' and any(m.type == 'ARMATURE' and m.object == arm and m.show_viewport
                                        for m in o.modifiers):
                add(o, arm, 'DEFORM')
            elif o.parent == arm and _how(o):
                add(o, arm, _how(o))
    for obj in objs:
        if _how(obj):
            add(obj, None, _how(obj))
        for o in obj.children_recursive:
            if o.visible_get() and _how(o):
                add(o, None, _how(o))
    return out


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


def modes(scene):
    """The onion modes that show something for what is ghosted now: Silhouette (something with
    faces), Wire and Points (meshes), Bones (armatures); only shapes of lines (cameras, lights,
    empties): Wire. (Worked out again when the edited set changes, else twice a second.)"""
    arms, objs = _edited(scene)
    key = (tuple(sorted(arms)), tuple(o.name for o in objs))
    now = time.time()
    if _modes_cache[0] == key and now - _modes_cache[1] < 0.5:
        return _modes_cache[2]
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
    if st.onion_mode != 'BONES' and mode_used(scene, st) != 'BONES':
        return None
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
            st.onion_max_tris, st.onion_bones, st.onion_show, _bone_state(scene, st))


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
        self.arm = None
        self.M0 = None          # object matrix when built
        self.rest_inv = None    # DEFORM: (B, 4, 4) inverse rest matrices of the bones


def _mesh_sig(o):
    mods = tuple((m.type, m.show_viewport, getattr(m, "levels", None)) for m in o.modifiers)
    n = len(o.data.vertices) if getattr(o.data, "vertices", None) is not None else 0
    return (o.type, n, mods)


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
    if n and (len(tri) > limit or len(ed) > limit * 1.5 or n > limit):
        co, tri, ed, inv = _decimate(co, tri, limit, ed)
    p.co, p.tri, p.edges = co, tri, ed
    p.M0 = Matrix(ev.matrix_world)
    if how == 'DEFORM':
        _weights(p, o, arm, me, inv)
    ev.to_mesh_clear()
    _proxies[o.name] = p
    return True


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


def bone_mats(arm, names, rest_inv):
    """(armature matrix, (B, 4, 4) how every bone bends now in the armature's space: pose @ rest^-1)."""
    import numpy as np
    Ma = np.array(arm.matrix_world, dtype=np.float64)
    out = np.empty((len(names), 4, 4))
    bones = arm.pose.bones
    for i, name in enumerate(names):
        pb = bones.get(name)
        out[i] = np.array(pb.matrix, dtype=np.float64) @ rest_inv[i] if pb is not None else np.eye(4)
    return Ma, out


def _pose(p):
    """World positions of a bent proxy for the pose of now."""
    import numpy as np
    arm = bpy.data.objects.get(p.arm)
    if arm is None or p.rest is None:
        return p.co
    Ma, Bs = bone_mats(arm, p.bones, p.rest_inv)
    v = np.einsum('pk,pkij,pj->pi', p.w, Bs[p.idx], p.rest)
    if p.free.any():
        v[p.free] = p.rest[p.free]          # (no weight: it goes with the armature object)
    v = v @ Ma.T
    return v[:, :3].astype(np.float32)


def _shown_by_weight(p, aff):
    """By weight: the proxy vertices the affected bones move (half their weight or more)."""
    import numpy as np
    if p.idx is None:
        return None
    mask = np.array([b in aff for b in p.bones] + [False], dtype=bool)
    return (p.w * mask[p.idx]).sum(axis=1) >= 0.5


# ---- capture -----------------------------------------------------------------------------

def clear():
    _frames.clear()
    _batches.clear()
    _stale.clear()


def clear_all():
    clear()
    _proxies.clear()


def changed():
    """The animation changed: the ghosts are captured again right away, the nearest first,
    also while a tool drags (the old ones stay drawn until each is renewed)."""
    global _changed_at
    if _frames:
        _stale.update(_frames)
        _changed_at = time.time()
        _schedule(0.01)


def all_frames(scene, st, limit=250):
    """Every frame a ghost can be on (to capture ahead of time): the keys (spaced like the ghosts),
    or every step frames, within the scene's (preview) range."""
    if scene.use_preview_range:
        f0, f1 = scene.frame_preview_start, scene.frame_preview_end
    else:
        f0, f1 = scene.frame_start, scene.frame_end
    if st.onion_step > 0:
        return [f for f in range(f0, f1 + 1)][:limit]
    keys = [k for k in _key_frames(scene) if f0 <= k <= f1]
    return keys[:limit]


def ready(scene, st):
    """(frames shown that are ready, frames shown in all), for the note in the timeline."""
    shows = [f for f, _i in frames_to_show(scene, st)]
    return [f for f in shows if f in _frames and f not in _stale], shows


def frames_to_show(scene, st):
    """Ghost frames around the current one: every `onion_step` frames, or the keys."""
    cur = scene.frame_current
    if st.onion_step > 0:
        before = [cur - st.onion_step * i for i in range(1, st.onion_before + 1)]
        after = [cur + st.onion_step * i for i in range(1, st.onion_after + 1)]
    else:
        keys = _key_frames(scene)
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
    try:
        for o, _arm, how in targets:
            if how == 'DEFORM' and not o.hide_viewport:
                o.hide_viewport = True
                hidden.append(o)
        arms = {a.name: a for _o, a, _h in targets if a is not None}
        for name in _edited(scene)[0]:
            arms.setdefault(name, bpy.data.objects[name])
        for f in frames:
            scene.frame_set(int(f))
            parts = []
            for o, _arm, how in targets:
                p = _proxies.get(o.name)
                if p is None:
                    continue
                if how == 'DEFORM':
                    parts.append((p.name, _pose(p), None))
                else:
                    parts.append((p.name, None, o.matrix_world.copy()))
            bones = []
            for arm in arms.values():
                M = arm.matrix_world
                for pb in arm.pose.bones:
                    if _bone_shown(pb, st):
                        bones += [tuple(M @ pb.head), tuple(M @ pb.tail)]
            _frames[f] = {"parts": parts, "bones": np.array(bones, dtype=np.float32) if bones else None}
            for key in [k for k in _batches if k[0] == f]:
                del _batches[key]
    finally:
        for o in hidden:
            o.hide_viewport = False


def _schedule(delay=0.05):
    global _timer_on
    if not _timer_on:
        _timer_on = True
        bpy.app.timers.register(_tick, first_interval=delay)


def _tick():
    """Build missing proxies (one object per tick, with a note), then capture missing or
    changed ghost frames, the nearest first, about 40 ms per tick."""
    global _timer_on, _busy, _sig, _cost, status
    try:
        scene = bpy.context.scene
        st = state.settings(scene)
        if st is None or not st.show_onion:
            status = ""
            _timer_on = False
            return None
        screen = bpy.context.screen
        playing = screen is not None and screen.is_animation_playing
        if state.modal_running and _cost > 0.03 and time.time() - _changed_at < 0.15:
            return 0.05
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
        shows = sorted(frames_to_show(scene, st), key=lambda x: abs(x[1]))
        want = [f for f, _i in shows if f not in _frames] + [f for f, _i in shows if f in _stale]
        if not want and not state.modal_running:
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
        try:
            done = []
            batch = []
            for f in want:
                batch.append(f)
                if len(batch) >= 4:
                    break
            t1 = time.time()
            for f in batch:
                _stale.discard(f)
            _capture(scene, batch, targets, st)
            done += batch
            per = (time.time() - t1) / max(len(batch), 1)
            _cost = per if not _cost else _cost * 0.7 + per * 0.3
        finally:
            scene.frame_set(cur, subframe=sub)
            _busy = False
        state.tag_redraw()
        if len(done) < len(want) or time.time() - t0 > 0.04:
            return 0.01
        if not state.modal_running and any(f not in _frames or f in _stale for f in all_frames(scene, st)):
            return 0.02             # the ghosts shown are ready: on with the rest, ahead of time
        _timer_on = False
        return None
    except Exception as ex:   # never let the timer die with a traceback storm
        import traceback
        traceback.print_exc()
        print("Editable Motion Paths: onion skin failed:", ex)
        _busy = False
        _timer_on = False
        status = ""
        return None


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
    for name, pos, M in data["parts"]:
        p = _proxies.get(name)
        if p is None:
            continue
        co = pos
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
    if st.onion_show == 'WHOLE':
        return {}, set()
    key = (_sig, tuple(sorted((n, id(p)) for n, p in _proxies.items())))
    if _filter_cache[0] == key:
        return _filter_cache[1]
    _filter_cache[1] = _filters_now(scene, st)
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
        if p.how == 'DEFORM' and p.arm in aff:
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
    shows = frames_to_show(scene, st)
    if any(f not in _frames for f, _i in shows) or status:
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
        for name, pos, M in data["parts"]:
            if name in left_out:
                continue
            p = _proxies.get(name)
            if p is None:
                continue
            pkind = "edges" if p.how == 'LINES' or (kind == "tris" and not len(p.tri)) else kind
            b = _part_batch(f if pos is not None else None, pkind, name, pos, sh if pkind == kind else
                            gpu.shader.from_builtin('UNIFORM_COLOR'), keep.get(name))
            if b is not None:
                draws.append((b, M, pkind))

        def each(fn):
            for b, M, pkind in draws:
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
            each(lambda b, k: b.draw(sh) if k == "tris" else None)
            gpu.state.color_mask_set(True, True, True, True)
            gpu.state.depth_test_set('EQUAL')
            sh.uniform_float("color", col + (a,))
            each(lambda b, k: b.draw(sh) if k == "tris" else None)
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
            each(lambda b, k: b.draw(sh if k == kind else lines))
            gpu.state.line_width_set(1.0)
            gpu.state.point_size_set(1.0)
    gpu.state.depth_mask_set(False)
    gpu.state.depth_test_set(prev_test)
