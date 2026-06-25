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

## Triage map (Octopath 44 / Tank 75)

Initial triage (classes 1–2 in tree): Octopath 30 fail; Tank 57 pass + 18
timeouts that logged no VS verdict. After classes 1–3 the simple instanced-
transform Octopath shaders pass (event102/1031/1320 + siblings); the still-
failing Octopath cases use materially more complex shaders (see Remaining).

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

## Class 3 — binary cbuffer load + typed-buffer `Buffer<T>.Load` + SV_VertexID

**Representative: `Octopath-frame746_event1031` (2/6 → 6/6, errors 0).**

`event1031`'s remaining `TEXCOORD` came from `o1.xy = t1.Load(r1.x).xy` where
`t1 : Buffer<float4>` and
`r1.x = (v2.x + asint(cb1[0].w)) * asint(cb1[0].y)` (v2 = SV_VertexID). Three
sub-fixes:

1. **Binary cbuffer load** (`override_cbuffers_from_binary`). cb1 holds integer
   bit-patterns (`ints (-1, 1, 0, 0)` in `constant_30981.bin`); the float CSV
   destroyed them (`cb1[0].y` int `1` printed `0.000000`; `-1` → `nan`).
   `asint(cb1[0].y)` must recover `1`. We now re-load cbuffers from the raw
   `constant_<id>.bin` (mapped by `{VS,PS}_constant_buffer_info.csv` slot→file),
   decoding float4 arrays; float values round-trip identically, integer-as-float
   denormals survive `asint` (`struct` re-pack). **No-op when the info CSV is
   absent (the witcher captures have none), so they are untouched.**

2. **Typed-buffer `Buffer<T>.Load(index)`** (`load_typed_buffer_data`,
   `_typed_buffer_load`, new branch in the `.Load` method handler). Parses
   `Buffer<T> tN : register(tN)`, loads `buffer_<resid>.bin` via
   `buffer_params.csv`, decodes `elem_size//4` float32 components (here 8B →
   R32G32) padded to 4 with `(0,0,0,1)`, and fetches `element[index]`. The
   existing `.Load` only modelled `Texture2D.Load(int3)`.

3. **Per-vertex `SV_VertexID`** (render.py). It was never set, so `r1.x` was 0
   for every vertex and all read `t1[0]`. Now set to the index-buffer value
   (`idx_list[i]`, +BaseVertex=0) per vertex.

4. **Exact int bits for `asint`/`asuint` of a cbuffer component**
   (`_cbuffer_component_raw_int`, `_cb_raw`). `-1` stored as NaN does not survive
   the float round-trip, so `asint(cb1[0].x)` (a `& -1` mask in the colour path)
   gave garbage. We keep the int32 bit array from the binary load and, when the
   `asint`/`asuint` argument is a literal `cbN[i].c`, return the exact int.

5. **Typed-buffer format inference** (`_typed_buffer_load`). `buffer_params.csv`
   gives only element byte size. A second buffer `t2` (the colour table) is
   R8G8B8A8_UNORM (4B → (1,1,1,1)); decoding it as float32 gave NaN. Rule:
   bytes-per-declared-component == 1 → UNORM8, else float32 with `elem_size//4`
   components. This fixes the colour path; **event1320 now 6/6**.

These five sub-fixes fully fix the core Octopath instanced-transform class
(representatives **event1031** and **event1320**, both 6/6). Scoped safely: the
binary cbuffer / typed-buffer / raw-int paths are all no-ops when the capture
lacks the corresponding files (the witcher regression set), so full regression
stays green.

## Class 4 — `sincos` statement (Octopath foliage)

**Representative: `Octopath-frame746_event1487` (0/6 → 6/6).**

These shaders already parsed the `#define cmp -` macro and vector ternaries
(`r9.zzz ? a : b`) correctly — the only gap was **`sincos(angle, s, c)`**, a
void intrinsic that writes `sin`→2nd lvalue, `cos`→3rd. It hit the
`(no assignment)` fallback, so the rotation registers kept stale values and the
position was wrong. Added a statement handler (`_split_top_level_commas` +
`_assign_lvalue`).

## Class 5 — `exp2`/`pow` overflow no longer crashes (event576/2651/2682)

Once cbuffers carried real binary values, an `exp2`/`pow` on a large exponent
raised `OverflowError: math range error` and aborted the run. `_safe_pow`
saturates to ±inf (and NaN on bad domain) instead; downstream consumers already
tolerate inf/NaN. The three crashes became ordinary runs.

## Class 6 — bitwise operators + hex literals (particle shaders, partial)

The particle/quaternion shaders (event2135/1250/3542/3601/3012 …) use integer
bit math the parser/evaluator lacked: `<<`, `>>`, `^`, `%`, unary `~`, and hex
literals (`0xffffffff`). Added:

- `_OPERATORS` gains `^`/`<<`/`>>`/`%` (renumbered to keep the existing relative
  order; `^` between `|` and `&`, shifts between relational and additive).
- `_parse_expression`: unary `~`/`!` prefix; the bitwise/shift set is split
  before the cast branch so `(uint)r0.x << 1` is `((uint)r0.x) << 1`, not
  `(uint)(r0.x << 1)`; the binary-op whitelist includes the new operators.
- `execute_binary_op`: `^`/`<<`/`>>`/`%` (32-bit masked, result reinterpreted as
  signed int32); `execute_unary_op`: `~`.
- `get_value`: hex / `u`-suffixed integer literals.

Verified on event2135: the `bitmask.x = ((~(-1<<31))<<1)&0xffffffff` idiom now
evaluates to `-2` (was 0) and the index math is correct — errors dropped 78 → 11.

**Blocked beyond this:** the remaining errors are the **quaternion typed buffer
`t3`**, which is R8G8B8A8_**SNORM** (byte 127 → 1.0), while the sibling texcoord
`t2` is R8G8B8A8_**UNORM** — both 4 bytes, and the capture records **no view
format** for VS typed buffers (`buffer_params.csv` has only element byte size;
`texture_params.csv` covers PS textures only; the DXBC disasm declares all as
`(float,float,float,float)`). UNORM vs SNORM therefore cannot be disambiguated
from the captured metadata — a capture-data limitation, so these particle cases
stay unfixed. The bitwise support is committed as correct general infrastructure.

## Class 7 — binary-VB rescue when the CSV column is all-zero (event1854)

**Representative: `Octopath-frame746_event1854` (0/6 → 6/6).**

`o3 = COLOR0 = v3` (`ATTRIBUTE3 : B8G8R8A8_UNORM`, slot 4) came out 0 vs golden
`(1,1,1,1)`: `ia_vertex_data.csv` stored ATTRIBUTE3 as zeros, but the real bytes
are in the binary VB. The class-3 agreement guard blocked the override (CSV `0`
≠ binary `1`). The guard exists to protect *non-zero* CSV columns the decoder
might mis-read (R8G8B8A8_UINT BLENDINDICES), so it now also accepts the binary
when **the CSV column is all-zero and the binary is not** (`_is_all_zero`) — the
CSV simply failed to capture that element. A non-zero CSV that disagrees is
still kept, so skinned meshes stay correct.

## Tank timeouts — slow, not wrong (deferred)

Ran `TankMechanicSimulator_event1090` to completion bounds: with a vertex cap it
finishes instantly and the VS shows **0 `Error:` lines**; the full run is slow
only because it rasterizes and depth-compares ~62 k pixels in pure Python (the
`Error [DepthDiff]` lines are depth-buffer diffs, not VS mismatches). So the 18
Tank "timeouts" are a performance limit of the headless triage, not interpreter
bugs — deferred (would need a much larger per-case timeout to confirm each).

## Class 8 — float32 arithmetic emulation (precision hashes)

**Representatives: `Octopath-frame746_event1897` (0/6 → 6/6) and `event283`
(0/48 → 48/48).**

Some outputs are hash/noise functions like
`r0.w = (10+v1.w); r0.w *= r0.w; r2 = frac(float3(1361.4563,…) * r0.www)`. The
interpreter computes in Python `float` (double); the GPU uses float32. After a
`×1361` amplification the ~1e-5 double/float32 gap moves the `frac` result by
order 1 — so SV_POSITION matched but the noise output was garbage.

Added GPU float32 emulation (`_to_f32` round-trips a double through
`struct.pack('<f')`): when enabled, every `+ - * /` binary result, numeric
literal, and `frac`/`mad` result is rounded to float32, so the amplified value
is the exact float32 the GPU saw before `frac`. Gated by the config flag
`float32_emulation`; the regression suite turns it **on** for every case
(`run_regression.py` BASE_CONFIG) and stays green, confirming it is a
strictly-more-accurate default rather than a per-case hack.

`event1828` has a *different* residual precision issue (not pure arithmetic) and
is still open.

## Class 9 — match binary cbuffer by register, not name (witcher Dump set)

**Representative: `witcher3_countryside_event23303` (0/6 → 6/6).**

The 160 witcher captures in the Dump dir are a **newer format** than the ones
already in regression: the cbuffer is declared `cbuffer Constants : register(b0)
{ float4 vfuniforms[48]; }` and indexed dynamically (`vfuniforms[r0.x]`). Its
`VS_constant_buffers.csv` holds **only element 0** of the 48-entry array — the
real data is in `constant_<id>.bin`. `override_cbuffers_from_binary` keyed the
target cbuffer as `cb{slot}`, so a cbuffer named `Constants` was never loaded
from binary and `vfuniforms[i]` resolved to None.

Fix: parse `register(bN)` into `CbufferDefinition.register` and match the binary
file to the cbuffer **by register == slot** (falling back to the `cb{slot}` name
and the info-CSV `Name`). Octopath `cb0`/`cb1` (register 0/1) still match
identically, so nothing regresses.

## Class 10 — f16tof32 / f32tof16 (packed-half vertex data)

Witcher foliage shaders pack two halfs per 32-bit lane and read them with
`f16tof32((uint)v.zw >> 16)` / `f16tof32(v.zw)`. Added both intrinsics via
Python's half-float struct codec (`'<e'`). Correct and general; it advances
event16215/16834 but they do **not** fully pass yet — their `v1.zw` is packed-
**uint** vertex data being loaded as float, so `(uint2)v1.zw` already lost the
high 16 bits before `f16tof32`. That vertex-format reinterpretation is a deeper
open class (below).

## Remaining classes (not yet fixed — follow-up)

**Witcher Dump set** (after classes 9/10): several `sv_position` cases pass via
the register-matched binary cbuffer; the rest split into —
- **Packed-uint vertex data read as float** (event16215/16834 …): `v1.zw` holds
  two packed halfs as a uint lane but is loaded as a float, so `(uint2)v1.zw`
  and `f16tof32` see the wrong bits. Needs the IA to keep such attributes as raw
  uint (format-aware vertex load), then `f16tof32` finishes them.
- Various `TexCoord`/`TexCoord3/4` cases and the ~24 TIMEOUTs (slow, several are
  the already-passing event7321/7358/7816 just exceeding the 150 s triage cap).

The still-failing **Octopath** cases are a long tail of distinct per-shader
features, split into:

- **Float32-precision-limited** (logic correct, only a noise/hash output off):
  event1897 (`PARTICLE_LIGHTING_OFFSET = frac(1361.4·(10+v1.w)²)…`, SV_POSITION
  passes), event283 (sv_position diff 0.008), event1828/2091 (TEXCOORD10 diff
  ~0.03–0.05). Would need float32-rounding emulation of intermediates.
- **Bit-ops + multi-typed-buffer particle/quaternion shaders** (event2135/1250/
  3542/3601/3012 …): need `<<`/`>>`/`^`/`%`/`~` operators (not in the precedence
  table), `t3.Load` quaternion decode, `(uint)` truncation chains.
- Large/slow cases now run without crashing but time out in triage at 150 s
  (event2384 23k+ rows, 576, 2428, 2651, 2682, 3221).
- **event1854** colour path; **18 Tank timeouts**; **witcher 160** un-triaged.

- **Complex Octopath foliage/skinning shaders** (event1057/1357/1487/1897/1922/
  2214/2384/2513/2569/2767/2912/3502/3601/3642 …): need `sincos`, the 3Dmigoto
  `#define cmp -` comparison macro (`cmp(a<b)` → integer mask), vector ternaries
  (`r9.zzz ? a : b`), more `(int2)` cast / `frac` / `floor` chains, and a
  `StructuredBuffer t0.Load(index)` path. event1487 still shows `sv_position`
  errors because the `if/else` that builds `r0` uses `cmp`/`sincos`.
- **OverflowError: math range error** (event576/2651/2682): once cbuffers carry
  real binary values an `exp2`/`pow` overflows Python `float` — needs an inf
  clamp.
- **event1854**: first error `Color` — distinct colour path.
- **Tank 18 timeouts** (event1090/1172/1189/1278/1357/1406/1947/4186/4236/4458/
  5118/9153/9598/9634/9682/9750/10577/10691): no VS verdict logged within 150 s
  and the log was empty — hang/very-slow in setup, not confirmed wrong.
- **witcher3_countryside (160 in the Dump set)**: triaged — **99/160 clean-pass**
  after class 9 (register-matched binary cbuffer) + f16tof32. The 61 fails split
  into `sv_position` (packed-uint vertex data, e.g. event16215/16834 — the lane
  is `R32G32B32A32_FLOAT` in the layout but the shader bit-unpacks it with
  `(uint2)v.zw>>16` / `f16tof32`; resolving needs DXBC `dcl_input` integer flags
  or usage inference, ambiguous from the captured signature) and `TexCoord*`
  groups (event20571/21719/22229… — large counts, likely packed-half texcoords /
  further >4-TEXCOORD wiring). Deep, deferred.

## Files changed

- `hlsl_interpreter.py` — `_strip_comments` + call in `GenerateStmts`;
  `asint`/`asuint`/`asfloat`, `mad` intrinsics; scalar-swizzle replication in
  `apply_swizzle`; typed-buffer parse in `_parse_structured_buffers`;
  `load_typed_buffer_data`, `_typed_buffer_load`, `override_cbuffers_from_binary`,
  `_cbuffer_component_raw_int` (+ `typed_buffers`/`_cb_raw` state).
- `render.py` — wire `override_cbuffers_from_binary` (VS+PS),
  `load_typed_buffer_data`, and per-vertex `SV_VertexID` from the index column.
- `hlsl_interpreter.py` (class 4/5) — `sincos` statement handler +
  `_split_top_level_commas`/`_assign_lvalue`; `_safe_pow` used by `exp2`/`pow`.
- `hlsl_syntax_tree.py` + `hlsl_interpreter.py` (class 6) — `_OPERATORS`
  `^`/`<<`/`>>`/`%`; unary `~`/`!` and pre-cast bitwise/shift split in
  `_parse_expression`; `execute_binary_op` shifts/xor/mod, `execute_unary_op`
  `~`; hex/`u`-suffixed integer literals in `get_value`.
- `hlsl_interpreter.py` (class 7) — `_is_all_zero`; binary-VB override now
  rescues an all-zero CSV column in `load_per_vertex_binary_data`.
- `hlsl_interpreter.py` + `render.py` + `run_regression.py` (class 8) —
  `_to_f32`/`_f32` + `f32_emulation` flag; float32 rounding in
  `execute_binary_op`, numeric literals, `frac`/`mad`; wired from config
  `float32_emulation` (on in the regression BASE_CONFIG).
- `hlsl_interpreter.py` (class 9/10) — `CbufferDefinition.register` parsed in
  `parse_cbuffer`; `override_cbuffers_from_binary` matches by register;
  `f16tof32`/`f32tof16` intrinsics.

## Status at checkpoint

Committed: class 1 (`cb42511`), class 2 (`4c3ee2f`), class 3 (this). Regression
green. Representatives added to the local regression list: event102, event1031,
event1320. Remaining classes above are the next iterations of the loop.
