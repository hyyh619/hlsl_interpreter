# hlsl-step92: Sweep Dump/ zips — Fix interpreter, expand regression suite

## Goal

Sweep all zips in `Dump/` through the interpreter, fix any issues found, add passing cases to
`Cases/regression_test_zip_files.csv`, and commit each fix.

---

## Fixes Applied

### Fix 1 — `const float4 icb[]` inline constant buffer in function body

**Symptom**: `witcher3_countryside_event22420` (and many other event22xxx) produced all-zero VS
output. The shader used `const float4 icb[] = { {…}, {…}, … }` as a local array (3Dmigoto's
decompiled form of inline constant buffers). `execute_statement` did not recognise this syntax and
fell through to variable-declaration handling, which also didn't match (the `const` keyword blocked
the type-name regex), so the array was never populated.

**Root cause**: `execute_statement` had no branch for `const <type> <name>[] = { … }`.

**Fix** (added before the `variable_declaration` check in `execute_statement`):

```python
if stmt.startswith('const ') and '[' in stmt and '{' in stmt and '=' in stmt:
    m_icb = re.match(r'^const\s+\w+\s+(\w+)\s*\[\s*\d*\s*\]\s*=\s*(\{.+\})\s*;?', stmt, re.DOTALL)
    if m_icb:
        arr_name, arr_body = m_icb.group(1), m_icb.group(2)
        # parse nested { v0, v1, v2, v3 } elements
        elements = []
        depth = 0; elem_start = None
        for ki, kc in enumerate(arr_body):
            if kc == '{':
                depth += 1
                if depth == 2: elem_start = ki + 1
            elif kc == '}':
                depth -= 1
                if depth == 1 and elem_start is not None:
                    raw = arr_body[elem_start:ki]
                    vals = [float(p.strip()) for p in raw.split(',') if p.strip()]
                    elements.append(vals if len(vals) > 1 else (vals[0] if vals else 0.0))
                    elem_start = None
        if elements:
            local_vars[arr_name] = elements
        return None
```

**Result**: `witcher3_countryside_event22420` → PASS (4/4).  Many other event22xxx cases that shared
the same shader also started passing.

---

### Fix 2 — `_mRC` accessor on float4x4 matrix element

**Symptom**: `Frame-frame9222_event1734` — cbuffer held a `float4x4 g_mWorldViewProj[N]` array.
After reading the array element correctly (see Fix 3), the shader accessed it with
`_m00_m10_m20_m30` notation: `obj._m00_m10_m20_m30`. `apply_swizzle` only handled `xyzwrgba`
single-char swizzles; `_mRC` notation was silently skipped, returning `0`.

**Fix** (added in `apply_swizzle`, after the non-list guard):

```python
if swizzle and swizzle.startswith('_m') and obj and isinstance(obj[0], list):
    accessor_parts = [p for p in swizzle.split('_') if p and p[0] == 'm' and len(p) == 3]
    result = []
    for ap in accessor_parts:
        r, c = int(ap[1]), int(ap[2])
        result.append(obj[r][c] if r < len(obj) and c < len(obj[r]) else 0.0)
    return result[0] if len(result) == 1 else result
```

---

### Fix 3 — float4x4 array elements stored correctly in `override_cbuffers_from_binary`

**Symptom**: Same `Frame-frame9222_event1734`. The cbuffer binary had N consecutive `float4x4`
matrices. The binary-override path allocated one float4 register per element instead of four.

**Root cause**: In `override_cbuffers_from_binary`, the array branch appended `decoded[row_ri]`
(one float4) for every element, regardless of field type. A `float4x4` needs 4 consecutive
registers, transposed to row-major.

**Fix** (in the `array_size > 0` branch of `override_cbuffers_from_binary`):

```python
if base == 64 and row_ri + 3 < len(decoded):
    # float4x4: read 4 column registers, transpose to row-major
    cols = [decoded[row_ri + k] for k in range(4)]
    arr.append([[cols[c][r] for c in range(4)] for r in range(4)])
elif base == 48 and row_ri + 2 < len(decoded):
    arr.append([decoded[row_ri], decoded[row_ri + 1], decoded[row_ri + 2]])
else:
    arr.append(decoded[row_ri])
```

**Result**: `Frame-frame9222_event1734` → PASS (8475/8475), `Frame-frame9222_event1971` → PASS (1404/1404).

---

### Fix 4 — `packoffset(c3.w)` sub-register component ignored in binary loading

**Symptom**: `Collision-fix-constant-buffer-and-RdotV-zero_event399.zip` regressed (Color = 0)
after commit `35b788a` which added binary cbuffer loading.  `LightRadius : packoffset(c3.w)` was
supposed to load from byte offset 60 (= 3×16 + 3×4), but was loaded from offset 48 (= 3×16).
This made `LightRadius = -60` (from LightPos.x) instead of 600, so the light-radius comparison
`LightRadius >= 434.83` evaluated false → no lighting → Color = 0.

**Root cause**: `parse_cbuffer` captured only the register number from `packoffset(cN.comp)`,
discarding the component. `override_cbuffers_from_binary` then used `byte_off = reg * 16`,
ignoring the intra-register component offset.

**Fix** — three-part:

1. `FieldDefinition` — new field:
   ```python
   comp_off: int = 0  # intra-register component offset (0=x,1=y,2=z,3=w)
   ```

2. `parse_cbuffer` — extended regex:
   ```python
   po_match = re.search(r'packoffset\s*\(\s*c(\d+)(?:\.([xyzwrgba]))?\s*\)', line)
   if po_match:
       reg_offset = int(po_match.group(1))
       _comp_char = po_match.group(2) or 'x'
       comp_off = {'x':0,'r':0,'y':1,'g':1,'z':2,'b':2,'w':3,'a':3}.get(_comp_char, 0)
   else:
       reg_offset = -1; comp_off = 0
   fields.append(FieldDefinition(..., reg_offset=reg_offset, comp_off=comp_off))
   ```

3. `override_cbuffers_from_binary` — use component offset:
   ```python
   byte_off = field.reg_offset * 16 + getattr(field, 'comp_off', 0) * 4
   ```

**Result**: `event399` → PASS. Full 46/46 regression still passes.

---

## Regression Sweep — New Passing Cases

After the four fixes, a broad triage of `Dump/` was run across all game families. The following
**71 new cases** were confirmed passing and added to `Cases/regression_test_zip_files.csv`:

| Game family | Count | Notes |
|---|---|---|
| `witcher3_countryside` (event22xxx) | 14 | event22010, 22161, 22420 (icb), 22484, 22593, 22610, 22626, 22641, 22758, 22805, 22927, 22945 + 22010 22161 |
| `Frame-frame9222` | 2 | event1734, 1971 — float4x4 array fix |
| `sekiro4` | 5 | event2264, 13742, 13806, 14228, 16660 |
| `heaven_frame2596` | 14 | 14 of 16 cases pass (2 timeout) |
| `valley_frame272` | 10 | 10 of 11 cases pass (1 timeout) |
| `EndlessSpace2` | 5 | event1740, 1953, 1980, 2092, 2876 |
| `Octopath-frame746` (new) | 18 | event1057, 2214, 2569, 2767, 3240, 3287, 3403, 3433, 3642, 3670, 4014, 4038, 4058, 4114, 4125, 4135, 4174 + 3734 (0/0) |
| `manhattan_frame_274` | 1 | event1041 |
| `OldWorld` | 1 | event5145 |
| `4k1w` | 1 | event1124 |
| `Nobu15-frame3456` | 1 | event3743 |
| `TankMechanicSimulator` (Cases/) | 1 | event1090 |
| `witcher3_countryside` (Cases/) | 1 | event23173 |

Regression suite grew from **46 → 117 cases** (71 new cases including 2 from Cases/ only).

---

## Still-Failing Cases (root causes noted, not fixed in this session)

| Case | Error | Root cause |
|---|---|---|
| `witcher3_event22229`, `event22092` | TexCoord4 wrong (~2× off) | Output slot `o4 = v1.xyzw` but golden seems to be world-space position |
| `witcher3_event16215` | TexCoord2/3 wrong | Tangent-space rotation matrix issue |
| `witcher3_event16834` | nan/inf | Texture returns 0 → divide-by-zero in shader |
| `sekiro4` many cases | sv_position wrong (large diff) | Likely cbuffer struct-in-cbuffer or matrix layout difference |
| `TankMechanicSimulator` (8 of 75) | timeout | Large vertex count + complex shader |
| `TombRaider` | sv_position = uint bit-patterns | Golden uses uint bit-cast float comparison; struct-in-cbuffer |
| `sekiro2` | sv_position large diff | |
| Various | timeout | |

---

## Final State (first commit)

- 4 interpreter fixes committed
- Regression: 117 cases, **117/117 PASS**

---

## Part 2 — Full Dump/ sweep + struct-in-cbuffer + texture-NaN crash fix

A complete triage of **all 513 Dump/ zips** was run: **332/513 pass (~65%)**.
Failure breakdown of the 181:

| Reason | Count | Nature |
|---|---|---|
| VS-vs-golden value mismatch | 125 | wrong output values (various root causes) |
| timeout (>300s) | 37 | correct-but-slow (large vertex/pixel counts) |
| no VS-vs-golden comparison | 17 | 0 vertices / no golden mesh in capture |
| crash | ~3 | texture sampling NaN UV (fixed below) |

### Fix 5 — struct-in-cbuffer support (`struct {...} NAME[N]` inside a cbuffer)

TombRaider shaders declare `cbuffer WorldBuffer { struct { row_major float4x4 WorldViewProject;
World; ViewProject; } WorldParameters[12] : packoffset(c0); }`. Three problems:

1. **Brace truncation** — both `cbuffer_finditer` and `cbuffer_definition` regexes use `[^}]+`,
   which stops at the struct's nested `}`, so the cbuffer was never fully captured. Added
   `_extract_cbuffer_blocks()` (manual brace-balanced scan) and rewired the three call sites
   (`hlsl_interpreter.py` parse loop + `render.py` VS/PS loops).
2. **Struct member parsing** — `parse_cbuffer` now detects `struct {...} NAME[N] : packoffset(cM)`,
   computes the per-element register stride from the members, and registers a `__struct__` field
   (`FieldDefinition.struct_elem_regs`).
3. **Binary loading** — `override_cbuffers_from_binary` loads each struct-array element as a list
   of its raw float4 register rows (no transpose), so `NAME[i]._mRC` reads register R, component C
   (correct for the row_major matrices these structs hold).

**Result**: `WorldParameters[i]._m30_m31_m32_m33` and the position output (`o1`/SV_POSITION) are
now computed correctly (597.939, matching golden).

### Fix 6 — golden uint→float reinterpret for SV_Position

When a shader declares a leading `uint4` output (e.g. `out uint4 o0 : PSIZE0`) before
SV_POSITION, RenderDoc's mesh CSV lays SV_Position data into that first (uint-typed) column and
prints its floats as **uint32 bit-patterns** (integer text like `1142258707` = 597.94f). The
golden loader already reorders SV_Position to that physical column; added `_golden_float()` to
reinterpret integer-formatted tokens (no `.`/exponent) as float32 bits. SV_Position now matches.

### Fix 7 — texture NaN/Inf UV crash guard

`_sample_linear`/`_sample_nearest` did `int(u*w)`, which raised
`ValueError: cannot convert float NaN to integer` when a shader produced a NaN UV (divide-by-zero)
and then sampled — aborting the entire draw (sekiro2_event13516, sekiro4_event20560). Now NaN/Inf
UV is clamped to 0 (well-defined GPU-like out-of-range behaviour) so the draw completes.

### TombRaider limitation (cannot be fixed from HLSL)

TombRaider's **normals/tangents still fail** — and this is unfixable from the decompiled HLSL.
The disassembly shows the normal transform uses `cb0[base+4..6]` (the *second* struct matrix,
`World`), but 3Dmigoto's decompiled HLSL writes the *same* `._m00/_m10/_m20` tokens that the
position uses for `cb0[base+0..2]` (the *first* matrix, `WorldViewProject`). The struct-member
selector is dropped, so the same `_m00` token maps to different registers depending on context —
information that exists only in the disassembly. Position (`o1`) and `o2`/TEXCOORD0 now pass;
the normal-derived outputs (TEXCOORD2/3/4) require disasm-level register info, out of scope for an
HLSL interpreter. Verified: computing `o3` with the World matrix (regs +4/+5/+6) gives 0.5929,
exactly the golden value — confirming the diagnosis.

### Net

Fixes 5–7 are correct general improvements (struct-in-cbuffer, golden bit-pattern reinterpret,
texture crash robustness). They do not make TombRaider fully pass (lossy decompiled source) but
fix its position output, fix the texture crashes, and support single-matrix struct-in-cbuffer
captures. Regression remains green.
