# Step 114 — Texture2D.Load + DXBC split-vector-load hazard fix (unblocks event1399)

## Goal

Verify `witcher3_countryside_event1399` and fix any remaining block/error.
After step113 made the raw `.img` data available, event1399 was the last
case still failing (3921/5658 rows). Close it.

## 1. Starting state

```
Total PASSED rows: 3921/5658     (1737 fail — exactly the COLOR.w==0 vertices)
Error: 5211 lines
Depth: golden 1401 | matched 1379 | extra 10357   (massively over-drawing)
```

The 1737 failing vertices are those with `COLOR.w == 0`, whose final position is
taken entirely from the `t1.Load`-based reference path
(`r0 = lerp(r3_from_t1, r0_skinned, saturate(COLOR.w))`). Two distinct bugs.

## 2. Bug A — Texture2D.Load not implemented

`execute_method_call_node` only handled `.Sample`; `t1.Load(int3(x,y,mip))`
fell through to "Unknown method" → returned None, so `r3` was garbage.

Fix:
- `texture.py`: new `Texture.load(x, y, mip, texture_desc)` — integer texel
  fetch, no filtering/addressing, out-of-bounds → 0 (D3D Load semantics). The
  decoded grid is top-left origin, so `[y][x]` maps directly to texel coords.
- `hlsl_interpreter.py`: `execute_method_call_node` handles `Load` — evaluates
  the int3 location, resolves the texture binding, calls `Texture.load`.

After this: errors 5211 → 3474, over-draw 10357 → 68. Much closer, but the
1737 vertices were still off in `sv_position.x/y` by ~0.02–0.05.

## 3. Bug B — DXBC split-vector-load index hazard

`t1.Load(30)` was confirmed correct (matches the raw `.img`). Tracing `r3`
showed the bone matrix rows used a **corrupted index**. The shader (after the
4-bone skinning) does:

```hlsl
r1.x = t0[r1.x].val[32/4];     // line 179
r1.y = t0[r1.x].val[32/4+1];   // line 180 — reads the JUST-OVERWRITTEN r1.x!
r1.z = t0[r1.x].val[32/4+2];   // line 181
```

The DXBC disassembly shows this is a **single** instruction:
```
108: ld_structured_indexable(stride=64) r1.xyz, r1.x, l(32), t0.xyzx
```
i.e. the index `r1.x` is read **once** and all three components written
atomically. 3Dmigoto's HLSL decompiler expands it into three per-component
lines; read with strict sequential semantics, line 179's write to `r1.x`
corrupts the index for lines 180–181 (it indexed `t0[~0]` instead of the bone).

Fix — emulate the GPU's read-index-once behaviour: cache the resolved
structured-buffer index across a consecutive *burst* of loads that share the
same textual index expression (e.g. `t0[r1.x]`), so it is resolved once per
burst. The burst is reset (in the statement executor) as soon as a statement no
longer references that indexed load, so a legitimately-updated index is re-read.

This is safe for every other case: when the index register is **not** overwritten
mid-burst (the normal case, incl. all the skinning blocks), the cached value
equals the live value, so behaviour is unchanged.

Implementation (`hlsl_interpreter.py`):
- `self._sb_index_burst = {'token': 't0[r1.x]', 'value': idx}` (or None).
- get_value's StructuredBuffer branch reuses the cached index when the token
  matches, else resolves and caches it.
- `_execute_void_main` resets the burst per vertex and, before each statement,
  clears it when the statement no longer contains the burst token.

## 4. Result

```
Total PASSED rows: 5658/5658
0 "Error:" lines
Depth: golden 1401 | matched 1375 | missing 26 | extra 42   (1417 pixels)
```

event1399 now passes the gate. Residual 26 missing + 42 extra are the known
sub-pixel edge / PS alpha-cutout differences (non-gating `Error [DepthDiff]`).

## 5. Regression

Added `witcher3_countryside_event1399.zip` to the suite → **15/15 passed**
(the Texture2D.Load, structured-buffer index cache, and .img changes don't
regress any prior case).

## 6. Commit

Committed `texture.py` + `hlsl_interpreter.py` + this session doc.
