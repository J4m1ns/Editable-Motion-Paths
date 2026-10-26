# Editable Motion Paths: developer guide (for people and AI assistants forking the add-on)

This file explains how the add-on is built, so you (or an AI assistant you work with) can change
it or add features on top of it without having seen the project before. Read it once from top to
bottom before changing code. `README.md` describes what the add-on does for an animator;
`AI_GUIDE.md` describes the scripting API for AI assistants that *use* the add-on. This file is
about the code.

## 1. What the add-on is

A Blender extension (Blender 4.4+, developed and tested on 5.x) for animators. It draws the
motion of a bone or an object in the 3D view as a curve (the **path**), with a dot per frame
(the spacing) and the **keys** on it, and lets the animator edit the motion right there: drag
keys and handles (G / R / S like in Edit Mode), smooth, ease, retime, sculpt the path with
brushes, add / dissolve keys like vertices, see onion-skin ghosts, a timing chart, a speed
graph and the character's center of mass.

Four principles shape every part of the code:

1. **Everything it changes is ordinary Blender animation.** Edits write F-Curve keys and handles
   (and F-Curve modifiers). Nothing is stored that Blender doesn't understand; a file opens fine
   without the add-on. The selection of path keys *is* the keyframe selection of the F-Curves,
   so undo and the Graph Editor stay in sync.
2. **The path follows edits in real time.** Only what the bone / object inherits (its parents,
   constraints) is baked by stepping frames; its own motion is evaluated from its F-Curves on
   every refresh (section 3).
3. **It behaves like Blender.** Hotkeys, modal operators, header text, redo panels, Esc / right
   click to cancel, snapping, pivot point, proportional editing: the same conventions as Edit
   Mode. When in doubt, do what Blender's mesh tools do.
4. **One undo step per action.** Every operator that changes animation has `'UNDO'` in
   `bl_options` and changes it once (modal operators restore a snapshot while dragging, see 5).

## 2. Layout

```
editable_motion_paths/          the extension (this folder is what gets zipped)
  blender_manifest.toml         extension manifest (id, version, Blender minimum, license)
  __init__.py                   bl_info, imports, register() / unregister() order
  props.py                      settings: scene.emp (EMP_Settings), wm.emp_brush, the add-on preferences
  state.py                      runtime state (not saved): path caches, change tracking, re-bake timers
  core.py                       path evaluation: PathCache, Target, channels, baking, writing keys
  compat.py                     differences between Blender versions (F-Curves of layered actions...)
  draw.py                       viewport drawing (gpu module): paths, keys, handles, charts, speed graph
  pick.py                       hit testing (what's under the mouse) and selection helpers
  edit.py                       helpers for operators that move keys / handles to world positions
  ui.py                         sidebar panels, menus, the WorkSpaceTool and its keymap (TOOL_KEYMAP)
  api.py                        scripting API (also used by AI assistants through MCP)
  ops_*.py                      operators, grouped by topic (see the table below)
  bezier.py, geom.py, fit.py,   math without Blender data: Bezier segments, geometry, key refitting
  easing.py, keys.py            (Clean Up), easing curves, key topology on F-Curves
  onion.py, com.py              onion skinning, center of mass (background work, own draw passes)
  icons/                        PNG icons (brushes, easing gallery)
README.md, AI_GUIDE.md, AI_DEV_GUIDE.md, LICENSE (GPL-3.0-or-later)
```

| module | what it holds |
|---|---|
| `ops_transform.py` | G / R / S / shear / twist of keys and handles, numeric input (`NumInput`), constraints, snapping |
| `ops_select.py` | click, drag, box / circle select, select more / less / linked / similar, hide / reveal; the click dispatcher (`EMP_OT_click`) also starts speed-graph and timing-chart drags |
| `ops_keys.py` | delete, dissolve, insert, extrude, duplicate, loop cut, knife, merge, copy / paste |
| `ops_shape.py` | shape tools: snap (Shift+S), mirror, bevel, spin, LoopTools (Circle, Arc, Flatten, Straighten, Follow Stroke) |
| `ops_tools.py` | path management (add / remove / update), Smooth, Exaggerate, Ease, handle types, Clean Up, center of mass operators, small view operators |
| `ops_slide.py` | G G: slide keys along the path (Spacing / Timing) |
| `ops_timing.py` | the spacing engine: `SpaceJob` (a segment's arc length over time) and `refit` (keys and handles that follow a target motion) |
| `ops_timetools.py` | Timing menu (Breakdown, Scale Time, Add Frames, Moving Hold, Offset Cascade, Snap), frame-dot / timing-chart drags, speed graph drags and smoothing |
| `ops_easing.py` | easing presets and the gallery (P) drawn in the viewport |
| `ops_sculpt.py` | Sculpt Motion Path tool: timing and shape brushes, Dissolve brush, brush strip |
| `ops_fmods.py` | F-Curve modifiers as a modifier stack for the active path |
| `ops_io.py` | path to Curve object, path follows a curve |
| `ops_value.py` | Edit Value popup (type exact values) |
| `snap.py`, `space.py` | Blender's snapping and transform pivot / orientation applied to path keys |

## 3. The core model (`core.py`)

### Paths and caches

A path is identified by `(object name, bone name or "", type)`, type one of `core.LOCATION`,
`ROTATION`, `SCALE`, `VALUE`. The list the user sees is `scene.emp.paths` (saved with the file);
the runtime data is a `core.PathCache` in `state.caches[key]` (not saved, rebuilt when needed).

```
world(f) = B(f) @ basis(f)
```

* `B(f)`: the **parent space** at frame f - everything the target inherits (parents,
  constraints, drivers). It is baked once by stepping through the frames (`core.bake_range`,
  in the background in steps: `core.BakeJob`) and kept in `cache.B`.
* `basis(f)`: the target's own loc / rot / scale, evaluated **from its F-Curves** on every
  `cache.refresh()`. So when a key moves, the path follows at once, without stepping frames.

Location paths trace the bone head (object origin). Rotation paths trace the bone tip (a
virtual length, *Rotation Length*). Scale and Value paths are drawn as ribbons / tracks on
the location path.

What a `PathCache` gives you after `refresh()`:

| attribute / method | meaning |
|---|---|
| `times`, `points`, `speeds` | samples of the path (world points), several per frame |
| `keys` | `KeyData` per key time: `t`, `co` (world point), `hl` / `hr` (world handle points or None), `sel`, `vals` (channel values), `idx` (keyframe index per channel, -1 = no key on that channel = a "partial" key), `type_l` / `type_r` (handle types), `dt_l` / `dt_r` (handle lengths in frames), `hidden` |
| `target()` | a `core.Target`: the object / pose bone and its F-Curves per channel group (`tg.channels(ptype).fcurves`) |
| `point(tg, t, vals=None)` | the world point at frame t (vals: channel values to use instead of the curves') |
| `solve(tg, t, W, ref_vals)` | the inverse: channel values that put the path at world point W at frame t (exact for location; a minimal swing rotation for rotation) |
| `owner_selected()`, `locked` | editable only when its bone / object is selected and it isn't locked (e.g. a connected bone's location) |
| `dirty`, `refresh(force=True)` | mark changed / rebuild from the curves |

`state.visible_caches(scene)` are the paths drawn; `state.editable_caches(scene)` the ones that
can be edited now (memoized per draw pass - call it freely).

### Writing animation

Never set keyframe points by hand in an operator; use the helpers so partial keys, cycles,
handle types and Blender's handle recalculation are handled:

```python
from . import edit
for cache, keys in edit.selected(context):                 # [(cache, [selected KeyData])]
    tg, fresh = edit.prepare(context, cache, [k.t for k in keys])   # key every channel there first
    w = edit.Writer([k.t for k in fresh])
    for k in fresh:
        w.key(cache, tg, k, k.co + offset)                  # put the key at a world position
        if k.hr is not None:
            w.handle(cache, tg, k, 'R', k.hr + offset)      # and a handle
    w.done([cache])                                         # fcurve.update(), refresh, redraw
```

Lower level: `core.write_key`, `core.write_handle`, `core.set_handle_types`, `core.ensure_keys`
(add missing channel keys - **lazily**, only when the user actually changes something, or the
curve changes just by starting a drag), `core.update_fcurves`.

For timing (where the dots fall, keeping the shape): build a `SpaceJob(cache, tg, f0, f1)`
(arc length over time of that part), describe the wanted motion as a function
`target(t) -> channel values` (usually: the arc length you want at t, mapped back with
`job._time_at_arc` and `cache.solve`), then `refit(job, tg, target, selection)` puts keys and
handles that follow it. Easing presets, Space, the speed graph and the timing brushes all do
this.

### Change tracking and re-baking (`state.py`)

`state.py` watches the depsgraph (`_on_depsgraph`): when the animation of a parent / constraint
/ driver changes, it schedules a re-bake (`state.schedule_rebake`) that runs in steps on a timer
(`BakeJob`, small budgets so the UI keeps up). Undo / redo, file load and the bake are handled
there too. Rules:

* **Never keep RNA references** (objects, bones, `scene.emp`, F-Curves) across undo or across
  timer ticks; keep names and look them up again (`core.resolve(obj_name, bone_name)`). A
  stale reference crashes Blender.
* Background work that steps frames must leave the scene as it found it (`state.keep_pose`,
  `state.flush_changes()` before stepping, `state.put_pose_back`).
* Call `state.tag_redraw()` after changing what is drawn, `pick.tag_anim_editors()` after
  changing keys (Graph Editor / Dope Sheet / timeline).

## 4. Settings (`props.py`)

* `scene.emp` (`EMP_Settings`): per-scene settings the animator changes while working (edit
  mode, what is shown, the path list, tool options). Saved with the file.
* `context.window_manager.emp_brush` (`EMP_BrushSettings`): sculpt brush settings.
* `EMP_Preferences` (the add-on's preferences): what you set once - colors, sizes, chart look,
  Smooth Display, notes. Names in `PREF_NAMES` are also readable as `st.<name>` on the scene
  settings (proxies), so drawing code reads one place. Add a preference to the class, draw it
  in `EMP_Preferences.draw`, and add it to `PREF_NAMES` **before** `"graph_corner"` (the
  slice `GRAPH_LOOK` counts from the end).
* Settings that are about the *view* (what is shown) must not be undone: they are listed in
  `state._UNDONE_SETTINGS`; check it when you add one.
* Blender rejects property `get` / `set` functions with two arguments: use one-argument
  wrappers.

## 5. Operators

Plain operators follow Blender's pattern; things specific to this add-on:

* **Poll / pass through.** The tool keymap (`ui.TOOL_KEYMAP`) has priority while the path tool
  is active, so an operator that doesn't apply (nothing selected, mouse not on a path) must
  `return {'PASS_THROUGH'}` from `invoke`, letting Blender (or another add-on) have the key.
* **Modal drags.** Take a snapshot first (`core.FCurveSnapshot(tg, tg.channels(ptype))`, or
  `ops_timetools.pose_snapshot(cache)` for whole poses); on every mouse move restore it and
  apply the change again from scratch (no drift); Esc / right click restores it and returns
  `{'CANCELLED'}`. Count `state.modal_running += 1` while running (background work waits),
  show the state with `area.header_text_set(...)`, clear both at the end.
* **Redo panel.** Operator properties are the redo panel; make `execute()` do the whole edit
  from the properties so changing them in the panel works. Use `options={'SKIP_SAVE'}` for a
  property that must start at its default every time.
* **Messages.** `self.report({'WARNING'}, "...")` with what to do ("Select keys of a path (2
  or more: the part between them)"). `state.flash_message(text, seconds)` shows a note at the
  bottom of the 3D view.
* Register a new class by adding it to the module's `classes` tuple (collected in
  `__init__._classes`). Put it in a menu / panel in `ui.py`; give it a hotkey in
  `TOOL_KEYMAP` (`_k("emp.my_op", 'Y', shift=True)`) only if it is used all the time -
  `t_ui`-style tests check the keymap has no duplicates.

## 6. Drawing (`draw.py`)

Everything is drawn with Blender's `gpu` module from two draw handlers on the 3D view:
`draw_view` (POST_VIEW, 3D: paths, ribbons, ghosts) and `draw_pixel` (POST_PIXEL, 2D: keys,
handles, numbers, the timing chart, the speed graph, the easing gallery, notes). A failing part
is caught and reported once (`_part_failed`) so one bug doesn't blank the whole overlay.

* Lines: `_flat(points, color, 'LINES' / 'LINE_STRIP' / 'LINE_LOOP', width)` (one color) and
  `_lines(coords, colors, width)` use the POLYLINE shaders, anti-aliased when *Smooth Display*
  is on (`_aa`). Dots: collect them in a `Discs()` and draw once (one batch per color, soft
  edges). Batches of big paths are cached on the cache (`_run_batches`) - drop the cache when
  you change what they depend on.
* Things a modal operator wants drawn go through `state` (e.g. `state.hud`, `state.box`,
  `state.speed_reach`): the operator sets them, `draw_pixel` draws them, the operator clears
  them at the end.
* Overlay drawing doesn't show in `GPUOffScreen.draw_view3d` (MCP screenshots); `api.render_view`
  draws the overlay into its own image for scripts.

## 7. The scripting API (`api.py`)

Functions an AI assistant (or a script) calls. Conventions, so it stays easy for a model to use:

* Identify paths by names: `fn(obj, bone=None, frames=None, ..., mode=None)`. A `mode` given to
  one call applies to that call only (`_in_mode`).
* Wrong names / frames raise `ValueError` that says what exists ("Did you mean ...?" via
  `_suggest`), never a bare `KeyError`.
* Run operators through `_run_op(bpy.ops.emp.<op>, frames, obj, bone, mode, **props)`: it
  isolates the selection to that path's keys (other paths and Blender's selection are left as
  they were) and runs the operator in a 3D-view context. Each call is one undo step
  (`_push(message)` for edits that don't go through an operator).
* Return plain Python data (dicts / lists / numbers) a model can read; round numbers.
* Document every new function in `AI_GUIDE.md` with a one-line example.

## 8. Adding a feature: a worked example

"Reverse the timing of the selected keys" (the last key's pose goes first):

1. **Operator** in the module that fits (`ops_timetools.py`, timing):
   ```python
   class EMP_OT_reverse_keys(bpy.types.Operator):
       """Reverse the timing of the selected keys of each path (the poses play backwards)"""
       bl_idname = "emp.reverse_keys"
       bl_label = "Reverse Keys"
       bl_options = {'REGISTER', 'UNDO'}

       def execute(self, context):
           done = 0
           for c in _selected_caches(context):
               sel = sorted(k.t for k in c.keys if k.sel)
               if len(sel) < 2:
                   continue
               a, b = sel[0], sel[-1]
               move_keys_in_time(c, {t: a + b - t for t in sel}, sel)   # (whole pose, selection kept)
               done += 1
           if not done:
               self.report({'WARNING'}, "Select 2 or more keys of a path")
               return {'CANCELLED'}
           pick.tag_anim_editors()
           return {'FINISHED'}
   ```
   and add it to that module's `classes`.
2. **UI**: a line in the Timing menu (`EMP_MT_timing.draw`), maybe the sidebar.
3. **API** (optional): `def reverse_keys(obj, bone=None, frames=None, mode=None): return
   _run_op(bpy.ops.emp.reverse_keys, frames, obj, bone, mode)` + a line in `AI_GUIDE.md`.
4. **Docs**: a sentence in `README.md` where the Timing menu is described.
5. **Test** (section 9): keys at 1, 5, 20 selected -> keys at 1, 16, 20, the pose of frame 20 now
   at 1, nothing else changed; undo puts it back.

## 9. Testing

Blender operators need a window, and many need real mouse events. The approach that works:
run a **separate Blender** with `--factory-startup` (never your own preferences / installed
copy), enable the add-on from your working folder, build a small scene from Python, drive it,
check numbers, write a log, quit.

```python
# test_reverse.py  -  blender --enable-event-simulate --factory-startup -p 0 0 1400 900 --python test_reverse.py
import sys, traceback
import bpy
sys.path.insert(0, r"C:\path\to\repo")                    # the folder that holds editable_motion_paths/
import addon_utils
addon_utils.enable("editable_motion_paths", default_set=True)
from editable_motion_paths import state

LOG = open(r"C:\path\to\test_reverse_log.txt", "w")

def check(name, ok, info=""):
    LOG.write("%s %s   %s\n" % ("PASS" if ok else "FAIL", name, info))

def steps():
    yield 0.3                                            # (let the window come up)
    # ... make an object, key it, add a path with bpy.ops.emp.path_add, select keys ...
    # ... run the operator in a 3D-view context, compare key frames / world points ...
    # simulated input: win.event_simulate(type='G', value='PRESS', x=..., y=...)
    yield 0.2

def run():
    gen = steps()
    def tick():
        try:
            return next(gen)
        except StopIteration:
            pass
        except Exception:
            LOG.write(traceback.format_exc())
        LOG.close()
        bpy.ops.wm.quit_blender()
    bpy.app.timers.register(tick, first_interval=0.5)

run()
```

* Operators that need the 3D view: `with bpy.context.temp_override(window=win, area=area,
  region=region): bpy.ops.emp.something(...)` with the 3D view's window region.
* `--enable-event-simulate` + `window.event_simulate(...)` gives real mouse / key events: move
  the mouse to the start position *before* pressing (drags start from the press position);
  yield a little between events so modal operators see them.
* Check invariants, not pixels: "inserting a key doesn't change the motion" (world points
  before == after), "the keys keep their frames", "undo restores the curves".
* Don't keep RNA references across `undo` in a test either; look objects up by name again.
* The author's own suite (about 60 scripts, a few thousand checks, synthetic scenes plus
  Blender Studio's rigs) isn't in the repository; write tests for what you change.

**Building and installing:**

```bash
blender --factory-startup --command extension build --source-dir editable_motion_paths --output-dir .
```

Install the zip in a clean profile to check it registers (set `BLENDER_USER_RESOURCES` to an
empty folder, then `bpy.ops.extensions.package_install_files(filepath=..., repo="user_default",
enable_on_install=True)` in a script). Bump `version` in `blender_manifest.toml` *and*
`bl_info` in `__init__.py` for a release.

## 10. Blender API pitfalls this code already works around

* `fcurve.keyframe_points.insert` changes the neighbors' handles: insert all keys first, then
  set handles. Its `'REPLACE'` option returns None for new keys.
* A curve with a Cycles modifier: never insert keys outside its range (it changes the period);
  wrap the frame into the loop and keep the first and last key equal.
* Handle type `ALIGNED` next to an `AUTO` handle on the other side makes Blender turn the
  aligned one when the curve updates; set both, or keep `FREE` until both are in line.
* Layered actions (Blender 4.4+ / 5.x): get F-Curves through `compat.get_fcurves(owner)`, not
  `action.fcurves`.
* Library overrides: new custom properties on an override's pose bones aren't saved; store
  per-shot data in the scene (see `com._store`).
* A `tag_redraw()` asked for while Blender is drawing can be lost; ask from a 0-second
  `bpy.app.timers.register` instead (see `draw.watch_tool`).
* An operator instance's `bl_idname` reads `"EMP_OT_name"`, not `"emp.name"` - compare classes
  or class attributes, not that string.
* An operator property named `mode` clashes with keyword parameters of helpers; underscore
  the helper's own parameters (`_run_op(_op, _frames, ...)`).
* Operator enum items are in `self.properties.bl_rna`, not `self.bl_rna`.
* Event-simulated keys have no `ascii`; numeric input maps key types.

## 11. Style

* Plain English in the UI, written for animators: say what happens ("the keys keep their
  frames"), not how. Title Case for buttons and properties, sentence case in tooltips.
* Comments explain *why* (a Blender quirk, a design choice), in the same voice as the existing
  ones; the code says *what*.
* No new Python dependencies (Blender's bundled Python has `numpy`; use it for heavy math in
  drawing and fitting).
* Keep it fast on big rigs: no work per frame in draw handlers beyond drawing (cache batches,
  memoize per draw pass), background work in small timed steps.
* Version differences go into `compat.py`.

## 12. License

GPL-3.0-or-later, like Blender. Forks must keep that license; mention what you changed.
