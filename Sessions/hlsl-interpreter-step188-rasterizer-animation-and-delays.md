# Step 188 — Live rasterizer animation + per-item animation delays

## Prompt

1. 动态 web 页面显示能够根据 rasterizer 执行的进度，动态展示像素点绘制的进展。
2. 针对 VS/PS/Rasterizer 动态展示绘制过程，可以设置每次顶点、primitive、像素的处理延迟
   时间来控制动画的进程。

## Starting point (after step 186/187)

The web viewer already animated **VS** (per-vertex) and **PS** (per-pixel)
progress by sharing the interpreter's growing lists and polling `/state`. The
**rasterizer** was still bulk: `rast.rasterize()` ran to completion, then
render.py fed the full pixel list in one shot — so the "Rasterizer" tab
snapped from empty to done. And there was no way to pace any stage, so on a fast
machine the whole pipeline finished inside one 250 ms poll and the animation was
invisible.

## Design

Two additions, both reusing the step-186 "share the growing list, slice on poll"
pattern:

### 1. Live rasterizer progress (deliverable 1)

- `Rasterizer.set_animation_hook(view)` attaches a `WebMeshView` (guarded on
  `hasattr(view, 'bind_rast_pixels')`, so it's a no-op for tk/html/none).
- `rasterize()` computes the total primitive count per topology and calls
  `view.bind_rast_pixels(self._pixels, total)` — sharing the **same list** the
  scan-converter appends to.
- After each primitive in every `_rasterize_*_list/strip/point` loop, a new
  `_anim_primitive_done()` reports `set_rast_progress(primitives, None, len(pixels))`
  and applies the per-primitive delay.
- `WebMeshView._snapshot` serves rasterizer pixels from the live list
  (`_rast_pixels_ref[:_rast_pixel_count]`) while the stage runs, and from the
  final `_rasterizer_pixels` list once render.py sets it. Added a `rast:
  {done,total,pixels}` block to `/state` and a **Rast progress bar** to the page.

### 2. Per-item animation delays (deliverable 2)

- `WebMeshView` holds `_delays = {vertex, primitive, pixel}` (seconds), with
  `set_delays()` / `get_delay(kind)`. The pipeline reads them **live** each
  iteration, so slider changes take effect immediately.
- Wiring: VS loop sleeps `get_delay('vertex')` per vertex; the rasterizer sleeps
  `get_delay('primitive')` per primitive; the PS loop sleeps `get_delay('pixel')`
  per pixel. When a delay is active, progress is reported every item (smooth
  animation) instead of the throttled 1-in-500 sampling.
- Control surface:
  - **Config seeds**: `anim_vertex_delay` / `anim_primitive_delay` /
    `anim_pixel_delay` (seconds) in the JSON, applied at `enable_mesh_view`.
  - **Live sliders**: a delay row in the page (ms) POSTs to `GET
    /set?vertex=..&primitive=..&pixel=..`; `/state.delays` feeds the sliders back
    (without fighting a mid-drag user). A Reset button zeroes them.

## Files changed

- `web_mesh_view.py` — `_delays` + `set_delays`/`get_delay`; `bind_rast_pixels`
  / `set_rast_progress`; live rasterizer slice in `_snapshot`; `/set` route;
  Rast progress bar + delay sliders + JS wiring (`pushDelays`, `syncDelaySliders`,
  auto-follow the `rasterizer` phase tab).
- `rasterizer.py` — `import time`; `_anim_view` + `set_animation_hook` +
  `_anim_primitive_done`; bind at `rasterize()` start; per-primitive hook in all
  five topology loops; final progress flush.
- `hlsl_interpreter.py` — `import time`; per-vertex delay in
  `executeVS_with_params`; per-pixel delay in `executePS_with_params`.
- `render.py` — attach `set_animation_hook` before `rast.rasterize`; seed delays
  from config at mesh-view enable.

## Verification

- Standalone: page contains the Rast bar + three delay sliders + `pushDelays` +
  `/set`; `GET /set?vertex=0.01&primitive=0.05&pixel=0.002` updates `get_delay`
  and is reflected in `/state.delays`; `/state` carries the `rast` block.
- Full pipeline, `Collision-...event104.zip` (1149 verts / 46015 rast px / 42726
  PS px), `anim_primitive_delay=0.01`, background poller: **99 distinct
  rasterizer-phase samples** with primitives growing `3→6→10→…→383` and pixels
  filling in live `0→2078→3909→…→46015`; phases `vs→rasterizer→ps→done`.
- Delay pacing: `anim_vertex_delay=0.002` → VS time `0.4s → 3.41s`
  (`1149 × 2ms ≈ +2.3s`), confirming the live per-vertex sleep.
- Regression: **118/123**, unchanged from step 186/187 — the 5 failures are
  pre-existing; delays default to 0 and the animation hook is never attached on
  the headless regression path (mesh view disabled).
