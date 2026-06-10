# Step 103 — Compare output depth against golden `diff_depth_output.csv`

## Task

1. Captures that have depth enabled ship a golden depth dump in
   `diff_depth_output.csv`.
2. When the depth test is enabled, compare the pipeline's per-pixel output depth
   against that golden.
3. The depth comparison needs its own tolerance, configured from the run JSON.

## Investigation

- **Golden file present?** All six regression captures contain
  `diff_depth_output.csv`. Format: `X,Y,Depth,Stencil` — the post-draw depth
  buffer for every pixel the draw wrote (event399: 2548 rows, matching the RT0
  golden count). Two captures (event28, event371) ship the file with **header
  only** (no data rows).
- **Existing pattern to mirror:** `diff_ps_output_rt0.csv` is consumed by
  `_load_golden_ps_output` + `_compare_ps_output` in `render.py`, invoked near
  the end of `_execute_pipeline`. That comparison already collapses multiple
  fragments per (x,y) to the nearest-depth winner (LESS-test semantics) and
  reports matched / mismatched / missing / extra. The new depth comparison
  follows the same shape.
- **"Depth test enabled" signal:** the pipeline sets `depth.config.depth_enable
  = True` when it loads `pre_draw_depth_stencil.csv`. That flag is the gate.
- **Tolerance config:** `pixel_tolerance` is already read from the JSON
  (`config.get('pixel_tolerance', ...)`); the new tolerance follows suit as a
  separate key so depth can be validated more tightly than color.

## Changes (`render.py`)

1. **`_load_golden_depth_output(path)`** — parses `diff_depth_output.csv` →
   `{(x, y): depth}`.
2. **`_compare_depth_output(golden, pixels, tolerance, log)`** — collapses
   rendered fragments to the nearest-depth winner per pixel (same as the RT0
   path), compares depth only within `tolerance`, and logs
   `Error [DepthDiff]: (x,y) depth out=… golden=… ddiff=…` lines (capped at 50)
   plus a `Golden depth pixels: … | matched | mismatched | missing | extra`
   summary.
3. **Config key `depth_tolerance`** (`config.get('depth_tolerance', 0.01)`),
   separate from `pixel_tolerance`. Added to `Cases/Default.json`.
4. **Gated invocation** after the RT0 comparison: only runs when
   `depth.config.depth_enable` is true and the golden dump is non-empty
   (so the header-only captures are skipped silently, mirroring the RT0 guard).

The `Error [DepthDiff]:` prefix intentionally matches the `Error [PixelDiff]:`
style — neither starts with `Error:`, so `run_regression.py`'s failure check
(`line.lstrip().startswith('Error:')`) is unaffected.

## Verification

Per-case depth comparison (tolerance 0.01):

| case | golden depth px | matched | mismatched | missing | extra |
|------|---:|---:|---:|---:|---:|
| event104 | 40754 | 40754 | 0 | 0 | 0 |
| event351 | 988 | 988 | 0 | 0 | 0 |
| event399 | 2548 | 2548 | 0 | 0 | 0 |
| event516 | 98 | 88 | 0 | 10 | 43 |
| event28 | 0 (header-only) | — skipped — |
| event371 | 0 (header-only) | — skipped — |

Every present depth value matches golden (mismatched = 0 everywhere). event516's
missing/extra are coverage-edge differences (our fragment set vs the GPU's), not
depth-value errors. **Tolerance is honored:** forcing `depth_tolerance=1e-7`
surfaces sub-`1e-6` `DepthDiff` lines on event399, confirming the JSON value
flows through.

**Regression: 6/6 PASS** (`python run_regression.py`) — the new comparison adds
log output only and changes no pipeline behavior.

## Result

The pipeline now validates per-pixel output depth against
`diff_depth_output.csv` whenever the depth test is enabled, with a
JSON-configurable `depth_tolerance`, reported as its own log section alongside
the existing RT0 color/depth comparison.
