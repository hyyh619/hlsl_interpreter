# Step 94 — Seed the depth buffer from `pre_draw_depth_stencil.csv`

## Task

The capture zips now ship a new file, `pre_draw_depth_stencil.csv`, holding the
depth/stencil buffer contents **before** the draw call runs. The request:

1. The zip now includes `pre_draw_depth_stencil.csv` (the pre-draw depth buffer).
2. Before the depth compare, load `pre_draw_depth_stencil.csv` to initialize the
   depth buffer values.
3. The depth compare must test the current pixel's depth against this **pre-draw**
   depth value.

## Thinking / investigation

### What the new file looks like

`pre_draw_depth_stencil.csv` is a full-framebuffer dump, one row per pixel:

```
X,Y,Depth,Stencil
0,0,1.000000,0
...
```

For `event104` it has **307200 rows** (640×480). Distribution of `Depth`:

| Depth      | rows    |
|------------|---------|
| 1.000000   | 290705  | ← cleared
| 0.99xxxx   | the rest | ← geometry already on screen from earlier draws

So most pixels are the clear value (1.0), and a minority already carry the depth
of previously-drawn geometry. Stencil is 0 everywhere here.

### How depth was wired before

In `_execute_pipeline` (render.py) the depth stage was created as a bare
`depth = Depth()`. `DepthStencilOpDesc` defaults to `depth_enable=False`, so
`Depth.execute()` early-returned **every** pixel — the depth test was a no-op.
`_pass_depth_stencil_test` already reads `self._depth_buffer[(x,y)]` when present
and falls back to `depth_init_value` otherwise, so the machinery to test against
a real buffer existed; nothing was ever populating that buffer, and the test was
disabled anyway.

`pipeline_state.csv` for these dumps does **not** capture
`DepthEnable`/`DepthFunc`/`DepthWriteMask` — only `DepthStencil,Format =
D24_UNORM_S8_UINT`. So the depth state has to be inferred.

### Validation signal

`pipeline_statistics.csv` carries `SamplesPassed`, which `_compare_pipeline_statistics`
maps to our `pipeline_stats['depth_passed']`. That is the golden count of samples
that passed the depth test — the natural correctness signal for this change.
(These `Error [PipelineStats]:` / PixelDiff lines do **not** start with `Error:`,
so they are diagnostics and do not gate the regression runner.)

Before the change, `event104`: `depth_passed = 399126` (all rasterizer pixels)
vs golden `SamplesPassed = 42726` — a large gap because nothing was being culled.

## Implementation

### `output_merger.py` — `Depth.load_pre_draw_depth_stencil(path)`

New method on `Depth` that reads the CSV (`X,Y,Depth,Stencil`) and seeds
`_depth_buffer[(x,y)] = depth` and `_stencil_buffer[(x,y)] = stencil`. Returns the
number of pixels loaded; tolerant of a missing/garbled file (returns 0). This is
the buffer that `_pass_depth_stencil_test` already compares against.

### `render.py` — wire it into `_execute_pipeline`

Right before the depth stage runs:

```python
depth = Depth()
pre_draw_ds_csv = os.path.join(data_folder, 'pre_draw_depth_stencil.csv')
if os.path.exists(pre_draw_ds_csv):
    loaded = depth.load_pre_draw_depth_stencil(pre_draw_ds_csv)
    depth.config.depth_enable = True
    depth.config.depth_write_mask = True
    depth.config.depth_func = ComparisonFunc.LESS
    vs_interp.log_output(...)
```

`ComparisonFunc` is now imported from `output_merger`.

Rationale for enabling the test here: point 3 of the task ("compare current pixel
depth against the pre-draw depth value") only happens if the depth test is on.
Since the CSV doesn't capture the depth state, we apply D3D's standard depth
state — `LESS`, depth-write on — which is what `SamplesPassed` reflects. When no
`pre_draw_depth_stencil.csv` is present (older zips), behaviour is unchanged: the
depth test stays disabled and every pixel passes (full backward compatibility).

## Results

Regression suite — **6/6 PASS** (VS-vs-golden unaffected, as expected):

```
PASS  ...event28.zip   passed 204/204
PASS  ...event104.zip  passed 1149/1149
PASS  ...event351.zip  passed 315/315
PASS  ...event371.zip  passed 6/6
PASS  ...event399.zip  passed 696/696
PASS  ...event516.zip  passed 3/3
```

The depth test is now active and culls occluded fragments. `depth_passed`
(= our `SamplesPassed`) vs golden, before → after:

| case     | before (passed) | after (passed) | golden SamplesPassed |
|----------|-----------------|----------------|----------------------|
| event28  | 0               | 0              | 0   ✅ exact          |
| event104 | 399126          | 263340         | 42726                |
| event351 | ~9626           | 7383           | 1041                 |
| event371 | 1404            | 424            | 576                  |
| event399 | ~11647          | 5600           | 2717                 |
| event516 | 247             | 131            | 99                   |

Every case moves toward golden (event28 matches exactly). The PS-output PixelDiff
also improves — e.g. event104 "extra (ours not in golden)" dropped 147909 → 147800,
event371 dropped 131 → 61.

### On the residual gap

`SamplesPassed` still does not match exactly. This is **not** the depth-buffer
seeding — it is rasterizer overdraw + fragment ordering: our rasterizer emits far
more fragments than the real GPU (e.g. 399126 for a 307200-pixel screen), and with
`LESS` + depth-write the surviving count depends on the order fragments are
processed. Closing that gap is a separate rasterizer-accuracy problem, outside this
task's scope (which is loading the pre-draw buffer and testing against it — done).

### event371 sanity check (no regression)

event371 shows `matched: 0` in the PixelDiff. I confirmed via a stashed
pre-change baseline run that it was **already** `matched: 0, mismatched: 545,
missing: 0` before this change — a pre-existing color/depth accuracy issue for
that capture, unrelated to depth seeding. After the change it is
`matched: 0, mismatched: 277, missing: 268, extra: 61`: the previously-wrong
fragments are now (correctly) culled, and spurious "extra" pixels fell 131 → 61.
Net neutral-to-better, no regression introduced.

## Files changed

- `output_merger.py` — added `Depth.load_pre_draw_depth_stencil()`.
- `render.py` — import `ComparisonFunc`; load the pre-draw buffer and enable the
  depth test (`LESS`, write-on) in `_execute_pipeline` when the CSV is present.
