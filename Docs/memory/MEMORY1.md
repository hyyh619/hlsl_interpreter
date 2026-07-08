# Memory Index

- [witcher3 array cbuffers & instanced inputs](witcher3-array-cbuffers-instanced-inputs.md) — array cbuffer (cb1[4]) and PerInstance vertex-buffer loading; instance buffer ByteOffset gotcha
- [golden vs_mesh SV_Position-first column order](golden-vs-mesh-sv-position-first.md) — *_vs_mesh.csv data dumps SV_Position first; map golden columns positionally not by header (component counts come from header groups, not declared type)
- [StructuredBuffer skinning support](structured-buffer-skinning-support.md) — bone-palette StructuredBuffer loading + NaN-mask and nested-`[]` parser fixes
- [raw .img texture loading](raw-img-texture-loading.md) — textures load from raw .img per DXGI format; BC7 falls back to .bmp; R/B-swap fix; event1399 data unblocked
- [nested if/else, intrinsics & rel tolerance](nested-ifelse-intrinsics-and-rel-tolerance.md) — step116: deep if/else parse, floor/frac/ceil/round/trunc, abs+rel golden tolerance; explains all-zero / per-vertex-identical / tiny-diff symptoms
- [per-vertex binary VB decode & R10A2](per-vertex-binary-vb-decode-r10a2.md) — step117: NORMAL/TANGENT are R10G10B10A2 in binary VB (CSV is zero); POSITION precision from binary; CSV-agreement guard protects UINT BLENDINDICES; IA missing-w default; >4 TEXCOORD key collision; inf-color clamp
- [RunDrawFromDump Octopath/Tank batch](rundrawfromdump-octopath-tank-batch.md) — step118: 280-zip Dump dir; triage-first workflow; Octopath fixes (comment strip, mad, scalar swizzle, binary cbuffer, typed Buffer.Load, SV_VertexID); unfixed sincos/cmp/ternary + exp2-overflow + Tank-timeout classes
