# Editable Motion Paths

Model your animation like geometry. Motion paths of bones and objects become editable curves
in the 3D Viewport, with keys as points, Bezier handles and Blender's own modeling tools and
hotkeys. Every edit goes straight into the F-Curves, so undo, the Graph Editor and the Dope
Sheet stay in sync. You shouldn't need the Graph Editor at all.

Requires Blender 4.4 or newer (tested on 5.2).

## Install

Edit > Preferences > Get Extensions > ⌄ (top right) > **Install from Disk…** → choose
`editable_motion_paths-2.0.0.zip`.

To remove it: Edit > Preferences > **Get Extensions**, find *Editable Motion Paths* among the
installed ones, and in the ⌄ menu at the right of its row choose **Uninstall** (Blender shows no
Remove button for extensions in the Add-ons tab).

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
| **2 Rotation** | Trajectory of the bone **tip** | Rotation keys (Euler, Quaternion, Axis Angle). Dragging a point turns the bone so the tip lands under the mouse. A selected key (with several selected: the active one) shows the bone's **X, Y and Z rings**, like Blender's rotate gizmo: drag a ring to turn around that axis only (it follows the mouse steadily even when the ring is seen edge-on); the green Y ring around the bone twists it around its own axis (like Ctrl+T). Dragging the key itself turns the tip freely, like a **trackball**. The faint circle shows how far the tip can reach; drag it to turn around the view axis (like R). A purple **ribbon** along the path, turned like the bone's X axis, shows the twist (only shown: the rings edit it; Display: width and side). **Ctrl+T** twists the bone around its own axis |
| **3 Scale** | Two crossed **ribbons** along the bone's own X and Z axes, as wide as the X and Z scale; their color shows the Y scale (blue below 1, red above) - or, in the preferences (*Scale Y: Ribbon*), a third green ribbon beside them (*outside* or *inside* the path's curves, like the Value track; on a circle always outside) that grows from a fixed inner edge like a graph, as wide as the Y scale, with one handle on its outer edge. A box at the current frame shows the current scale, its values written next to it (Display: *Box Values*; *Values at Keys* writes them at the keys) | Scale keys: a selected key shows a **scale gizmo** whose X and Z handles sit on the ribbon edges (drag an edge to make that ribbon wider; Y along the bone; the white one scales all). **Click** a gizmo handle to show that channel's handles as **whiskers** along the ribbon (time along the path, value across it) and pick both of them; drag a whisker to edit it. **V** and **T** (handle type, interpolation) work on the selected keys of that channel only, like on one curve in the Graph Editor. Clicking a whisker picks that handle (Shift: add another; undo brings the picking back). **Alt+S** makes the picked handles longer or shorter, **Alt+Shift+S** moves them up or down (both handles of a smooth key tilt together; one handle: the other one follows in line); with none picked, the channel's handles of the selected keys; I / O / B: before the key, after it, both. So the handles can be shaped from the keyboard alone. **S** scales all axes (X / Y / Z: one) |
| **4 Value** | A **track** for one animated value (IK/FK switch, custom property, constraint influence…), *above the path* or *beside the path* (follows its shape in the plane of the motion, *outside* or *inside* its curves, and stays put when the view turns; on a circle a bigger value is always farther out) | That value: move its keys away from / toward the path. Pick the value in the sidebar or tool header |

Scale and Value paths of something that stays in place are drawn around a **circle** like a
clock face, lying flat around the world Z axis (it stays put when the view turns): the frame
range goes once around it and the current frame runs around (Display > Layout: Auto / Along the
Path / Circle, with radius and frame range).

In the path list, the arrow button next to a path isolates its curves of the current mode in
the **Graph Editor** (the bone's other curves are hidden); it stays pressed while they are
isolated, and a second click shows them all again.

The mode follows the bone you pick: a bone with only rotation keys (FK) switches to Rotation,
one with only location keys to Location. Paths that can't be edited in the current mode (a
connected bone in Location, locked channels) are gray, and the hint at the bottom says why;
in the path list such a path has a lock icon whose tooltip says the same.

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
| **H / Shift+H / Alt+H** | hide the selected keys / the others / reveal. Hidden keys take the path around them along (a faint dashed ghost still shows it): the path falls apart into pieces, and Ctrl+L, L and proportional editing stay on the visible piece (a mask) |
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
| **E** | extrude from the first or last key: a new key that continues the motion, with the handle type of the key it grows from; the mouse places it, the wheel sets how many frames later (inside the path, use Shift+D) |
| **Shift+D** | duplicate: copies go halfway to the next key and follow the mouse; the **wheel** moves them through the motion a frame at a time (jumping over other keys). *Duplicate in Time* (menu) places copies with left / right instead |
| **Ctrl+C / Ctrl+V** | copy the pose of the active key / paste it on the current frame: the channels of the current mode (*Whole Pose* in the redo panel: all of them) |
| **Ctrl+R** | loop cut: evenly spaced keys in the segment under the mouse (wheel: number of cuts) |
| **Ctrl+K** | knife, like the mesh Knife: keys are inserted where the line crosses the path. LMB: point (drag: draw), RMB: fixes this cut (its keys stay where they are when you turn the view) and starts a new one; nothing is cut until Enter, and Esc cancels all the cuts, like the mesh Knife, Shift: snap to the middle of a segment, Ctrl: no snapping (it snaps to keys and frame dots), A: angle constraint (screen / relative), X / Y / Z: along an axis, S: frame numbers, Ctrl+Z: undo a point, Enter / Space / double-click: cut; the view can be turned while cutting |
| **Ctrl+B** | bevel: round a key into a Bezier arc. It goes through the keys around it (the path outside the arc keeps its shape and timing); when one side reaches the end of the path, the other goes on and the arc gets longer on that side. C: **Clamp** (stop at the next keys), A: width, S or wheel: segments, P: profile, 0.5 = circle |
| **M** | merge at the last selected (active, white ring) key, the first, the center or the 3D cursor |
| **X**, **Delete**, **Ctrl+X** | delete menu / delete / dissolve |
| **V**, **T** | handle type / interpolation |
| **Alt+C** | loop menu: toggle Cycles (whole pose, or only the current mode), fix the loop seam |

**Shape and timing**

| Input | Action |
|---|---|
| **Alt+S** | smooth (T: Shape / Time; Shape also turns the handles along the path, *Smooth Handles*; in Scale mode: the handles of the picked channel) |
| **Alt+E** | exaggerate |
| **Shift+E** | ease (I / O / B while dragging: slow in, slow out, both; drag left for the opposite: faster through the key) |
| **Shift+T** | timing menu (see Timing and speed) |
| Drag a **frame dot** | move that frame along the path: the frames around it follow, the keys and the shape stay (click a dot: go to that frame) |
| **U** | display settings under the mouse (right click a setting > *Reset to Default (Motion Paths)*: Blender's own Reset doesn't reach panels opened with a key) |
| **Ctrl+H** / **Ctrl+Shift+H** | hide / show the handles / the frame dots (timing) |
| **P** | easing gallery under the mouse: click a picture to apply it |
| **J** | modifiers of the active path under the mouse (the mouse wheel scrolls a long list) |
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
That is Blender's own behaviour. To keep edits local, turn it off:

1. For new animation: *Edit > Preferences > Animation > F-Curves > Default Smoothing*: **None**.
2. For curves that already exist (it is a setting of each curve): the **Smoothing: None**
   button at the bottom of the sidebar (*Motion Paths > Curve Smoothing*) sets it on every curve
   of the selected bones / objects; it says how many of them still use Continuous Acceleration
   (grayed out when none do), and whether new curves will get it. Pressing it re-solves the auto
   handles once the plain way, so the path may shift a little at that moment. Dragging a key or
   a handle on a curve that still uses it shows a note at the bottom. (Measured: with it on,
   even putting a handle back where it is moves the path by ~3%, because the whole curve
   re-solves; with it off, nothing moves.) By hand: Graph Editor sidebar > *F-Curve* tab > *Auto Handle Smoothing*,
   one curve at a time.

## Timing and speed

* **Frame dots** are handles for the spacing: drag one along the path. The **timing chart** in
  the bottom right corner of the viewport (a dark block like the speed graph's; with both on,
  one block) shows where every frame lies between the selected
  keys (one key: the segments around it; none: the keys around the current frame), like an
  animator's chart; drag its ticks the same way. An optional **speed graph** sits
  right above it, as wide as the chart: for the selected keys, the segments on both sides of a
  single selected key, or the keys around the current frame (*Segment*), a set frame range
  (*Range*) or the scene's frame range (*Timeline*). Keys are marked on it with their frame
  numbers (the selected ones orange), the current frame in green. The speed graph can be
  dragged: on empty space the **current frame** follows the mouse; a **key's line** (it lights up
  under the mouse) moves that key in **time** (the whole pose; Ctrl: subframes); a **key's dot**
  (it grows under the mouse), dragged up or down, makes the motion faster or slower at that key
  (both segments around it make up for it); the **curve**:
  the part of it you grab goes up (faster there) or down (slower), the rest of the segment makes
  up for it - grab it on the left to change the start of the segment, on the right for its end.
  The path keeps its shape and the keys their frames (keys are added where needed, like *Ease*;
  they are selected - *Select New Keys* in the preferences); after a drag the keys at both ends
  of the graph are selected too (one key selected: its neighbors - *Keep the Graph's Keys*), so
  the graph doesn't jump to other keys. While you drag, the graph keeps its frames and height.
* Where the chart and the graph are and how they look - the **corner**, margins, width, heights,
  text size, background, curve and key colors - is in the add-on's preferences (*Timing Chart and
  Speed Graph*); the 3D view shows each change right away, and *Reset the Charts' Look* puts it
  back. In the top corners they leave room for the navigation gizmo and the view's name.
* **Timing menu (Shift+T**, also in the right-click menu and the Timing & Speed panel), all
  dragged left / right:
  * *Breakdown*: blend the selected keys between the poses of their neighbors, favoring one
    side (like Tween Machine). Slide (below) moves keys in time or along the path instead;
  * *Scale Time*: stretch or squash the timing of the selection (R: ripple);
  * *Add / Remove Frames* after the active key, moving everything after it;
  * *Moving Hold*: a hold that still drifts a little (wheel: how many frames, mouse: how far it
    drifts);
  * *Offset Cascade*: keys selected on several bones / objects; each one starts a few frames
    after the one before (overlap, follow-through);
  * *Snap to Frames*.

* **Slide (G G)**: *Timing* (default) keeps the poses and moves keys in time; the frame dots
  spread out or bunch up. *Spacing* (T) keeps the frames and slides the poses along the path,
  and the path keeps its shape.
  **C** (*Follow*): the key keeps its handles and follows the path itself. In *Timing* it moves
  in time and onto the path where the motion was at the new frame; in *Spacing* it slides along
  the path without retiming the frames around it. Past the first / last key the motion goes
  on. With *Whole Pose*, the bone's other keys on those frames move too.
* **Space** (Timing & Speed panel, right-click menu): changes the spacing between the selected
  keys (or of the whole path) while the path keeps its shape. *Even* gives constant speed, and
  *Slow In* (start slowly), *Slow Out* (end slowly) and *Slow In & Out* give clean eases
  (wheel: strength), like Blender's easing. Drag right to
  blend in. Keys are added only where the old ones can't follow; the report shows how close
  the result is.
* **Ease (Shift+E)**: slow in / slow out at the selected keys. *Keep the Path* (default) only
  changes the timing: the frames bunch up at the key along the same path (keys are added where
  needed). *Handles* (T while dragging) flattens the key's handles and makes them longer in
  time, the old way. *In* slows the bone down into the key, *Out* lets it leave the key slowly.
  Dragged the other way (negative) it does the opposite: with *Keep the Path* the bone moves
  faster through the key (the frames spread out there and gather in between); with *Handles*
  the key's handles get longer in the same direction, so the path around the key gets wider.
* **Easing Presets** (**P**, the Timing menu, or the Timing & Speed panel): a library of easing curves
  with pictures - **Linear** (one button: it's the same every way), Sine, Quadratic, Cubic,
  Quartic, Quintic, Exponential, Circular, Back, Bounce, Elastic, each In, Out or In & Out -
  for the selected part of the path (the selected
  keys; one key: the segments around it). *Keep the Path* retimes along the path (Back and
  Elastic overshoot along the direction of the motion); *Channels* sets Blender's easing
  interpolation on the channels you pick (drag across them to switch several; one always stays
  on), like in the Graph Editor. *Bounce* has *Bounces* and *Bounciness*, *Elastic* its
  amplitude and period. **P** opens them as a gallery under the mouse: big pictures to click,
  with the mode, direction and strength (wheel) above them; after a click the redo panel has
  all the settings.

## Shape tools

* **Smooth** (Shape: removes bumps, keeps arcs and timing; Time: evens out the keys) and
  **Exaggerate** (more or less bend relative to the neighbors or the center). Drag left /
  right; negative values sharpen or tone down.
* **Bevel**, **Spin** (continue the motion on an arc around the 3D cursor: orbits, swings).
* **LoopTools**: *Circle* (a perfect arc; *Angle* turns it around the line between its ends,
  *Invert* bends it the other way, *Rotate* slides the keys along the circle so the arc swings
  to and fro, *Radius*, *Regular* spacing), *Arc* (one smooth arc that
  follows the shape and flows on into the path around it without kinks; *Bulge*, *Smooth
  Ends*), *Flatten* (one plane: best fit or facing the view), *Straighten* (a line),
  *Follow Stroke* (draw the motion with the Annotate tool, D + drag, and the selected part of the
  path follows it) and *Relax*. They keep the timing: the new shape is travelled with the old
  spacing (*Keep Timing* in the redo panel).
* **Curves**: *Path to Curve* makes a Curve object (a Bezier point per key, or a point per
  frame). *Follow Curve* makes the path follow any curve object: keep the timing, even speed,
  or one key per curve point. That way you can export, edit as a curve and bring it back.

## Modifiers

The **Modifiers** panel (sidebar, the wrench in the tool header, or **J** under the mouse) is an F-Curve modifier stack
for the active path in the current mode, like the modifier stack in modeling: add Noise, Cycles,
Built-In Function, Generator, Envelope, Limits or Stepped; the X / Y / Z buttons (W X Y Z on
quaternions, the components of a value) choose which channels it works on: click one to use only
that channel, Shift+Click to add or remove it, and the dot next to them puts it on all of them (a
ring: some are left out, a filled dot: it is on all already). Its settings stay the same on all of
them. Its header works like a modeling modifier's: collapse arrow, monitor
(show it in the viewport), a drop-down with Apply (bake into keys and clean up), Apply to Selected
(baked only into the selected parts of the path, with smooth seams; the modifier goes), Duplicate,
Copy, Paste and
Move Up / Down / to First / to Last, and X to remove. The Modifier Tools menu next to Add
Modifier applies, removes, hides or expands them all. Alt+C's Cycles shows up here too. The
path follows every change of a modifier's settings right away.

## Clean Up and loops

* **Clean Up** (Keys panel, or X > Limited Dissolve) reduces keys while keeping the motion:
  with **Keep Timing** (on by default, T while dragging) where the bone is on every frame stays
  within a tolerance; off, only the shape of the path counts - keys go as long as the path keeps
  its shape, and the timing along it may change (fewer keys, a cleaner path). Drag left / right
  to set how close it stays and watch the result live (the original path stays as a ghost); M
  switches Clean / Adaptive, H keeps holds, P keeps poses.
  It refits handles and keeps corners, the two ends of every hold, the poses (where the motion
  stops for a moment or turns back), and loops intact. *Clean* removes keys; *Adaptive*
  rebuilds them with as few as possible.
* **Cycles**: no key is ever added outside a looping curve's range, and the first and last
  keys stay in sync. The part of the path outside the loop's keys is drawn faintly (Display >
  Loop Repeats). **Fix Loop** (Alt+C) matches the other end of the loop to the selected
  first or last key.

## Sculpt Motion Path

A second tool in the toolbar (under Edit Motion Path) paints on the paths with brushes, like
sculpt mode. Pick a brush in the strip of pictures next to the toolbar (the name shows under
the mouse), in the tool header, or with a **right click** (brushes, radius, strength, falloff).
Its header also has the onion skin, center of mass, display and modifier buttons of the edit tool:

* timing brushes - the shape of the path stays, only where the bone is on each frame changes:
  **Push** (drag the frames along the path), **Slow Down** (the frames gather under the brush),
  **Speed Up** (they spread out), **Even** (even spacing). While you paint, the new frame dots
  are shown; when you let go, the keys follow;
* shape brushes: **Grab** (pull the path with the mouse), **Smooth** (the keys, and their handles
  turn along the path), **Pinch** (draw the path toward the brush). With **Insert Keys** (on by default) a part with too few keys under the
  brush gets keys there first (the motion doesn't change), so the brush can shape it;
* **Dissolve**: paint over the keys the path doesn't need and they go - one by one, the nearest
  to the brush first, each only if the motion stays within *Tolerance* (percent of the path
  size). *Keep Timing* (on): where the bone is on every frame must stay; off: only the shape of
  the path counts, so more keys can go. The old path shows as a ghost while you paint, and one
  note at the end says how many keys went and how far the path moved. The ends of the path stay.

**F**: radius, **Shift+F**: strength (shown like Blender's radial control), **Ctrl**: the
opposite effect (Slow Down becomes Speed Up, Pinch pushes away), **Shift**: Smooth,
**Ctrl+Shift**: Even, **Alt**: Dissolve, whatever the brush. Falloff as in sculpt mode. The brush settings are
not part of undo (Ctrl+Z undoes the strokes only). The edit tool's keys work here too: 1-4
modes, U, J, P, Shift+T, Alt+C, Ctrl+H.

**Selected keys are a mask**, like masking in sculpt mode: with keys selected (in the edit
tool), the brushes change only the part of the path between the first and the last selected
key (one selected key: the segments around it); other paths stay as they are. **Alt+A**
clears the selection, so the whole path can be painted again (A selects all).

## Onion skinning

The onion button in the tool header (its ⌄ arrow: the settings) shows ghosts of the edited bones
/ objects - or, with no path shown, of the selection - on the frames around the current one: on
the keys (with a key on every frame, at least 4 frames apart; looping curves - a Cycles modifier
- go on into the next and the previous loop), or every few frames (before /
after, step, opacity; colors in the preferences). **Update Ghosts** builds them again after
editing a mesh. Modes: *Silhouette* (the meshes in one flat color per ghost),
*Wire*, *Points* (the lightest; *Size* sets the dots and lines), *Bones* (sticks: the visible
bones, or only the selected ones). The farther a ghost is from the current frame, the fainter.
*Show*: the **Whole** character, only the part the edited bones move (**By Weight**: their
vertex weights, and of the bones that follow them - children, IK chains), or only the objects
they move (**By Object**, for characters made of separate parts: a part with no weight from
them isn't shown). Curves and texts ghost like meshes (a curve without thickness as its line),
empties as their own display type (cube, sphere, arrows...), cameras and lights like Blender
draws them (an area light with its shape), other objects as the empty type chosen in the
preferences (*Other Objects*); metaballs don't (their shape changes all the time). A ghost light
lights nothing. A child without keys of its own (on an animated empty) can be added to the
path list to get ghosts (it has no path to edit: a note at the bottom says so). Only the modes
that show something are offered: *Silhouette* for something with faces, *Points* for meshes, *Bones*
where there are bones (a light or an empty alone: *Wire* only, so there's nothing to choose).

Heavy characters stay fast: once, with a *Preparing ghosts* note at the bottom, every mesh the
armature deforms is simplified like Decimate (no holes) down to *Detail*, keeping its bone
weights. After that, a ghost frame only evaluates the bones (the real meshes are left out) and
the simplified mesh is bent by its weights; parts that don't bend (objects on a bone, props,
cameras, lights) are one shape drawn with a different matrix per ghost (instanced). So the
ghosts follow the edits live, the nearest ones first, also while a key is dragged, and the old
ghost stays until its new one is ready (nothing blinks). Back faces are skipped, *Wire* and
*Points* use the same simplified mesh (the wire stays connected), and ghosts show in X-ray
too. *In Front* (on by default) draws them over everything. After editing a mesh's shape, *Update Ghosts* builds its ghost again (a change in the
number of vertices or in the modifiers does that by itself).

The ghosts are captured ahead of time for every frame they can be on (the keys, or every step
frames, in the scene's range), the nearest first, so playback shows them all; while playing,
the missing ones are captured a frame at a time (the fps drops a little until they are all
there). The Timeline / Dope Sheet shows it: a thin bar under every ready frame, and a dot at
the ghosts shown now (filled: ready, hollow: being captured). Changes renew them: the
animation, and also an object changed on the same frame (a reset scale, a moved child).
While you drag a bone or an object with Blender's own G / R / S and *Auto Keying* is on, the
ghosts show what the neighboring frames will look like once the key is in (the pose is put on
the curves only for a moment; Esc leaves everything as it was). Without Auto Keying nothing
changes on the other frames until you key, so the ghosts stay as they are - unless
**Preview Unkeyed Pose** is on (onion settings): then the ghosts show the neighbors as if the pose
you are changing were keyed on this frame, during G / R / S and after it, until the frame
changes (a pose that isn't keyed is gone then, and so is its preview).

## Center of mass

The button next to the onion one (a ring with an arrow down; its ⌄ arrow: the settings) shows
where the character's weight is - a guide for believable poses; it changes nothing. It works
for the edited armature or the active one, with or without a path:

* the **center of mass** of the pose, live while you pose (a white dot), and a **plumb line**
  down to the floor;
* the **support** (on by default): the outline of what touches the floor; where the plumb line
  meets the floor turns green inside it (a stable pose), red outside (falling, or moving), gray
  when nothing touches the floor (in the air);
* the **velocity** (on by default): an arrow where the body is going - *Adaptive* (as far as it
  gets in about 8 frames), *Fixed* (a set length: only the direction) or *Adaptive + Length*;
* the **trajectory** (off by default): the center's path over the frames - a jump's arc should
  be a parabola. Its *Range* is like the paths': the *Scene*, the armature's *Action*, the
  *Paths* shown or *Manual* frames, and it can be drawn only *Around the Current Frame*;
* the **floor**: *Auto* (the character's lowest point over the animation), a *Height* (a
  rooftop, a table) or the *3D Cursor*'s height.

The mass of every deform bone is worked out once from its vertex weights (with a *Preparing
center of mass* note): each vertex counts its area times its distance from the bone, about the
volume around it, so a torso weighs more than a hand. **Limb Weight** (the center's settings):
select a controller in Pose Mode - an IK hand, a foot, an FK bone - and it shows how much of the
body the limb it moves is (worked out from the meshes); set another share if it's off (a heavy
boot, a prop in the hand). The limb lights up while it changes; the arrow next to it goes back to
the worked out share. With the center shown, the right-click menu has **Edit Limb Weight...** (a
small popup under the mouse). A controller that moves the whole body (the root, the torso) has
no weight of its own - pick the chest, the hips or a limb. Space switches that are off (a head
that could follow the hand, influence 0) don't make a bone part of a limb. The weight is shown
in percent, or in kilograms / pounds (the preferences: *Limb Weight In*) from the character's
*Body Mass*. A new weight works the trajectory out again from scratch. (It is kept on the
controller bone as the custom property `com_limb`; a
deform bone's `com_mass` still scales that one bone, e.g. 0 for a cape.) The velocity, the floor
level and the trajectory are worked out in the background frame by frame (only the bones are
evaluated, like the onion ghosts) and follow your edits.

## Display

**Global / Local** (the globe / local icon of each path in the path list): *Global* paths are the
motion in the world; *Local* leaves the parents' motion out - the path is drawn in the parent's space as it is
on the current frame, so it shows (and you edit) only the bone's or object's own motion, and it
moves along with the parent when the frame changes.

The color button in the tool header (or **U**) opens the display settings you change while
animating, only those of the current mode (1-4); the sidebar's *Display* panel has all of them.
The ones you set once - colors, line width, key size, ribbon widths, the rotation path length,
Scale Y as color or ribbon, In Front, Ghost While Editing, Loop Repeats, Bone Lines - are in the
add-on's preferences (*Colors, Sizes, Keys...* at the bottom of the sidebar's Display panel),
the same in every file.

* **Color**: *Speed* (blue slow, red fast), **Jitter**, *Before / After* and *Solid*. Jitter
  is a heat map of roughness. Smooth motion stays gray: a frame is rough when its
  acceleration follows neither the trend before it nor the one after it - so a key between two
  smooth Bezier segments (the acceleration only changes there) stays calm, while a pop (a key
  where the speed or the direction jumps, e.g. a Linear segment after a flat Bezier handle) and
  noise glow yellow to red. It is measured against the motion's typical acceleration, so a
  small change while a foot is planted stays calm, fast smooth motion too. Select the red part and smooth it. *Threshold* (right under the
  color, in the display settings) sets the sensitivity.
* Baked keys (a key every frame or two) are drawn as small dots, so dense motion stays
  readable.
* Turning off **Show Overlays** hides the paths, and viewport hotkeys then work as in plain
  Blender.
* A key with a dark center is keyed on only some channels. *Pin Partial Keys* keeps such keys
  in place when a neighbor is edited.

## Hotkeys and other add-ons

The tool's own hotkeys come first while the Edit Motion Path tool is active. Keys the tool
doesn't use go on to Blender and other add-ons as usual. If you rely on another add-on's hotkey
that the tool also uses, turn off **Hotkey Priority** in the add-on's preferences: then, where
another add-on uses the same key in the 3D view, its key works and the path tool's doesn't (the
mouse buttons always stay with the tool). The add-on never switches other hotkeys off itself.

Files don't depend on the add-on: everything it changes is ordinary Blender animation (keys,
handles, F-Curve modifiers), so a file opens without errors where the add-on isn't installed;
only its own settings (stored with the scene) are then unused.

## Range and updates

Paths are calculated over the Scene, Action or a Manual frame range (always including every
key of the path) and update in real time. When other animation such as a parent changes, they
recalculate on their own (*Auto Update*).

## Scripting and AI assistants

`emp = next(m for n, m in sys.modules.items() if n.endswith("editable_motion_paths.api"))`
gives a Python API: add paths, read and `analyze()`
trajectories, `jitter()`, move keys in world space, smooth, exaggerate, ease, slide, `space`,
`subdivide`, `dissolve_keys`, `bevel`, `extrude`, `loop_tool`, `path_to_curve`,
`follow_curve`, Clean Up and more. `preview_objects()` builds the paths as real geometry so
they show up in screenshots. See [AI_GUIDE.md](AI_GUIDE.md); you can give it to Claude in a
chat connected to Blender through MCP.
