# Step 91 — Implement full D3D11 depth clip (geometric near/far clipping)

## Prompt

> 检查是否实现了 depth clip → 实现完整的 D3D 的 depth clip 功能。
> 把你思考、执行和结果都写入到一份 md 文件中，放到 Sessions 目录下。

## 1. Investigation — was depth clip implemented?

**No.** The audit of `rasterizer.py` found:

1. **`depth_clip_enable` was dead config.** Declared on `RasterizerConfig` and read from JSON / `create_default_config`, but **never consulted by any logic** — setting it `False` did nothing. It also was **not parsed** from `pipeline_state.csv` (the capture's `Rasterizer,DepthClip,True` row was ignored).
2. **Only a per-pixel proxy existed for triangles.** `_rasterize_triangle` rejected a fragment when its interpolated NDC z fell outside `min_depth..max_depth` (viewport range). This conflated two distinct D3D concepts:
   - near/far **clip** (fixed at NDC z ∈ [0,1]), and
   - viewport depth **remap** (`MinDepth/MaxDepth`).
   It also ignored `depth_clip_enable` (couldn't be turned off).
3. **No true geometric clipping.** D3D clips primitives against the near plane *before* the perspective divide, splitting triangles and interpolating new vertices. Here there was none — a vertex behind the camera (`w ≤ 0`) would project through a flipped/garbage divide.
4. **Points and lines had no depth test at all**, and no viewport depth remap was applied anywhere.

User chose: **完整几何裁剪** (full geometric clipping).

## 2. Design

Clip in **homogeneous clip space** before the perspective divide. The D3D clip volume is `0 ≤ z ≤ w`, which implies `w > 0`. Modelled as half-space tests `dist(p) ≥ 0`:

| Plane | Predicate | When active |
|-------|-----------|-------------|
| w > 0 | `w - eps` | **always** (keeps the perspective divide finite; removes behind-camera geometry) |
| near  | `z`       | only when `depth_clip_enable` |
| far   | `w - z`   | only when `depth_clip_enable` |

Key correctness facts that made this safe and small:

- **Linear-in-clip-space interpolation.** New vertices at plane crossings interpolate position (incl. `w`) *and* every attribute linearly in clip space (`_lerp_vertex`). The rasterizer's existing perspective divide (`attr/w`) then restores perspective-correct values — because attribute and `w` were interpolated identically.
- **Fully-inside is identity.** Sutherland–Hodgman returns the *original* vertex objects when no edge crosses a plane, so the overwhelmingly common case (all geometry on-screen) produces a single unchanged triangle — **zero floating-point perturbation**, hence no regression risk.
- **The per-pixel depth test becomes redundant.** After geometric clipping every surviving vertex has NDC z ∈ [0,1]; a covered fragment's depth is a convex combination of those, so it stays in range. The old `min_depth ≤ depth ≤ max_depth` rejection was removed.
- **`depth_clip_enable = False`** = D3D semantics: near/far planes dropped (geometry kept), output depth **clamped** to the viewport range instead.
- **Viewport depth remap** finally applied: `depth = MinDepth + ndc_z·(MaxDepth − MinDepth)` (identity for the default [0,1], so no regression impact).

Because the regression gate is VS-output-vs-golden (independent of rasterization), and the in-range path is byte-identical, this is regression-neutral.

## 3. Changes (`rasterizer.py`)

- Added module constant `_CLIP_W_EPS` (the always-on w>0 plane).
- New helpers:
  - `_map_depth(ndc_z)` — viewport remap + clamp-when-disabled.
  - `_lerp_vertex(va, vb, t)` — clip-space linear interpolation of a whole vertex dict.
  - `_clip_planes()` — the active half-space predicates per `depth_clip_enable`.
  - `_clip_segment(v0, v1)` — Liang–Barsky line clip (returns clipped endpoints or `None`).
  - `_clip_polygon_against_plane(verts, dist)` — Sutherland–Hodgman.
  - `_clip_triangle(v0, v1, v2)` — clips against all active planes → fan of sub-triangles.
- `_rasterize_triangle` is now a thin wrapper: clip → rasterize each sub-triangle via the renamed core `_raster_triangle_core`, returning the aggregate status (`rasterized`/`culled`/`clipped`).
- `_raster_triangle_core` (the old body): removed the per-pixel depth-range rejection; depth now goes through `_map_depth`; wireframe edges drawn from the clipped sub-triangle vertices.
- `_rasterize_point`: w-clip + near/far discard (when enabled) + `_map_depth`.
- `_rasterize_line`: clips the segment first, interpolates depth from post-clip **NDC z** (was clip-space z) through `_map_depth`.
- `load_config_from_pipeline_state_csv`: now parses `Rasterizer,DepthClip`/`DepthClipEnable`.

## 4. Verification

**Regression suite — 6/6 PASS** (`python run_regression.py`), all rows matched (204/204, 1149/1149, 315/315, 6/6, 696/696, 3/3). The in-range identity path means VS-vs-golden is untouched.

**Targeted depth-clip checks** (ad-hoc script, since regression captures are all on-screen):

| Test | Result |
|------|--------|
| A. Fully-inside triangle | `_clip_triangle` returns 1 sub-tri with the **same vertex objects** (identity) ✅ |
| B. Triangle straddling near plane | clip ON → 2 sub-tris, fewer pixels than OFF, all depths ∈ [0,1]; clip OFF renders the extra region with clamped depth ✅ |
| C. Vertex behind camera (`w < 0`) | geometrically removed, no garbage, depths ∈ [0,1] ✅ |
| D. Point list | clip ON keeps 1/3 (near/far out-of-range discarded); OFF keeps 3/3, depths clamped to `[0, 0.5, 1.0]` ✅ |
| E. `pipeline_state.csv` `DepthClip,False` | parsed → `depth_clip_enable = False` ✅ |

## 5. Known limitations

- Fan re-triangulation of a clipped polygon can double-cover the shared diagonal edge under the inclusive (`>= 0`) fill rule. Harmless for nearest-depth occlusion (same depth), and never triggered by on-screen geometry. A top-left fill rule would remove it if exactness is needed later.
- x/y guard-band clipping is still handled by the existing screen bounding-box clamp (per-pixel), not geometric — out of scope for *depth* clip.
