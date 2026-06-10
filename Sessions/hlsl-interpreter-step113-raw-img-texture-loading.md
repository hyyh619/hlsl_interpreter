# Step 113 — Load textures from raw .img data (format-aware) instead of BMP

## Task

The case zips now ship texture data as **raw `.img` files** (alongside the old
`.bmp` previews). Change texture loading to use the raw `.img` data, decoding it
according to each texture's DXGI **format** (from `texture_params.csv`).

## 1. Investigation

The zips now contain, per mip/array slice, both
`<Stage>_slot_<slot>_res_<resid>_mip<m>_arr0.bmp` **and** `…_mip<m>_arr0.img`.
The `.img` is the raw, untransformed texel dump; the `.bmp` is RenderDoc's
8-bit-per-channel preview.

Texture formats present across the cases (`texture_params.csv` `Format` column):

| Format | Where | Notes |
|---|---|---|
| `R8G8B8A8_UNORM` | Collision PS, witcher PS slot1/2 | uncompressed, 4 B/texel |
| `R32G32B32A32_FLOAT` | witcher3 event1399 VS t1 | uncompressed, 16 B/texel |
| `R0G0B0A0_UNORM` | witcher3 event1433/1450 PS slot0 | **block-compressed** (16 B/block → BC7; RenderDoc shows 0-bit channels) |

Two findings that shaped the design:

- **R8G8B8A8 `.img` vs BMP differ by an R/B swap.** For event28 PS t0, the raw
  `.img` bytes at texel (0,0) are `(8,10,11,255)` = R,G,B,A per the format,
  while the existing BMP path decoded them as `(11,10,8,255)` (R/B swapped).
  The raw `.img` is authoritative — using it fixes a latent channel swap.
- **`R0G0B0A0_UNORM` is BC7, not BC3.** mip0 of the 128×256 texture is
  32768 B = 16 B/block. A trial BC3 decode produced nonsensical alpha, so the
  block format is BC7 (also 16 B/block). BC7 decoding is large/complex and
  these are PS-only (non-gating) textures, so raw decode is deferred and the
  loader falls back to the captured `.bmp` (RenderDoc's correct decode) for
  block-compressed formats.

## 2. Implementation

`texture.py`:

- `TextureDesc` gains `FormatStr` (raw DXGI format name).
- New `_decode_raw_texels(data, fmt, w, h)`: decodes uncompressed formats from
  a `_FMT_SPECS` table (component type × channel order) into the existing
  top-left-origin RGBA-float grid. Supports unorm8/snorm8/unorm16/float16/
  float32 and RGBA/BGRA/BGRX/RG/R/A channel orders. Returns `None` for
  unsupported (block-compressed) formats.
- `_load_img_pixels` reads a `.img` and decodes it; `_load_level_pixels` routes
  `.img → raw decode`, falling back to the **sibling `.bmp`** when the raw
  format is unsupported. `_load_mip_levels` computes each level's dimensions by
  halving the base (ViewFirstMip = 0 for all captures) and decodes per level.
- Raw `.img` is row-major top-left origin (no vertical flip, unlike BMP).

`render.py`:

- `_collect_mip_paths` now prefers `.img` per mip level, falling back to `.bmp`.
- `_load_stage_textures` passes the `Format` string into `TextureDesc`.

## 3. Verification

```
# Collision event28 PS t0 (R8G8B8A8_UNORM) — raw .img, correct channel order
slot0 (0,0) decoded RGBA: [0.0314, 0.0392, 0.0431, 1.0]   (== 8,10,11 / 255)

# event1433 PS slot0 (R0G0B0A0 / BC7) → falls back to sibling .bmp  (ok)
# event1433 PS slot1/2 (R8G8B8A8) → raw .img  (ok)

# event1399 VS t1 (R32G32B32A32_FLOAT) — now EXACT floats from .img:
t1 texel(0,0) RGBA: [0.5549, 0.0959, 1.3417, 1.0]   # values > 1.0, impossible in the 8-bit BMP
```

Regression suite: **14/14 passed** — no gating regression (PS-color changes are
reported as non-gating `Error [PixelDiff]`, and VS-stage validation is
unaffected for every passing case).

## 4. Side effect: event1399 data limitation lifted

Step110 documented event1399 as blocked because its `t1` SRV
(`R32G32B32A32_FLOAT`) was only dumped as a lossy 8-bit BMP. The capture now
ships `VS_slot_1_res_113813_mip0_arr0.img` (1296 B = 81×16) and the loader reads
the exact floats. The remaining piece to fully pass event1399 is interpreter
support for **`Texture2D.Load`** in the VS stage (integer texel fetch); the data
is no longer the blocker.

## 5. Commit

Committed `texture.py` + `render.py` + this session doc.
