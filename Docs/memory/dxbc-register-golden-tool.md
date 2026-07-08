---
name: dxbc-register-golden-tool
description: How to localize VS output divergence — decompile bug vs shared-infra bug — using the DXBC register-level golden
metadata: 
  node_type: memory
  type: project
  originSessionId: a82a52a7-5e75-4227-bd4a-b609d4e69d1d
---

`dxbc_diff.py` + `dxbc_interp.py` (added step 154) are a register-level golden: `dxbc_interp.py` is a vs_5_0 disassembly VM that executes the exact GPU instruction stream from `VS_shader_disasm.txt`; `dxbc_diff.py` runs it on a case zip/folder, traces per-instruction intermediate registers, and diffs final `oN` outputs against the golden `*_vs_mesh.csv`.

**Why:** The HLSL interpreter reads the lossy 3Dmigoto-decompiled `VS_shader.hlsl`; the disasm is exact. So when a VS output diverges from golden, this tool decides the cause: if the DXBC VM matches golden but the HLSL interpreter doesn't → decompile bug; if the DXBC VM also fails → shared-infra bug (input loading, texture sampling, etc.), not decompile.

**How to apply:** `python dxbc_diff.py ./Cases/<case>.zip <vertex_idx>`. For witcher/decompile-suspect cases, run this before assuming a decompile issue. It found two real things on witcher event16215: a per-instance vertex-buffer loading bug (now fixed) and that o2/o3 tangent divergence is a shared Texture2DArray detail-normal sampling gap (`sample_l` returns [0,0]), NOT decompile lossiness. Run `python run_regression.py` (125 cases, vs_only) after any interpreter change.
