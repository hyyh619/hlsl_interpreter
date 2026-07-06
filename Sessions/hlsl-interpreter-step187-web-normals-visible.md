# Step 187 — Fix: normal vectors not showing in the dynamic web view

## Prompt

动态 web 视图的 normal 向量显示没有展示出来，请修复。

## Diagnosis

First I ruled out a **data** problem. Running the pipeline in `web` mode and
dumping `/state`:

- `witcher3_countryside_event7301.zip` (the Default case) — a 3-vertex
  fullscreen-quad pass — has **no normal data at all** (`n: null`), so there is
  genuinely nothing to draw.
- `Collision-...event104.zip` — 1149 vertices, **all 1149 carry normals**
  (`n: [0,0,-1]` etc.), on both input and output. So the data pipeline is fine.

So the bug is in **rendering**. Two compounding causes:

1. **Object-space offset + camera-facing normals collapse to a point.** The old
   code built the normal endpoint as `project(v.p + v.n*nlen)` — i.e. it added
   the normal in object space and projected it. At the default view (`rx=0,
   ry=0`) the camera looks straight down the z-axis, so a normal facing `±z`
   projects to **zero length** and is invisible. In event104 *every* normal is
   `(0,0,-1)` (facing the camera), so nothing rendered. Reproduced with the JS
   transform math: for `n=(0,0,-1)` at `rx=ry=0` the screen-space length is
   exactly `0`.

2. **Length tied to object-space span, then scaled down.** `nlen` was
   `0.12 * bounds-span`; projecting that through the fit-to-view scale can shrink
   the on-screen segment to sub-pixel for large-coordinate meshes.

## Fix (`web_mesh_view.py`, poller page JS)

1. **Mild default tilt.** Initial rotation changed from `rx=0, ry=0` to
   `rx=22, ry=-32` so the mesh is never dead-on axis-aligned; `±z` normals now
   get a real screen component. (Verified: `n=(0,0,-1)` → screen length `0.618`,
   and all of `±z / x / y` normals now project to a visible line.)

2. **Fixed screen-space normal length.** Normals are now drawn as a constant
   20-px segment along the *projected* normal direction, computed from
   `transform(v.n)` (the same rotation `project` uses; `project` flips y, so the
   y component is negated). The screen delta is normalized to the fixed pixel
   length, so normals are visible at any zoom and for any coordinate magnitude.
   Style bumped to a brighter `#7fd7ff`, `lineWidth 1.5`, plus a small dot at the
   arrow tip.

3. **Camera-facing fallback + empty-data hint.** If a normal's projected length
   is still ~0 (pointing straight at/away from the camera), a tip dot is drawn at
   the vertex so *something* shows. If the toggle is on but the mesh has no normal
   data at all, the canvas prints `(selected mesh has no normal data)` so the user
   knows it's a data issue, not a broken toggle.

## Verification

- The served page contains the tilt (`rx=22, ry=-32`), the new screen-space
  normal code, and the no-data hint.
- Reproduced the JS projection in Python: at the new tilt every tested normal
  orientation (`(0,0,±1)`, `(1,0,0)`, `(0,1,0)`) yields a non-zero screen length
  → renders as a line; at the old `rx=ry=0` the `(0,0,-1)` case was `0`.
- `/state` confirmed to carry normals for event104 (1149/1149 in and out).
- Regression: unchanged from step 186 (the edit is confined to the web viewer's
  HTML/JS template; the headless regression path never instantiates it).

## Files changed

- `web_mesh_view.py` — default view tilt + screen-space fixed-length normal
  rendering with tip dot and no-data hint.
