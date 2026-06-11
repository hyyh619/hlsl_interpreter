# Step 115 — Decide bitmap dumps from pipeline_state.csv RenderTarget / DepthStencil

## Task

Use the RenderTarget / DepthStencil bindings in `pipeline_state.csv` to decide
whether the pipeline should dump the pixel-color bitmap and/or the depth-buffer
bitmap.

## 1. Investigation

`pipeline_state.csv` records the output-merger bindings:

| Case | `RenderTarget,Target[0]_Format` | `DepthStencil,Format` | Meaning |
|---|---|---|---|
| Collision event28/104/… | `R8G8B8A8_UNORM` | `D24_UNORM_S8_UINT` | color + depth |
| witcher3 event895/1433/… | *(absent)* | `D16_UNORM` | depth only (Z/shadow-style pass) |

So a depth-only pass (no render target bound) should **not** write a color
bitmap, while a normal pass writes both. The old logic dumped the color bitmap
whenever the pipeline produced any fragments, regardless of whether a render
target was bound — so depth-only passes (most witcher3 cases) wrongly produced a
`*_output.bmp`.

The rasterizer already parsed `render_target_format` from the `RenderTarget`
section (used for the output-merger write clamp). Nothing tracked the
depth-stencil binding.

## 2. Implementation

`rasterizer.py`:
- Added `RasterizerConfig.depth_stencil_format`.
- `load_config_from_pipeline_state_csv`: parse the `DepthStencil,Format` row;
  also treat a `Target[0]_Format` / `Format` value of `UNKNOWN` as *unbound*
  (so both fields are `None` unless a real format is present).

`render.py` (output-merger dump block):
- `has_render_target = render_target_format is not None`
- `has_depth_stencil = depth_stencil_format is not None`
- Dump the **color** bitmap only when `has_render_target` (and the PS ran);
  otherwise log `Output color bitmap skipped: no render target bound`.
- Dump the **depth** bitmap only when `has_depth_stencil`; otherwise log the
  analogous skip line.
- Both still require surviving fragments; when there are none, the existing
  "pipeline produced no pixels" message is kept.

## 3. Verification

```
event1433 (DepthStencil only, no RenderTarget):
  Output color bitmap skipped: no render target bound (...RenderTarget absent).
  Saved depth bitmap: ..._depth.bmp (4609 pixels)        ✓ depth only

event104 / 351 / 399 / 516 (RenderTarget + DepthStencil):
  Saved output-merger bitmap: ..._output.bmp             ✓ color
  Saved depth bitmap:        ..._depth.bmp               ✓ depth

event28 (RenderTarget + DepthStencil, but all fragments depth-fail):
  Output/depth bitmaps skipped: pipeline produced no pixels.   ✓
```

Regression suite: **15/15 passed** — bitmap-dump gating does not affect the VS
comparison or the pixel/depth comparison, so no case regresses.

## 4. Commit

Committed `rasterizer.py` + `render.py` + this session doc.
