# Step 106 — StructuredBuffer skinning, nested-subscript parser fix, NaN-masking fix (witcher3_countryside_event994)

## Goal

Third case in the witcher3_countryside loop: `witcher3_countryside_event994.zip`.

## 1. Symptom

```
Total PASSED rows: 2490/2490     # (falsely) reported PASS
0 "Error:" lines
Rasterizer pixels: 0             # but pipeline produced NO pixels
Golden depth pixels: 54 | matched: 0 | missing: 54
Primitives clipped: 830  (not clipped: 0)
```

VS "passed" yet every primitive was clipped and zero pixels were produced —
contradictory.

## 2. Root-cause investigation

Instrumented the clip stage: every triangle's `sv_position` was
`[nan, nan, nan, nan]`. So:

- The interpreter was producing **NaN** SV_Position.
- The clip stage correctly rejected NaN vertices (`nan >= 0` is False) →
  "830 clipped".
- The VS comparison **falsely reported PASS** on NaN.

Three distinct bugs surfaced:

### Bug A — VS comparison masks NaN
`compare_vs_output_with_golden_params` used `if abs(ov - gv) > tol:` to flag a
mismatch. For `ov = NaN`, `abs(NaN-gv) > tol` is `False`, so a NaN output
silently counted as a pass. Replaced with `if not (abs(ov - gv) <= tol):` —
identical for finite values, but NaN/inf (where every comparison is False) is
now correctly flagged. (Applied to both the vector-component and scalar
branches.)

### Bug B — expression parser splits operators inside `[...]`
This shader is a skinned mesh and reads its bone palette as
`t0[r1.y].val[48/4]`. The `[SYNTAX TREE]` trace showed it parsed as
`Value(t0[r1.y].val[48)` divided by `4` — the top-level operator finder tracked
`()` depth but **not `[]`**, so the `/` inside the second subscript was treated
as a top-level division → garbage / `inf`.

Fixed `hlsl_syntax_tree.py`: `_find_top_level_operator_cached`,
`_find_ternary_colon`, and `_split_args_cached` now increment/decrement depth on
`[`/`]` as well as `(`/`)`.

### Bug C — StructuredBuffer not supported
```hlsl
struct t0_t { float val[16]; };
StructuredBuffer<t0_t> t0 : register(t0);
...
r2.x = t0[r1.y].val[48/4];   // bone matrix palette lookup
```
`t0` was never loaded, so reads were undefined and the weight normalisation
`v2 / dot(weights)` produced NaN/inf. The palette is shipped as the 39 MB
`VS_slot_0_res_1678_buffer.bin` (614400 elements × 64 B).

## 3. Fix (StructuredBuffer support)

`hlsl_interpreter.py`:

- `self.structured_buffers` registry.
- `_parse_structured_buffers(code)`: parses `struct` layouts and
  `StructuredBuffer<T> name : register(tN)`, recording member layout and byte
  stride.
- `load_structured_buffer_data(folder)`: binds each buffer to
  `VS_slot_{reg}_res_*_buffer.bin`, keeping raw bytes (decoded on demand to
  avoid materialising the whole palette).
- `_structured_buffer_member(sb, index, member)`: decodes one element's member
  on demand (float / int / uint).
- `_eval_subscript(expr, local_vars)`: evaluates a subscript to int, handling
  literals, constant arithmetic (`48/4`, `48/4+1`) and variable refs (`r1.y`).
- `get_value`: array-subscript branch now resolves StructuredBuffer access
  `t0[i].member` / `t0[i].member[k]`, and uses `_eval_subscript` for all
  index expressions (also benefits the cb1[i] array-cbuffer path).

`render.py`: calls `_parse_structured_buffers(vs_code)` and
`load_structured_buffer_data(data_folder)` during VS setup.

(5 of the remaining 6 witcher3 cases also use StructuredBuffer, so this is
shared infrastructure, not a one-off.)

## 4. Result for event994

```
Loaded StructuredBuffer 't0' (t0): 614400 elements, stride 64B
Total PASSED rows: 2490/2490   (now a genuine pass — NaN masking removed)
0 "Error:" lines
Depth: golden 54 | matched 46 | missing 8 | extra 2  (48 pixels rendered, was 0)
```

Residual 8 missing + 2 extra edge pixels = the step102 sub-pixel coverage
limitation (non-gating).

## 5. Regression

Added `witcher3_countryside_event994.zip`. Result: **9/9 passed** (6 Collision +
895 + 907 + 994). The parser bracket-depth fix and the NaN-masking fix are
strict improvements — no finite-valued case changes behaviour.

## 6. Commit

Committed interpreter + parser + render changes and this session doc.
