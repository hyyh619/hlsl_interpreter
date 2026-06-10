# Step 109 — event1341 clean pass (skinned mesh)

## Goal

Sixth case in the witcher3_countryside loop: `witcher3_countryside_event1341.zip`.

## 1. Run result

```
Loaded StructuredBuffer 't0' (t0): 614400 elements, stride 64B
Total PASSED rows: 1506/1506
0 "Error:" lines
Depth: golden 312 | matched 305 | missing 7 | extra 4   (309 pixels rendered)
```

## 2. Analysis

Skinned mesh with TEXCOORD + SV_Position outputs (StructuredBuffer bone palette,
array cbuffers, instanced transforms). Runs cleanly with the steps 104–106
infrastructure: VS output matches golden for all 1506 vertices.

Residual: 7 missing + 4 extra edge pixels out of 312 (~98% match) — the step102
sub-pixel coverage / top-left-rule boundary limitation. Non-gating
`Error [DepthDiff]` lines.

No code change required.

## 3. Regression

Added `witcher3_countryside_event1341.zip` → **12/12 passed**.

## 4. Commit

Committed this session doc (no source change).
