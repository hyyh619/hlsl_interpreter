# Step 189 — Pixel-view zoom, stage replay, and per-instruction Vertex/Pixel trace

## Prompt

1. 为 Rasterizer / Pixel Shader / Output Merger 窗口提供缩放按钮。
2. 为 VS / Rasterizer / PS / Output Merger 各阶段提供重放按钮，可重新执行指定的绘制流程。
3. 新建一个 "Selected Pixel Info" 窗口，执行某个被选中像素的 PS 指令，显示每条指令的结果。
4. "Selected Vertex Info" 窗口也要显示完整的 VS 指令流每条指令的执行过程与结果。

## Design

Items 2–4 need the **web server to invoke the interpreter on demand** (after the
first run, when the main thread is idle in the stdin loop). `ThreadingHTTPServer`
already handles each request on its own thread, so a `/replay` or `/trace_*`
request runs on a handler thread while `/state` polls continue on others — the
replay animation stays live. A controller serialises interpreter access with a
lock (the interpreter is not re-entrant).

### Instruction trace (items 3, 4)

The interpreter already emits per-statement `[STMT] … => …` lines via
`debug_print` when debug is on. Rather than build a parallel tracer, I capture
those lines for a single item:

- `debug_print` gains a `_trace_sink`: when set, statement lines are appended to
  it, and in `_trace_only` mode the normal stdout/log is suppressed (a trace
  click doesn't pollute the run log).
- `_trace_single_execution(...)` forces debug on, resets `_eval_counter` so
  `_should_print` is true for the one execution, runs `_execute_void_main`, and
  restores all state — safe to call after the pipeline finished.
- `trace_vs_vertex(index, …)` traces one vertex; `trace_ps_pixel(pixel, …)`
  rebuilds the pixel's PS inputs exactly as `executePS_with_params` does (incl.
  quad context for ddx/ddy) and traces it.

### Stage replay (item 2)

`_PipelineController` (render.py) captures the built interpreters / rasterizer /
depth and the stage parameters plus the current `vs_results` / `pixels`.
`replay(stage)` re-runs from that stage onward (`vs → rasterizer → ps → om`),
feeding the web view so the animation replays. **Depth is stateful**: `execute()`
writes depths, so a second pass would reject every fragment — fixed by adding
`Depth.snapshot_buffers()` / `restore_buffers()`, snapshotting the pre-draw
buffer once, and restoring before each rast/om replay. (Verified: `replay vs`
went from `pixels=0` to `pixels=42726` after the fix.)

### Pixel-view zoom (item 1)

`drawPixels` now applies a pixel-view zoom + pan and records its transform;
`/pctl` shows **+ / − / Reset** buttons for the Rasterizer/PS/OM tabs (plus wheel
zoom and drag pan). `makeView` gained an `isActive` guard so the VS wireframe's
drag/rotate handlers don't fire while a pixel tab is showing.

### Wiring

- New routes on `WebMeshView`: `GET /replay?stage=`, `/trace_vertex?i=`,
  `/trace_pixel?x=&y=`, dispatched to the controller (`set_controller`).
- Right-click a vertex → `traceVertex` fetches and renders the VS trace under the
  Selected Vertex Info panel; left-click a pixel (pixel tab) → `tracePixel` shows
  the PS trace in the new Selected Pixel Info panel with the clicked pixel
  highlighted.
- `bind_rast_pixels` / `bind_ps_pixels` now clear the previous run's final pixel
  buffers so replays animate from empty instead of showing stale finals.

## Files changed

- `hlsl_interpreter.py` — `_trace_sink`/`_trace_only`; `debug_print` sink;
  `_trace_single_execution`, `trace_vs_vertex`, `trace_ps_pixel`.
- `output_merger.py` — `snapshot_buffers` / `restore_buffers`.
- `web_mesh_view.py` — controller hook + 3 routes; pixel-view zoom/pan + buttons;
  Selected Pixel Info panel; replay buttons; vertex/pixel trace fetch + render;
  `isActive` guard; bind_* clear stale finals.
- `render.py` — `_PipelineController` (replay + trace, depth-snapshot restore);
  hoisted PS refs; pre-draw depth snapshot; controller registration.

## Verification

- Page serves with every new control (replay buttons, pixel zoom +/−/Reset,
  Selected Pixel Info panel, trace/replay fetch calls).
- Full pipeline on `Collision-...event104.zip` via the live server:
  - `trace_vertex 0` → ok, 61 `[STMT]`/`[DECL]` lines.
  - `trace_pixel (394,298)` → ok, 15 lines.
  - `replay ps` → ok (42726 px); `replay vs` → ok (1149 verts, **42726 px** —
    depth-restore confirmed, was 0 before the fix).
- Regression: **118/123**, unchanged — trace/replay paths are inert on the
  headless path (no mesh view, `_trace_sink` None; `debug_print` behaves exactly
  as before when the sink is unset).
