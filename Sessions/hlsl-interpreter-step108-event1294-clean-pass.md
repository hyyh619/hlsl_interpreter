# Step 108 — event1294 clean pass (skinned mesh)

## Goal

Fifth case in the witcher3_countryside loop: `witcher3_countryside_event1294.zip`.

## 1. Run result

```
Loaded StructuredBuffer 't0' (t0): 614400 elements, stride 64B
Total PASSED rows: 3876/3876
0 "Error:" lines
Depth: golden 22 | matched 21 | missing 1 | extra 0   (21 pixels rendered)
```

## 2. Analysis

Another skinned mesh (StructuredBuffer bone palette + array cbuffers +
instanced transforms). Runs cleanly with the steps 104–106 infrastructure:
VS output matches golden for all 3876 vertices.

The only residual is **1 missing depth pixel** at (545,344) — a single edge
pixel, the step102 sub-pixel coverage / top-left-rule boundary limitation.
This is a non-gating `Error [DepthDiff]` line, not a per-component VS error.

No code change required.

## 3. Regression

Added `witcher3_countryside_event1294.zip` → **11/11 passed**.

## 4. Commit

Committed this session doc (no source change).
