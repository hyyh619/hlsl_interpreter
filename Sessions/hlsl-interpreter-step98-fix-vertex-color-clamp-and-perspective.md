# Step 98 — Fix PS color mismatch in event399 (vertex-color clamp + perspective)

## Task

`Collision-fix-constant-buffer-and-RdotV-zero_event399.zip` reported ~349
`Error [PixelDiff]` output-merger color mismatches (matched 2181/2548). Analyze
the full render pipeline and fix the color mismatch.

> Note: these `Error [PixelDiff]:` lines do **not** start with literal `Error:`,
> so `run_regression.py` never counted them — event399 "passed" 696/696 (VS
> golden) while silently rendering 349 wrong pixels. The pixel compare is a soft
> metric today.

## Thinking / investigation

The PS is trivial: `o0 = v1 (COLOR0) * DiffuseTexture.Sample(v2)`. So a wrong
output color is either a wrong interpolated input (v1/v2) or a wrong texel.

I instrumented the pipeline (temporary, since removed) to pull ground truth:

1. **Per-pixel dump** (`v1`, `v2`, `out`) for several mismatched pixels. Found
   `v1` (lit vertex color) reading exactly `[1,1,1]` and outputs ~3× too dark.
   Since `out = v1 * texel`, this isolated the error to **either** the
   interpolated color **or** the texel.

2. **Texel probe.** Sampling the captured 10-level mip chain at the exact UVs
   showed our pipeline faithfully sampled the loaded texture (~mip3), but **no
   mip level at our UV produced the golden value** — the golden texel was
   brighter/grayer than anything at that UV.

3. **Golden VS data.** The golden `*_vs_mesh.csv` COLOR column is **not in
   [0,1]** — vertices carry HDR lit colors like `1.12145` and `1.30868`. The PS
   later scales the sampled texel by these. Our `v1` read `[1,1,1]` — clamped.

4. **DXBC interpolation mode.** `PS_shader_disasm.txt` declares
   `dcl_input_ps linear v1.xyzw` / `linear v2.xy` → D3D interpolates COLOR
   **perspective-correct and unclamped**.

### Root cause #1 (the real bug)

`rasterizer.py`'s attribute interpolation special-cased `color` (and `normal`):
it interpolated them with **plain (affine) barycentric** weights and then
**clamped each component to [0,1]**:

```python
if attr_lower in ['color', 'normal']:
    val = bary_x*c0 + bary_y*c1 + bary_z*c2     # NOT perspective-correct
    if attr_lower == 'color':
        val = max(0.0, min(1.0, val))           # WRONG: clamps HDR vertex color
```

Both are wrong for D3D `linear` interpolators:
- Clamping caps the HDR lit color at 1.0, so `out = 1.0*texel` instead of
  `~1.3*texel` → systematically too dark.
- Affine (non-perspective) interpolation skews the color across the triangle.

D3D clamps to the RT range only at the **output-merger write** (already handled
post-PS by `_clamp_output_colors`), never on the interpolant.

### Secondary observation (LOD) — investigated, left as-is

The residual after fix #1 is a slight LOD over-estimate at tiny minified
triangles: our quad-derivative LOD runs ~0.5–1 high where this texture has a
sharp mip2→mip3 step, nudging us onto the darker level. I confirmed the
texcoords match golden (VS 696/696, no texcoord errors) and the quad spacing is
correct, so the gradient itself is right — the discrepancy is at the
software-vs-GPU rasterization noise floor for ~15px triangles.

I **tried** honoring the captured `Mip=Point` filter (round LOD to a single
level, no trilinear blend). It made event399 **worse** (96 → 121 mismatches):
rounding our slightly-high LOD up snaps onto the dark mip3, whereas trilinear
blending tracks the GPU result more closely. So I kept trilinear blending and
documented why, rather than overfit.

## Implementation

### `rasterizer.py` — both interpolators

Removed the `color`/`normal` special case in `_interpolate_with_barycentric`
(triangles) and `_interpolate_attributes_line` (lines). All PS-input attributes
now use the existing **perspective-correct** path with **no clamp** — matching
DXBC `linear`. Added a comment explaining the output-merger is the only clamp.

### `texture.py` — `sample()` cleanup (behavior-preserving)

Collapsed the 12-branch min/mag/mip filter ladder into a small helper that
picks the within-level filter (Min when minifying, Mag when magnifying; Point→
nearest, Linear→bilinear) and always trilinearly blends the two bracketing mip
levels. A comment records that the capture is `Mip=Point` but trilinear tracks
the GPU better here given our LOD. Verified byte-identical pixel output on every
other regression case.

## Results

### event399 pixel compare (regression config)

| | matched | mismatched | 
|---|---|---|
| before | 2154 | 383 |
| after  | **2391** | **146** |

(Under the user's quoted config the same fix moves 2181/349 → 2434/96.) The
remaining mismatches are mostly just over the 0.15 tolerance at minified
silhouette edges (the LOD residual above).

### Before/after across the suite — no regressions

Stashed the change and re-ran the full suite to get a clean baseline. My change
affected **only event399**; event104 (26839/13867), event351 (953/6), event371
(34/243) and event516 (88/0) were **byte-identical** before and after — every
other sampler has Min==Mag so the `sample()` refactor is a no-op, and their
vertex colors don't exceed [0,1].

### Regression suite — 6/6 PASS

```
PASS event28  204/204   PASS event104 1149/1149  PASS event351 315/315
PASS event371 6/6       PASS event399 696/696    PASS event516 3/3
```

## Files changed

- `rasterizer.py` — drop the COLOR/NORMAL affine+clamp special case in both the
  triangle and line attribute interpolators; all attributes are now
  perspective-correct and unclamped (D3D `linear`).
- `texture.py` — refactor `sample()`'s filter selection into a clear
  minify/magnify + trilinear path (behavior-preserving), with a note on the
  Mip=Point vs trilinear trade-off.
