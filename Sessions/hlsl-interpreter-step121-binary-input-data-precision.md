# Step 121 — Binary input data precision: VB/IB/cbuffer/texture binary loading

## Goal

Upgrade the pipeline's input-data loading to use binary files captured by
RenderDoc/3Dmigoto instead of the rounded CSV representations, achieving
full float32 precision for vertex buffers, constant buffers, and textures.

The four changes requested:

1. **Cannot load from parsed CSV files** — `**_constant_buffer.csv` and
   `ia_vertex_data.csv` give only rounded decimal text; binary gives exact bits.
2. **Vertex/Index buffer** — read `ib_res_{id}.bin` / `vb_slot{N}_res_{id}.bin`
   directly using `ia_input_layouts.csv` for resource IDs and formats.
3. **Constant buffers** — use `**_constant_buffer_info.csv` → load
   `constant_{id}.bin`, use `**_constant_buffers.csv` for field layout.
4. **SRV/RTV/UAV textures** — use `.img` files instead of `.bmp`.

---

## Implementation (carried over from previous session)

All four changes were implemented in the previous session context.  This
session focused on fixing regressions revealed by the regression suite.

### Files changed

- **`hlsl_interpreter.py`** — `_decode_vertex_element`, `override_cbuffers_from_binary`
- **`render.py`** — vertex loading block, CSV-baseline + binary-override strategy

---

## Bug fixes applied in this session

### Fix 1 — SNORM vertex format decode

**Symptom**: TEXCOORD10 / TEXCOORD11 all zeros for Witcher event1854 (ATTRIBUTE1/2
attributes use `R8G8B8A8_SNORM`).

**Root cause**: `_decode_vertex_element` fell to the float32 else-branch for SNORM
formats, then called `struct.unpack_from('<4f', raw)` on only 4 bytes →
`struct.error` → returned `[0, 0, 0, 0]`.

**Fix** (in `_decode_vertex_element`, after the UNORM block):

```python
elif 'SNORM' in fmt_u and comp_byte_width == 2:
    ints = struct.unpack_from(f'<{comp_count}h', raw)
    vals = [max(-1.0, v / 32767.0) for v in ints]
elif 'SNORM' in fmt_u and comp_byte_width == 1:
    ints = struct.unpack_from(f'<{comp_count}b', raw)
    vals = [max(-1.0, v / 127.0) for v in ints]
```

---

### Fix 2 — float4x4 cbuffer stored column-major, must transpose to row-major

**Symptom**: All six Collision cases failed — world transform not applied.
Log showed `o4.xyz = World._m03_m13_m23 + r0.xyz => ['1664.0000', ...]`
(translation column was zeros instead of `[-1350, -130, -1400]`).

**Root cause**: D3D11 HLSL stores `float4x4` in cbuffer registers **column-major**:
`decoded[ri+k]` is column k (each a `float4`).  `override_cbuffers_from_binary`
was storing them as rows, so `matrix[row][col]` lookups returned wrong values.

The `_mRC` accessor notation confirms this: `_m03` means row 0, column 3 —
which is the translation column in a standard world matrix.

**Fix** (in `override_cbuffers_from_binary`, base==64 branch):

```python
if base == 64:      # float4x4
    if ri + 3 < len(decoded):
        # D3D HLSL stores float4x4 column-major in cbuffer registers:
        # decoded[ri+k] is column k.  The interpreter accesses elements
        # as matrix[row][col], so transpose to row-major here.
        cols = [decoded[ri], decoded[ri+1],
                decoded[ri+2], decoded[ri+3]]
        field.data = [[cols[c][r] for c in range(4)] for r in range(4)]
```

---

### Fix 3 — CSV baseline + binary override (missing fallback for slots with no binary)

**Symptom**: event1341 and event1399 (Witcher) — NORMAL, TANGENT, COLOR, TEXCOORD1
were all zeros; bone skinning broke because BLENDINDICES were wrong.

**Root cause**: The vertex loading block started with empty dicts and only applied
binary overrides.  VB slots 2 and 3 had `ResourceId::0` (no binary file), so
those attributes remained empty; the binary-only path had no fallback.

**Fix** (in `render.py`, vertex loading block): Load CSV first as the baseline,
then overlay binary overrides with `dict.update()`:

```python
csv_vertex_data = vs_interp.load_ia_vertex_data(ia_vertex_csv, vs_input_params)
# ... re-index csv_vertex_data to match idx_list ...
bin_overrides = vs_interp.load_per_vertex_binary_data(
    ia_layouts_csv, data_folder, vs_input_params, idx_list,
    csv_vertex_data=None,
)
for i, ov in enumerate(bin_overrides):
    if ov and i < len(vertex_data):
        vertex_data[i].update(ov)
```

---

### Fix 4 — UINT/SINT 1-byte and 2-byte vertex format decode

**Symptom**: BLENDINDICES (bone indices, `R8G8B8A8_UINT`) returned zeros in
binary override, overwriting correct CSV values → wrong skinning.

**Root cause**: `_decode_vertex_element` handled UINT/SINT only for 4-byte
components (`comp_byte_width == 4`).  `R8G8B8A8_UINT` has `comp_byte_width=1`;
it fell to the float32 else-branch → `struct.error` → `[0, 0, 0, 0]`.

**Fix** (in `_decode_vertex_element`, after the 4-byte UINT/SINT block):

```python
elif ('UINT' in fmt_u or 'SINT' in fmt_u) and comp_byte_width == 2:
    code = 'h' if 'SINT' in fmt_u else 'H'
    vals = list(struct.unpack_from(f'<{comp_count}{code}', raw))
elif ('UINT' in fmt_u or 'SINT' in fmt_u) and comp_byte_width == 1:
    code = 'b' if 'SINT' in fmt_u else 'B'
    vals = list(struct.unpack_from(f'<{comp_count}{code}', raw))
```

---

## Regression results

| Phase | Pass / Total | Notes |
|-------|-------------|-------|
| Before this session | 24 / 46 | IB bug present |
| After IB fix + SNORM | 37 / 46 | |
| After float4x4 + UINT | 42 / 46 | |
| After CSV fallback | ~43–44 / 46 | event1341 verified 1506/1506 manually |
| Final clean run | **45 / 46** | only event399 fails (pre-existing) |

### Known remaining failures (pre-existing)

**event1450** — passes 1728/1728 after UINT decode fix (was partially failing in
intermediate states due to the binary override zeroing bone indices).

**Collision-event399** — Color output = 0:
- `o1.xyz = r0.xyz * r1.www` — `r1.w` is uninitialized (only `r1.xyz` was set)
- `r2.xyzw = -(ColorMaterialMode == int4(1,5,2,3))` — integer vector comparison
  semantics may differ between interpreter and GPU
- These are pre-existing interpreter limitations, not regressions from this session.

---

## Key design notes for future reference

### D3D11 cbuffer matrix storage

`float4x4` in a cbuffer occupies 4 consecutive register slots.  The GPU stores
matrices **column-major**: register k holds column k of the matrix.

The HLSL `_mRC` notation is row R, column C (0-indexed).  So `World._m03` is
row 0, column 3 — the X translation in a standard TRS world matrix.

When loading from binary, always transpose: `field.data[row][col] = cols[col][row]`.

### CSV baseline strategy

The binary override approach must always start from a CSV baseline.  Some VB
slots have no binary file (`ResourceId::0` in `ia_input_layouts.csv`), in which
case CSV is the only source.  Binary overrides are more precise where available,
but must never silently zero out attributes for which no binary exists.

### SNORM decode formula

D3D11 SNORM: `max(-1.0, value / 127.0)` for 8-bit, `max(-1.0, value / 32767.0)`
for 16-bit.  The `max(-1.0, ...)` clamps `0x80` / `0x8000` (−128 / −32768) to
exactly −1.0 per the D3D spec.
