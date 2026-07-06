# Step 186 — Dynamic web mesh viewer (live VS/PS progress + normals toggle)

## Prompt

1. 请把 view html 静态页面展示，改为动态 web 页面显示
2. 能够根据 VS 执行的进度，动态展示顶点绘制的进展
3. 能够根据 PS 执行的进度，动态展示像素绘制的进展
4. 能够动态开关显示当前顶点的法线向量

## Goal

Step 185 added `HtmlMeshView` — a **static** one-shot viewer: it accumulates
all pipeline data and, on `show()` (called once at the very end of the run),
writes a single self-contained `mesh_view.html` with every datum baked into a
JSON literal, then opens the browser. Nothing animates; you only ever see the
finished frame.

This step makes the viewer **dynamic**: a live web page, served by a background
HTTP server, that the browser polls while the pipeline is still executing — so
vertices appear as the VS shades them and pixels fill in as the PS shades them,
with a live toggle for per-vertex normal vectors.

## Design & thinking

### Why a server, not a file

A static `file://` HTML can't update after it's written. To show *progress* the
page must re-read state over time. The cleanest stdlib-only way (no third-party
deps, per project rules) is a tiny `http.server.ThreadingHTTPServer` on a daemon
thread serving two routes, with the browser polling on a timer:

- `GET /`      → the poller page (self-contained HTML+JS)
- `GET /state` → a JSON snapshot, rebuilt under a lock on every request

Polling (250 ms) was chosen over SSE/WebSockets: it is trivial with
`BaseHTTPRequestHandler`, survives disconnects, and needs no framing protocol.

### Zero-copy live snapshots

The naïve approach — rebuild and serialize the whole geometry array on every
shaded vertex — is O(n²). Instead the viewer **shares the interpreter's own
growing lists**:

- `bind_vs_results(results_ref, total)` hands the viewer the exact list
  `executeVS_with_params` appends each vertex's canonical output to.
- Per vertex the interpreter only bumps an integer counter via
  `set_vs_progress(done, total)` (a lock + int — microseconds).
- `/state` slices `results_ref[:done]` and serializes *only when the browser
  polls* (~4×/s), not per vertex.

Same pattern for PS via `bind_ps_pixels(pixels_ref, total)` /
`set_ps_progress` — the PS colours `Pixel` objects in place, so
`pixels_ref[:done]` is the shaded-so-far slice.

### New file: `web_mesh_view.py` — `WebMeshView`

Drop-in for `HtmlMeshView`/`MeshView` (same setter API: `set_input_data`,
`set_output_data`, `set_rasterizer_pixels`, `set_pixel_shader_output`,
`set_output_merger_pixels`, `set_pipeline_stats`, `set_primitive_topology`,
`clear`, `show`, `close`, plus the `_draw_*` / `set_hlsl_interpreter*` no-ops).
Extra live hooks (no-ops on the other viewers, so callers guard with `hasattr`):
`bind_vs_results` / `set_vs_progress` / `bind_ps_pixels` / `set_ps_progress` /
`set_phase`. All state is behind an `RLock`; a `seq` counter increments on every
mutation.

The page reuses the step-185 projection / wireframe / pixel-image rendering, but
wraps `DATA` in a `fetch('/state')` poll loop and adds:
- VS and PS **progress bars** (done/total) + a phase label
  (`init`→`vs`→`rasterizer`→`ps`→`done`), auto-following the active stage;
- a **"Show Normals" checkbox** that draws a short blue line from each vertex
  along its (projected) normal on both input and output canvases — toggled live,
  no reload.

### Interpreter & orchestrator wiring

- `hlsl_interpreter.py`: import `WebMeshView` (guarded), add a `'web'` branch to
  `enable_mesh_view`, and add the live hooks to `executeVS_with_params` (bind +
  per-~1/500 progress) and `executePS_with_params` (bind + per-~1/500 progress).
  Both hooks are gated on `self._mesh_view_enabled and hasattr(view, 'bind_*')`,
  so tk/html/none viewers and the headless regression path are completely inert.
- `render.py`: accept `'web'` in `_resolve_mesh_view_mode`; feed the rasterizer
  coverage + `set_phase('rasterizer')` right after `rast.rasterize`; and
  `set_phase('done')` at the final mesh-view block.

### Bug found & fixed: PS runs on a *separate* interpreter

First integration run showed VS animating but PS jumping straight from
`rasterizer` → `done` with no live pixels. Cause: `render.py` executes the pixel
shader on a **fresh `ps_interp`** created by `_make_interpreter`, which has **no
mesh view attached** (`_mesh_view is None`), so the PS live hooks never fired.
Fix: before `executePS_with_params`, share the VS interpreter's viewer to
`ps_interp` (only when it's a `WebMeshView`, guarded by
`hasattr(.., 'bind_ps_pixels')`). After this, PS pixels fill in live.

### Cosmetic fix: `clear()` no longer zeroes progress

`render.py` calls `show_input_mesh_from_params` → `clear()` **after** the VS loop
finishes, which was snapping the completed VS bar back to 0/0. `WebMeshView.clear`
now resets geometry/pixel buffers and the shared list refs but **keeps** the
numeric VS/PS progress counters (the next run rebinds via `bind_*`).

## Verification

Standalone server test (browser open stubbed out): `/` serves the page with the
title injected and the normals toggle present; `/state` reflects live VS growth
(output vertices 1→2→3 as `set_vs_progress` advances) and PS growth (ps_pixels
grow as pixels are coloured); phase transitions to `done`.

Full-pipeline test in `web` mode on
`Collision-fix-constant-buffer-and-RdotV-zero_event104.zip` (1149 verts,
46015 rasterizer fragments, 42726 PS pixels), with a background poller sampling
`/state`, captured the live progression:

```
(phase, vs_done, ps_done, out_verts, ps_px)
('vs',        819, 0,    819,  0)      # vertices appearing as VS shades
('vs',       1149, 0,   1149,  0)
('rasterizer',1149, 0,  1149, 46015)   # fragment coverage
('ps',       1149, 256, 1149,  256)    # pixels filling in as PS shades
('ps',       1149, 3146,1149, 3146)
 ... 
('ps',       1149,42726,1149,42726)
('done',     1149,42726,1149,42726)    # VS bar preserved at 100%
```

All four deliverables confirmed: (1) dynamic server-backed page, (2) live VS
vertex progress, (3) live PS pixel progress, (4) live normals toggle.

**Regression: 118/123 — no regression introduced.** The 5 failing cases
(`witcher3_countryside_event1643/1834/1852/2322`, `manhattan_frame_274_event1041`)
are **pre-existing**: with the code changes stashed, `event1643` still fails
0/1110 at clean HEAD, and the added code path is inert whenever the mesh view is
disabled (the regression config). Baseline and post-change counts match.

## Files changed

- `web_mesh_view.py` (new) — `WebMeshView`: HTTP server + live poller page.
- `hlsl_interpreter.py` — import guard, `enable_mesh_view` `'web'` branch, VS/PS
  live progress hooks.
- `render.py` — `'web'` mode, rasterizer/`done` phase hooks, share viewer to
  `ps_interp`, docstring.
- `Cases/Default.json` — `mesh_view_mode: "web"` (fixed the ignored
  `mesh_view_Mode` typo key) so the default run uses the dynamic viewer.
