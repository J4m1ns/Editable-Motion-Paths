"""Key reduction / refitting for Clean Up.

The edited channels of a path are approximated by C1 cubic Hermite splines whose knots
are shared by all channels (a key on the path = a key on every channel). Knot values are
the original values; the slopes at the knots are found by linear least squares over all
sampled frames (one tridiagonal system per channel). Corners of the original motion
(broken / vector handles, linear segments) and the ends of the range keep their original
left / right slopes.  The quality of a fit is measured where the animator sees it: as the
distance between the fitted and the original path in 3D.
"""

import bisect

from . import core

STEP = 0.5          # sampling step in frames
EPS_D = 0.02        # finite difference step for slopes


def _h(u):
    u2 = u * u
    u3 = u2 * u
    return 2 * u3 - 3 * u2 + 1, u3 - 2 * u2 + u, -2 * u3 + 3 * u2, u3 - u2


def _thomas(a, b, c, d):
    """Solve a tridiagonal system (a: sub, b: main, c: super diagonal)."""
    n = len(b)
    if n == 0:
        return []
    cp = [0.0] * n
    dp = [0.0] * n
    cp[0] = c[0] / b[0] if n > 1 else 0.0
    dp[0] = d[0] / b[0]
    for i in range(1, n):
        m = b[i] - a[i] * cp[i - 1]
        if abs(m) < 1e-30:
            m = 1e-30
        cp[i] = c[i] / m if i < n - 1 else 0.0
        dp[i] = (d[i] - a[i] * dp[i - 1]) / m
    x = [0.0] * n
    x[-1] = dp[-1]
    for i in range(n - 2, -1, -1):
        x[i] = dp[i] - cp[i] * x[i + 1]
    return x


class Fitter:
    def __init__(self, cache, tg, r0, r1, target=None):
        """Fit the channels over [r0, r1]: to their current curves, or to `target(t)` (channel
        values of a new motion inside the range; outside it the curves stay as they are)."""
        self.cache = cache
        self.tg = tg
        ch = tg.channels(cache.ptype)
        self.ch = ch
        self.chans = [c for c, fc in enumerate(ch.fcurves) if fc is not None and len(fc.keyframe_points)]
        self.r0, self.r1 = r0, r1
        self.target = target
        self._vals = {}
        self._der = {}
        n = max(1, int(round((r1 - r0) / STEP)))
        self.S = [r0 + (r1 - r0) * i / n for i in range(n + 1)]
        self.Y = [self.value(t) for t in self.S]
        self.P = [cache.measure(tg, t, v) for t, v in zip(self.S, self.Y)]
        # slope scale per channel, to tell corners from smooth keys
        self.scale = {}
        for c in self.chans:
            d = [abs(self.Y[i + 1][c] - self.Y[i][c]) / (self.S[i + 1] - self.S[i]) for i in range(len(self.S) - 1)]
            self.scale[c] = max(d) if d else 0.0
        lo = [min(p[i] for p in self.P) for i in range(3)]
        hi = [max(p[i] for p in self.P) for i in range(3)]
        self.size = max(sum((h - l) ** 2 for h, l in zip(hi, lo)) ** 0.5, 1e-6)

    # -- original curve ----------------------------------------------------------

    def value(self, t):
        v = self._vals.get(t)
        if v is None:
            if self.target is not None and self.r0 - 1e-9 <= t <= self.r1 + 1e-9:
                v = self.target(t)
            else:
                v = self.ch.values(t)
            self._vals[t] = v
        return v

    def derivs(self, t):
        """(left, right) slope per channel of the original curves at t."""
        d = self._der.get(t)
        if d is None:
            a = self.value(t - EPS_D)
            m = self.value(t)
            b = self.value(t + EPS_D)
            d = self._der[t] = ([(m[c] - a[c]) / EPS_D for c in range(len(m))],
                                [(b[c] - m[c]) / EPS_D for c in range(len(m))])
        return d

    def is_corner(self, t):
        dl, dr = self.derivs(t)
        for c in self.chans:
            if abs(dl[c] - dr[c]) > 0.15 * self.scale[c] + 1e-7:
                return True
        return False

    def hold_keys(self, keys):
        """Keys at both ends of a hold (no motion between neighbor keys). A hold made of many
        keys (baked) keeps only its first and last key: the ones inside add nothing."""
        out = set()
        limit = self.size * 1e-3
        run = None
        for a, b in zip(keys, keys[1:]):
            pa = self.cache.measure(self.tg, a, self.value(a))
            still = True
            t = a
            while t < b:
                t = min(t + STEP, b)
                if (self.cache.measure(self.tg, t, self.ch.values(t)) - pa).length > limit:
                    still = False
                    break
            if still:
                run = (run[0] if run else a, b)
            elif run:
                out |= set(run)
                run = None
        if run:
            out |= set(run)
        return out

    def pose_keys(self, keys):
        """Keys at the poses of the motion: where it stops for a moment or turns back
        (contacts, extremes). Animators keep keys there."""
        if len(keys) < 3:
            return set()
        pts = {t: self.cache.measure(self.tg, t, self.value(t)) for t in keys}
        h = 0.5
        speed = {}
        for t in keys:
            a = self.cache.measure(self.tg, t - h, self.value(t - h)) if t - h >= self.r0 else pts[t]
            b = self.cache.measure(self.tg, t + h, self.value(t + h)) if t + h <= self.r1 else pts[t]
            speed[t] = (b - a).length / (2 * h)
        mean = sum(speed.values()) / len(speed) or 1e-9
        out = set()
        for i in range(1, len(keys) - 1):
            t = keys[i]
            d0 = pts[t] - pts[keys[i - 1]]
            d1 = pts[keys[i + 1]] - pts[t]
            # a stop for a moment: slower than both neighbors (inside a hold every key is still,
            # and only the hold's ends matter, see hold_keys)
            slow = speed[t] < 0.15 * mean and speed[t] < speed[keys[i - 1]] and speed[t] < speed[keys[i + 1]]
            back = d0.length > 1e-9 and d1.length > 1e-9 and d0.angle(d1) > 2.2     # ~125 degrees
            if slow or back:
                out.add(t)
        return out

    # -- fitting -------------------------------------------------------------------

    def fit(self, knots, fixed, given=None, half=None):
        """Slopes {knot: ([left per channel], [right per channel])} for sorted `knots`;
        `fixed` knots keep their original left/right slopes, or the ones in `given`.
        `half` {knot: 'L' or 'R'}: only that side is kept, the other one is fitted.
        Only the samples between the first and the last knot are used."""
        half = half or {}
        lo, hi = knots[0], knots[-1]
        pairs = [(t, y) for t, y in zip(self.S, self.Y) if lo - 1e-9 <= t <= hi + 1e-9]
        S = [p[0] for p in pairs]
        Y = [p[1] for p in pairs]
        vals = [self.value(t) for t in knots]
        slopes = {}
        for t in knots:
            if t in fixed or t in half:
                d = given[t] if given and t in given else self.derivs(t)
                slopes[t] = (list(d[0]), list(d[1]))
        nk = len(knots)
        for c in self.chans:
            # unknown per knot; it is the knot's right slope in the segment after it and its
            # left slope in the segment before it (a half fixed knot uses only one of them)
            idx_l, idx_r = {}, {}
            n = 0
            for i in range(nk):
                t = knots[i]
                if t in fixed and t not in half:
                    continue
                side = half.get(t)
                if side != 'L':
                    idx_l[i] = n
                if side != 'R':
                    idx_r[i] = n
                n += 1
            A = [0.0] * n
            B = [1e-12] * n
            C = [0.0] * n
            D = [0.0] * n
            seg = 0
            for s, t in enumerate(S):
                while seg < nk - 2 and t > knots[seg + 1]:
                    seg += 1
                t0, t1 = knots[seg], knots[seg + 1]
                h = t1 - t0
                u = (t - t0) / h
                h00, h10, h01, h11 = _h(u)
                const = h00 * vals[seg][c] + h01 * vals[seg + 1][c]
                ai = h10 * h
                aj = h11 * h
                ui = idx_r.get(seg)
                uj = idx_l.get(seg + 1)
                if ui is None:
                    const += ai * slopes[knots[seg]][1][c]
                if uj is None:
                    const += aj * slopes[knots[seg + 1]][0][c]
                r = Y[s][c] - const
                if ui is not None:
                    B[ui] += ai * ai
                    D[ui] += ai * r
                if uj is not None:
                    B[uj] += aj * aj
                    D[uj] += aj * r
                if ui is not None and uj is not None:
                    C[ui] += ai * aj
                    A[uj] += ai * aj
            m = _thomas(A, B, C, D)
            for i in range(nk):
                t = knots[i]
                if t not in slopes:
                    z = [0.0] * len(vals[0])
                    slopes[t] = (list(z), list(z))
                if i in idx_l:
                    slopes[t][0][c] = m[idx_l[i]]
                if i in idx_r:
                    slopes[t][1][c] = m[idx_r[i]]
        return slopes

    def evaluate(self, knots, slopes, t):
        i = max(0, min(len(knots) - 2, bisect.bisect_right(knots, t) - 1))
        t0, t1 = knots[i], knots[i + 1]
        h = t1 - t0
        u = (t - t0) / h
        h00, h10, h01, h11 = _h(u)
        v0, v1 = self.value(t0), self.value(t1)
        out = list(self.Y[0])
        for c in range(len(v0)):
            if c in self.chans:
                out[c] = h00 * v0[c] + h10 * h * slopes[t0][1][c] + h01 * v1[c] + h11 * h * slopes[t1][0][c]
            else:
                out[c] = v0[c]
        return out

    def errors(self, knots, slopes, lo=None, hi=None):
        """3D distance between the fitted and the original path at every sample."""
        errs = []
        for t, p in zip(self.S, self.P):
            if (lo is not None and t < lo) or (hi is not None and t > hi):
                continue
            v = self.evaluate(knots, slopes, t)
            errs.append(((self.cache.measure(self.tg, t, v) - p).length, t))
        return errs

    def max_error(self, knots, slopes, lo=None, hi=None):
        e = self.errors(knots, slopes, lo, hi)
        return max(e) if e else (0.0, self.r0)

    # -- strategies ----------------------------------------------------------------

    def prune(self, knots, fixed, tol):
        """Remove knots one at a time (the one that hurts least first) while the whole
        path stays within tolerance."""
        knots = list(knots)
        keep = set(fixed)
        slopes = self.fit(knots, fixed)
        while True:
            best = None
            for i in range(1, len(knots) - 1):
                t = knots[i]
                if t in keep:
                    continue
                # cheap local estimate: refit a small window around the removed knot, with
                # the window ends held at their current slopes
                w0 = max(0, i - 3)
                w1 = min(len(knots) - 1, i + 3)
                sub = knots[w0:i] + knots[i + 1:w1 + 1]
                given = {sub[0]: slopes[sub[0]], sub[-1]: slopes[sub[-1]]}
                fx = (set(fixed) & set(sub)) | {sub[0], sub[-1]}
                for x in fx:
                    if x not in given:
                        given[x] = self.derivs(x)
                s_loc = self.fit(sub, fx, given)
                e = self.max_error(sub, s_loc, sub[0], sub[-1])[0]
                if e <= tol and (best is None or e < best[0]):
                    best = (e, i)
                if best is not None and best[0] < tol * 0.05:
                    break
            if best is None:
                break
            _e, i = best
            trial = knots[:i] + knots[i + 1:]
            s = self.fit(trial, fixed)
            if self.max_error(trial, s)[0] <= tol:
                knots = trial
                slopes = s
            else:
                keep.add(knots[i])
        return knots, slopes

    def refine(self, knots, fixed, tol, half=None, max_add=60):
        """Keep `knots` and add more where the error is largest until the path fits."""
        knots = sorted(knots)
        slopes = self.fit(knots, fixed, None, half)
        for _ in range(max_add):
            e, t = self.max_error(knots, slopes)
            if e <= tol:
                break
            # the whole frame nearest to the worst error that isn't a key yet
            base = float(round(t))
            cand = [base + d for d in (0.0, -1.0, 1.0, -2.0, 2.0)]
            cand = [x for x in cand if x not in knots and self.r0 < x < self.r1]
            if not cand:
                break
            bisect.insort(knots, cand[0])
            slopes = self.fit(knots, fixed, None, half)
        return knots, slopes

    def adaptive(self, fixed, tol, max_keys=400):
        """Grow knots where the error is largest until the path fits, then prune."""
        knots = sorted(set([self.r0, self.r1]) | set(fixed))
        slopes = self.fit(knots, fixed)
        while len(knots) < max_keys:
            e, t = self.max_error(knots, slopes)
            if e <= tol:
                break
            f = float(round(t))
            if f in knots or not (self.r0 < f < self.r1):
                cand = [x for x in (float(int(t)), float(int(t)) + 1.0) if x not in knots and self.r0 < x < self.r1]
                if not cand:
                    break
                f = cand[0]
            bisect.insort(knots, f)
            slopes = self.fit(knots, fixed)
        return self.prune(knots, fixed, tol)


# ----------------------------------------------------------------------------------
# writing the result
# ----------------------------------------------------------------------------------

def write(fitter, knots, slopes, select=True):
    """Replace the keys of the fitted channels inside [r0, r1] by the knots."""
    ch = fitter.ch
    r0, r1 = fitter.r0, fitter.r1
    touched = []
    for c in fitter.chans:
        fc = ch.fcurves[c]
        kps = fc.keyframe_points
        # what lies outside the range must not change: keep the outer handles of the ends
        outer_l = outer_r = None
        prev_x = next_x = None
        interp_end = None
        for kp in kps:
            x = kp.co[0]
            if abs(x - r0) < 1e-3:
                # relative to the key: it moves with the key if the key's value changes
                outer_l = ((kp.handle_left[0] - x, kp.handle_left[1] - kp.co[1]), kp.handle_left_type)
            if abs(x - r1) < 1e-3:
                outer_r = ((kp.handle_right[0] - x, kp.handle_right[1] - kp.co[1]), kp.handle_right_type)
                interp_end = kp.interpolation
            if x < r0 - 1e-3:
                prev_x = x
            if x > r1 + 1e-3 and next_x is None:
                next_x = x
        # the neighbors outside the range: Blender reshapes their handles when a key is
        # inserted next to them, so remember them and put them back afterwards
        outside = []
        for kp in kps:
            x = kp.co[0]
            if prev_x is not None and abs(x - prev_x) < 1e-3:
                outside.append((x, 'R', tuple(kp.handle_right), kp.handle_right_type))
            if next_x is not None and abs(x - next_x) < 1e-3:
                outside.append((x, 'L', tuple(kp.handle_left), kp.handle_left_type))
        for i in reversed(range(len(kps))):
            if r0 - 1e-3 <= kps[i].co[0] <= r1 + 1e-3:
                kps.remove(kps[i], fast=True)
        n = len(knots)
        # insert every knot first: inserting into a curve subdivides the segment and changes
        # the handles of the neighbor keys, so handles are only set once all keys exist
        for t in knots:
            kps.insert(t, fitter.value(t)[c], options={'FAST'})
        at = {round(kp.co[0], 3): kp for kp in kps}
        for i, t in enumerate(knots):
            kp = at[round(t, 3)]
            y = fitter.value(t)[c]
            ml = slopes[t][0][c]
            mr = slopes[t][1][c]
            kp.interpolation = 'BEZIER'
            tp = knots[i - 1] if i > 0 else prev_x
            tn = knots[i + 1] if i < n - 1 else next_x
            dtl = (t - tp) / 3.0 if tp is not None else 1.0 / 3.0
            dtr = (tn - t) / 3.0 if tn is not None else 1.0 / 3.0
            corner = abs(ml - mr) > 1e-9
            kp.handle_left_type = 'FREE' if corner else 'ALIGNED'
            kp.handle_right_type = 'FREE' if corner else 'ALIGNED'
            kp.co = (t, y)
            kp.handle_left = (t - dtl, y - ml * dtl)
            kp.handle_right = (t + dtr, y + mr * dtr)
            if i == 0 and outer_l is not None:
                kp.handle_left_type = 'FREE'
                kp.handle_right_type = 'FREE'
                kp.handle_left = (t + outer_l[0][0], y + outer_l[0][1])
            if i == n - 1:
                if outer_r is not None:
                    kp.handle_left_type = 'FREE'
                    kp.handle_right_type = 'FREE'
                    kp.handle_right = (t + outer_r[0][0], y + outer_r[0][1])
                if interp_end is not None:
                    kp.interpolation = interp_end
            on = (round(t, 3) in select) if isinstance(select, (set, frozenset)) else select
            kp.select_control_point = on
            kp.select_left_handle = on
            kp.select_right_handle = on
        for x, side, h, htype in outside:
            kp = at.get(round(x, 3))
            if kp is None:
                continue
            if side == 'R':
                kp.handle_right_type = htype
                kp.handle_right = h
            else:
                kp.handle_left_type = htype
                kp.handle_left = h
        touched.append(fc)
    core.update_fcurves(touched)
    return touched
