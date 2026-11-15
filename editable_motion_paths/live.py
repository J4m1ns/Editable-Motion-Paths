"""The paths of children follow an edit of a parent at once (issue #12: a sword's tip while its arm turns).

A path is its parent space B(f) - baked frame by frame - times its own channels. When a bone / object that has a
path itself is edited, everything it carries by plain parenting moves with it rigidly: the world change of the
parent on each frame, D(f) = B(f) @ basis_now(f) @ basis_baked(f)^-1 @ B(f)^-1, put in front of a child's baked
parent space gives the child's new one exactly - no frames stepped through, a fraction of a millisecond for a long
range. Parents edited one under the other (arm, then sword) are applied from the root down.

Where the chain isn't plain (a constraint on a bone between them, a bone that doesn't inherit rotation / scale in
full, a parent with constraints of its own) nothing is guessed: the child's path waits for the re-bake as before.
After each full bake (and an undo that brings an older bake back) what the bake was made from is noted."""

from . import core, state

_anc = {}       # owner (object, bone) -> {"c", "B", "fs", "basis"}: a parent with a path, as baked
_kids = {}      # cache key -> (baked B list, fs): every path's parent space as baked
_chain = {}     # owner -> [ancestor owners that carry it rigidly, root first]
_live = {}      # cache key -> the B list put on the cache here (a live one)
_depth = {}
_moved_before = set()    # ancestors edited since the bake (an edit undone back to the bake's curves: worked out once more)


def _owner_depth(obj, owner, bone):
    n = 0
    if bone and owner is not None:
        n += len(owner.parent_recursive) + 1
    o = obj
    while o is not None and o.parent is not None:
        n, o = n + 1, o.parent
    return n


def _active(cons):
    return [c for c in cons if not c.mute and getattr(c, "enabled", True) and c.influence > 0.0]


def _plain_parent(owner, arm=None):
    """Its constraints leave its world as parent space @ basis (none, or full Child Ofs)."""
    return all(core._full_child_of(c, arm) for c in _active(owner.constraints))


def rigidly_carried(a_obj, a_bone, c_obj, c_bone):
    """Does C hang from A by plain parenting only - every joint between them (C itself included, A not) without
    constraints, bones that inherit rotation and scale in full: an edit of A moves C with it, rigidly."""
    if a_obj is None or c_obj is None or (a_obj == c_obj and a_bone == c_bone):
        return False
    o, b = c_obj, c_bone
    for _i in range(1000):
        if o is None:
            return False
        if b and o.type == 'ARMATURE' and o.pose is not None:
            pb = o.pose.bones.get(b)
            while pb is not None:
                if o == a_obj and pb.name == a_bone:
                    return True
                if _active(pb.constraints) or not pb.bone.use_inherit_rotation or pb.bone.inherit_scale != 'FULL':
                    return False
                pb = pb.parent
            b = ""
            if o == a_obj and not a_bone:
                return True
        elif o == a_obj and not a_bone and (o != c_obj or c_bone):
            return True
        if _active(o.constraints):
            return False        # (an object between them - or C itself - with constraints)
        if o.parent is None or o.parent_type not in {'OBJECT', 'BONE', 'ARMATURE'}:
            return False
        o, b = o.parent, (o.parent_bone if o.parent_type == 'BONE' else "")
    return False


def _basis_list(tg, fs, n):
    out = []
    for i in range(n):
        t = float(fs + i)
        out.append(tg.basis(tg.loc.values(t), tg.rot.values(t), tg.scale.values(t)))
    return out


def _full_space(c):
    """This path's B is the whole parent space (world = B @ basis) - not a location path's own map."""
    return c.ptype != core.LOCATION or not state._own_loc_space(c.obj_name, c.bone_name)


def clear():
    _anc.clear()
    _kids.clear()
    _chain.clear()
    _live.clear()
    _depth.clear()
    _moved_before.clear()


def snapshot():
    """After a full bake (or an undo that brought one back): what it was made from."""
    clear()
    try:
        owners = {}
        for c in state.caches.values():
            if c.B:
                owners.setdefault((c.obj_name, c.bone_name), []).append(c)
        if len(owners) < 2:
            return
        res = {}
        for k in owners:
            obj, owner = core.resolve(*k)
            if owner is not None:
                res[k] = (obj, owner)
                _depth[k] = _owner_depth(obj, owner, k[1])
        for ka, cs in owners.items():
            if ka not in res:
                continue
            a = next((c for c in cs if _full_space(c)), None)
            obj, owner = res[ka]
            if a is None or not _plain_parent(owner, obj if ka[1] else None):
                continue
            kids = [kb for kb in owners if kb != ka and kb in res and rigidly_carried(obj, ka[1], res[kb][0], kb[1])]
            if not kids:
                continue
            tg = a.target()
            if tg is None:
                continue
            a.refresh()
            _anc[ka] = {"c": a, "B": a.B, "fs": a.fs, "basis": _basis_list(tg, a.fs, len(a.B)), "sig": a.sig}
            for kb in kids:
                _chain.setdefault(kb, []).append(ka)
        if not _anc:
            return
        for kb in _chain:
            _chain[kb].sort(key=lambda k: _depth.get(k, 0))
        for kb in _chain:
            for c in owners.get(kb, ()):
                _kids[c.key] = (c.B, c.fs)
    except (ReferenceError, AttributeError):
        clear()


def _identity(m):
    return all(abs(m[i][j] - (1.0 if i == j else 0.0)) < 1e-9 for i in range(4) for j in range(4))


def _apply(D, fsD, B, fsB):
    """D (per frame, from fsD) in front of B (per frame, from fsB)."""
    n = len(D)
    out = []
    for i, b in enumerate(B):
        j = min(max(fsB + i - fsD, 0), n - 1)
        out.append(D[j] @ b if D[j] is not None else b)
    return out


def update():
    """The edited parents' curves now against the bake: their children's paths moved with them. True if any."""
    if not _anc:
        return False
    try:
        return _update()
    except (ReferenceError, AttributeError, IndexError):
        clear()
        return False


def _update():
    deltas = {}         # ancestor owner -> (fs, [D per frame or None])
    for ka in sorted(_anc, key=lambda k: _depth.get(k, 0)):
        info = _anc[ka]
        a = info["c"]
        if state.caches.get(a.key) is not a or (a.B is not info["B"] and a.B is not _live.get(a.key)):
            continue        # (baked again some other way meanwhile)
        a.refresh()
        if a.sig == info["sig"] and ka not in _moved_before:
            continue        # (its curves as baked)
        tg = a.target()
        if tg is None:
            continue
        basis = _basis_list(tg, info["fs"], len(info["basis"]))
        _moved_before.add(ka)
        if all(_identity(n @ o.inverted_safe()) for n, o in zip(basis, info["basis"])):
            continue
        # its own parent space as the parents above it moved it
        B = info["B"]
        for kp in _chain.get(ka, ()):
            if kp in deltas:
                B = _apply(deltas[kp][1], deltas[kp][0], B, info["fs"])
        D = []
        for b, n, o in zip(B, basis, info["basis"]):
            d = b @ n @ o.inverted_safe() @ b.inverted_safe()
            D.append(None if _identity(d) else d)
        if any(d is not None for d in D):
            deltas[ka] = (info["fs"], D)
    moved = False
    for key, (B0, fs0) in list(_kids.items()):
        c = state.caches.get(key)
        if c is None or (c.B is not B0 and c.B is not _live.get(key)) or c.fs != fs0:
            continue
        M = B0
        for ka in _chain.get(key[:2], ()):
            if ka in deltas:
                M = _apply(deltas[ka][1], deltas[ka][0], M, fs0)
        if M is B0:
            if c.B is not B0:            # (the parents back as baked - an undo of the edit)
                c.B = B0
                _live.pop(key, None)
                c.dirty = True
                moved = True
            continue
        c.B = M
        _live[key] = M
        c.dirty = True
        moved = True
    return moved
