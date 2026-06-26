# Step 120 — GPU denormal FTZ, ftoi cast semantics, and a long-tail assessment

## Goal

Work the remaining RunDrawFromDump long tail the user listed:
atmosphere-output precision (event20899 + unexamined event23458), packed-uint
octahedral normals (event16215/16834), unsupported raw structured buffers
(event22201), quaternion SNORM/UNORM ambiguity, and the slow-rasterization
timeouts.

## Results summary

| Case / class | Outcome |
|---|---|
| event23458 | **Fixed** — was already resolved by the step-119 multi-array cbuffer fix (stale triage log). 615/615. |
| event20899 (atmosphere) | **Fixed** — GPU denormal flush-to-zero. 0 → 2160/2160. Added to regression. |
| `(int)`/`(uint)` cast of float vertex input | **Fixed** — corrected over-aggressive class-11 bitcast (DXBC ftoi/ftou). Regression 46/46. |
| event16215 / event16834 (octahedral) | **Blocked** — need bit-exact VS Texture2DArray sampling (GetDimensions + SampleLevel + multi-slice load). |
| event22201 (raw buffer) | **Blocked** — capture never dumped the `t21` raw buffer (disasm: "needs manual fix"). |
| quaternion SNORM/UNORM | **Blocked** — capture records no VS typed-buffer view format. |
| timeouts (~26 + Tank) | **Perf, not a bug** — pure-Python rasterization over large draws. |

## Fix 1 — event20899: GPU denormal flush-to-zero (FTZ)

event20899's atmosphere outputs o0/o1 were wrong. Traced the whole chain to one
constant: `cb12[271].z`. Its raw bytes are `0x00000001` — the integer 1's bit
pattern, which as a float32 is the **denormal** `1.4e-45`. The shader uses it as
a *float* condition: `cb12[271].z ? r2.x : 1`.

GPUs run shader arithmetic in flush-denormals-to-zero (FTZ) mode, so the GPU sees
`0.0` (falsy) and takes the false branch → `r2.x = 1` → `r2.x = r3.x = saturate(
r0.w*0.002 − 0.3) = 0` → `o0 = cb12[46]` (= golden). The interpreter kept the
tiny-but-nonzero denormal (truthy), took the wrong branch, and `r2.x` came out
`0.9967`, corrupting o0 and the entire downstream o1 in-scattering chain.

**Fix:** flush subnormal float32 values (`0 < |x| < 1.1754943508e-38`) to `0.0`
when decoding the binary cbuffer (`override_cbuffers_from_binary`). `asint`/
`asuint` are unaffected — they read exact int bits from `_cb_raw`, not the float.
Commit `c398d86`.

## Fix 2 — `(int)`/`(uint)` cast of a float vertex input

While chasing event16834's NaN sv_position, found the class-11 cast heuristic was
too aggressive. It reinterpreted the raw float32 bits for *any* `(int)`/`(uint)`
cast of a float vertex-input attribute. That is right only for packed-data
extraction, where the cast feeds a bit op:

- event16215: `(uint2)v1.zw >> int2(16,16)` — bit-shifted → **bitcast** correct.
- event16834: `(int2)v1.zw` used directly as a **texture coordinate** → must be
  `ftoi` (round toward zero). Bitcasting turned `2.0` into `1073741824`, reading
  an out-of-bounds texel → `t1.Load` returned 0 → `0/0 = NaN` in the position.

**Fix:** bitcast only in the bitwise-operand path (new `_eval_bitwise_operand`,
triggered by `>> << & | ^`); everywhere else `(int)`/`(uint)` value-converts,
matching DXBC ftoi/ftou. uint-declared index attributes (BLENDINDICES etc.) were
never in the float-only vertex-input set, so they are unaffected (event1031,
event1399 still pass). Commit `122e4f4`. Regression 46/46.

This unblocked event16834's first two NaN sources (the cast, then a now-valid
`t1.Load`), exposing the next blockers below.

## Blocked classes (honest assessment)

### Octahedral normals — event16215 / event16834
These are instanced foliage shaders that do **vertex texture fetch**. After the
ftoi fix, event16834 still NaNs because:
- `t0.GetDimensions(0, fDest.x, …)` is unimplemented → returns 0 → `0.5/0 = inf`.
- `t0.SampleLevel(...)` is "Unknown method" — `SampleLevel` isn't implemented.
- The VS texture loader loads only array slice `arr0`, but the shader samples
  slice `v1.z = 2` of a `Texture2DArray`.

So completing event16834 needs GetDimensions + SampleLevel (explicit LOD) +
multi-slice Texture2DArray loading and slice selection, all bit-exact (a R16_UNORM
height feeds the position). event16215 additionally reconstructs its normal/
tangent (TexCoord2/TexCoord3) through a branchy `t4.SampleLevel` path. This is a
substantial texture-array infrastructure effort with uncertain bit-exact payoff —
deferred rather than ship half-built, untested texture features.

### Raw structured buffer — event22201
The disasm literally carries `// No code for instruction (needs manual fix):
ld_raw_indexable(raw_buffer) ... t21` plus `dcl_resource_raw t21`. The VS reads a
raw buffer the capture never dumped. (FTZ did drop its error count 112 → 54, but
the raw-buffer reads remain unsatisfiable.) Capture-data limitation — unfixable
here.

### Quaternion typed-buffer SNORM vs UNORM
Unchanged from step 118: a t3 quaternion buffer is `R8G8B8A8_SNORM` while the
same-size t2 is `R8G8B8A8_UNORM`, and the capture records no VS typed-buffer view
format to tell them apart. Capture-data limitation.

### Timeouts (~26 witcher + Tank)
Pure-Python rasterization/depth over large draws (tens of thousands of pixels).
The VS-vs-golden grade would pass; only wall-clock is the issue. Perf, not a
correctness bug — deferred (matches the Tank classification).

## Regression

46/46 after both fixes (event20899 added as the FTZ representative). event23341
(multi-array) and event20899 (FTZ) both in the suite.
