---
name: raw-img-texture-loading
description: Textures load from raw .img dumps decoded per DXGI format; BC formats fall back to .bmp
metadata:
  type: project
---

Case zips ship texture data as raw `.img` dumps (`<Stage>_slot_<slot>_res_<resid>_mip<m>_arr0.img`) alongside the old `.bmp` previews. Since step113 the loader prefers `.img` and decodes per the `Format` column in `texture_params.csv`:
- `texture.py`: `TextureDesc.FormatStr`; `_decode_raw_texels()` handles uncompressed formats (`_FMT_SPECS`: unorm8/snorm8/unorm16/float16/float32 × RGBA/BGRA/BGRX/RG/R/A) into the top-left RGBA-float grid (raw .img is row-major top-left — NO vertical flip, unlike BMP). `_load_level_pixels` falls back to the sibling `.bmp` for unsupported formats.
- `render.py`: `_collect_mip_paths` prefers `.img`; `_load_stage_textures` passes `Format` into `TextureDesc`.

Gotchas:
- Raw `R8G8B8A8_UNORM` .img is authoritative R,G,B,A order; the old BMP path had a latent **R/B swap** (now fixed by using .img).
- `R0G0B0A0_UNORM` is RenderDoc's label for **BC7** (16 B/block, not BC3) — block decode not implemented, falls back to the `.bmp`.
- PS-color comparison is `Error [PixelDiff]` = **non-gating** (only `Error:` + PASSED==total gate). So texture-decode changes don't affect the regression gate.
- event1399's t1 (R32G32B32A32_FLOAT) — the step110 lossy-BMP blocker — now loads exact floats from .img.

**event1399 is now fully PASSING (step114, 5658/5658).** Two more fixes were needed beyond .img: (1) `Texture2D.Load` (integer texel fetch) — `Texture.load()` in texture.py + `.Load` in `execute_method_call_node`; (2) a **DXBC split-vector-load hazard** — `r1.xyz = t0[r1.x].val[32]` is one `ld_structured` (index read once) but 3Dmigoto decompiles it into per-component lines where `r1.x = t0[r1.x]…` corrupts the index. Fix: cache the structured-buffer index across a consecutive burst of loads sharing the same textual index (`self._sb_index_burst`), reset per-statement in the executor. Regression is now 15/15.

Related: [[structured-buffer-skinning-support]].
