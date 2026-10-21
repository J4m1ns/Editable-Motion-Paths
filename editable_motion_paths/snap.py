"""Snapping path keys to the scene with Blender's snap settings (Shift+Tab, magnet icon).

Vertex snapping also snaps to the keys of other paths; Face snapping projects onto the
surface under the mouse (a foot onto the floor).  Geometry is gathered once per transform
and projected with numpy, so even dense meshes stay interactive.
"""

import bpy
from mathutils import Vector
from bpy_extras import view3d_utils

from . import compat, state

try:
    import numpy as np
except ImportError:     # pragma: no cover
    np = None

MAX_VERTS = 400000
GEOMETRY = {'VERTEX', 'EDGE', 'EDGE_MIDPOINT', 'FACE', 'FACE_PROJECT', 'FACE_NEAREST'}


def elements(ts):
    return set(ts.snap_elements)


def _excluded(context, owners):
    """Objects that move with the edited targets (Snap to Self off): their armature's meshes
    and children."""
    names = set()
    for obj in owners:
        names.add(obj.name)
        for o in context.view_layer.objects:
            p = o.parent
            while p is not None:
                if p == obj:
                    names.add(o.name)
                    break
                p = p.parent
            for m in getattr(o, "modifiers", ()):
                if m.type == 'ARMATURE' and m.object == obj:
                    names.add(o.name)
    return names


class Snapper:
    def __init__(self, context, owners, skip_keys):
        """owners: objects being edited, skip_keys: set of (path key, frame) that move."""
        self.context = context
        ts = context.scene.tool_settings
        self.exclude = set() if ts.use_snap_self else _excluded(context, owners)
        self.skip_keys = skip_keys
        self._geo = None
        self._proj = None

    # -- geometry ------------------------------------------------------------

    def _gather(self):
        verts, edges = [], []
        if np is not None:
            dg = self.context.evaluated_depsgraph_get()
            total = 0
            for inst in dg.object_instances:
                ob = inst.object
                if ob.type != 'MESH' or ob.original.name in self.exclude:
                    continue
                orig = ob.original
                if not orig.visible_get() or orig.name.startswith("EMP "):
                    continue
                me = ob.data
                n = len(me.vertices)
                if n == 0 or total + n > MAX_VERTS:
                    continue
                co = np.empty(n * 3, dtype=np.float32)
                me.vertices.foreach_get("co", co)
                co = co.reshape(n, 3).astype(np.float64)
                m = np.array(inst.matrix_world, dtype=np.float64)
                w = co @ m[:3, :3].T + m[:3, 3]
                ne = len(me.edges)
                ed = np.empty(ne * 2, dtype=np.int64)
                me.edges.foreach_get("vertices", ed)
                edges.append(ed.reshape(ne, 2) + total)
                verts.append(w)
                total += n
        self.mesh = np.concatenate(verts) if verts else None
        self.edges = np.concatenate(edges) if edges else None
        keys = []
        for c in state.visible_caches(self.context.scene):
            if not c.valid:
                continue
            for k in c.keys:
                if (c.key, round(k.t, 3)) not in self.skip_keys:
                    keys.append(k.co.copy())
        self.keys = keys
        self._geo = True

    def _project(self, region, rv3d):
        if self._proj is not None:
            return self._proj
        if self._geo is None:
            self._gather()
        out = None
        if self.mesh is not None:
            pm = np.array(rv3d.perspective_matrix, dtype=np.float64)
            h = np.c_[self.mesh, np.ones(len(self.mesh))] @ pm.T
            wv = h[:, 3]
            ok = wv > 1e-6
            xy = np.full((len(h), 2), 1e9)
            xy[ok, 0] = (h[ok, 0] / wv[ok] + 1.0) * 0.5 * region.width
            xy[ok, 1] = (h[ok, 1] / wv[ok] + 1.0) * 0.5 * region.height
            out = xy
        self._proj = out
        return out

    # -- queries -------------------------------------------------------------

    def find(self, region, rv3d, mouse, elems):
        """(world point, kind) to snap to near the mouse (region coordinates), or None."""
        radius = 15.0 * compat.ui_scale()
        best = None
        m = Vector(mouse)
        if elems & {'VERTEX', 'EDGE', 'EDGE_MIDPOINT'}:
            xy = self._project(region, rv3d)
            if 'VERTEX' in elems:
                for p in self.keys:
                    s = view3d_utils.location_3d_to_region_2d(region, rv3d, p)
                    if s is not None:
                        d = (s - m).length
                        if d < radius and (best is None or d < best[0]):
                            best = (d, p.copy(), 'KEY')
                if xy is not None:
                    d2 = ((xy - np.array(mouse)) ** 2).sum(axis=1)
                    i = int(np.argmin(d2))
                    d = float(d2[i]) ** 0.5
                    if d < radius and (best is None or d < best[0]):
                        best = (d, Vector(self.mesh[i]), 'VERTEX')
            if xy is not None and self.edges is not None and elems & {'EDGE', 'EDGE_MIDPOINT'} \
                    and (best is None or best[2] != 'VERTEX'):
                hit = self._edge(region, rv3d, xy, mouse, radius, 'EDGE_MIDPOINT' in elems and 'EDGE' not in elems)
                if hit is not None and (best is None or hit[0] < best[0]):
                    best = hit
        if best is not None:
            return best[1], best[2]
        if elems & {'FACE', 'FACE_PROJECT', 'FACE_NEAREST'}:
            p = self._ray(region, rv3d, mouse)
            if p is not None:
                return p, 'FACE'
        return None

    def _edge(self, region, rv3d, xy, mouse, radius, midpoint):
        a = xy[self.edges[:, 0]]
        b = xy[self.edges[:, 1]]
        m = np.array(mouse)
        ab = b - a
        ln = (ab ** 2).sum(axis=1)
        ln[ln < 1e-12] = 1e-12
        f = np.clip(((m - a) * ab).sum(axis=1) / ln, 0.0, 1.0)
        if midpoint:
            f[:] = 0.5
        p = a + ab * f[:, None]
        d2 = ((p - m) ** 2).sum(axis=1)
        i = int(np.argmin(d2))
        d = float(d2[i]) ** 0.5
        if d >= radius:
            return None
        A = Vector(self.mesh[self.edges[i, 0]])
        B = Vector(self.mesh[self.edges[i, 1]])
        if midpoint:
            return d, (A + B) * 0.5, 'EDGE_MIDPOINT'
        # closest point of the edge to the view ray (perspective correct)
        o = view3d_utils.region_2d_to_origin_3d(region, rv3d, mouse)
        r = view3d_utils.region_2d_to_vector_3d(region, rv3d, mouse)
        u = B - A
        w0 = A - o
        aa, bb, cc = u.dot(u), u.dot(r), r.dot(r)
        dd, ee = u.dot(w0), r.dot(w0)
        den = aa * cc - bb * bb
        s = 0.0 if abs(den) < 1e-12 else (bb * ee - cc * dd) / den
        s = min(1.0, max(0.0, s))
        return d, A + u * s, 'EDGE'

    def _ray(self, region, rv3d, mouse):
        o = view3d_utils.region_2d_to_origin_3d(region, rv3d, mouse)
        d = view3d_utils.region_2d_to_vector_3d(region, rv3d, mouse)
        dg = self.context.evaluated_depsgraph_get()
        scene = self.context.scene
        for _ in range(8):
            ok, loc, _n, _i, ob, _m = scene.ray_cast(dg, o, d)
            if not ok:
                return None
            if ob is not None and ob.original.name not in self.exclude and not ob.original.name.startswith("EMP "):
                return loc
            o = loc + d * 1e-4
        return None
