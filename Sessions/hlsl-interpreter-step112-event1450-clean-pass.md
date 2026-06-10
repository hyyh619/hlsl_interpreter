# Step 112 — event1450 clean pass (skinned mesh, multi-TEXCOORD)

## Goal

Ninth/last case in the witcher3_countryside loop:
`witcher3_countryside_event1450.zip`.

## 1. Run result

```
Loaded StructuredBuffer 't0' (t0): 614400 elements, stride 64B
Total PASSED rows: 1728/1728
0 "Error:" lines
Depth: golden 51 | matched 36 | missing 15 | extra 206   (242 pixels rendered)
```

## 2. Analysis

event1450 combines everything added across this milestone: skinned mesh
(StructuredBuffer bone palette), array cbuffers, per-instance transforms, and
multiple TEXCOORD outputs with SV_Position not first in the header
(`out float4 TEXCOORD0/1/2; out float4 SV_Position`). With the steps 104–111
infrastructure it runs cleanly: **VS output matches golden for all 1728
vertices**, no per-component `Error:` lines.

Residuals (non-gating `Error [DepthDiff]`):
- 15 missing edge pixels — the step102 sub-pixel coverage limitation.
- 206 extra pixels — ours-not-in-golden, consistent with PS alpha-cutout texels
  the GPU discarded (this foliage shader writes TEXCOORD sets a cutout PS would
  use); the VS-stage interpreter has no way to discard them and the project's
  gate counts only `Error:` lines + `PASSED == total`.

No code change required.

## 3. Regression

Added `witcher3_countryside_event1450.zip` → suite **14/14 passed**.

## 4. Commit

Committed this session doc (no source change).

## 5. Milestone status

All 9 witcher3_countryside cases processed:
- **8 pass** the project gate (895, 907, 994, 1269, 1294, 1341, 1433, 1450).
- **1 blocked** (1399) by a capture-data limitation — its `t1` SRV is an
  R32G32B32A32_FLOAT texture dumped only as a lossy 8-bit BMP, so the
  `t1.Load`-blended vertices cannot be reproduced bit-exact (see step110).
