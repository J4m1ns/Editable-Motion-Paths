# Editable Motion Paths: guide for AI assistants (Claude via Blender MCP)

Paste this into a chat (or project instructions) where Claude controls Blender through the
Blender MCP add-on. The **Editable Motion Paths** extension must be installed and enabled.

## Why this helps

A screenshot of a single frame doesn't show *motion*. The add-on turns motion into things
you can read and see:

* the **trajectory** of a bone or object as a curve,
* **spacing**: a dot per frame, colored by speed (blue slow, green, yellow, red fast),
* the **keys** on the curve,
* a **numeric analysis**: holds, pops, sharp turns, eases.

It also lets you fix the motion in world space ("raise the foot at frame 25 by 5 cm")
instead of editing F-Curves blind.

## Setup (run with the MCP code-execution tool)

```python
import sys
# the add-on's API (works for the extension and a plain add-on install)
emp = next(m for n, m in sys.modules.items() if n.endswith("editable_motion_paths.api"))

emp.add_paths("Zombie_Rig", ["CTRL_Foot_L", "CTRL_Hand_IK_R"], mode='LOCATION')
print(emp.list_paths())
```

`mode` is `'LOCATION'` (the head / origin trajectory, which edits location keys) or
`'ROTATION'` (the bone-tip trajectory, which edits rotation keys). Use
`emp.set_mode(...)` to switch.

## Read the motion

```python
a = emp.analyze("Zombie_Rig", "CTRL_Foot_L")
print(a["summary"])          # distance, speeds, holds, pops, sharp turns, how keys are hit
p = emp.get_path("Zombie_Rig", "CTRL_Foot_L")   # frames, points, speed_per_frame, keys+handles
```

How to read the result:

* **pops** are sudden speed changes from one frame to the next. They usually read as a hitch.
* **sharp turns** are direction changes of more than 50 degrees within one frame. Good for
  contacts and impacts, bad in an arc that should flow.
* **keys**: "passes through" means the bone keeps its speed through the key. "eases" means
  it slows into and out of the key. "stops" means it halts there.
* **holds** are frame ranges with no motion.

## See it in a screenshot

The add-on draws its overlay with Python, and the MCP screenshot does not show that. Make
real preview objects instead, then take the screenshot:

```python
emp.setup_view("Zombie_Rig", "CTRL_Foot_L", angle=(1.3, 0, 1.2))  # Solid shading, framed
emp.preview_objects()        # curve colored by speed, octahedra on keys and on every frame
# ... take the viewport screenshot with the MCP tool ...
emp.preview_objects(False)   # remove them when done
```

The preview objects show:

* white octahedra for keys and orange ones for selected keys,
* small gray octahedra for frames (spacing),
* a green octahedron for the current frame.

Call `emp.preview_objects()` again after every edit. If you can read local files, use
`emp.render_view(r"C:\path\shot.png")` instead. It renders the real overlay, handles
included.

## Edit

Each edit is one undo step, so you can try something and undo it.

```python
emp.move_key("Zombie_Rig", "CTRL_Foot_L", frame=25, offset=(0, 0, 0.05))   # world-space nudge
emp.move_key("Zombie_Rig", "CTRL_Foot_L", frame=25, target=(0.1, -0.2, 0.3))
emp.insert_key("Zombie_Rig", "CTRL_Foot_L", frame=30)       # keeps the motion
emp.smooth("Zombie_Rig", "CTRL_Foot_L", frames=[19, 25, 27], factor=1.0)  # shape; <0 sharpens
emp.ease("Zombie_Rig", "CTRL_Foot_L", frames=[25], factor=0.8)            # slow in/out at a key
emp.ease("Zombie_Rig", "CTRL_Foot_L", frames=[25], factor=0.8, side='IN') # slow in only
emp.exaggerate("Zombie_Rig", "CTRL_Foot_L", factor=0.3)     # <0 tones down, -1 flattens
emp.slide("Zombie_Rig", "CTRL_Foot_L", frames=[33], offset=3)              # retime, keep pose (TIMING)
emp.slide("Zombie_Rig", "CTRL_Foot_L", frames=[33], offset=3, slide_mode='SPACING')
emp.slide("Zombie_Rig", "CTRL_Foot_L", frames=[40], offset=5, continue_motion=True)  # extend
emp.cleanup("Zombie_Rig", "CTRL_Torso", method='ADAPTIVE', tolerance=1.0)  # fewer keys, same 3D shape
emp.fix_loop("Zombie_Rig", "CTRL_Head", frame=1)  # close a broken loop: last key := key 1
emp.set_handle_type("Zombie_Rig", "CTRL_Foot_L", frames=[25], handle_type='VECTOR')
emp.update()   # recalculate after you change animation by other means
```

Timing and topology (2.0):

```python
emp.space("Zombie_Rig", "CTRL_Hand_L", frames=[10, 30], profile='EVEN')        # constant speed, same shape
emp.space("Zombie_Rig", "CTRL_Hand_L", frames=[10, 30], profile='SLOW_IN_OUT', strength=2.5)
emp.dissolve_keys("Zombie_Rig", "CTRL_Torso", frames=[14, 18])  # remove keys, keep the shape
emp.subdivide("Zombie_Rig", "CTRL_Foot_L", frames=[13, 25], cuts=2)
emp.bevel("Zombie_Rig", "CTRL_Hand_L", frames=[20], width=3, segments=2)   # round a sharp corner
emp.extrude("Zombie_Rig", "CTRL_Foot_L", frame=97, frames_later=6)      # continue the motion
emp.loop_tool("Zombie_Rig", "CTRL_Hand_L", frames=[8, 12, 16, 20], tool='CIRCLE')  # clean arc
emp.path_to_curve("Zombie_Rig", "CTRL_Foot_L")                           # -> object "Path CTRL_Foot_L"
emp.follow_curve("Zombie_Rig", "CTRL_Foot_L", curve="MyCurve", timing='KEEP')
print(max(emp.jitter("Zombie_Rig", "CTRL_Head"), key=lambda fv: fv[1]))  # roughest frame
emp.set_mode('VALUE'); emp.set_value_channel("Zombie_Rig", "CTRL_Hand_L", '["ik_fk"]')
```

Roughness from `jitter()` above about 0.4 usually reads as a hitch or noise: smooth those
frames.

In ROTATION mode, `move_key` turns the bone so its tip points at the target. Points are
world coordinates of the bone tip (or of the head in LOCATION mode).

## A good loop

1. Run `analyze` on the important controls (feet, hands, hips, head) and check for pops,
   unwanted sharp turns, and arcs.
2. Use `preview_objects` and a screenshot to check the arcs and the spacing visually.
3. Make one targeted edit (`move_key`, `smooth`, `ease`, `space`, `slide`...). For a hit or stop
   that reads too linear, use `ease` on that key. For a wobbly arc, use `smooth`.
4. Analyze and look again. Undo if it got worse.
