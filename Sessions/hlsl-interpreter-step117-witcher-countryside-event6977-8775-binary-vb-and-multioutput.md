# Step 117 — Witcher 3 countryside event6977…event8775

Fix the 12 new captures in `Cases/witcher3_countryside_zip_files.csv`
(event6977, 7264, 7301, 7321, 7358, 7816, 7889, 7934, 7951, 7967, 8573, 8775)
until none emit `Error:` lines, then keep the regression green.

## Initial scan

Ran every case headless and grepped for `Error:` / `Total PASSED rows`:

| case | before | symptom |
|------|--------|---------|
| event6977 | **PASS** 4632/4632 | baseline |
| event7264 | 0/3 | every row: `sv_position[3] output=0 golden=1.0` |
| event7301 | 0/3 | same |
| event7321 | 0/3 | same |
| event7358 | 0/3 | same |
| event7889 | 0/4 | same |
| event7816 | 1883/2763 | tiny `sv_position` diffs ~0.006 (just over 0.005) |
| event7934 | 0/441 | TEXCOORD2/4/5 wrong, constant `-0.577350` across rows |
| event7951 | 0/1548 | same family |
| event7967 | 0/2934 | same family |
| event8573 | 0/10572 | same family |
| event8775 | 0/84 | same family |

Four distinct root causes emerged.

## Fix #1 — IA missing-component default (committed separately)

**Cases:** event7264 / 7301 / 7321 / 7358 / 7889.

These are trivial `o0 = v0` (or fullscreen-triangle) shaders. Every row failed
only on `SV_Position.w`: output `0`, golden `1.0`.

`load_ia_vertex_data` built the per-vertex value list from the component
columns *present in the data* (an `R32G32B32` POSITION element yields x,y,z),
not from the declared VS-input type. A `float4 v0 : POSITION` fed by a
3-component element therefore got a 3-element list and `v0.w` read as `0`.
D3D initialises the vertex-input register to `(0,0,0,1)` before overwriting
from `.x`, so a missing `.w` must default to `1.0`.

Fix: collect the contiguous components actually supplied, then pad to
`min(declared_component_count, 4)` with the `(0,0,0,1)` defaults and slice to
the declared width. (Commit: *"Fix IA missing-component default…"*.)

## Fix #2 — semantic→canonical key collision for >4 TEXCOORDs

**Cases:** unblocked event7934 / 7951 / 7967 / 8573 / 8775 (with Fix #3).

The Witcher mesh shaders output up to six `TEXCOORD` registers
(`o0..o5 : TEXCOORD0..5`) plus `SV_Position`. Both the golden loader
(`load_vs_golden_from_mesh_csv`) and the result builder (`executeVS_with_params`)
map a semantic to a canonical key via `_get_output_semantic_to_key_map`, with a
fallback chain `get(sem_full, get(sem_base, …))`.

The map only knows `TEXCOORD0..3`. `TEXCOORD4`/`TEXCOORD5` missed `sem_full`
and fell back to the **base** `'TEXCOORD' → 'TexCoord'`, colliding with
`TEXCOORD0`. With several TEXCOORDs collapsing onto one key, the golden dict
(and the result dict) silently kept only the *last* writer, so most outputs
were never compared and the few that were got mislabelled.

Fix: drop the base fallback for indexed semantics — `key = get(sem_full,
sem_full)`. Indexed semantics that aren't explicitly mapped now key by their
full name (`TEXCOORD4`, `TEXCOORD5`, …), which is unique. The rasterizer only
consumes `TexCoord`/`TexCoord2` (= TEXCOORD0/1, still explicitly mapped), so
the PS path is unaffected.

## Fix #3 — per-vertex binary VB decode + R10G10B10A2 normals

**Cases:** event7934 / 7951 / 7967 / 8573 / 8775.

These shaders transform NORMAL/TANGENT. The layout reports them as
`R0G0B0A0_UNORM` with `CompByteWidth = 0` and RenderDoc writes **all-zero**
NORMAL/TANGENT columns into `ia_vertex_data.csv`. The interpreter therefore
read `v2 = v3 = 0`, so `v2*2-1 = (-1,-1,-1)` and every normalized output
collapsed to the constant `normalize(-1,-1,-1) = -0.5774`.

The real bytes live in `vb_slot{N}_res_{resid}.bin`. New code:

* `_parse_ia_layouts` — shared layout parser (factored out of
  `load_per_instance_data`).
* `load_index_column` — reads the `IDX` (index-buffer value) column of
  `ia_vertex_data.csv`.
* `load_per_vertex_binary_data` — decodes non-instanced elements from the
  binary VB, fetching each drawn vertex by its `IDX` so it lines up with the
  post-index POSITION/TEXCOORD columns RenderDoc already produced.

The degenerate `CompByteWidth = 0` width is inferred from the gap to the next
element in the slot (or the slot stride). A **4-component element packed into
4 bytes is not R8G8B8A8** — RenderDoc would have named that. The Witcher packs
NORMAL/TANGENT as **R10G10B10A2_UNORM** (10/10/10/2 bits in one uint32, the
2-bit alpha carrying the tangent handedness sign). Verified against golden:
the decoded tangent `[0.546,-0.588,-0.597, w=1.0]` matched `TEXCOORD5`
`[0.578,-0.557,-0.597]` and the `±1` handedness, where R8 gave `w=0.6` (wrong).
Added an `R10G10B10A2` branch to `_decode_vertex_element`.

→ event7934 went 0/441 → **441/441**; 7951/7967/8573/8775 likewise clean.

## Fix #4 — POSITION precision from the binary VB

**Case:** event7816.

After Fix #1, event7816 still failed ~880 rows by a hair: `sv_position` diffs
of 0.0055–0.0074, just over the 0.005 tolerance (relative error ~1e-4).

POSITION is `R16G16B16A16_UNORM` and was read from `ia_vertex_data.csv`, which
rounds to ~5 decimals. After the per-mesh decompress
`pos = v0 * cb2[8] + cb2[9]` (a large bounding-box scale), the ~5e-6 CSV
rounding amplifies into ~0.006 of clip-space `w` — past tolerance.

Fix: extend `load_per_vertex_binary_data` to re-decode non-instanced elements
from the binary VB, which carries the exact bytes the GPU used. Decoded values
are fit to the declared register width with the same `(0,0,0,1)` defaults
(`_fit_value_to_width`). → event7816 **2763/2763**.

**Agreement guard (critical refinement).** A first cut that blindly overrode
*every* non-instanced element from the binary regressed three previously-green
skinned meshes (event1341/1399/1450) and crashed event7358 with
`OverflowError: cannot convert float infinity to integer`. Cause: those meshes
carry `BLENDINDICES : R8G8B8A8_UINT` in slot 0, a format
`_decode_vertex_element` doesn't model — it fell through to a float32 read,
producing `[0,0,0,0]` and destroying the bone indices (and via skinning, the
position → `inf`). The CSV had them right.

So the binary decode is applied as a *precision refinement*, gated by
`_values_agree`: for a non-degenerate element the binary value replaces the CSV
value only when the two already agree component-wise (within `max(1e-3,
1e-2·|csv|)`); on disagreement the trusted CSV value is kept. Degenerate
elements (CSV holds zeros) always take the binary. This keeps the POSITION
precision win for event7816 while leaving BLENDINDICES — and any other format
this decoder can't yet read — untouched. `csv_vertex_data` is threaded into
`load_per_vertex_binary_data` for the comparison.

## event7358 — slow, not wrong (regression timeout bump)

Once Fix #1 made `SV_Position.w` correct, event7358's fullscreen triangle is no
longer degenerate, so the rasterizer covers the whole 192×672 viewport and the
**pure-Python pixel shader runs on 129 024 pixels (~458 s)**. The VS validation
itself passes 3/3 with no `Error:` lines; only the regression's 600 s/case
timeout tripped. Raised the per-case timeout in `run_regression.py` to 1800 s —
the slowness is the expected cost of a software PS, not a correctness break.

## Result

All 12 countryside captures emit **no `Error:` lines**:

```
event6977 4632/4632   event7264 3/3      event7301 3/3
event7321 3/3         event7358 3/3      event7816 2763/2763
event7889 4/4         event7934 441/441  event7951 1548/1548
event7967 2934/2934   event8573 10572/10572  event8775 84/84
```

Added all 12 to `Cases/regression_test_zip_files.csv`; full regression green.

## Files touched

* `hlsl_interpreter.py` — `load_ia_vertex_data` padding (Fix #1); collision-free
  keys in `load_vs_golden_from_mesh_csv` + `executeVS_with_params` (Fix #2);
  `_parse_ia_layouts`, `load_index_column`, `load_per_vertex_binary_data`,
  `_fit_value_to_width`, `_values_agree`, R10G10B10A2 in
  `_decode_vertex_element` (Fix #3/#4).
* `render.py` — wire per-vertex binary override (with CSV-agreement guard)
  after per-instance load.
* `run_regression.py` — per-case timeout 600 s → 1800 s.
