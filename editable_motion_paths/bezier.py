"""F-Curve Bezier segment math, independent of Blender data.

A segment between two keys is the cubic Bezier P0 (key a), P1 (a's right handle),
P2 (b's left handle), P3 (key b) in (frame, value) space, evaluated by solving x(u) = t
the way Blender does it.  Used to insert keys without changing the curve (exact split)
and to dissolve keys while keeping the shape (fit the handle lengths of the neighbors).
"""


def correct(p0, p1, p2, p3):
    """Blender's BKE_fcurve_correct_bezpart: handles reaching past each other in time are
    shortened (keeping their direction)."""
    ln = p3[0] - p0[0]
    l1 = abs(p1[0] - p0[0])
    l2 = abs(p3[0] - p2[0])
    if l1 + l2 <= ln or l1 + l2 == 0.0:
        return p1, p2
    f = ln / (l1 + l2)
    return ((p0[0] + (p1[0] - p0[0]) * f, p0[1] + (p1[1] - p0[1]) * f),
            (p3[0] + (p2[0] - p3[0]) * f, p3[1] + (p2[1] - p3[1]) * f))


def point(p0, p1, p2, p3, u):
    v = 1.0 - u
    a = v * v * v
    b = 3.0 * v * v * u
    c = 3.0 * v * u * u
    d = u * u * u
    return (a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
            a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1])


def param_at(p0, p1, p2, p3, t):
    """Curve parameter u where x(u) = t (x is monotonic once corrected)."""
    lo, hi = 0.0, 1.0
    if t <= p0[0]:
        return 0.0
    if t >= p3[0]:
        return 1.0
    for _ in range(50):
        mid = (lo + hi) * 0.5
        if point(p0, p1, p2, p3, mid)[0] < t:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-10:
            break
    return (lo + hi) * 0.5


def split(p0, p1, p2, p3, t):
    """De Casteljau split at frame t: (a_right, new_left, new_key, new_right, b_left).
    The two new segments are exactly the old curve."""
    p1, p2 = correct(p0, p1, p2, p3)
    u = param_at(p0, p1, p2, p3, t)

    def lerp(a, b):
        return (a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u)
    a = lerp(p0, p1)
    b = lerp(p1, p2)
    c = lerp(p2, p3)
    d = lerp(a, b)
    e = lerp(b, c)
    f = lerp(d, e)
    return a, d, f, e, c


class Sampler:
    """Fast evaluation of one segment at many frames (dense table in u)."""

    N = 96

    def __init__(self, p0, p1, p2, p3):
        p1, p2 = correct(p0, p1, p2, p3)
        self.pts = [point(p0, p1, p2, p3, i / self.N) for i in range(self.N + 1)]

    def values(self, ts):
        pts = self.pts
        out = []
        j = 0
        n = len(pts) - 1
        for t in ts:
            while j < n - 1 and pts[j + 1][0] < t:
                j += 1
            (x0, y0), (x1, y1) = pts[j], pts[j + 1]
            f = 0.0 if x1 - x0 < 1e-12 else min(1.0, max(0.0, (t - x0) / (x1 - x0)))
            out.append(y0 + (y1 - y0) * f)
        return out


def fit_lengths(x0, y0, s0, x1, y1, s1, ts, ys):
    """Handle lengths (a, b) in frames for the segment from (x0, y0) with right slope s0 to
    (x1, y1) with left slope s1 that best follow the samples (ts, ys). The slopes stay as
    they are, so the curve keeps its direction (C1) at both keys. Returns (a, b, max_err)."""
    L = x1 - x0
    if L <= 1e-9:
        return L / 3.0, L / 3.0, 0.0

    def cost(fa, fb):
        a, b = fa * L, fb * L
        s = Sampler((x0, y0), (x0 + a, y0 + s0 * a), (x1 - b, y1 - s1 * b), (x1, y1))
        e = 0.0
        for y, z in zip(s.values(ts), ys):
            e += (y - z) * (y - z)
        return e

    best = None
    grid = [i / 10.0 for i in range(1, 10)]
    for fa in grid:
        for fb in grid:
            if fa + fb > 1.0 + 1e-9:
                continue
            c = cost(fa, fb)
            if best is None or c < best[0]:
                best = (c, fa, fb)
    c, fa, fb = best
    step = 0.05
    while step > 0.001:
        improved = False
        for da, db in ((step, 0.0), (-step, 0.0), (0.0, step), (0.0, -step),
                       (step, -step), (-step, step)):
            na, nb = fa + da, fb + db
            if na < 0.005 or nb < 0.005 or na + nb > 1.0:
                continue
            nc = cost(na, nb)
            if nc < c:
                c, fa, fb = nc, na, nb
                improved = True
        if not improved:
            step *= 0.5
    a, b = fa * L, fb * L
    s = Sampler((x0, y0), (x0 + a, y0 + s0 * a), (x1 - b, y1 - s1 * b), (x1, y1))
    err = max((abs(y - z) for y, z in zip(s.values(ts), ys)), default=0.0)
    return a, b, err
