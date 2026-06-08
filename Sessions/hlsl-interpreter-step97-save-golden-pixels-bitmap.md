# Step 97 — Save the golden pixel colors as a bitmap

## Task

After `_execute_pipeline` finishes the pixel compare, directly save the **golden**
pixel colors to a bitmap image. The bitmap size is set from the viewport size.

This mirrors step 96 (which saved *our* output-merger pixels), but for the
reference data — so the golden RT0 image can be eyeballed next to our render.

## Thinking

### What's available

- **Golden pixel data** is already loaded for the compare:
  `_load_golden_ps_output('diff_ps_output_rt0.csv')` returns
  `{(x, y): {'color': [r, g, b, a], 'depth': f}}`. The loader re-orders the
  capture's `B,G,R,A` columns to RGBA, so `color` is RGBA in [0,1].
  **One color per screen pixel already** — no depth collapse needed (unlike our
  pixels, where several fragments can land on the same (x,y)).
- **Viewport size** is on the rasterizer: `rast.config.viewport` (`x`, `y`,
  `width`, `height`) — 640×480 at (0,0) for these captures.
- **BMP encoder** already exists from step 96 inside `_save_output_pixels_bitmap`
  — but it was inlined, not reusable.

### Design decisions

1. **Reuse the encoder.** Factored the 24-bit BI_RGB bottom-up BMP writer out of
   `_save_output_pixels_bitmap` into a shared `_write_bmp24(frame, w, h, path, log)`,
   plus a shared `_color_to_byte(v)` quantizer ([0,1] float → clamped 8-bit). Both
   the output and golden savers now build a `height × width` top-down frame of
   `(r,g,b)` triples and hand it to the same writer. No behavior change for step 96.
2. **No collapse for golden.** The golden dict is already keyed by (x,y), so the
   golden saver just iterates it directly — simpler than the output saver's
   nearest-depth collapse.
3. **Placement / size identical to step 96.** Image = viewport `w × h`; each
   golden pixel placed at `(x − viewport.x, y − viewport.y)`; outside-viewport
   entries dropped; untouched pixels black; BGR channel order; bottom-up rows
   (Y-flip). This guarantees the golden BMP and the output BMP are pixel-aligned
   for side-by-side comparison.
4. **Output path.** `golden_bitmap_path` from config (resolved relative to the
   config file) if set; otherwise `<zip-stem>_golden.bmp` next to the log file
   (falling back to the config dir) — exactly the step-96 convention, just a
   `_golden` suffix instead of `_output`.
5. **Where it runs.** Only inside the `if golden_ps:` block, right after
   `_compare_ps_output`. No golden data → no golden image (and the compare is
   skipped anyway).

## Implementation

### `render.py` — encoder refactor

- New `_color_to_byte(v)` — `[0,1]` float → clamped 8-bit int.
- New `_write_bmp24(frame, width, height, path, log)` — the step-96 BMP byte
  writer (BITMAPFILEHEADER + BITMAPINFOHEADER, padded bottom-up scanlines),
  now standalone. Returns True on success.
- `_save_output_pixels_bitmap` slimmed to build its frame and call the two
  helpers (identical output to step 96).

### `render.py` — `_save_golden_pixels_bitmap(golden, viewport, path, log)`

New helper: builds a viewport-sized top-down frame from the golden dict
(`golden[(x,y)]['color']` → BGR bytes at `(x−x0, y−y0)`), then calls
`_write_bmp24`. Logs path, dimensions, and pixel count.

### `render.py` — `_execute_pipeline`

After the `_compare_ps_output` call, resolve the golden output path
(`golden_bitmap_path` config / `<stem>_golden.bmp` beside the log) and call
`_save_golden_pixels_bitmap(golden_ps, rast.config.viewport, golden_bmp, log)`.

## Results

### Single-case run (event351, via Default.json)

```
Output-Merger Pixels vs Golden (diff_ps_output_rt0.csv, tolerance=0.15):
  Golden pixels: 988 | matched: 953 | mismatched: 2 | missing: 33 | extra: 25
Saved golden bitmap: ...event351_golden.bmp (640x480, 988 pixel(s) written)
Saved output-merger bitmap: ...event351_output.bmp (640x480, 980 pixel(s) written)
```

- **Size** matches the viewport (640×480) — correct.
- **988 pixels written** == the 988 golden pixels reported by the compare — every
  golden entry landed in the image.
- **File size** = 921654 bytes = 54 + 640·480·3 — header + padded pixel array.
- **Round-trips** through the project's own `Texture._parse_bmp_pixels`
  (480 rows × 640 cols × 4 ch) — a well-formed BMP.

### Regression suite — 6/6 PASS

```
PASS  ...event28.zip   passed 204/204
PASS  ...event104.zip  passed 1149/1149
PASS  ...event351.zip  passed 315/315
PASS  ...event371.zip  passed 6/6
PASS  ...event399.zip  passed 696/696
PASS  ...event516.zip  passed 3/3
```

Each case now also drops a 640×480 `_golden.bmp` (921654 bytes) into
`Cases/regression_logs/` alongside its `_output.bmp` and log.

## Config

New optional key in the run JSON:

```json
"golden_bitmap_path": "my_golden.bmp"
```

When omitted, the image is saved as `<zip-stem>_golden.bmp` next to the log file
(or the config file if no log path is configured).

## Files changed

- `render.py` — extracted `_color_to_byte()` and `_write_bmp24()` from
  `_save_output_pixels_bitmap()` (shared encoder); added
  `_save_golden_pixels_bitmap()`; call it in `_execute_pipeline` right after the
  golden PS-output compare, with a config-driven / log-relative output path.
