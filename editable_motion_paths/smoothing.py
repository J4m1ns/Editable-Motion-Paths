"""Smoothing the curves a path shows but doesn't edit: the bone's / object's other channels (its
location in a rotation path) and those of the bones / objects it hangs from - in mocap every bone
trembles, and a path shows the tremble of everything above it. Used by the Dissolve brush (Smooth),
the Smooth brush and the Smooth tool (Smooth Parents)."""

import numpy as np

from . import compat, core


def chain_owners(tg, parents=True, chain=0):
    """[(object, owner)]: the path's bone / object, then (parents) what it hangs from, nearest first -
    chain of them at most (0: all of them, like an IK constraint's Chain Length)."""
    obj, owner = tg.obj, tg.owner
    out = [(obj, owner)]
    if not parents:
        return out
    above = []
    if owner is not obj:
        above += [(obj, pb) for pb in owner.parent_recursive]
    o = obj.parent
    while o is not None:
        above.append((o, o))
        o = o.parent
    return out + (above[:chain] if chain > 0 else above)


def chain_curves(tg, ptype, parents=True, chain=0):
    """[(key, F-Curve)]: the transform curves that move the path besides the ones edited."""
    own = {id(fc) for fc in tg.channels(ptype).fcurves if fc is not None}
    out = []
    for ob, ow in chain_owners(tg, parents, chain):
        fcs = compat.get_fcurves(ob)
        if not fcs:
            continue
        pre = ow.path_from_id() + "." if ow is not ob else ""
        for fc in fcs:
            if id(fc) in own or len(fc.keyframe_points) < 4 or not fc.data_path.startswith(pre):
                continue
            prop = fc.data_path[len(pre):]
            if prop not in core._TRANSFORMS:
                continue
            out.append(((ob.name, fc.data_path, fc.array_index), fc))
    return out


def key_values(fc):
    """(n, 2): every key's frame and value."""
    kps = fc.keyframe_points
    co = np.empty(len(kps) * 2, dtype=np.float64)
    if len(kps):
        kps.foreach_get("co", co)
    return co.reshape(-1, 2)


def smoothed_value(raw, t, sigma):
    """A key's value on a curve through the keys around it (a parabola weighted by distance in time,
    over ~sigma frames): the tremble goes, the arcs stay. None at the curve's ends."""
    T, V = raw[:, 0], raw[:, 1]
    if len(T) < 3 or t <= T[0] + 1e-6 or t >= T[-1] - 1e-6:
        return None
    x = T - t
    w = np.exp(-(x * x) / (2.0 * sigma * sigma))
    w[np.abs(x) > 3.0 * sigma] = 0.0
    use = w > 1e-6
    if use.sum() >= 4:
        X = np.stack([np.ones(use.sum()), x[use], x[use] * x[use]], axis=1)
        Wx = X * w[use][:, None]
        try:
            return float(np.linalg.solve(X.T @ Wx, Wx.T @ V[use])[0])
        except np.linalg.LinAlgError:
            pass
    return float((w * V).sum() / max(w.sum(), 1e-12))


def smooth_range(curves, t0, t1, sigma, amount=1.0, done=None, weight=None):
    """Smooth the keys of curves [(key, F-Curve, values as they were)] between frames t0 and t1: each
    goes `amount` of the way (times weight(t), if given) from its value as it was to the smoothed
    one. done: (key, frame) already smoothed - skipped, and added to. Returns the curves changed."""
    touched = []
    for key, fc, raw in curves:
        if len(raw) < 3:
            continue
        at = {round(float(t), 3): float(v) for t, v in raw}
        changed = False
        for kp in fc.keyframe_points:
            t = kp.co[0]
            r = round(t, 3)
            if not t0 - 1e-6 <= t <= t1 + 1e-6 or (done is not None and (key, r) in done):
                continue
            v = smoothed_value(raw, t, sigma)
            if v is None:
                continue
            if done is not None:
                done.add((key, r))
            f = amount * (weight(t) if weight is not None else 1.0)
            v0 = at.get(r, kp.co[1])
            v = v0 + (v - v0) * f
            d = v - kp.co[1]
            if abs(d) < 1e-12:
                continue
            kp.co[1] = v
            kp.handle_left[1] += d
            kp.handle_right[1] += d
            changed = True
        if changed:
            touched.append(fc)
    return touched


class _Owner:
    """(what core.FCurveSnapshot needs of a target: the object)"""

    def __init__(self, obj):
        self.obj = obj


def snapshots(curves):
    """Restorable copies of the curves [(key, F-Curve, ...)], one per object."""
    import bpy
    by_ob = {}
    for item in curves:
        key, fc = item[0], item[1]
        by_ob.setdefault(key[0], []).append((key[1], key[2], fc))
    out = []
    for name, items in by_ob.items():
        ob = bpy.data.objects.get(name)
        if ob is not None:
            out.append(core.FCurveSnapshot(_Owner(ob), items=items))
    return out
