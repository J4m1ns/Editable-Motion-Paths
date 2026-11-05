"""Shared helpers for operators that put path keys (and handles) at new world positions."""

from . import core, pick, state


def selected(context, min_keys=1):
    """[(cache, [selected keys])] of the editable paths."""
    out = []
    for c in state.editable_caches(context.scene):
        ks = [k for k in c.keys if k.sel]
        if len(ks) >= min_keys:
            out.append((c, ks))
    return out


def prepare(context, cache, frames):
    """Key every channel on `frames` (and pin partial neighbors) before editing; returns the
    fresh target and the keys at `frames`."""
    tg = cache.target()
    st = state.settings(context.scene)
    if core.ensure_keys(tg, cache.ptype, frames, None, st.pin_partial if st else True):
        cache.refresh(force=True)
        tg = cache.target()
    want = {round(t, 3) for t in frames}
    return tg, [k for k in cache.keys if round(k.t, 3) in want]


class Writer:
    """Collects key / handle writes of one operator run."""

    def __init__(self, frames=()):
        self.touched = set()
        core.cycle_skip = {round(t, 3) for t in frames}

    def key(self, cache, tg, k, W, move_handles=True):
        """Put key k at world position W (rotation paths: aim the tip at W)."""
        vals = cache.solve(tg, k.t, W, k.vals)
        core.write_key(tg.channels(cache.ptype), k, vals, move_handles, self.touched)
        return vals

    def handle(self, cache, tg, k, side, W, vals=None, htype=None, dt=None):
        """Put the handle end of key k at world position W."""
        vals = k.vals if vals is None else vals
        dt = (k.dt_r if side == 'R' else k.dt_l) if dt is None else dt
        if dt < core.EPS:
            return
        slopes = cache.slopes_from_point(tg, k, side, W, vals, dt)
        core.write_handle(tg.channels(cache.ptype), k, side, slopes, dt, vals, htype, self.touched)

    def done(self, caches):
        core.update_fcurves(self.touched)
        for c in caches:
            c.dirty = True
            c.refresh(force=True)
        pick.tag_anim_editors()
        state.tag_redraw()
