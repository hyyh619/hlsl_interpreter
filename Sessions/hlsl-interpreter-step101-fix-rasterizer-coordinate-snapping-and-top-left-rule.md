# Step 101 — Make triangle rasterization conform to D3D11 §3.4.1 (Coordinate Snapping) and §3.4.2.1 (Top-Left Rule)

## Task

Audit the triangle traversal in `rasterizer.py` against the D3D11 rasterizer
rule and fix any non-conformance:

- **§3.4.1 Coordinate Snapping** — after clipping, perspective divide and
  viewport scale, vertex x/y are snapped to `n.8` fixed-point integers (8
  fractional bits = a 1/256 sub-pixel grid). Culling and attribute
  interpolation are set up on the *snapped* positions.
- **§3.4.2 Triangle Rasterization Rules / §3.4.2.1 Top-Left Rule** — a pixel is
  covered when its sample location falls inside the triangle. A sample exactly
  on an edge belongs to the triangle only if that edge is a **top** edge
  (exactly horizontal and above the others) or a **left** edge (not horizontal,
  on the left side). This prevents adjacent triangles from overdrawing shared
  edges.

## Thinking / investigation

### What the code did before

`_raster_triangle_core` ([rasterizer.py]) projected vertices with
`Viewport.transform_to_screen`, which does `int(...)` — truncating each vertex
to a **whole pixel**. There was no sub-pixel precision at all, so §3.4.1's `n.8`
snapping was effectively "snap to n.0".

`_shade_lane` then evaluated the three edge functions at the integer **corner**
`p = (x, y)`, not the pixel-center sample `(x + 0.5, y + 0.5)` the spec's
coverage test assumes.

The coverage test was:

```python
inside = (area > 0 and w0 >= 0 and w1 >= 0 and w2 >= 0) or \
         (area < 0 and w0 <= 0 and w1 <= 0 and w2 <= 0)
```

Every edge is **inclusive** (`>= 0` / `<= 0`). So a sample landing exactly on a
shared edge passed the test for **both** adjacent triangles → the Top-Left rule
was simply absent, and shared edges were double-covered (overdraw, the exact
artifact §3.4.2.1 exists to prevent).

### Two violations identified

1. **§3.4.1** — integer truncation instead of 1/256 sub-pixel snapping, and the
   coverage sample taken at the pixel corner instead of its center.
2. **§3.4.2.1** — Top-Left rule not implemented; all edges inclusive.

### Designing the Top-Left test (winding-agnostic)

The triangle can be wound either way (the project supports both `FrontFace`
settings and `CullMode.NONE`), and screen space is **y-down** (the viewport
transform flips Y). To make one rule cover every case I normalize by the sign
of the signed area so the interior is always the `e_i >= 0` half-space:

```
edge_sign = +1 if area > 0 else -1
e_i = w_i * edge_sign        # interior <=> e_i >= 0 for all i
```

For a sample exactly on an edge (`e_i == 0`), include it iff that edge is top or
left. Classifying from the edge's direction vector `D = (Q - P) * edge_sign`
(canonical winding) in y-down screen space:

```
is_top  = (D.y == 0 and D.x < 0)   # horizontal, pointing left
is_left = (D.y > 0)                # points downward (screen y grows down)
top_left = is_top or is_left
```

I verified this against two opposite windings of a right triangle by hand
(area = -1 case and area = +1 case); in both, the formula picks out exactly the
one top edge and the one (or two) left edges that geometry demands. `w0` is the
edge opposite v0 (uses v1→v2), `w1` opposite v1 (v2→v0), `w2` opposite v2
(v0→v1), so the per-edge flags `tl0/tl1/tl2` pair with `w0/w1/w2`.

### Why `== 0.0` is safe here (not a float gamble)

Snapped vertices are exact multiples of 1/256; the center adds 0.5 = 128/256.
Every difference fed to the edge function is therefore an exact dyadic rational,
and for screen coordinates up to a few thousand pixels the products and sums
stay well under 2^53, so they are represented **exactly** in float64. An on-edge
sample yields a bit-exact `0.0`. The `== 0.0` on-edge test is reliable.

## Changes

`rasterizer.py`:

1. **New `Viewport.transform_to_screen_subpixel`** — same projection as
   `transform_to_screen` but returns floats and snaps x/y to the 1/256 grid
   (`round(v * 256) / 256`) instead of truncating. `transform_to_screen` is kept
   unchanged for the point/line paths (they walk whole pixels).

2. **`_raster_triangle_core`** now:
   - projects the three vertices with `transform_to_screen_subpixel` (snapped
     sub-pixel positions drive area/cull/edge-functions/interpolation, per
     §3.4.1);
   - computes the pixel bounding box with `floor`/`ceil` over the sub-pixel
     extents (the integer pixels whose center could be covered);
   - precomputes `edge_sign` and the per-edge top-left flags `tl0/tl1/tl2`.

3. **`_shade_lane`** now samples the **pixel center** `(x + 0.5, y + 0.5)` and
   applies the Top-Left coverage test: interior on every edge, and any edge the
   sample lies exactly on must be top or left.

The 2×2 quad-aligned traversal (for PS derivative lanes) is unchanged — only the
sample point and the inside test moved.

## Verification

### Regression suite (mandatory) — all pass

```
6/6 passed
  event28   204/204
  event104  1149/1149
  event351  315/315
  event371  6/6
  event399  696/696
  event516  3/3
```

VS-vs-golden comparison happens before rasterization, so it is unaffected (and
no case crashes). The regression suite does not check pixel coverage, so I added
a direct coverage test.

### Direct coverage test — watertight, no overdraw

Split an 8×8 quad into two triangles sharing the diagonal
`(0,0)–(8,8)` (cull off), rasterized each, compared covered pixel sets:

```
Tri A pixels: 36
Tri B pixels: 28
Shared-edge double-covered pixels: 0
Union: 64
```

`Union = 64 = 8×8` with **zero** double-coverage proves the Top-Left rule is
working: the shared diagonal is drawn by exactly one triangle, the quad tiles
with no gaps and no overlap. Before the fix the inclusive `>= 0` test would have
double-drawn the diagonal.

## Result

The triangle path now conforms to D3D11 §3.4.1 (n.8 sub-pixel snapping,
center-sample coverage, snapped geometry feeding cull + interpolation) and
§3.4.2.1 (Top-Left rule). All 6 regression captures still pass; coverage is
watertight and overdraw-free on shared edges.
