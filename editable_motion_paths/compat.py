"""Small helpers that hide API differences between Blender versions."""

import bpy

_HAS_SLOTS = hasattr(bpy.types, "ActionSlot")
_PB_SELECT = "select" in bpy.types.PoseBone.bl_rna.properties


def get_fcurves(id_data, create=False):
    """F-Curve collection animating `id_data` (supports `.find(path, index=)`), or None."""
    ad = getattr(id_data, "animation_data", None)
    if ad is None or ad.action is None:
        return None
    action = ad.action
    if _HAS_SLOTS:
        from bpy_extras import anim_utils
        slot = ad.action_slot
        if slot is not None:
            cb = anim_utils.action_get_channelbag_for_slot(action, slot)
            if cb is None and create:
                cb = anim_utils.action_ensure_channelbag_for_slot(action, slot)
            if cb is not None:
                return cb.fcurves
        if not hasattr(action, "fcurves"):
            return None
    return action.fcurves


def ensure_fcurve(id_data, data_path, index, group_name=""):
    # (no animation yet - keying a control from scratch: an action of its own)
    ad = id_data.animation_data or id_data.animation_data_create()
    if ad.action is None:
        ad.action = bpy.data.actions.new(id_data.name + "Action")
    action = ad.action
    if hasattr(action, "fcurve_ensure_for_datablock"):
        try:
            return action.fcurve_ensure_for_datablock(id_data, data_path, index=index, group_name=group_name)
        except TypeError:
            # (Blender 4.4: no group yet)
            return action.fcurve_ensure_for_datablock(id_data, data_path, index=index)
    fc = action.fcurves.find(data_path, index=index)
    if fc is None:
        fc = action.fcurves.new(data_path, index=index, action_group=group_name)
    return fc


def remove_fcurve(id_data, data_path, index):
    coll = get_fcurves(id_data)
    if coll is None:
        return
    fc = coll.find(data_path, index=index)
    if fc is not None:
        coll.remove(fc)


def bone_selected(pb):
    return pb.select if _PB_SELECT else pb.bone.select


def bone_select_set(pb, value):
    if _PB_SELECT:
        pb.select = value
    else:
        pb.bone.select = value
        pb.bone.select_head = value
        pb.bone.select_tail = value


def ui_scale():
    prefs = bpy.context.preferences
    return prefs.system.ui_scale if prefs else 1.0
