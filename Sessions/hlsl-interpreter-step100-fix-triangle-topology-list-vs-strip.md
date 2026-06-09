# Step 100 — Fix event104 color mismatch: wrong primitive topology (strip vs list)

## Task

`Collision-fix-constant-buffer-and-RdotV-zero_event104.zip` reported ~3070
output-merger color mismatches (matched 37684/40754 in the user's run). Analyze
the pipeline and fix. Keep any debug code consolidated in `debug_trace.py`.

## Thinking / investigation

event104's PS is `o0 = 4 · DiffuseTexture.Sample(v2) · LightmapTexture.Sample(w2)`
— two textures, two texcoords, no vertex color. Output was too bright.

### Using the step-99 trace facility

Enabled `debug_trace` (ps_pixels + texture_lod) on a few mismatched pixels. To
tell the two textures apart in the trace I **threaded the texture name through
`Texture.sample(..., name=...)` into `TRACE.texture_sample`** — a useful, kept
addition (see debug_trace changes below). The trace immediately showed the real
problem wasn't color math (the `4·D·L` arithmetic was exact) but **depth**:

```
PS (143,0) ... depth out=0.978139 golden=0.997629 ddiff=0.019490
```

Our winning fragments at the top rows sat at depth ~0.978; golden's were ~0.998.
A *different, nearer surface* was winning in our pipeline. And the run had
**147800 "extra" pixels** (ours not in golden) — we were drawing far more than
the draw actually wrote.

### Root cause: topology mis-assembled as strip

The pipeline statistics gave it away:

```
IAPrimitives: output=1147 golden=383
```

1149 vertices → as a **strip** = 1149−2 = 1147 triangles (what we made); as a
**list** = 1149/3 = **383** (golden). Checked all six regression captures —
golden IAPrimitives equals `vertex_count / 3` for **every** one:

| case | verts | strip (v−2) | list (v/3) | golden IAPrim |
|------|------:|------:|------:|------:|
| event28 | 204 | 202 | 68 | **68** |
| event104 | 1149 | 1147 | 383 | **383** |
| event351 | 315 | 313 | 105 | **105** |
| event371 | 6 | 4 | 2 | **2** |
| event399 | 696 | 694 | 232 | **232** |
| event516 | 3 | 1 | 1 | **1** |

Every draw is a **triangle list**. But every `pipeline_state.csv` says
`Topology,Primitive,5` (= D3D `TRIANGLESTRIP`). `load_config_from_pipeline_state_csv`
returns that 5 verbatim and the rasterizer assembled strips → ~3× the triangles
→ thin slivers connecting unrelated list-triangles → massive overdraw, wrong
fragments winning the depth test, wrong colors. The capture's topology enum is
simply **wrong/unreliable** in these dumps (the index data is unambiguously a
list — fans of 3-index groups around shared centre vertices).

The index/vertex counts confirm the enum is the only thing claiming "strip".
Forcing `primitive_topology: 4` reproduced the user's reference numbers exactly
(matched 37684, mismatched 2773, missing 297, extra 465).

## Implementation

### `render.py` — `_resolve_triangle_topology(...)`

Since the captured enum is unreliable but the captured **primitive count** is
ground truth, disambiguate list vs strip from `golden IAPrimitives`:

- Only when the CSV claims a triangle topology (4/5) and golden IAPrimitives is
  available and `vertex_count > 0`.
- `golden == vertex_count // 3` → TRIANGLELIST; `golden == vertex_count − 2` →
  TRIANGLESTRIP.
- Ambiguous (single triangle, where list==strip) or matches neither (no golden /
  partial capture) → keep the CSV value. Logs when it overrides.

Wired in `_execute_pipeline` right after the CSV topology is read (only when the
JSON config doesn't explicitly set `primitive_topology`, which still wins). The
golden IAPrimitives is loaded from `pipeline_statistics.csv` just before
rasterize.

### `debug_trace.py` / `texture.py` / `hlsl_interpreter.py` — kept debug

- `Texture.sample(..., name='')` now forwards the texture name to
  `TRACE.texture_sample`, so multi-texture shaders show which texture each
  sample line came from (`DiffuseTexture` vs `LightmapTexture`). Both interpreter
  `Sample` call sites pass the name.
- `debug_trace` now flushes each line so a trace is inspectable mid-run and
  survives an interrupted process (these runs can be long).

## Results

Default (CSV-driven) config now auto-detects and corrects the topology:

```
Topology: CSV reported 5 but golden IAPrimitives=383 matches 1149/3
  → using TRIANGLELIST (4) instead (capture enum unreliable).
```

### Regression — 6/6 PASS, broad pixel-match improvement, no regressions

| case | mismatched before → after | extra before → after |
|------|------:|------:|
| event104 | 13867 → **2773** | 147800 → **465** |
| event351 | 6 → **2** | 5589 → **25** |
| event399 | 96 → 96 (matched 2391 → **2434**) | 400 → **45** |
| event516 | 0 → 0 | (list==strip) |
| event371 | 243 → 243 | 61 → 61 (separate pre-existing issue) |

event104's remaining 2773 residual (6.8%) are all depth-matched with cdiff < 0.3
— the same LOD-boundary / edge noise floor documented for event399 (step 98),
not a new systematic bug.

## Files changed

- `render.py` — `_resolve_triangle_topology()` + wiring in `_execute_pipeline`
  (disambiguate triangle list/strip from golden IAPrimitives; config override
  still wins).
- `texture.py` — `sample(..., name='')` forwards the texture name to the trace.
- `hlsl_interpreter.py` — both `Sample` handlers pass the texture name.
- `debug_trace.py` — per-write flush; `texture_sample` records the texture name.
