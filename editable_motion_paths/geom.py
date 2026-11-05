"""Small geometry helpers for the shape tools (no Blender data)."""

import math

from mathutils import Matrix, Vector


def best_plane(pts):
    """(center, normal) of the plane closest to the points (smallest principal axis)."""
    c = sum(pts, Vector()) / len(pts)
    m = Matrix(((0.0,) * 3,) * 3)
    for p in pts:
        d = p - c
        for i in range(3):
            for j in range(3):
                m[i][j] += d[i] * d[j]
    try:
        import numpy as np
        w, v = np.linalg.eigh(np.array([list(r) for r in m]))
        n = Vector(v[:, 0].tolist())
        if n.length > 1e-12:
            return c, n.normalized()
    except Exception:
        pass
    # power iteration on (trace*I - M) finds the smallest eigenvector of M
    tr = m[0][0] + m[1][1] + m[2][2] + 1e-12
    a = Matrix.Identity(3) * tr - m
    v = Vector((0.3, 0.5, 0.8))
    for _ in range(60):
        v = a @ v
        if v.length < 1e-15:
            return c, Vector((0.0, 0.0, 1.0))
        v.normalize()
    return c, v


def project_plane(p, c, n):
    return p - n * (p - c).dot(n)


def best_circle(pts):
    """(center, normal, radius) of the circle closest to the points (plane fit, then an
    algebraic circle fit in that plane)."""
    c, n = best_plane(pts)
    u = n.orthogonal().normalized()
    v = n.cross(u).normalized()
    xy = [((p - c).dot(u), (p - c).dot(v)) for p in pts]
    # Kasa fit: x^2 + y^2 + D x + E y + F = 0, normal equations 3x3
    s = [[0.0] * 3 for _ in range(3)]
    r = [0.0] * 3
    for x, y in xy:
        row = (x, y, 1.0)
        z = -(x * x + y * y)
        for i in range(3):
            r[i] += row[i] * z
            for j in range(3):
                s[i][j] += row[i] * row[j]
    try:
        sol = Matrix(s).inverted() @ Vector(r)
    except ValueError:
        return c, n, max((p - c).length for p in pts)
    D, E, F = sol
    cx, cy = -D / 2.0, -E / 2.0
    rad = math.sqrt(max(cx * cx + cy * cy - F, 1e-12))
    return c + u * cx + v * cy, n, rad


def closest_on_line(p, a, d):
    """Closest point to p on the line through a with direction d (normalized)."""
    return a + d * (p - a).dot(d)


def line_corner(a, ta, b, tb):
    """Point where the tangent lines at a (direction ta) and b (direction tb) come closest
    (the corner a bevel rounds), or None if they are parallel."""
    w0 = a - b
    aa, bb, cc = ta.dot(ta), ta.dot(tb), tb.dot(tb)
    dd, ee = ta.dot(w0), tb.dot(w0)
    den = aa * cc - bb * bb
    if abs(den) < 1e-9:
        return None
    s = (bb * ee - cc * dd) / den
    t = (aa * ee - bb * dd) / den
    return ((a + ta * s) + (b + tb * t)) * 0.5


def profile_exponent(profile):
    """Mesh Bevel's profile shape: 0.5 circle, 0.25 straight, 1 sharp corner, 0 concave."""
    profile = min(max(profile, 1e-3), 0.999)
    return -math.log(2.0) / math.log(math.sqrt(profile))


def bevel_points(a, corner, b, segments, profile):
    """Points from a to b rounding `corner` (superellipse in the skewed frame of the corner)."""
    r = profile_exponent(profile)
    o = a + b - corner
    out = []
    for i in range(segments + 1):
        th = (math.pi * 0.5) * i / segments
        f1 = math.cos(th) ** (2.0 / r) if math.cos(th) > 1e-12 else 0.0
        f2 = math.sin(th) ** (2.0 / r) if math.sin(th) > 1e-12 else 0.0
        out.append(o + (a - o) * f1 + (b - o) * f2)
    return out


def point_on_polyline(pts, arc, s):
    """Point at arc length s along a polyline with cumulative lengths `arc`."""
    if s <= 0.0:
        return pts[0].copy()
    for i in range(1, len(arc)):
        if arc[i] >= s:
            f = 0.0 if arc[i] - arc[i - 1] < 1e-12 else (s - arc[i - 1]) / (arc[i] - arc[i - 1])
            return pts[i - 1].lerp(pts[i], f)
    return pts[-1].copy()


def cumulative(pts):
    arc = [0.0]
    for a, b in zip(pts, pts[1:]):
        arc.append(arc[-1] + (b - a).length)
    return arc
