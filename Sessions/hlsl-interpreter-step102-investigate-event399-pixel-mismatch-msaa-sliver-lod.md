# Step 102 — Investigate event399 output-merger pixel mismatches (MSAA / sub-pixel sliver LOD)

## Task

`Collision-fix-constant-buffer-and-RdotV-zero_event399.zip` reports output-merger
color mismatches vs golden (`diff_ps_output_rt0.csv`): **150 mismatched / 2548**
at tolerance 0.1 (72 at tol 0.15). Depth matches golden exactly on every
mismatched pixel; `missing=0, extra=0`. Analyze the whole pipeline and fix.

## Method

Systematic debugging — root cause before any fix. Used the step-99 `debug_trace`
facility (ps_pixels / texture_lod / derivatives) plus temporary fragment/VS dumps
(all removed afterward; `render.py` restored clean).

## What was ruled OUT (with evidence)

| Hypothesis | Verdict | Evidence |
|---|---|---|
| step-101 rasterizer change caused it | **No — it improved things** | pre-101: 96 mismatched + 18 missing + 45 extra. post-101: 72 mismatched, 0 missing, 0 extra. |
| Alpha blending | **No** | `Blend Target[0]_Enable=False` in pipeline_state.csv. |
| Overlapping / wrong depth-winner | **No** | each mismatched pixel has one rasterized fragment; its depth matches golden to 6 dp. |
| Wrong vertex color | **No** | PS is `o0 = COLOR0 * Diffuse.Sample(uv)`; color interpolation verified. |
| Wrong geometry (VS) | **No** | our VS `SV_POSITION` for the relevant verts matches the golden `vs_mesh.csv` to 5 dp. |
| **LOD computation bug** | **No — LOD is provably correct** | hand-computed perspective-correct UV gradient of the triangle gives LOD **3.242**; the interpreter's quad derivative gives **3.245**. Identical. |
| Mip=Point vs trilinear blend | doesn't fix | `round(LOD)` makes several pixels worse. |
| 4× MSAA resolve | **only 11/50** | simulated D3D 4× sample pattern + per-sample depth + resolve-average. |
| Global LOD bias | partial only | bias −0.7 → 72 drops to 40; cannot eliminate; physically unjustified. |

## Root cause (what the mismatches actually are)

`pipeline_state.csv` has **`Rasterizer,MultisampleEnable,True`**. The mesh in this
region is **sub-pixel slivers** — e.g. prim221 is a ~2×4 px triangle whose three
UVs span ~27×36 texels of the 512² texture. Geometrically that is heavy
minification → LOD ≈ 3.24 → a dark, averaged-down mip. Our single-sample,
spec-correct pipeline produces exactly that.

The golden RT0 is the GPU's **MSAA-resolved** image. Two distinct effects appear
in the 150 mismatched pixels, **neither of which a spec-correct single-sample
rasterizer reproduces**:

1. **Genuine MSAA edge coverage** (minority, e.g. (413,364)): golden is *darker*
   than our fragment because background / neighbouring-triangle samples average
   in. 4× resolve moves us toward golden here (0.417 → 0.193 vs golden 0.231).

2. **Sub-pixel sliver LOD** (majority, e.g. (367,352), (367,354)): golden is
   *brighter* than **every** triangle that covers the pixel, at any coverage.
   Per-sample enumeration at (367,352): the only covering triangles give 0.075 /
   0.297 / 0.558 — a 4× resolve ≈ 0.43, but golden = **0.667**. At (367,354) all
   four samples are nearest prim221 (0.321) so resolve = single-sample = 0.321,
   yet golden = 0.647. The only value matching golden is the sliver sampled at
   **low LOD (mip0–2 ≈ 0.70)** — i.e. the GPU used a far lower LOD for these
   sub-pixel slivers than the triangle's analytic UV gradient dictates.

Effect (2) is mathematically inconsistent with the triangle's own UV mapping, so
it is not a bug in our LOD math. It reflects **hardware-specific LOD/coverage
behaviour for sub-pixel primitives under MSAA** (how a particular GPU computes
derivatives for quads whose helper lanes lie outside a sliver, and how coverage
interacts with the resolve). This is not derivable from the D3D rasterization
spec and is the classic reason reference rasterizers do not match GPU edge AA
pixel-for-pixel.

## Why "implement full MSAA" does not close this

A correct 4× MSAA implementation:
- helps the effect-(1) edge pixels, but
- cannot raise effect-(2) pixels above the brightest covering triangle, and
- *regresses* some pixels (e.g. (367,354): single-sample already equals the
  resolve; (367,352): resolve 0.43 is farther from golden 0.667 than our current
  0.518).

The full 4× simulation confirmed this: **11/50** reproduced, with regressions.
The capture ships no per-sample buffer, so an MSAA implementation cannot even be
validated beyond the resolved image.

## Outcome

No spec-compliant change fixes the bulk of these 150 pixels — they are inherent
MSAA edge-antialiasing + GPU-specific sub-pixel-sliver LOD differences (2.8–5.9%
of pixels, all at sliver edges, all near tolerance). Recommended handling:
document as a known limitation; optionally implement MSAA as a pipeline-feature
for the edge pixels it genuinely helps, with eyes open that it will not close
event399. Pending user direction (see conversation) before writing pipeline code.

## Artifacts

Investigation only — no source changes committed. `render.py` instrumentation and
all temp files (`vs_dump.json`, `frag_all.csv`, trace configs/logs) removed.
