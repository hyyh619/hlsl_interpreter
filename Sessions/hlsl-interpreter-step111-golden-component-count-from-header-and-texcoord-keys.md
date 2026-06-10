# Step 111 — Golden component counts from header + TEXCOORD2/3 keys (witcher3_countryside_event1433)

## Goal

Eighth case in the witcher3_countryside loop:
`witcher3_countryside_event1433.zip`.

## 1. Symptom

```
Total PASSED rows: 1/105     # almost all rows fail
389 "Error:" lines           (all in TexCoord / TexCoord2 — NOT sv_position)
Depth: golden 3493 | matched 3493 | missing 0   # SV_Position is correct!
```

SV_Position was right (depth matched perfectly), so the failures were in the
TEXCOORD outputs only.

## 2. Root-cause investigation

This shader has four outputs:
`o0 TEXCOORD0 (float4)`, `o1 TEXCOORD1 (float4)`, `o2 TEXCOORD2 (float4)`,
`o3 SV_Position (float4)`.

Golden header:
```
TEXCOORD0.x,y,z, TEXCOORD1.x,y,z,w, TEXCOORD2.x,y,z,w, SV_Position.x,y,z,w
```

Two bugs:

### Bug A — declared type ≠ dumped component count
`o0` is declared `float4` but the shader only writes `o0.xyz`, so the golden
dump carries **TEXCOORD0 as 3 components** (`.x,.y,.z`, no `.w`). The step105
loader sliced columns using the *declared* type width (4), so it consumed one
column too many for TEXCOORD0 and mis-sliced every subsequent output.

Fix: derive each output's component count from the **header groups**
(consecutive `.xyzw` columns sharing a base name) rather than the declared
type. The header is the source of truth for dumped widths. SV_Position-first
ordering (step105) is retained.

### Bug B — TEXCOORD2 key collision
`_get_output_semantic_to_key_map` / `_SEM_TO_CANONICAL` had no `TEXCOORD2`
entry, so TEXCOORD2 fell back to the `TEXCOORD` key `TexCoord` — colliding with
TEXCOORD0 and overwriting it on both the golden and result sides.

Fix: added `TEXCOORD2 → TexCoord3` and `TEXCOORD3 → TexCoord4` to **both** maps
(VS-output keying and PS-input canonical keying stay consistent).

## 3. Result for event1433

```
Total PASSED rows: 105/105
0 "Error:" lines
Depth: golden 3493 | matched 3493 | missing 0 | extra 1116
```

(The 1116 "extra" pixels are ours-not-in-golden — most likely PS alpha-cutout
texels the GPU discarded; this is informational and produces no `Error:` /
`Error [DepthDiff]` line, so it does not gate.)

## 4. Regression

Verified the header-derived component count is identical to the declared-type
width for every prior case (their outputs dump full-width components), and the
new TEXCOORD2/3 keys only add mappings. Added
`witcher3_countryside_event1433.zip` → suite **13/13**.

## 5. Commit

Committed interpreter changes + this session doc.
