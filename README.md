# Editable Motion Paths

Edit motion paths of bones and objects right in the 3D Viewport. You get points on the path,
handles, and the usual Blender hotkeys. Every edit goes straight into the F-Curves, so undo,
the Graph Editor and the Dope Sheet stay in sync.

Requires Blender 4.4 or newer (tested on 5.2).

## Install

Edit > Preferences > Get Extensions > ⌄ (top right) > **Install from Disk…** → choose
`editable_motion_paths-1.6.2.zip`.

## Quick start

1. In Pose Mode, select one or more bones (or objects in Object Mode).
2. Open Sidebar (N) > **Motion Paths** > **Add**.
3. Press **Edit Paths**, or pick the *Edit Motion Path* tool in the toolbar.
4. Choose what you edit with **1 Location / 2 Rotation**. The switch is also at the top of the
   panel and in the tool header.
5. Click a key on the path to show its handles, then drag the key or a handle.

Only the paths of *selected* bones/objects can be edited. Other paths are drawn gray. Clicking
a gray path selects its bone.

## Location and Rotation

A path belongs to a bone. The edit mode decides which of its trajectories is shown and edited,
so the two are never mixed. The mode follows the bone you pick: when the selected bone or
object only has location keys, it switches to Location, and when it only has rotation keys
(FK), it switches to Rotation. A message at the bottom of the viewport says so. A path that can't be edited in the current mode (a connected
bone in Location, locked channels) is drawn gray, and the hint at the bottom tells you what to
press.

| Mode | What is drawn | What editing changes |
|---|---|---|
| **Location** | Trajectory of the bone head / object origin | Location keys. Handles are the F-Curve bezier handles shown in 3D |
| **Rotation** | Trajectory of the bone **tip** | Rotation keys (Euler, Quaternion, Axis Angle). Dragging a point turns the bone so the tip lands under the mouse. **R** rotates handles like on location paths, **R R** twists the bone around its own axis |

*Rotation Path Length* (Display) makes small rotations easier to see.

## Slide keys along the path (G G)

Select keys and press **G** twice, or use *Slide Along Path* in the panel or the right-click
menu. Then move the mouse along the path. A blue marker follows the path and shows where the
key goes and its new frame. It follows the curve without jumping across loops. Past the first
and last key, the path continues as a dashed line, so a key can go beyond the end (for
example from frame 60 to 65). Press **T** to switch between the two modes:

* **Spacing** (the default): the key keeps its frame and slides along the trajectory under
  the mouse, so the bone gets there earlier or later in space. The shape of the path stays the
  same.
* **Timing** (like the timeline): the key keeps its pose and moves to a different frame. The
  frame dots around it spread out or bunch up. With *Whole Pose* on (the default), the bone's
  other keys on that frame (rotation, scale, custom properties) move too, like the Dope Sheet
  summary row. Only the keys you see on the path limit the slide; the other channels stop at
  their own neighbors.

* **C**, Continue: like G G C in mesh editing. The pose follows the trajectory and keeps
  going past the first or last key. The motion is extrapolated with the direction and speed
  of the last segment, so the animation continues.

Nothing is written until you actually move. Pressing G G and confirming leaves the curves
exactly as they were. The same goes for G, R, S, Smooth, Exaggerate and Ease.

Keys snap to whole frames. Hold **Ctrl** for subframes, or type an offset such as `3` or `-2`.
A key can't pass the neighboring keys, and the result can be adjusted in the *Adjust Last
Operation* panel.

## Proportional Editing

Press **O**, or use the toggle in the panel or tool header. Moving, rotating or scaling keys
then also moves the unselected keys nearby, with a smooth falloff. This works like sculpting
the path.

* **Mouse wheel** or **Page Up/Down** during G/R/S changes the radius (shown as a circle).
* **Shift+O** chooses the falloff: Smooth, Sphere, Root, Inverse Square, Sharp, Linear,
  Constant or Random.
* **Connected** (chain icon, on by default) measures the distance *along the path*, so only
  keys of the same path are affected. Turn it off to use straight 3D distance, which affects
  all editable paths.
* Affected keys turn orange according to their influence. The settings also appear in the
  *Adjust Last Operation* panel.

## Controls (Edit Motion Path tool)

| Input | Action |
|---|---|
| **1 / 2** | edit Location / Rotation |
| Click key | select it and show its handles |
| Drag key / handle | move it |
| Shift+Click | add to / toggle the selection |
| Ctrl+Click on a key | select every key between the active key and this one (like Pick Shortest Path). On a looping path it goes the shorter way along the curve, across the seam if needed |
| **Ctrl+X** | dissolve: one selected key is removed and its neighbors refitted so the shape stays; with several keys selected, Clean Up that range |
| Ctrl+Click on the path | insert a key at that frame (the motion is preserved) |
| Double-click key | jump to its frame |
| Drag on empty space, **B** | box select (Shift extend, Ctrl subtract) |
| **G G** | slide keys along the path (**T**: Timing / Spacing, **C**: continue the motion) |
| **G / R / S** | move / rotate / scale; **X Y Z** constrains to an axis (press twice for local, i.e. the channel axes), **Shift+X/Y/Z** constrains to a plane, type numbers, **Ctrl** snaps, **Shift** is precise |
| **R** on one key | rotate its handles around the key (flat Auto Clamped handles open up) |
| **S** on one key | scale its handles (speed through the key) |
| **R R** on rotation-path keys | twist the bone around its own axis |
| **E** | ease: slow in / slow out (**I / O / B** while dragging: in, out, both) |
| **O** / **Shift+O** | proportional editing on/off / falloff (wheel changes radius during G/R/S) |
| **V** | handle type (Auto Clamped, Auto, Aligned, Free, Vector) |
| **L** on the first / last key | fix the loop seam (match the other end to this key) |
| **Alt+S** / **Alt+E** | Smooth / Exaggerate |
| **X / Delete** | delete keys |
| **A / Alt+A / Ctrl+I** | select all / none / invert |
| Right-click | path menu |

If nothing on a path is selected, **G/R/S/A/X** fall back to Blender's normal bone/object
behaviour.

## Ease (E): slow in / slow out

Bezier keys control easing with two things: the handle's slope and its length *in time*. A
handle you drag in 3D only changes the slope, so on its own it gives just a soft ease. **Ease**
changes both, so the motion really comes to rest at the key.

* Select keys, press **E** (or the Ease button) and drag right. The frame dots bunch up at the
  key, and at 1.0 the bone comes to rest there.
* Drag left (negative values) to move through the key at a more constant speed.
* While dragging, **I** eases only the arrival, **O** only the departure, and **B** both.

## Smooth and Exaggerate

These work on the selected keys, or on all keys if none are selected. Drag the mouse left and
right to set the amount, then click. You can fine-tune the result in the *Adjust Last Operation*
panel at the bottom left.

* **Smooth**: positive values smooth, and higher values smooth more. Negative values sharpen
  (anti-smooth).
  * **Method: Shape** (the default) smooths the form. Each key is pulled toward where a smooth
    curve through its neighbors would pass, so bumps and corners go away and arcs keep their
    size. Keys only move across the path, never along it, so the timing and spacing stay the
    same. *Fix Corners* also aligns broken handles.
  * **Method: Time** averages the keys in time. It evens out the spacing but flattens arcs.
* **Exaggerate**: scales how far the path bends away from a baseline, so 0.5 means 50% bigger.
  Negative values tone it down, and -1 flattens it.
  * **Relative To: Neighbors** uses the straight line between the keys around the selection.
  * **Relative To: Center** uses the average of the selection, which makes a whole motion
    bigger or smaller.

## Display

* **Color: Speed** shows slow parts in blue and fast parts in red. The dots show spacing
  (one per frame).
* The green dot is the current frame.
* Turning off **Show Overlays** hides the paths too, so you can watch the animation clean.
  While they're hidden, viewport hotkeys work as in plain Blender. Panel buttons still work.
* While you drag, a ghost of the original path is shown.
* A key with a dark center is keyed on only some of its channels. With *Pin Partial Keys* on
  (the default), such keys near an edited key get keyed on their other channels too, so they
  stay where they are instead of following the edit.
* Handles shorter than about 36 px are drawn longer so you can still grab them. Long handles
  are drawn at their true length.

## Looping animation (Cycles modifier)

Curves with a Cycles modifier keep their loop:

* No key is ever added outside a looping curve's key range, because that would change its
  period. A key the path needs at frame 0 of a 1–97 loop goes to frame 96, which is the same
  pose.
* Editing the first key of a loop also moves the last one by the same amount, so the seam
  stays closed. *Repeat with Offset* is supported.

### Fix Loop (L)

When a loop has a jump or a hitch at the seam, select the first or the last key of the path,
whichever is right, and press **L** (or use *Fix Loop* in the panel or menu). The key at the
other end of the loop is matched to it: same pose and same handles, so the seam has neither a
jump nor a speed change.

* With *Whole Pose* on (the default), every channel of the bone is fixed: location, rotation,
  scale and custom properties.
* On *Repeat with Offset* curves (moving loops, such as a walk that goes forward), only the
  handles are matched. The offset is kept.

## Clean Up

Reduces the number of keys while keeping the motion. Use *Clean Up* in the panel or the
right-click menu. It works on the selected range (2 or more keys selected) or on the whole
path. Unlike cleaning in the Graph Editor, it:

* measures the error **in 3D**, as how far the path (the bone head, or the tip on rotation
  paths) moves away from the original. *Tolerance* is a percentage of the path size;
* **refits the handles** of the keys that remain (least squares over all frames), so the same
  shape needs fewer keys;
* keeps **corners** (Vector or broken handles, linear segments, impacts) and, with *Keep
  Holds*, the places where the motion stops (contacts, held poses) exactly as they were;
* keeps loops intact (Cycles period and seam).

There are two methods:

* **Clean** removes the keys that aren't needed.
* **Adaptive** rebuilds the keys from scratch. It adds keys where the shape needs them most,
  then removes the extra ones. This gives the fewest keys, and they don't have to sit on the
  old frames.

For example, a baked motion with a key on every frame (60 keys) goes down to 8–10 keys at 1%
tolerance. Adjust *Method* and *Tolerance* in the *Adjust Last Operation* panel. The report
shows the key count before and after and the largest deviation.

## Range and updates

The path is calculated over the Scene, Action or a Manual frame range. Scene and Action always
include every key of the path, so a key slid past the end stays visible. You can also draw only
part of it around the current frame. Paths update in real time while you edit them. If you
change other animation, such as a parent bone, the paths recalculate on their own (*Auto
Update*). The refresh button forces a recalculation.

## Scripting and AI assistants

`import editable_motion_paths_api as emp` gives a Python API. You can add paths, read
trajectories, and run `analyze()`, a readable summary of holds, pops, sharp turns and eases.
You can also move keys in world space, smooth, exaggerate and slide. `preview_objects()`
builds the paths as real geometry so they show up in any screenshot. See
[AI_GUIDE.md](AI_GUIDE.md); you can give it to Claude in a chat connected to Blender through
MCP.
