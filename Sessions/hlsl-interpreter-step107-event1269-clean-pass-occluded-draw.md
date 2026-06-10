# Step 107 — event1269 clean pass (fully-occluded skinned draw)

## Goal

Fourth case in the witcher3_countryside loop: `witcher3_countryside_event1269.zip`.

## 1. Run result

```
Loaded StructuredBuffer 't0' (t0): 614400 elements, stride 64B
Total PASSED rows: 1509/1509
0 "Error:" lines
0 "Error [DepthDiff]" lines
Output/depth bitmaps skipped: pipeline produced no pixels.
  OK  PSInvocations:   output=0 golden=0
  OK  SamplesPassed:   output=0 golden=0
```

## 2. Analysis

event1269 is another skinned mesh using `StructuredBuffer<t0_t> t0` and array
cbuffers — the exact feature set added in steps 104–106. With those fixes in
place it ran cleanly on the first attempt:

- VS output matches golden for all 1509 vertices.
- The draw produces **no visible pixels**, and the golden capture agrees:
  `SamplesPassed golden=0`, `PSInvocations golden=0`. The geometry is fully
  occluded by pre-existing depth (the pre-draw depth buffer rejects every
  fragment), so 0 output pixels is the *correct* result, not a gap. The depth
  comparison reports no golden depth pixels, hence 0 DepthDiff lines.

No code change was required — this case is covered by the StructuredBuffer +
array-cbuffer + instanced-input + golden-column-order work already committed.

## 3. Regression

Added `witcher3_countryside_event1269.zip` to the suite → **10/10 passed**.

## 4. Commit

Committed this session doc (no source change).
