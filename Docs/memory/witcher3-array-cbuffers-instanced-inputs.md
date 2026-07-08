---
name: witcher3-array-cbuffers-instanced-inputs
description: How the hlsl_interpreter supports array cbuffers and per-instance vertex inputs (witcher3 captures)
metadata:
  type: project
---

The witcher3_countryside captures (step104+) introduced two features the interpreter gained in step104:

1. **Array cbuffers** — `float4 cb1[4]` declared in HLSL, stored in `VS_constant_buffers.csv` as `cb1_v0, cb1_v1, ...`. Handled by: `parse_cbuffer` strips `[N]` → `FieldDefinition.array_size`; `load_cbuffer_data_from_csv` fills array fields from `<name>_v<idx>` rows into a list-of-vectors; `get_value` has an array-subscript branch for `cb1[0]`, `cb2[8].xyz`, `arr[i].w`.

2. **Instanced vertex inputs** — `INSTANCE_TRANSFORM*` etc. are NOT in `ia_vertex_data.csv` (per-vertex slot 0 only). They live in a separate `PerInstance=True` binary vertex buffer `vb_slot{N}_res_{resid}.bin`. `load_per_instance_data` (in hlsl_interpreter.py) parses `ia_input_layouts.csv` and decodes instance 0 at `binding.ByteOffset + instance*stride + element.ByteOffset`. **Critical gotcha:** the bound `ByteOffset` for the slot (from the VertexBuffer section) is often non-zero (e.g. 1727552) — instance data does NOT start at byte 0 of the file. render.py merges instance-0 inputs into every vertex.

**Assumption:** single-instance draws (all vertices share instance 0). Confirmed for witcher3 cases by `golden rows == vertex rows`. Multi-instance not yet modelled.

`Cases/` is gitignored, so `regression_test_zip_files.csv` and `Default.json` edits are local-only and never committed.
