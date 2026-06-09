# Step 99 — Keep the event399 debug instrumentation as a config-gated trace

## Task

The temporary instrumentation that cracked the event399 color/LOD bug
(step 98) was useful but thrown away. Keep that "dump intermediate data to a
file" capability in the source, **controlled by a flag** so it can be switched
on for future debugging without re-adding ad-hoc prints.

## Thinking

Step 98 used three throwaway probes (to stderr):
1. **PS per-pixel** — `v1` (color), `v2` (texcoord), output, for target pixels.
2. **Texture LOD** — `u, v, lod, ddx, ddy, result` per `Texture.Sample`.
3. **Quad derivatives** — the 2×2 lane UVs and the resulting gradients.

Those three are exactly the things to inspect when a pixel color is wrong
(what the PS received/emitted, what the texture unit sampled, what gradient
drove the mip). So the design goal: one unified, always-present facility, OFF by
default, with negligible cost when off, writing to a file when on.

### Design

- A single process-wide `TRACE` singleton in a new `debug_trace.py`.
- Three independent channels (`ps_pixels`, `texture_lod`, `derivatives`) under a
  master `enabled` switch, configured from the run JSON `debug_trace` block.
- **Pixel attribution / filtering.** Texture and derivative traces don't
  naturally know which screen pixel they belong to, so the PS loop sets
  `TRACE.set_pixel(x, y)` each iteration. An optional `target_pixels` list
  restricts output to specific `"x,y"` pixels (empty ⇒ all). This is what makes
  the trace usable — you focus on the handful of mismatching pixels.
- **Phase tagging.** Quad-derivative LOD re-executes the PS for neighbor lanes
  (throwaway samples at lod 0). The interpreter sets `TRACE.set_phase('deriv')`
  around that re-entry so those lines are tagged `phase=deriv` and the real
  sample is `phase=main` — otherwise the trace is impossible to read.
- **Zero-cost when off.** Every call site guards on a plain boolean attribute
  (`if TRACE.ps_pixels:` / `TRACE.texture_lod` / `TRACE.derivatives`) before
  building any strings.

## Implementation

### New `debug_trace.py`

`_DebugTrace` with `configure(config, base_dir, default_stem)`, `close()`,
`set_pixel`/`set_phase`, and the three channel methods `ps_pixel`,
`texture_sample`, `deriv`. Compact float/vector formatting. Best-effort file
open (never breaks a run). Module-level `TRACE` singleton.

### Wiring (all guarded, no-op when disabled)

- `render.py` `_execute_pipeline`: `TRACE.configure(...)` right after the log
  path is resolved (debug file lands next to the log / config dir), and
  `TRACE.close()` after the bitmaps are written, logging the trace path.
- `hlsl_interpreter.py` `executePS_with_params`: `set_pixel` + `set_phase('main')`
  per pixel; `TRACE.ps_pixel(lane, input_data, ps_output_color)` after shading.
- `hlsl_interpreter.py` `_get_lane_locals`: `set_phase('deriv')`/`'main'` around
  the neighbor-lane re-execution.
- `hlsl_interpreter.py` `_compute_uv_derivatives`: `TRACE.deriv(...)` with the
  quad lane UVs.
- `texture.py` `sample()`: `TRACE.texture_sample(u, v, lod, ddx, ddy, result)`
  at both return points.

## Config

Add a `debug_trace` block to the run JSON (paths resolve next to the log file):

```json
"debug_trace": {
    "enabled": true,
    "file": "pipeline_debug.log",
    "ps_pixels": true,
    "texture_lod": true,
    "derivatives": true,
    "target_pixels": ["366,354", "408,367"]
}
```

Omit the block (or set `enabled: false`) to disable — the default.

## Results

Ran event399 with the block above targeting two pixels. Sample trace lines:

```
DDX (366,354) tl=(0.44593,0.78456) tr=(0.44442,0.77588) bl=(0.45321,0.75478) ddx=[-0.00151,-0.00868] ddy=[0.00728,-0.02978]
TEX (366,354) phase=main  uv=(0.44593,0.78456) lod=3.97236 ... result=[0.25457,0.25036,0.24875,1.00000]
PS  (366,354) lane=0 v1=[0.36471,0.36471,0.36471,1.00000] v2=[0.44593,0.78456] ... out=[0.09284,0.09131,0.09072,1.00000]
```

- Channels, `phase` tags, and `target_pixels` filtering all behave as designed.
- Confirms the kept instrumentation reproduces exactly what was used in step 98
  (unclamped HDR `v1`, per-sample LOD, quad gradients).

### Regression — 6/6 PASS, tracing off by default

```
PASS event28 204/204   PASS event104 1149/1149  PASS event351 315/315
PASS event371 6/6      PASS event399 696/696    PASS event516 3/3
```

No `*_debug.log` files are produced by the suite (no `debug_trace` block in the
regression config), confirming the facility is a true no-op when disabled.

## Files changed

- `debug_trace.py` — **new**: config-gated `TRACE` facility (PS-pixel / texture-LOD
  / derivative channels, per-pixel filtering, phase tagging, file output).
- `render.py` — configure/close the trace around `_execute_pipeline`.
- `hlsl_interpreter.py` — set pixel/phase context and emit PS-pixel + derivative traces.
- `texture.py` — emit per-`Sample` LOD traces.
