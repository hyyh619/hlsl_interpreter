# Step 90 — Compare pipeline statistics & output-merger pixels against golden

## Goal

The capture zips gained two new golden artifacts. The task:

1. Read golden pipeline statistics from `pipeline_statistics.csv`.
2. Compare them against the statistics `render.py` produces after a run.
3. On mismatch, print the offending counter. `VSInvocations` and `SamplesPassed`
   are the focus — mismatch on those is an **error**, every other counter is a
   **warning**.
4. Read `diff_ps_output_rt0.csv` (the DX11 output-merger result — per-pixel color
   and depth) and compare it against the pixels `_execute_pipeline` produces.
   Print any value that doesn't match.
5. Color/depth comparison needs a tolerance, configurable from the JSON config,
   defaulting to `0.01`.
6. Write this summary.

## Data formats discovered

**`pipeline_statistics.csv`** — header `RawCounter,Description,Value,,` then one row
per counter:

```
InputVerticesRead,Vertices read by the input assembler,204,,
VSInvocations,Vertex shader invocations,204,,
IAPrimitives,Primitives read by the input assembler,68,,
...
SamplesPassed,Samples that passed the depth/stencil test,0,,
```

Note: in these 3Dmigoto captures the rasterizer/PS counters
(`RasterizerInvocations`, `RasterizedPrimitives`, `PSInvocations`) are dumped as
`0` — they were not captured, not genuinely zero. `SamplesPassed` is the
meaningful depth/stencil survivor count.

**`diff_ps_output_rt0.csv`** — header `X,Y,B,G,R,A,Depth,Stencil`. **Channel order
is B,G,R,A** (not RGB), one row per output pixel:

```
312,228,0.196078,0.349020,0.498039,1.000000,0.996887,0
```

When a draw is fully occluded the file is just the header (e.g. event28).

## Design decisions

- **Mapping golden counters → our `pipeline_stats`** (`_PIPELINE_STAT_MAP` in
  `render.py`):
  | golden counter | our key |
  |---|---|
  | `InputVerticesRead`, `VSInvocations` | `vertices` (len of VS results) |
  | `IAPrimitives`, `RasterizerInvocations` | `primitives` |
  | `RasterizedPrimitives` | `not_culled` |
  | `PSInvocations` | `ps_pixels` |
  | `SamplesPassed` | `depth_passed` (rasterizer pixels − depth-failed) |

  Stages we don't emulate (HS/DS/GS/CS) have no mapping and are silently skipped
  rather than producing noise.

- **Error vs warning prefix, and the regression interaction.** The spec says
  print *"Error"* for `VSInvocations`/`SamplesPassed`. But `run_regression.py`
  gates a case as FAIL on any log line starting with `Error:` (the VS-vs-golden
  per-component channel). The interpreter currently over-produces pixels (no true
  depth occlusion / frustum culling), so `SamplesPassed` will not match golden
  yet — a literal `Error:` would flip all six regression cases to FAIL.

  Resolved (confirmed with the user): use a **distinct, non-gating prefix** —
  `Error [PipelineStats]:` / `Error [PixelDiff]:` for errors and
  `Warning [PipelineStats]:` for warnings. These are clearly error-level and
  greppable, but because they don't start with the bare token `Error:` the
  regression runner stays gated only on the VS-vs-golden channel. The pipeline
  inaccuracies are still surfaced loudly in the log.

- **One winner per (x, y).** Golden RT0 is post-output-merger: one value per
  pixel. The interpreter has no real depth occlusion, so multiple rendered pixels
  can land on the same (x, y). We collapse them keeping the **nearest** (smallest
  depth), which is what a standard `LESS` depth test would keep.

- **Tolerance.** New config key `pixel_tolerance` (default `0.01`) applied to both
  the max per-channel color diff and the depth diff.

- **Volume control.** Detailed `Error [PixelDiff]:` lines are capped at 50, then a
  `... (N more ... suppressed)` line, followed by a summary tallying
  matched / mismatched / missing / extra. (event104 has ~40 k golden pixels — we
  must not dump 40 k lines.)

## Implementation

All in `render.py`:

- `_fmt4()` — compact 4-decimal list formatter for logs.
- `_load_golden_pipeline_statistics(path)` → `{counter: int}`.
- `_compare_pipeline_statistics(golden, pipeline_stats, log)` — counter-by-counter,
  error on the two key counters, warning on the rest.
- `_load_golden_ps_output(path)` → `{(x,y): {'color':[r,g,b,a], 'depth':f}}`
  (re-orders the B,G,R,A capture to RGBA).
- `_compare_ps_output(golden, pixels, tolerance, log, max_report=50)` — collapses
  to one winner per pixel, compares color+depth within tolerance, caps detail
  lines, prints a summary.

`_execute_pipeline` reads `pixel_tolerance` from config and, after the existing
"Pipeline Statistics" block, loads each golden CSV (when present) and runs the two
comparisons against the final `pixels` list.

## Results / verification

`event371` (6 verts, golden `SamplesPassed=576`):

```
Pipeline Statistics vs Golden (pipeline_statistics.csv):
  OK  InputVerticesRead: output=6 golden=6
  OK  VSInvocations: output=6 golden=6
  Warning [PipelineStats]: IAPrimitives mismatch: output=4 golden=2 ...
  Warning [PipelineStats]: PSInvocations mismatch: output=1404 golden=0 ...
  Error  [PipelineStats]: SamplesPassed mismatch: output=1404 golden=576 ...

Output-Merger Pixels vs Golden (diff_ps_output_rt0.csv, tolerance=0.01):
  Error [PixelDiff]: (312,228) color out=[0.0065, 0.0078, 0.0039, 1.0000]
        golden=[0.4980, 0.3490, 0.1961, 1.0000] cdiff=[0.4915, 0.3413, 0.1922, 0.0000]
        | depth out=0.996572 golden=0.996887 ddiff=0.000315
  ... (495 more mismatch/missing line(s) suppressed)
  Golden pixels: 545 | matched: 0 | mismatched: 545 | missing: 0 | extra: 131
```

The comparison itself is correct. It surfaces two real, pre-existing pipeline
gaps (not regressions from this change):

- **`SamplesPassed`**: we keep every rasterized pixel (no depth occlusion / no
  near-far clipping), so 1404 vs golden 576.
- **Color**: depth lines up to ~3e-4 (perspective-correct interpolation is
  right), but PS output color is far darker than golden — a VS/PS lighting or
  texture-sampling discrepancy worth a follow-up.

`event28` (fully occluded draw): golden `SamplesPassed=0` and an empty
`diff_ps_output_rt0.csv`. The stats comparison reports the `SamplesPassed`
mismatch; the pixel comparison is skipped (empty golden → nothing to compare).

**Regression suite: 6/6 PASS** — confirming the new error-level lines do not trip
the `Error:` gate.

## Follow-ups (not in scope here)

- Implement back-face culling / depth occlusion so `SamplesPassed` converges.
- Investigate the PS color darkness vs golden (lighting / sampler state).
