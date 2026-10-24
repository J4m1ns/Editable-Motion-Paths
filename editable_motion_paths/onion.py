"""Onion skinning: ghosts of the edited bones / objects on the frames around the current one.

Ghost frames are captured once (scene.frame_set + the evaluated meshes and bones) in small
chunks while Blender is idle, kept as plain arrays, and drawn from GPU batches, so drawing
them costs almost nothing and the viewport stays fast. After an animation change they are
captured again a moment later. Modes: Silhouette (one flat color per ghost), Wire, Points
(the lightest), Bones (sticks)."""

import time

import bpy
import gpu
from gpu_extras.batch import batch_for_shader

from . import state

_frames = {}            # frame -> {"tris": array, "edges": array, "points": array, "bones": array}
_batches = {}           # (frame, kind) -> GPU batch
_dirty_since = None     # time of the last animation change (captured again after a pause)
_busy = False           # capturing right now (frame_set must not count as an edit)
_timer_on = False
_sig = None             # what the ghosts were captured for (objects, settings)


def busy():
    return _busy


def _signature(scene, st):
    """What the captured ghosts belong to: the edited paths and the detail setting."""
    return (tuple(c.key[:2] for c in state.editable_caches(scene)), st.onion_max_tris)


def clear():
    _frames.clear()
    _batches.clear()


def changed():
    """The animation changed: capture the ghosts again (after a short pause)."""
    global _dirty_since
    if _frames:
        _dirty_since = time.time()
        _schedule(0.6)


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
            if nt:
                tri = np.empty(nt * 3, dtype=np.int32)
                me.loop_triangles.foreach_get("vertices", tri)
                tri = tri.reshape(-1, 3)
                if nt > limit:
                    tri = tri[::int(np.ceil(nt / limit))]
                data["tris"].append(co[tri.ravel()])
            ne = len(me.edges)
            if ne:
                ed = np.empty(ne * 2, dtype=np.int32)
                me.edges.foreach_get("vertices", ed)
                ed = ed.reshape(-1, 2)
                if ne > limit:
                    ed = ed[::int(np.ceil(ne / limit))]
                data["edges"].append(co[ed.ravel()])
            data["points"].append(co[::max(1, n // limit)])
            ev.to_mesh_clear()
        for arm in arms:
            M = arm.matrix_world
            for pb in arm.pose.bones:
                if pb.bone.hide:
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
    """Capture missing ghost frames a few at a time (about 40 ms per tick), while idle."""
    global _timer_on, _busy, _dirty_since, _sig
    try:
        scene = bpy.context.scene
        st = state.settings(scene)
        if st is None or not st.show_onion:
            _timer_on = False
            return None
        screen = bpy.context.screen
        if state.modal_running or (screen is not None and screen.is_animation_playing):
            return 0.3
        if _dirty_since is not None:
            if time.time() - _dirty_since < 0.5:
                return 0.2
            clear()
            _dirty_since = None
        meshes, arms = _targets(scene)
        sig = _signature(scene, st)
        if sig != _sig:
            clear()
            _sig = sig
        want = [f for f, _i in frames_to_show(scene, st) if f not in _frames]
        if not want:
            _timer_on = False
            return None
        cur, sub = scene.frame_current, scene.frame_subframe
        t0 = time.time()
        _busy = True
        try:
            done = []
            for f in want:
                _capture(scene, [f], meshes, arms, st)
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
    sh = gpu.shader.from_builtin('UNIFORM_COLOR')
    kind = {'SILHOUETTE': "tris", 'WIRE': "edges", 'POINTS': "points", 'BONES': "bones"}[st.onion_mode]
    prev_test = gpu.state.depth_test_get()
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE' if st.onion_in_front else 'LESS_EQUAL')
    n_b = max(1, st.onion_before)
    n_a = max(1, st.onion_after)
    for f, i in sorted(shows, key=lambda x: -abs(x[1])):     # the farthest first
        b = _batch(f, kind, sh)
        if b is None:
            continue
        fade = 1.0 - (abs(i) - 1) / ((n_b if i < 0 else n_a) + 0.5)
        col = tuple(st.onion_color_before if i < 0 else st.onion_color_after)
        a = st.onion_opacity * max(0.15, fade)
        if kind == "tris":
            # one flat layer per ghost: depth first, then the color only where it is in front
            gpu.state.depth_mask_set(True)
            gpu.state.color_mask_set(False, False, False, False)
            sh.uniform_float("color", (0, 0, 0, 0))
            b.draw(sh)
            gpu.state.color_mask_set(True, True, True, True)
            gpu.state.depth_test_set('EQUAL')
            sh.uniform_float("color", col + (a,))
            b.draw(sh)
            gpu.state.depth_test_set('NONE' if st.onion_in_front else 'LESS_EQUAL')
            gpu.state.depth_mask_set(False)
        else:
            if kind == "bones":
                gpu.state.line_width_set(3.0 * us)
            elif kind == "points":
                gpu.state.point_size_set(2.0 * us)
            sh.uniform_float("color", col + (min(1.0, a * 1.6),))
            b.draw(sh)
            gpu.state.line_width_set(1.0)
            gpu.state.point_size_set(1.0)
    gpu.state.depth_mask_set(False)
    gpu.state.depth_test_set(prev_test)
