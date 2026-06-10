# Step 104 — Array cbuffers & per-instance vertex inputs (witcher3_countryside_event895)

## Goal

Run the new witcher3_countryside capture cases listed in
`Cases/witcher3_countryside_zip_files.csv`, find and fix any pipeline errors,
keep the regression suite green, and commit each fix. This step covers the
first case: `witcher3_countryside_event895.zip`.

## 1. Symptom

First run crashed before producing any output:

```
File "hlsl_interpreter.py", line 2668, in load_cbuffer_data_from_csv
    self.log_output(f"  {f.name} ({ft}): [{', '.join(f'{v:.5f}' for v in data)}]")
TypeError: 'NoneType' object is not iterable
```

After the crash was fixed, the VS output was completely wrong:

```
Error: Row 0 sv_position[0]: output=83.421631 golden=0.093390 diff=83.328241
Error: Row 0 sv_position[1]: output=-34.681599 golden=0.359850 diff=35.041449
...
Total PASSED rows: 0/4428
```

## 2. Root-cause investigation (whole-pipeline analysis)

This capture is the first one to use **two features never exercised before**:

### (a) Array constant buffers

`VS_shader.hlsl` declares cbuffers as arrays, not matrices:

```hlsl
cbuffer cb2 : register(b2) { float4 cb2[10]; }
cbuffer cb1 : register(b1) { float4 cb1[4]; }
```

`VS_constant_buffers.csv` stores the elements with a `<name>_v<index>` naming:
`cb1_v0, cb1_v1, …`. Three gaps:

1. **`parse_cbuffer`** split `float4 cb1[4]` into `field_name = "cb1[4]"` — the
   `[4]` was never stripped, and no array length was recorded.
2. **`load_cbuffer_data_from_csv`** only understood matrix `.row` names and
   plain scalars, so it never matched the `cb1_v0` element rows. `field.data`
   stayed `None`.
3. **`get_value`** had no array-subscript path, so `cb1[0].yyyy` could not be
   resolved even once the data was loaded. (The crash itself was in the cbuffer
   logging code trying to iterate the `None` data.)

Prior cases only ever used `float4x4` matrix cbuffers (`.row0`-style), so array
cbuffers had simply never been implemented.

### (b) Instanced vertex inputs

`main()` takes per-instance inputs:

```hlsl
float4 v1 : INSTANCE_TRANSFORM0,
float4 v2 : INSTANCE_TRANSFORM1,
float4 v3 : INSTANCE_TRANSFORM2,
float4 v4 : INSTANCE_LOD_PARAMS0,
uint   v5 : SV_InstanceID0,
```

`ia_input_layouts.csv` shows these come from a **separate per-instance vertex
buffer (input slot 7, `PerInstance=True`)**, while `ia_vertex_data.csv` only
contains the per-vertex POSITION stream (slot 0):

```
1,INSTANCE_TRANSFORM0,R32G32B32A32_FLOAT,4,4,7,ResourceId::20417,0,True,1
...
VertexBuffer slot 7: ResourceId::20417  ByteOffset=1727552  ByteStride=64
```

`load_ia_vertex_data` therefore left `v1/v2/v3/v4` at zero. With the instance
transform zeroed, the VS math collapses to
`o0 = (cb1[0].w, cb1[1].w, cb1[2].w, cb1[3].w)` — exactly the wrong values
observed (`83.42, -34.68, -0.46, 1.0`), confirming the diagnosis.

**Key detail:** the instance data does **not** start at byte 0 of the binary.
The bound `ByteOffset` for slot 7 is `1727552`. Reading from offset 0 produced
plausible-but-wrong transforms (y completely off); reading at the bound offset
reproduced golden to within tolerance. Verified offline before coding:

```
computed o0: [0.09324, 0.35981, 0.49656, 1.0]
golden     : [0.09339, 0.35985, 0.49645, 1.0]   # diff only from CSV rounding
```

## 3. Fix

`hlsl_interpreter.py`:

- `FieldDefinition`: added `array_size: int = 0`.
- `parse_cbuffer`: strip `[N]` from the field name and record `array_size`.
- `load_cbuffer_data_from_csv`: for array fields, gather `<name>_v0..v{N-1}`
  rows into a list-of-vectors (same shape as a matrix).
- cbuffer logging: print array fields element-by-element (this was the crash
  site).
- `get_value`: new array-subscript branch resolving `cb1[0]`, `cb2[8].xyz`,
  `arr[i].w` against local vars, globals, and cbuffer fields.
- New `load_per_instance_data(...)`: parses `ia_input_layouts.csv` (element +
  vertex-buffer-binding sections), and for each `PerInstance=True` element
  decodes instance 0 from `vb_slot{N}_res_{resid}.bin` at
  `binding.ByteOffset + instance*stride + element.ByteOffset`, mapping it to
  the VS input param by semantic. Helpers `_decode_vertex_element` (FLOAT /
  half / UNORM / UINT formats), `_half_to_float`, `_extract_resid`. Sets
  `SV_InstanceID` to the instance index.
- Added `import struct`.

`render.py`:

- After `load_ia_vertex_data`, call `load_per_instance_data(...)` and merge the
  instance-0 inputs into every vertex dict.

**Assumption documented in code:** single-instance draws (all vertices share
instance 0). All witcher3_countryside cases have `golden rows == vertex rows`,
confirming a single instance. Multi-instance draws are not yet modelled.

## 4. Result for event895

```
Total PASSED rows: 4428/4428      # VS output now matches golden exactly
0 lines starting with "Error:"
Output-merger: 403 pixels written; Golden depth 406 | matched 393 | missing 13 | extra 10
```

The only residual diffs are `Error [DepthDiff]` lines — 13 missing + 10 extra
edge pixels (~97% match) lying along a single near-diagonal triangle edge, with
MSAA disabled. These are the sub-pixel coverage / top-left-rule boundary
limitation already investigated and accepted in step102, **not** the
per-component VS error this task targets. The project's regression gate
(`run_regression.py`) intentionally counts only `Error:` (colon) lines and
`PASSED == total`, so DepthDiff lines are informational and non-gating.

## 5. Regression

Added `witcher3_countryside_event895.zip` to
`Cases/regression_test_zip_files.csv`. Full suite:

```
7/7 passed
  ... all 6 Collision cases unchanged ...
  PASS  witcher3_countryside_event895.zip  passed 4428/4428
```

No existing case regressed; the array-cbuffer and instance-loading changes are
backward compatible (matrix cbuffers and non-instanced draws are untouched).

## 6. Commit

Committed as a single fix covering the interpreter + render wiring + regression
list + this session doc.
