---
name: rundrawfromdump-octopath-tank-batch
description: "RunDrawFromDump/Dump has 280 zips (Octopath GS/instanced, Tank, witcher); triage-first workflow + Octopath interpreter gaps (comments, mad, binary cbuffer, typed Buffer.Load, SV_VertexID) and the still-unfixed sincos/cmp/ternary class"
metadata: 
  node_type: memory
  type: project
  originSessionId: 7c0f27af-4a6f-47c0-8d6b-58cbf18bf15a
---

`C:\Development\Verisilicon\DirectX\tools\hy\RunDrawFromDump\Dump` holds **280**
capture zips: Collision×1, Octopath-frame746×44, TankMechanicSimulator×75,
witcher3_countryside×160. Octopath captures add a **Geometry Shader** stage and
`*_vs_mesh.csv`/`*_gs_mesh.csv` golden, plus files the witcher set lacks:
`{VS,PS,GS}_constant_buffer_info.csv` (slot→`constant_<id>.bin`),
`buffer_params.csv` (typed/structured buffer → `buffer_<resid>.bin`).

Workflow that works: **triage first** with a headless runner (per-case timeout;
parse the VS verdict from the log even on PS timeout), group failures by first
`Error:` signature + shader features, fix per root-cause class, commit each with
one representative added to the (gitignored, local) regression CSV.

Step118 fixed 3 Octopath classes (regression 39/39; reps event102, event1031,
event1320):
1. `GenerateStmts` never stripped `//`/`/* */`; 3Dmigoto's `// Needs manual fix`
   comment block fused with the next assignment → dropped. Added
   `_strip_comments`. Also added `asint/asuint/asfloat`.
2. `mad(a,b,c)` unimplemented + scalar swizzle `.xx` on a scalar returned None →
   garbage SV_POSITION on instanced StructuredBuffer-matrix meshes. See
   [[per-vertex-binary-vb-decode-r10a2]] for the related VB decode.
3. Binary cbuffer reload (`override_cbuffers_from_binary`) so `asint`/`asuint`
   get real int bits (float CSV prints int 1 as 0, -1 as NaN); typed
   `Buffer<T>.Load(index)` (`load_typed_buffer_data`, format inferred from
   bytes-per-component: 1→RGBA8_UNORM else float32×elem_size//4); per-vertex
   `SV_VertexID` = index-buffer value; `_cbuffer_component_raw_int` for exact
   int32 of a `cbN[i].c` arg. All no-ops when the info/param CSVs are absent, so
   witcher cases are untouched.

Step118 also fixed 2 more classes (regression 40/40; rep event1487 added):
4. `sincos(angle,s,c)` void statement (writes sin→2nd lvalue, cos→3rd) — the
   `#define cmp -` macro and vector ternaries `r9.zzz?a:b` already worked.
5. `_safe_pow` saturates exp2/pow to ±inf instead of raising OverflowError
   (event576/2651/2682 no longer crash).

Step118 classes 6-7 (regression 41/41; rep event1854 added):
6. Bitwise operators `^`/`<<`/`>>`/`%` + unary `~` + hex/`u` literals (added to
   `_OPERATORS` in hlsl_syntax_tree.py with renumbering, `execute_binary_op`/
   `execute_unary_op`, `get_value`). Pre-cast split so `(uint)r0.x<<1` parses
   right. event2135 errors 78->11.
7. binary-VB override now rescues an ALL-ZERO csv column (`_is_all_zero`) — fixes
   event1854 colour (B8G8R8A8_UNORM in binary VB, CSV had 0). Non-zero CSV
   disagreement still kept (protects BLENDINDICES).

Step118 classes 9-10 (witcher Dump set — a NEWER format than the witcher cases
already in regression; cbuffer named `Constants : register(b0)` with dynamic
`vfuniforms[48]`, CSV captures only element 0):
9. `override_cbuffers_from_binary` now matches the binary file to the cbuffer by
   `register==slot` (parsed into `CbufferDefinition.register`), not by `cb{slot}`
   name — so `Constants`/`vfuniforms` loads from constant_<id>.bin. event23303
   0/6->6/6. Octopath cb0/cb1 unaffected.
10. `f16tof32`/`f32tof16` intrinsics (Python `'<e'` half codec).

STILL BLOCKED / deferred:
- witcher Dump set (160 zips) triaged: 99/160 clean-pass after class 9.
- class 11 (commit 36bb4be): usage-based format inference — `(uint)`/`(int)` cast
  of a FLOAT-typed vertex attribute, and `f16tof32` of a float, reinterpret the
  float32 bits (`_bitcast_to_int`) instead of converting. Restricted to
  float-typed inputs (`_vertex_input_names`) so uint index attrs (event1031) are
  unaffected. Fixes event16215 sv_position (390->180 errs); residual TexCoord is
  octahedral-normal decode (open). event16834 similar.
- still-failing witcher: `TexCoord*` groups (event20571/21719/22229), octahedral
  normal residual (event16215/16834).
- Quaternion typed buffers (event2135/1250/3542/3601/3012): t3 is R8G8B8A8_SNORM
  but sibling texcoord t2 is R8G8B8A8_UNORM, both 4 bytes; the capture records NO
  view format for VS typed buffers (buffer_params.csv = byte size only;
  texture_params.csv = PS textures only; disasm = all `(float,...)`). UNORM vs
  SNORM undecidable from metadata — a capture-data limitation, NOT fixable.
- float32-precision noise hashes: FIXED by class 8 (`_to_f32` rounds every
  + - * /, literal, frac/mad to float32 via struct.pack('<f'); config
  `float32_emulation`, VS-stage only so the per-pixel PS stays fast; on in
  run_regression BASE_CONFIG). event1897 6/6, event283 48/48. event1828 has a
  different non-arithmetic precision issue (still open); event2091 just slow.
- 18 Tank timeouts: pure-Python rasterize+depth-compare of ~62k px is just slow
  (event1090 VS has 0 errors); perf, not a bug. witcher 160 not re-triaged.
