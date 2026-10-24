"""Onion skinning: ghosts of the edited bones / objects on the frames around the current one.

Ghost frames are captured once (scene.frame_set + the evaluated meshes and bones) in small
chunks while Blender is idle, kept as plain arrays, and drawn from GPU batches, so drawing
them costs almost nothing and the viewport stays fast. After an animation change they are
captured again right away, the nearest first, also while a tool drags (live); the old ghost
stays drawn until its new one is ready, so nothing blinks. Modes: Silhouette (one flat color
per ghost), Wire, Points (the lightest), Bones (sticks); heavy meshes are melted down to the
Detail limit once and all modes draw that same simplified mesh."""

import time

import bpy
import gpu
from gpu_extras.batch import batch_for_shader

from . import state

_frames = {}            # frame -> {"tris": array, "edges": array, "points": array, "bones": array}
_batches = {}           # (frame, kind) -> GPU batch
_stale = set()          # captured frames the animation has changed since (still drawn until renewed)
_changed_at = 0.0       # time of the last animation change
_cost = 0.0             # how long one ghost frame takes to capture (running average, seconds)
_busy = False           # capturing right now (frame_set must not count as an edit)
_timer_on = False
_sig = None             # what the ghosts were captured for (objects, settings)


def busy():
    return _busy


def _signature(scene, st):
    """What the captured ghosts belong to: the edited paths and the detail / bone settings."""
    return (tuple(c.key[:2] for c in state.editable_caches(scene)), st.onion_max_tris, st.onion_bones,
            _bone_state(scene, st))


def _bone_state(scene, st):
    """Which bones the Bones mode shows (it changes with the selection / visibility)."""
    if st.onion_mode != 'BONES':
        return None
    out = []
    for c in state.editable_caches(scene):
        obj = bpy.data.objects.get(c.obj_name)
        if obj is not None and obj.type == 'ARMATURE':
            out.append(tuple(pb.name for pb in obj.pose.bones if _bone_shown(pb, st)))
    return tuple(out)


def _bone_shown(pb, st):
    from . import compat
    b = pb.bone
    if st.onion_bones == 'SELECTED':
        return compat.bone_selected(pb)
    if b.hide:
        return False
    cols = list(getattr(b, "collections", ()))
    return not cols or any(getattr(col, "is_visible_effectively", getattr(col, "is_visible", True)) for col in cols)


def clear():
    _frames.clear()
    _batches.clear()
    _stale.clear()


def changed():
    """The animation changed: the ghosts are captured again right away, the nearest first,
    also while a tool drags (the old ones stay drawn until each is renewed)."""
    global _changed_at
    if _frames:
        _stale.update(_frames)
        _changed_at = time.time()
        _schedule(0.01)


def _targets(scene):
    """(meshes to ghost, armatures to ghost the bones of) for the editable paths."""
    meshes, arms = [], []
    seen = set()
    for c in state.editable_caches(scene):
        obj = bpy.data.objects.get(c.obj_name)
        if obj is None or obj.name in seen:
            continue
        seen.add(obj.name)
        if obj.type == 'ARMATURE':
            arms.append(obj)
            for o in scene.objects:
                if o.type == 'MESH' and o.visible_get() and any(
                        m.type == 'ARMATURE' and m.object == obj for m in o.modifiers):
                    meshes.append(o)
                elif o.parent == obj and o.type == 'MESH' and o.visible_get() and o not in meshes:
                    meshes.append(o)
        elif obj.type in {'MESH', 'CURVE', 'SURFACE', 'META', 'FONT'}:
            meshes.append(obj)
        for o in scene.objects:
            if o.parent == obj and o.type == 'MESH' and o.visible_get() and o not in meshes and \
                    obj.type != 'ARMATURE':
                meshes.append(o)
    return meshes, arms


def frames_to_show(scene, st):
    """Ghost frames around the current one: every `onion_step` frames, or the keys."""
    cur = scene.frame_current
    if st.onion_step > 0:
        before = [cur - st.onion_step * i for i in range(1, st.onion_before + 1)]
        after = [cur + st.onion_step * i for i in range(1, st.onion_after + 1)]
    else:
        keys = sorted({round(k.t) for c in state.editable_caches(scene) for k in c.keys})
        before = [t for t in reversed(keys) if t < cur][:st.onion_before]
        after = [t for t in keys if t > cur][:st.onion_after]
    return [(f, -(i + 1)) for i, f in enumerate(before)] + [(f, i + 1) for i, f in enumerate(after)]


def _decimate(co, tri, limit, edges=None):
    """Fewer triangles without holes, like Decimate: vertices that fall into the same cell of a
    grid melt into one, triangles that collapse go away; the grid gets coarser until the mesh
    fits the limit. With `edges` (the mesh's own edges, no triangle diagonals) they are melted
    the same way and returned too: a coarser wire that stays connected."""
    import numpy as np
    lo, hi = co.min(axis=0), co.max(axis=0)
    diag = float(np.linalg.norm(hi - lo)) or 1.0
    cells = max(8.0, (limit * 0.5) ** 0.5 * 2.0)
    best = (co, tri, np.arange(len(co)))
    for _i in range(12):
        q = np.floor((co - lo) / (diag / cells)).astype(np.int64)
        uniq, inv = np.unique(q, axis=0, return_inverse=True)
        inv = inv.reshape(-1)
        sums = np.zeros((len(uniq), 3), dtype=np.float64)
        np.add.at(sums, inv, co)
        newco = (sums / np.bincount(inv)[:, None]).astype(np.float32)
        t2 = inv[tri]
        keep = (t2[:, 0] != t2[:, 1]) & (t2[:, 1] != t2[:, 2]) & (t2[:, 0] != t2[:, 2])
        t2 = t2[keep]
        _u, first = np.unique(np.sort(t2, axis=1), axis=0, return_index=True)
        t2 = t2[np.sort(first)]     # the same triangle twice: once (its winding kept for culling)
        best = (newco, t2, inv)
        if len(t2) <= limit or cells <= 8.0:
            break
        cells *= 0.75
    if edges is None:
        return best[:2]
    newco, t2, inv = best
    e2 = inv[edges]
    e2 = np.unique(np.sort(e2[e2[:, 0] != e2[:, 1]], axis=1), axis=0)
    return newco, t2, e2


def _capture(scene, frames, meshes, arms, st):
    import numpy as np
    dg = bpy.context.evaluated_depsgraph_get()
    limit = max(500, st.onion_max_tris)
    for f in frames:
        scene.frame_set(int(f))
        data = {"tris": [], "edges": [], "points": [], "bones": []}
        for ob in meshes:
            ev = ob.evaluated_get(dg)
            try:
                me = ev.to_mesh()
            except RuntimeError:
                continue
            if me is None:
                continue
            M = np.array(ev.matrix_world, dtype=np.float32)
            n = len(me.vertices)
            co = np.empty(n * 3, dtype=np.float32)
            me.vertices.foreach_get("co", co)
            co = co.reshape(-1, 3) @ M[:3, :3].T + M[:3, 3]
            me.calc_loop_triangles()
            nt = len(me.loop_triangles)
            tri = np.empty(nt * 3, dtype=np.int32)
            me.loop_triangles.foreach_get("vertices", tri)
            tri = tri.reshape(-1, 3)
            ne = len(me.edges)
            ed = np.empty(ne * 2, dtype=np.int32)
            me.edges.foreach_get("vertices", ed)
            ed = ed.reshape(-1, 2)
            ev.to_mesh_clear()
            vco = co
            if n and (nt > limit or ne > limit * 1.5 or n > limit):
                # the same melted mesh for every mode: the wire stays connected (no stride
                # gaps), the points spread evenly
                vco, tri, ed = _decimate(co, tri, limit, ed)
            if len(tri):
                data["tris"].append(vco[tri.ravel()])
            if len(ed):
                data["edges"].append(vco[ed.ravel()])
            if len(vco):
                data["points"].append(vco)
        for arm in arms:
            M = arm.matrix_world
            for pb in arm.pose.bones:
                if not _bone_shown(pb, st):
                    continue
                data["bones"] += [tuple(M @ pb.head), tuple(M @ pb.tail)]
        _frames[f] = {k: (np.concatenate(v) if v and k != "bones" else (np.array(v, dtype=np.float32) if v else None))
                      for k, v in data.items()}
        for kind in ("tris", "edges", "points", "bones"):
            _batches.pop((f, kind), None)


def _schedule(delay=0.05):
    global _timer_on
    if not _timer_on:
        _timer_on = True
        bpy.app.timers.register(_tick, first_interval=delay)


def _tick():
    """Capture missing or changed ghost frames, the nearest first, about 40 ms per tick.

    While a tool drags, a light scene is captured on every tick (the ghosts follow the edit
    live); a heavy one waits until the mouse rests a moment, so the drag never stutters."""
    global _timer_on, _busy, _sig, _cost
    try:
        scene = bpy.context.scene
        st = state.settings(scene)
        if st is None or not st.show_onion:
            _timer_on = False
            return None
        screen = bpy.context.screen
        if screen is not None and screen.is_animation_playing:
            return 0.3
        if state.modal_running and _cost > 0.03 and time.time() - _changed_at < 0.15:
            return 0.05
        meshes, arms = _targets(scene)
        sig = _signature(scene, st)
        if sig != _sig:
            clear()
            _sig = sig
        shows = sorted(frames_to_show(scene, st), key=lambda x: abs(x[1]))
        want = [f for f, _i in shows if f not in _frames] + [f for f, _i in shows if f in _stale]
        if not want:            # (stale frames not shown now are renewed when they show again)
            _timer_on = False
            return None
        cur, sub = scene.frame_current, scene.frame_subframe
        t0 = time.time()
        _busy = True
        try:
            done = []
            for f in want:
                t1 = time.time()
                _stale.discard(f)
                _capture(scene, [f], meshes, arms, st)
                _cost = (time.time() - t1) if not _cost else _cost * 0.7 + (time.time() - t1) * 0.3
                done.append(f)
                if time.time() - t0 > 0.04:
                    break
        finally:
            scene.frame_set(cur, subframe=sub)
            _busy = False
        state.tag_redraw()
        if len(done) < len(want):
            return 0.01
        _timer_on = False
        return None
    except Exception as ex:   # never let the timer die with a traceback storm
        print("Editable Motion Paths: onion skin failed:", ex)
        _busy = False
        _timer_on = False
        return None


def _batch(f, kind, shader):
    b = _batches.get((f, kind))
    if b is None:
        data = _frames.get(f, {}).get(kind)
        if data is None or len(data) == 0:
            return None
        prim = {"tris": 'TRIS', "edges": 'LINES', "points": 'POINTS', "bones": 'LINES'}[kind]
        b = _batches[(f, kind)] = batch_for_shader(shader, prim, {"pos": data})
    return b


def _point_shader():
    """Points need a shader that sets their size (the plain one ignores point_size_set on
    Vulkan / Metal)."""
    try:
        return gpu.shader.from_builtin('POINT_UNIFORM_COLOR')
    except (ValueError, TypeError):
        return gpu.shader.from_builtin('UNIFORM_COLOR')


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
    if any(f not in _frames for f, _i in shows):
        _schedule()
    kind = {'SILHOUETTE': "tris", 'WIRE': "edges", 'POINTS': "points", 'BONES': "bones"}[st.onion_mode]
    sh = _point_shader() if kind == "points" else gpu.shader.from_builtin('UNIFORM_COLOR')
    prev_test = gpu.state.depth_test_get()
    # (in front: the depth still has to be written for the one-layer silhouette, and with the
    # depth test off nothing is written - so "always" passes instead)
    test = 'ALWAYS' if st.onion_in_front else 'LESS_EQUAL'
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set(test)
    n_b = max(1, st.onion_before)
    n_a = max(1, st.onion_after)
    for f, i in sorted(shows, key=lambda x: -abs(x[1])):     # the farthest first
        b = _batch(f, kind, sh)
        if b is None:
            continue
        # the farther from the current frame, the fainter
        n = n_b if i < 0 else n_a
        fade = (n - abs(i) + 1) / float(n)
        col = tuple(st.onion_color_before if i < 0 else st.onion_color_after)
        a = st.onion_opacity * max(0.08, fade ** 1.5)
        if kind == "tris":
            # one flat layer per ghost: depth first, then the color only where it is in front
            # (back faces are skipped: half the work, and nothing changes in a silhouette)
            gpu.state.face_culling_set('BACK')
            gpu.state.depth_mask_set(True)
            gpu.state.color_mask_set(False, False, False, False)
            sh.uniform_float("color", (0, 0, 0, 0))
            b.draw(sh)
            gpu.state.color_mask_set(True, True, True, True)
            gpu.state.depth_test_set('EQUAL')
            sh.uniform_float("color", col + (a,))
            b.draw(sh)
            gpu.state.depth_test_set(test)
            gpu.state.depth_mask_set(False)
            gpu.state.face_culling_set('NONE')
        else:
            if kind in {"bones", "edges"}:
                gpu.state.line_width_set(st.onion_size * us * (1.0 if kind == "bones" else 0.4))
            elif kind == "points":
                gpu.state.point_size_set(st.onion_size * us)
            sh.uniform_float("color", col + (min(1.0, a * 1.6),))
            b.draw(sh)
            gpu.state.line_width_set(1.0)
            gpu.state.point_size_set(1.0)
    gpu.state.depth_mask_set(False)
    gpu.state.depth_test_set(prev_test)
