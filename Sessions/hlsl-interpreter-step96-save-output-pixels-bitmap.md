# Step 96 — Save the output-merger pixel colors as a bitmap

## Task

After `_execute_pipeline` finishes the pixel compare, save the final output
Pixel colors directly to a bitmap image. The bitmap size is set from the
viewport size.

## Thinking

### What's available

- **Viewport size** lives on the rasterizer: `rast.config.viewport` has `x`,
  `y`, `width`, `height` (loaded from `pipeline_state.csv`'s `Viewport,*` rows).
  For these captures it's 640×480 at origin (0,0).
- **Pixel color**: `Pixel.ps_output_color` (the PS output, post output-merger
  clamp) with `Pixel.color` (interpolated) as a fallback. Colors are floats,
  clamped to [0,1] for UNORM targets.
- **No BMP writer existed** — `texture.py` only *parses* BMPs
  (`_parse_bmp_pixels`). So I had to write an encoder.

### Design decisions

1. **Size = viewport.** Image is `int(round(width)) × int(round(height))`.
   Pixels are placed at `(x − viewport.x, y − viewport.y)`; anything outside the
   viewport rectangle is dropped. Untouched pixels stay black.
2. **One winner per (x,y): nearest depth wins.** The interpreter keeps every
   surviving fragment, so a screen pixel can have several. I collapse with
   `p.depth < prev.depth`, identical to `_compare_ps_output`'s golden collapse,
   so the saved image matches what the depth test would keep.
3. **Color source / quantization.** `ps_output_color or color or black`, clamp
   to [0,1], `round(v*255)` to 8-bit, written **BGR** (BMP channel order).
4. **24-bit BI_RGB BMP, bottom-up.** Standard 54-byte header
   (`BITMAPFILEHEADER` + `BITMAPINFOHEADER`), each scanline padded to a 4-byte
   boundary. BMP stores rows bottom-up, so the last screen row (`py = height−1`)
   is written first — this is the Y-flip.
5. **Output path.** `output_bitmap_path` from config if set (resolved relative
   to the config file). Otherwise `<zip-stem>_output.bmp` written **next to the
   log file** (so headless/regression runs keep the image beside the case log
   rather than cluttering `Cases/`), falling back to the config dir when no log
   path is set. The data_folder is a temp dir that gets deleted, so the image
   must be written somewhere persistent.

## Implementation

### `render.py` — `_save_output_pixels_bitmap(pixels, viewport, path, log)`

New helper: collapses pixels (nearest-depth), builds a top-down `height × width`
frame of `(r,g,b)` byte triples, then writes a 24-bit bottom-up BMP. Returns
True on success; logs the path, dimensions, and count of pixels written.

### `render.py` — `_execute_pipeline`

After the golden PS-output compare, resolve the output path and call the helper:

```python
out_bmp = config.get('output_bitmap_path', '')
if out_bmp:
    out_bmp = out_bmp if os.path.isabs(out_bmp) else os.path.join(config_dir, out_bmp)
else:
    stem = os.path.splitext(os.path.basename(config.get('data_path','')))[0] or 'output'
    out_dir = os.path.dirname(log_file_path) if log_file_path else config_dir
    out_bmp = os.path.join(out_dir, f"{stem}_output.bmp")
_save_output_pixels_bitmap(pixels, rast.config.viewport, out_bmp, vs_interp.log_output)
```

## Results

### Single-case run (event104)

```
Saved output-merger bitmap: ...event104_output.bmp (640x480, 188506 pixel(s) written)
```

- **Size** matches the viewport (640×480) — correct.
- **File size** = 921654 bytes = 54 + 640·480·3 — header + padded pixel array,
  exactly right.
- **Round-trips** through the project's own `Texture._parse_bmp_pixels`
  (480 rows × 640 cols × 4 ch), confirming a well-formed BMP.

### Orientation / placement check

Spot-compared BMP pixels against the golden `diff_ps_output_rt0.csv` colors at
matching `(x,y)`. The hues and locations agree (e.g. `(103,286)` ours orange
`[0.69,0.231,0.11]` vs golden orange `[0.953,0.322,0.149]`), confirming the
Y-flip and placement are correct — a wrong flip would scatter unrelated colors.
The magnitude differences are the **pre-existing** PS-interpretation accuracy
gap for this capture (documented in step 94's PixelDiff), not a bitmap defect:
the bitmap faithfully renders whatever the `pixels` already hold.

### Regression suite — 6/6 PASS

```
PASS  ...event28.zip   passed 204/204
PASS  ...event104.zip  passed 1149/1149
PASS  ...event351.zip  passed 315/315
PASS  ...event371.zip  passed 6/6
PASS  ...event399.zip  passed 696/696
PASS  ...event516.zip  passed 3/3
```

Each case now also drops a 640×480 BMP (921654 bytes) into
`Cases/regression_logs/` alongside its log.

## Config

New optional key in the run JSON:

```json
"output_bitmap_path": "my_render.bmp"
```

When omitted, the image is saved as `<zip-stem>_output.bmp` next to the log file
(or the config file if no log path is configured).

## Files changed

- `render.py` — added `_save_output_pixels_bitmap()` (24-bit BMP encoder +
  nearest-depth collapse); call it at the end of `_execute_pipeline` after the
  pixel compare, with a config-driven / log-relative output path.
