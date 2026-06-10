# Step 105 — Golden VS-mesh column order: SV_Position is dumped first (witcher3_countryside_event907)

## Goal

Second case in the witcher3_countryside loop: `witcher3_countryside_event907.zip`.
Run, find/fix errors, keep regression green, commit, document.

## 1. Symptom

```
Total PASSED rows: 0/13680
Error: 82080 "Error:" lines
```

Sample (row 0):

```
Error: Row 0 TexCoord[0]:   output=0.000000  golden=-0.838130
Error: Row 0 TexCoord[1]:   output=0.000000  golden=-0.901420
Error: Row 0 sv_position[0]: output=-0.838269 golden=0.484680
Error: Row 0 sv_position[1]: output=-0.901458 golden=1.000000
Error: Row 0 sv_position[2]: output=0.484790  golden=0.000000
Error: Row 0 sv_position[3]: output=1.000000  golden=0.000000
```

Yet the depth comparison **matched 1513/1519** (~99%). A genuinely wrong
SV_Position cannot match the golden depth buffer — so the VS output was not
actually wrong. Something about the *comparison* was off.

## 2. Root-cause investigation

This shader declares its outputs as `out float2 o0 : TEXCOORD0` **then**
`out float4 o1 : SV_Position0`, and the body literally sets `o0.xy = float2(0,0)`
— so our `TexCoord = (0,0)` is correct, and our `SV_Position` is correct (it
reproduces the golden depth at the right screen pixels).

The golden `*_vs_mesh.csv`:

```
header: VTX,IDX,TEXCOORD.x,TEXCOORD.y,SV_Position.x,SV_Position.y,SV_Position.z,SV_Position.w
row 0 : 0,2551,-0.83813,-0.90142,0.48468,1.00000,0.00000,0.00000
```

Our correct VS output row 0: `SV_Position=(-0.838,-0.901,0.485,1.0)`,
`TexCoord=(0,0)`. So the golden **data** columns are:

```
[-0.838, -0.901, 0.485, 1.0, 0.0, 0.0]  ==  [SV_Position.xyzw, TexCoord.xy]
```

but the **header** labels them `[TEXCOORD.xy, SV_Position.xyzw]`.

**Root cause:** RenderDoc's VS-output mesh export lays the data out with the
**SV_Position attribute first**, then the remaining outputs in declared order —
even though the column header names follow declaration order. When SV_Position
is *not* output register 0, the header labels are shifted relative to the data.
`load_vs_golden_from_mesh_csv` mapped golden columns purely by header name, so
it read the (position) data under the `TEXCOORD.*` labels as TexCoord and the
(next) data under `SV_Position.*` as position → everything mis-compared.

The Collision suite never hit this because its SV_Position is already output
register 0, so header order == data order.

## 3. Fix

Rewrote `load_vs_golden_from_mesh_csv` to assign the physical component columns
**positionally** in `[SV_Position, then remaining outputs in declared order]`,
instead of trusting header labels:

1. Collect physical component-column indices (those ending in `.x/.y/.z/.w`).
2. Reorder `vs_output_params` with the SV_Position param first.
3. Walk the params, giving each a contiguous slice of physical columns sized by
   its type's component count.

When SV_Position is already first (Collision suite), this produces the exact
same column assignment as before — a no-op. For event907 it correctly maps the
first 4 data columns to SV_Position and the last 2 to TexCoord.

## 4. Result for event907

```
Total PASSED rows: 13680/13680
0 lines starting with "Error:"
Depth: golden 1519 | matched 1513 | missing 6 | extra 3
```

Residual: 6 missing + 3 extra edge-pixel `Error [DepthDiff]` (the step102
sub-pixel coverage limitation, non-gating).

## 5. Regression

Added `witcher3_countryside_event907.zip` to the suite. Result:

```
8/8 passed   (6 Collision + event895 + event907)
```

No regression — the positional mapping is identical to the previous
header-based mapping for every case whose SV_Position is output register 0.

## 6. Commit

Committed interpreter change + this session doc.
