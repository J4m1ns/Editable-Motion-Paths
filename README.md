# Editable Motion Paths

Model your animation like geometry. Motion paths of bones and objects become editable curves
in the 3D Viewport, with keys as points, Bezier handles and Blender's own modeling tools and
hotkeys. Every edit goes straight into the F-Curves, so undo, the Graph Editor and the Dope
Sheet stay in sync. You shouldn't need the Graph Editor at all.

Requires Blender 4.4 or newer (tested on 5.2).

## Install

Edit > Preferences > Get Extensions > ⌄ (top right) > **Install from Disk…** → choose
`editable_motion_paths-2.0.0.zip`.

## Quick start

1. In Pose Mode, select one or more bones (or objects in Object Mode).
2. Open Sidebar (N) > **Motion Paths** > **+**.
3. Press **Edit Paths**, pick the *Edit Motion Path* tool in the toolbar, or press **Ctrl+T**
   (Pose Mode).
4. Click a key on the path to show its handles, then drag the key or a handle, or use
   G / R / S like on a mesh.

Only the paths of *selected* bones/objects can be edited; other paths are drawn gray, and
clicking a gray path selects its bone. If nothing on a path is selected, the hotkeys fall back
to Blender's normal bone / object behaviour.

## Modes: 1 Location, 2 Rotation, 3 Scale, 4 Value

| Mode | What is drawn | What editing changes |
|---|---|---|
| **1 Location** | Trajectory of the bone head / object origin | Location keys |
| **2 Rotation** | Trajectory of the bone **tip** | Rotation keys (Euler, Quaternion, Axis Angle). Dragging a point turns the bone so the tip lands under the mouse. **Ctrl+T** twists the bone around its own axis |
| **3 Scale** | Two crossed **ribbons** along the bone's own X and Z axes, as wide as the X and Z scale; their color shows the Y scale (blue below 1, red above). A box at the current frame shows the current scale | Scale keys: a selected key shows a **scale gizmo** (drag the X / Y / Z handle, or the white one for all), or **S** / **Alt+S** (Shrink / Fatten) with X / Y / Z |
| **4 Value** | A **track** for one animated value (IK/FK switch, custom property, constraint influence…), *above the path* or *beside the path* (follows its shape, so it works however the path turns) | That value: move its keys away from / toward the path. Pick the value in the sidebar or tool header |

The mode follows the bone you pick: a bone with only rotation keys (FK) switches to Rotation,
one with only location keys to Location. Paths that can't be edited in the current mode (a
connected bone in Location, locked channels) are gray, and the hint at the bottom says why.

## Blender's own settings

The tool uses the settings you know from modeling, in the header and with the usual keys:

* **Proportional editing**: **O**, falloff **Shift+O**, connected **Alt+O** (Blender's own
  toggles). Paths keep their own radius (mouse wheel during G/R/S). Connected measures along
  the path; Projected measures on the screen.
* **Pivot point** (**.** pie): Median, Bounding Box, 3D Cursor, Active key. *Individual
  Origins* turns or scales the handles of every selected key around the key itself.
* **Transform orientation** (**,** pie): Global, Local (the channel axes), View, Cursor,
  Parent, Custom, and **Normal = Path**: Y along the motion, X sideways, Z up. Press X / Y / Z
  once for the header orientation, twice for Local (or Global).
* **Snapping** (**Shift+Tab**, magnet icon, **Ctrl** while moving): Increment, Vertex (also
  the keys of other paths), Edge, Face (put a foot key onto the floor). Snap Target and Snap to
  Self work as usual.
* **3D cursor**: **Shift+S** pie with Cursor to Selected / Active and Selection to Cursor /
  Active / Grid.

## Controls (Edit Motion Path tool)

**Select**

| Input | Action |
|---|---|
| Click / Shift+Click | select / toggle a key or handle (inside a key's dot the key always wins) |
| Ctrl+Click on a key | select every key up to it (the shorter way around a loop) |
| Drag on empty space, **B**, **C** | box select, circle select |
| **A / Alt+A / Ctrl+I** | all / none / invert |
| **Ctrl+L / L / Shift+L** | whole path / path under the mouse / deselect it |
| **Ctrl+NumPad + / -** | grow / shrink the selection along the path |
| **Shift+G** | select similar: handle type, interpolation, keyframe type, partial keys, holds |
| **[ / ]** | keys before / after the current frame |
| **H / Shift+H / Alt+H** | hide the selected keys / the others / reveal. Hidden keys take the path around them along: the path falls apart into pieces, and Ctrl+L, L and proportional editing stay on the visible piece (a mask) |
| Double-click a key | jump to its frame |

**Transform**

| Input | Action |
|---|---|
| **G / R / S** | move / rotate / scale. X Y Z: axis, Shift+X/Y/Z: plane, type numbers, Shift: precise |
| **R R** | trackball |
| **Ctrl+T** | twist the bone (rotation paths) |
| **G G**, **Shift+V** | slide in time: move the mouse right for later, left for earlier |
| **R / S** on one key | turn / stretch its handles (speed through the key) |
| **Ctrl+M** | mirror (then X / Y / Z) |
| **Shift+Ctrl+Alt+S** | shear |

**Keys (topology)**

| Input | Action |
|---|---|
| Ctrl+Click on the path | insert a key; the motion doesn't change |
| **E** | extrude: a new key that continues the motion, with the handle type of the key it grows from; the mouse places it, the wheel sets how many frames later |
| **Shift+D** | duplicate: copies go halfway to the next key and follow the mouse (frames in the redo panel). *Duplicate in Time* (menu) places copies with left / right instead |
| **Ctrl+C / Ctrl+V** | copy the pose of the active key / paste it on the current frame |
| **Ctrl+R** | loop cut: evenly spaced keys in the segment under the mouse (wheel: number of cuts) |
| **K** | knife: draw a line, keys are inserted where it crosses the path |
| **Ctrl+B** | bevel: round a key into a Bezier arc (A: width, S or wheel: segments, P: profile, 0.5 = circle) |
| **M** | merge at the last selected (active, white ring) key, the first, the center or the 3D cursor |
| **X**, **Delete**, **Ctrl+X** | delete menu / delete / dissolve |
| **V**, **T** | handle type / interpolation |
| **Alt+C** | loop menu: toggle Cycles, fix the loop seam |

**Shape and timing**

| Input | Action |
|---|---|
| **Alt+S** | smooth (T: Shape / Time; in Scale mode: shrink / fatten) |
| **Alt+E** | exaggerate |
| **Shift+E** | ease (I / O / B while dragging: slow in, slow out, both) |
| Right-click | path menu: **Edit Value…** (type exact values for the active key), Subdivide, Spin, Space, LoopTools, Snap, Curves, … |

Nothing is written until you actually move: starting a tool and confirming without moving
leaves the curves exactly as they were.

## Predictable topology

* **Insert** (Ctrl+Click, Loop Cut, Knife, Subdivide) never changes the motion. New keys get
  Auto Clamped handles when that keeps the path within 0.5% of its size; otherwise the curve
  is split exactly and the new key gets Aligned handles.
* **Delete** removes keys; the other keys keep their handle types.
* **Dissolve** removes keys and refits only the handle *lengths* of the two keys around them,
  keeping their direction, so nothing outside those two keys moves. If plain Delete would be
  as close, it does that instead, so Dissolve is never worse than Delete (in tests usually
  5–10× closer to the original).
* Moving rotation keys moves their handles with them on every channel, exactly like in the
  Graph Editor.

Blender 5's default F-Curve smoothing, *Continuous Acceleration*, re-solves all auto handles
of a curve together. So with it, editing one key also nudges the auto handles a few keys away.
That is Blender's own behaviour; set the smoothing to *None* if you want edits to stay local.

## Timing and speed

* **Slide (G G)**: *Timing* (default) keeps the poses and moves keys in time; the frame dots
  spread out or bunch up. *Spacing* (T) keeps the frames and slides the poses along the path.
  **C** continues the motion past the first / last key. With *Whole Pose*, the bone's other
  keys on those frames move too.
* **Space** (Timing & Speed panel, right-click menu): changes the spacing between the selected
  keys (or of the whole path) while the path keeps its shape. *Even* gives constant speed, and
  *Slow In*, *Slow Out* and *Slow In & Out* give clean eases (wheel: strength). Drag right to
  blend in. Keys are added only where the old ones can't follow; the report shows how close
  the result is.
* **Ease (Shift+E)**: slow in / slow out at the selected keys, by changing the handle slope and
  its length in time.

## Shape tools

* **Smooth** (Shape: removes bumps, keeps arcs and timing; Time: evens out the keys) and
  **Exaggerate** (more or less bend relative to the neighbors or the center). Drag left /
  right; negative values sharpen or tone down.
* **Bevel**, **Spin** (continue the motion on an arc around the 3D cursor: orbits, swings).
* **LoopTools**: *Circle* (a perfect arc), *Flatten* (one plane), *Straighten* (a line,
  keeping the spacing), *Follow Stroke* (draw the motion with the Annotate tool, D + drag, and
  the selected part of the path follows it), *Relax* and *Space*.
* **Curves**: *Path to Curve* makes a Curve object (a Bezier point per key, or a point per
  frame). *Follow Curve* makes the path follow any curve object: keep the timing, even speed,
  or one key per curve point. That way you can export, edit as a curve and bring it back.

## Clean Up and loops

* **Clean Up** (Keys panel, or X > Limited Dissolve) reduces keys while keeping the path in 3D
  within a tolerance. It refits handles, keeps corners and holds, and keeps loops intact.
  *Clean* removes keys; *Adaptive* rebuilds them with as few as possible.
* **Cycles**: no key is ever added outside a looping curve's range, and the first and last
  keys stay in sync. **Fix Loop** (Alt+C) matches the other end of the loop to the selected
  first or last key.

## Display

The color button in the tool header opens the display settings (colors, dots, ribbons, value
track) without going to the sidebar.

* **Color**: *Speed* (blue slow, red fast), *Before / After*, *Solid*, and **Jitter**: a heat
  map of roughness. Smooth motion stays gray; hitches, pops and noisy wobbles glow yellow to
  red. Select the red part and smooth it. *Threshold* sets the sensitivity.
* Baked keys (a key every frame or two) are drawn as small dots, so dense motion stays
  readable.
* Turning off **Show Overlays** hides the paths, and viewport hotkeys then work as in plain
  Blender.
* A key with a dark center is keyed on only some channels. *Pin Partial Keys* keeps such keys
  in place when a neighbor is edited.

## Range and updates

Paths are calculated over the Scene, Action or a Manual frame range (always including every
key of the path) and update in real time. When other animation such as a parent changes, they
recalculate on their own (*Auto Update*).

## Scripting and AI assistants

`import editable_motion_paths_api as emp` gives a Python API: add paths, read and `analyze()`
trajectories, `jitter()`, move keys in world space, smooth, exaggerate, ease, slide, `space`,
`subdivide`, `dissolve_keys`, `bevel`, `extrude`, `loop_tool`, `path_to_curve`,
`follow_curve`, Clean Up and more. `preview_objects()` builds the paths as real geometry so
they show up in screenshots. See [AI_GUIDE.md](AI_GUIDE.md); you can give it to Claude in a
chat connected to Blender through MCP.
