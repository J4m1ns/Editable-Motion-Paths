"""Key topology on F-Curves: insert keys without changing the motion, dissolve keys while
keeping it.  These work per channel; the operators measure the result on the path in 3D.

Rules (tested in dev/tests/t_inv.py):
  * Insert keeps the curve.  New keys get Auto Clamped handles when that changes the path by
    less than INSERT_TOL of its size; otherwise the curve is split exactly (the new key and
    the neighbors' facing handles become Aligned).
  * Delete removes keys; the neighbors keep their handle types.
  * Dissolve removes keys and refits only the facing handles of the two neighbors: their
    directions stay, their lengths are fitted to the removed shape.  Everything outside the
    two neighbors stays exactly as it was.  When plain Delete is as close, Delete is used, so
    Dissolve is never worse than Delete.
"""

from . import bezier, core

AUTO = {'AUTO', 'AUTO_CLAMPED'}
SMOOTH = AUTO | {'ALIGNED'}
INSERT_TOL = 0.005      # of the path size
STEP = 0.25             # sampling step in frames


def _frange(a, b, step=STEP):
    n = max(1, int(round((b - a) / step)))
    return [a + (b - a) * i / n for i in range(n + 1)]


def _kp_data(kp):
    return (tuple(kp.co), tuple(kp.handle_left), tuple(kp.handle_right),
            kp.handle_left_type, kp.handle_right_type, kp.interpolation)


def _slope(data, side, fallback):
    (x, y), hl, hr = data[0], data[1], data[2]
    h = hr if side == 'R' else hl
    if abs(h[0] - x) < 1e-9:
        return fallback
    return (h[1] - y) / (h[0] - x)


def _neighbor_types(tl, tr, side):
    """Handle types of a neighbor key whose `side` handle gets set explicitly."""
    if tl in SMOOTH and tr in SMOOTH:
        return 'ALIGNED', 'ALIGNED'
    nl = 'FREE' if (side == 'L' or tl in AUTO or tl == 'VECTOR') else tl
    nr = 'FREE' if (side == 'R' or tr in AUTO or tr == 'VECTOR') else tr
    return nl, nr


def _find(kps, x):
    for kp in kps:
        if abs(kp.co[0] - x) < 1e-4:
            return kp
    return None


# ----------------------------------------------------------------------------
# Insert
# ----------------------------------------------------------------------------

def _bracket(kps, t):
    """Indices (a, b) of the keys around frame t (a or b is None outside the range)."""
    a = b = None
    for i, kp in enumerate(kps):
        x = kp.co[0]
        if x < t - 1e-4:
            a = i
        elif x > t + 1e-4:
            b = i
            break
    return a, b


def insert_channel(fc, t, exact=False):
    """Insert a key at frame t on one F-Curve. Returns the keyframe (or None if one exists).

    The new key takes the interpolation of the segment it splits. With `exact`, the Bezier
    segment is split exactly (de Casteljau), otherwise Blender's auto handles are used."""
    kps = fc.keyframe_points
    for kp in kps:
        if abs(kp.co[0] - t) < 1e-4:
            return None
    a, b = _bracket(kps, t)
    da = _kp_data(kps[a]) if a is not None else None
    db = _kp_data(kps[b]) if b is not None else None
    value = fc.evaluate(t)
    kp = kps.insert(t, value, options={'FAST'})
    if da is not None:
        kp.interpolation = da[5]
    elif db is not None:
        kp.interpolation = db[5]
    if exact and da is not None and db is not None and da[5] == 'BEZIER':
        _apply_split(kps, t, da, db)
    fc.update()
    return _find(kps, t)


def _apply_split(kps, t, da, db):
    (xa, ya), (xb, yb) = da[0], db[0]
    ar, nl, nk, nr, bl = bezier.split((xa, ya), da[2], db[1], (xb, yb), t)
    ka, kb, kn = _find(kps, xa), _find(kps, xb), _find(kps, t)
    if ka is None or kb is None or kn is None:
        return
    tl, tr = _neighbor_types(da[3], da[4], 'R')
    ka.handle_left_type, ka.handle_right_type = tl, tr
    ka.handle_left = da[1]
    ka.handle_right = ar
    tl, tr = _neighbor_types(db[3], db[4], 'L')
    kb.handle_left_type, kb.handle_right_type = tl, tr
    kb.handle_right = db[2]
    kb.handle_left = bl
    kn.handle_left_type = kn.handle_right_type = 'ALIGNED'
    kn.co = (t, nk[1])
    kn.handle_left = nl
    kn.handle_right = nr


def insert_path_keys(cache, tg, frames, exact=None):
    """Insert keys on every keyed channel of the path at `frames`, keeping the motion.

    exact=None decides per frame: Auto Clamped if the path moves less than INSERT_TOL of
    its size, otherwise an exact split. Channels that loop are keyed inside their range.
    Returns the list of frames that got a new key."""
    ch = tg.channels(cache.ptype)
    fcs = [(c, fc) for c, fc in enumerate(ch.fcurves) if fc is not None and len(fc.keyframe_points)]
    if not fcs:
        core.ensure_keys(tg, cache.ptype, frames)
        return list(frames)
    pts = cache.points
    size = max((max(p[i] for p in pts) - min(p[i] for p in pts)) ** 2 for i in range(3)) ** 0.5 \
        if pts else 1.0
    tol = max(size, 1e-6) * INSERT_TOL
    done = []
    for t in frames:
        per = []
        for c, fc in fcs:
            tt = core.wrap_time(t, core.cycle_info(fc))
            kps = fc.keyframe_points
            if any(abs(kp.co[0] - tt) < 1e-4 for kp in kps):
                continue
            a, b = _bracket(kps, tt)
            # auto handles of the neighbors change too, which bends one more segment
            span = (kps[max(a - 1, 0)].co[0] if a is not None else tt - 1.0,
                    kps[min(b + 1, len(kps) - 1)].co[0] if b is not None else tt + 1.0)
            per.append((fc, tt, _kp_data(kps[a]) if a is not None else None,
                        _kp_data(kps[b]) if b is not None else None, span))
        if not per:
            continue
        # path frames around t that the new keys can change (loops: shifted back from the
        # wrapped frame)
        lo = min(p[4][0] + t - p[1] for p in per)
        hi = max(p[4][1] + t - p[1] for p in per)
        ts = _frange(min(lo, t), max(hi, t))
        before = [cache.point(tg, s, ch.values(s)) for s in ts]
        for fc, tt, da, db, _span in per:
            insert_channel(fc, tt, exact=bool(exact))
        if exact is None:
            dev = max((cache.point(tg, s, ch.values(s)) - p).length for s, p in zip(ts, before))
            if dev > tol:
                for fc, tt, da, db, _span in per:
                    if da is not None and db is not None and da[5] == 'BEZIER':
                        _apply_split(fc.keyframe_points, tt, da, db)
                        fc.update()
        done.append(t)
    return done


# ----------------------------------------------------------------------------
# Delete / Dissolve
# ----------------------------------------------------------------------------

def delete_channel(fc, times):
    kps = fc.keyframe_points
    ts = {round(t, 3) for t in times}
    idx = [i for i, kp in enumerate(kps) if round(kp.co[0], 3) in ts]
    for i in reversed(idx):
        kps.remove(kps[i], fast=True)
    if idx:
        fc.update()
    return len(idx)


def dissolve_channel(fc, times):
    """Remove the keys at `times` and refit the facing handles of the keys around each
    removed run (see the module doc). Returns the number of removed keys."""
    kps = fc.keyframe_points
    n = len(kps)
    ts = {round(t, 3) for t in times}
    rem = [i for i, kp in enumerate(kps) if round(kp.co[0], 3) in ts]
    if not rem:
        return 0
    runs = []
    for i in rem:
        if runs and runs[-1][1] == i - 1:
            runs[-1][1] = i
        else:
            runs.append([i, i])
    orig = [_kp_data(kp) for kp in kps]
    segs = []
    for i, j in runs:
        p, q = i - 1, j + 1
        if p < 0 or q >= n or orig[p][5] != 'BEZIER':
            continue
        x0, x1 = orig[p][0][0], orig[q][0][0]
        inner = _frange(x0, x1)
        # Delete also re-shapes the neighbors' auto handles: compare over the outer segments too
        xa = orig[p - 1][0][0] if p > 0 else x0
        xb = orig[q + 1][0][0] if q + 1 < n else x1
        outer = _frange(xa, xb)
        segs.append(dict(p=p, q=q, inner=inner, yi=[fc.evaluate(t) for t in inner],
                         outer=outer, yo=[fc.evaluate(t) for t in outer]))
    for i in reversed(rem):
        kps.remove(kps[i], fast=True)
    fc.update()
    chosen = []
    for s in segs:
        err_del = max(abs(fc.evaluate(t) - y) for t, y in zip(s['outer'], s['yo']))
        dp, dq = orig[s['p']], orig[s['q']]
        (x0, y0), (x1, y1) = dp[0], dq[0]
        yi, inner = s['yi'], s['inner']
        s0 = _slope(dp, 'R', (yi[1] - yi[0]) / (inner[1] - inner[0]))
        s1 = _slope(dq, 'L', (yi[-1] - yi[-2]) / (inner[-1] - inner[-2]))
        a, b, err_fit = bezier.fit_lengths(x0, y0, s0, x1, y1, s1, inner, yi)
        if err_fit < err_del * 0.98 - 1e-9:
            chosen.append((dp, dq, a, b, s0, s1))
    if chosen:
        # neighbors back to their original handles first (Delete re-shaped auto handles),
        # then the fitted facing handles
        for dp, dq, a, b, s0, s1 in chosen:
            for d, side in ((dp, 'R'), (dq, 'L')):
                kp = _find(kps, d[0][0])
                if kp is None:
                    continue
                kp.handle_left_type, kp.handle_right_type = _neighbor_types(d[3], d[4], side)
                kp.handle_left = d[1]
                kp.handle_right = d[2]
        for dp, dq, a, b, s0, s1 in chosen:
            (x0, y0), (x1, y1) = dp[0], dq[0]
            kp = _find(kps, x0)
            if kp is not None:
                kp.handle_right = (x0 + a, y0 + s0 * a)
            kp = _find(kps, x1)
            if kp is not None:
                kp.handle_left = (x1 - b, y1 - s1 * b)
        fc.update()
    return len(rem)
