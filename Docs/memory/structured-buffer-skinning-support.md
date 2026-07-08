---
name: structured-buffer-skinning-support
description: StructuredBuffer (skinning bone palette) support and two related harness/parser fixes added in step106
metadata:
  type: project
---

Many witcher3 cases are skinned meshes reading a bone palette via `StructuredBuffer<t0_t> t0 : register(t0)` with `t0[r1.y].val[48/4]`. Support added in step106 (hlsl_interpreter.py):
- `self.structured_buffers` registry; `_parse_structured_buffers(code)` parses struct layouts + `StructuredBuffer<T> name : register(tN)`.
- `load_structured_buffer_data(folder)` binds to `VS_slot_{reg}_res_*_buffer.bin` (keeps raw bytes, decodes on demand — palettes are huge, e.g. 614400×64B).
- `_structured_buffer_member(sb, idx, member)` decodes a member; `_eval_subscript(expr)` evaluates literal/const-arithmetic(`48/4`)/variable(`r1.y`) indices.
- `get_value` array branch resolves `t0[i].member[k]`. render.py calls parse + load during VS setup.

Two general fixes made alongside:
1. **NaN masking** in `compare_vs_output_with_golden_params`: was `if abs(ov-gv) > tol` which is False for NaN → NaN silently counted as PASS. Now `if not (abs(ov-gv) <= tol)`. Diagnostic tell: VS "passes" but pipeline produces 0 pixels / all primitives clipped → output is actually NaN.
2. **Nested subscript parsing** in hlsl_syntax_tree.py: `_find_top_level_operator_cached`, `_find_ternary_colon`, `_split_args_cached` tracked `()` depth but not `[]`, so `48/4` inside `t0[i].val[48/4]` split as a top-level division. Now they track `[]` too.

Related: [[witcher3-array-cbuffers-instanced-inputs]], [[golden-vs-mesh-sv-position-first]].
