# Step 118 — RunDrawFromDump batch: Octopath / TankMechanicSimulator / witcher triage & fixes

## Task

Run every zip under
`C:\Development\Verisilicon\DirectX\tools\hy\RunDrawFromDump\Dump` (280 archives,
4 groups: Collision×1, Octopath-frame746×44, TankMechanicSimulator×75,
witcher3_countryside×160). For each case that emits `Error:` lines, find the
root cause in the pipeline, fix it without breaking other cases, keep the
regression suite green, commit per root-cause class, and add one representative
of each fixed class to the regression list.

## Approach

Sequential per-case full runs would take many hours (large Octopath/Tank
captures, full-screen pixel shaders). Instead: **triage first** with a headless
runner (`scratchpad/triage.py`, per-case timeout, VS verdict parsed from the log
even on PS timeout), group failures by error signature + shader features, then
fix each root-cause class once and re-triage.

---

## Class 1 — 3Dmigoto comment block drops the first statement + missing `as*` intrinsics

**Representative: `Octopath-frame746_event102` (1/4 → 4/4).**

Symptom: only `TexCoord` (TEXCOORD0 = `o0`) mismatched; `o0` came out 0 while
`SV_POSITION` was correct. With `cb0[1]=(1,1,0,0)`, `o0.xy = v1.xy*cb0[1].xy +
cb0[1].zw` reduces to `v1.xy`, yet the interpreter produced 0.

Root cause (two bugs):

1. **Comment block fused with the next statement.** 3Dmigoto emits
   ```hlsl
   // Needs manual fix for instruction:
   // unknown dcl_: dcl_input_sgv v2.x, instance_id
     o0.xy = v1.xy * cb0[1].xy + cb0[1].zw;
   ```
   `GenerateStmts` splits on `;` but never stripped `//` comments, so the two
   comment lines + the assignment became one statement; the assignment was not
   recognised (`=> (no assignment)`) and `o0` kept its default 0. Added
   `_strip_comments` (removes `//` line and `/* */` block comments, preserving
   string literals and newlines) and call it at the top of `GenerateStmts`.

2. **`asint`/`asuint`/`asfloat` unimplemented** → returned None and poisoned the
   expression (`o2.x = (int)v2.x + asint(cb0[0].x)` became None). Added them as
   32-bit bit-pattern reinterpretations (`struct.pack`/`unpack`), scalar+vector.

Both are broad, common patterns (the comment block appears in almost every
3Dmigoto shader), so this change is validated against the full regression suite
before commit.

---

## Triage map (Octopath + Tank)

_(filled in as triage completes)_

## Class 2 — `mad` intrinsic + scalar-swizzle replication (Octopath transform)

**Representative: `Octopath-frame746_event1031`** (sv_position errors → 0).

The Octopath instanced meshes transform the position through a matrix stored in
a `StructuredBuffer<t0_t{float val[4]}>`, indexed by
`mad((int2)v1.xx, int2(36,36), int2(1,3))` etc. Two bugs produced garbage
`SV_POSITION`:

1. **`mad(a,b,c)` was unimplemented** → returned None, so the row indices
   `v1.x*36 + {1,2,3}` were None and the wrong `t0` rows were read. Added
   `mad = a*b + c`, component-wise with scalar broadcasting.

2. **Scalar swizzle replication returned None.** `apply_swizzle` only allowed
   `.x` on a scalar; `(int2)v1.xx` (v1 is a scalar `uint`) gave None, re-breaking
   the index `mad`. HLSL lets a scalar be replicated with `.x/.xx/.xxx/.xxxx`;
   `apply_swizzle` now does that.

With both, `event1031`'s `SV_POSITION` matches golden (all `sv_position[*]`
errors gone). The dominant Octopath class fails first on `sv_position`, so this
is the high-impact fix.

## Class 3 (in progress) — binary cbuffer load + typed-buffer `Buffer<T>.Load`

`event1031`'s remaining `TEXCOORD` error: `o1.xy = t1.Load(r1.x).xy` where
`t1 : Buffer<float4>` and
`r1.x = (v2.x + asint(cb1[0].w)) * asint(cb1[0].y)` (v2 = SV_VertexID).

- cb1 holds **integer bit-patterns** (`ints (-1, 1, 0, 0)` in
  `constant_30981.bin`); the float CSV destroyed them (`cb1[0].y` int `1` printed
  as `0.000000`, `cb1[0].x` int `-1` as `nan`). `asint(cb1[0].y)` must recover
  `1`, so cbuffers must be read from the raw binary
  (`VS_constant_buffer_info.csv` maps slot→`constant_<id>.bin`).
- `t1` is a typed buffer (`buffer_params.csv`: TypedBuffer, `buffer_30974.bin`),
  here R32G32_FLOAT; `Buffer<T>.Load(index)` must fetch element `index`. The
  existing `.Load` handler only models `Texture2D.Load(int3)`.

## Remaining classes

- **Octopath transform class** (event1031/1057/1250/1320/1487/1828/1897/1922/
  2091/2135/2214/2384/2428/2513 …): first error `sv_position[0]`, large garbage
  values. Shaders use `StructuredBuffer<t0_t{float val[4]}>` indexed
  `t0[i].val[0/4+k]`, integer `mad((int2)v1.xx, int2(36,36), int2(1,3))`,
  per-instance `ATTRIBUTE13 : R32_UINT` (absent from ia_vertex_data.csv),
  `Buffer<float4> t1.Load`, `SV_VertexID`, `SV_ClipDistance` output. Under
  investigation.
- **event1854 class**: first error `Color[0]` — distinct.

## Files changed

- `hlsl_interpreter.py` — `_strip_comments` + call in `GenerateStmts`;
  `asint`/`asuint`/`asfloat` in the intrinsic dispatch.
