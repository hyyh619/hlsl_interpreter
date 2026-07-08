---
name: per-vertex-binary-vb-decode-r10a2
description: NORMAL/TANGENT are R10G10B10A2 in binary VB (CSV dumps zero); POSITION precision from binary; CSV-agreement guard protects UINT BLENDINDICES
metadata: 
  node_type: memory
  type: project
  originSessionId: 7c0f27af-4a6f-47c0-8d6b-58cbf18bf15a
---

Witcher 3 countryside captures store some per-vertex inputs in a way
`ia_vertex_data.csv` can't represent, so the interpreter decodes them from the
binary `vb_slot{N}_res_{resid}.bin` (see `load_per_vertex_binary_data` in
hlsl_interpreter.py, wired in render.py after per-instance load).

Key facts (step117):
- **NORMAL/TANGENT** are dumped as `R0G0B0A0_UNORM` / `CompByteWidth=0` and the
  CSV columns are all **zero**. The real bytes are R10G10B10A2_UNORM (10/10/10/2
  bits in one uint32; the 2-bit alpha is the tangent handedness sign ±1). A
  4-component element packed into 4 bytes ⇒ treat as R10G10B10A2, NOT R8G8B8A8.
- Fetch each drawn vertex by its **IDX** (index-buffer value) column, so the
  binary decode lines up with the already-index-resolved POSITION/TEXCOORD CSV
  columns. Slot byte stride/offset from `ia_input_layouts.csv` VertexBuffer rows.
- **POSITION** (R16G16B16A16_UNORM) read from the 5-decimal CSV loses precision;
  after a large per-mesh decompress (`pos = v0*cb2[8]+cb2[9]`) it amplifies clip
  `w` past tolerance (event7816). Binary is the exact GPU bytes.
- **Agreement guard** (`_values_agree`): for non-degenerate elements the binary
  value replaces the CSV value ONLY when they agree (precision refinement);
  degenerate (zero-CSV) elements always take the binary. This is critical —
  blindly decoding everything corrupted `BLENDINDICES : R8G8B8A8_UINT` on
  skinned meshes (event1341/1399/1450), a format `_decode_vertex_element`
  doesn't model (fell through to float32 → [0,0,0,0]).
- Also fixed: >4 TEXCOORD outputs collided on canonical keys — unmapped indexed
  semantics now key by full name (see [[golden-vs-mesh-sv-position-first]]).
- Full-screen pass shaders (event7358) shade ~129k px in pure Python (~458s);
  regression per-case timeout raised to 1800s, and `_color_to_byte` now clamps
  inf/NaN so a sliver pixel's infinite color doesn't crash the bitmap dump.

Related: [[witcher3-array-cbuffers-instanced-inputs]], [[raw-img-texture-loading]],
[[structured-buffer-skinning-support]].
