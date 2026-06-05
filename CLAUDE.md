# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A pure-Python software emulation of the Direct3D 11 graphics pipeline that **interprets HLSL shader source directly** (no compilation, no `eval`). It runs the full fixed-function + programmable pipeline — Vertex Shader → Rasterizer → Depth/Stencil → Pixel Shader → Output Merger — against captured GPU data and compares VS output against "golden" reference data to validate correctness. Inputs are RenderDoc / 3Dmigoto frame-capture dumps packaged as zip archives.

There are **no third-party dependencies** — only the Python standard library plus `tkinter` (for the optional mesh viewer GUI). There is no `requirements.txt`, build step, lint config, or test suite.

## Running

```bash
python render.py ./Cases/Default.json
```

The single CLI argument is a JSON config file. `Cases/Default.json` is the canonical entry point; it points `data_path` at a zip in `Cases/`. Config keys of note: `data_path` (zip to run), `log_file_path` (relative to the config file — Default writes `Cases/output.log`), `execute_count` (`-1` = all vertices), `float_tolerance`, `mesh_view_enabled`, `early_z`, `log_file_mode` (`"w"` truncates).

VS Code launch configs exist (`.vscode/launch.json`, "Python hlsl_interpreter"). The `.vscode/tasks.json` build tasks are **stale leftovers from an unrelated project (Irrlicht examples)** — ignore them.

## The verify-by-log workflow

This project has no unit tests. Correctness is validated by running the pipeline and grepping the log:

1. `python render.py ./Cases/Default.json`
2. Read `Cases/output.log` and search for lines starting with `Error:` — these are per-component VS-vs-golden mismatches (`output=… golden=… diff=…`).
3. Iterate on the fix until no `Error:` lines remain.

`output.log` also contains `[STMT]`/`[SYNTAX TREE]` traces showing each interpreted statement and its parsed tree — the primary debugging surface for interpreter bugs.

## Architecture

`render.py` is the orchestrator. It branches on the config:
- **Zip workflow** (`_run_zip_workflow` → `_execute_pipeline`): the current path. Extracts the zip to a temp dir and drives the pipeline from loose CSV/HLSL/BMP files (see file conventions below).
- **Legacy workflow** (`_run_legacy_workflow`): the old struct-based path, kept for back-compat with older JSON configs that specify `hlsl_file_path`/`csv_folder_path` and use `struct`-defined VS_INPUT/VS_OUTPUT. Triggered only when `data_path` is absent.

Module responsibilities:
- **`hlsl_interpreter.py`** (~3300 lines, the core). `HLSLInterpreter` parses HLSL (cbuffers, structs, functions, texture/sampler bindings), then walks statements and evaluates expressions. Two execution entry styles mirror the two workflows: `executeVS_with_params`/`executePS_with_params`/`_execute_void_main` (signature-driven, current) vs. `executeVS`/`executePS`/`interpret` (struct-driven, legacy). `VertexPool`/`Vertex` hold per-vertex IO for the mesh viewer.
- **`hlsl_syntax_tree.py`**. A hand-written recursive-descent expression parser producing `SyntaxTreeNode` trees, evaluated by `evaluate_syntax_tree` in the interpreter. **Operator precedence lives in the `_OPERATORS` dict** in this file — if you touch precedence, that table is the source of truth. Top-level operator finding, arg splitting, and ternary-colon scanning are `@lru_cache`d module-level functions for performance.
- **`rasterizer.py`**. `Rasterizer.rasterize` dispatches by primitive topology (triangle list/strip, line list/strip, point list). Triangle rasterization uses edge functions + perspective-correct barycentric interpolation; viewport transform and back/front-face culling are config-driven. Reads state from `pipeline_state.csv` (`load_config_from_pipeline_state_csv`) or a JSON `rasterizer_param.json`.
- **`output_merger.py`**. `Depth` runs the depth/stencil test, supporting both early-Z (before PS) and late-Z (after PS) per the `early_z` config flag.
- **`texture.py`**. `Texture`/`TextureDesc`/`Sampler` — BMP parsing, mipmap generation, nearest/linear sampling, and address modes (wrap/mirror/clamp/border). Backs HLSL `Texture2D.Sample(...)` calls.
- **`pixel.py`**. `Pixel` dataclass: the rasterizer→depth→PS interchange record (screen x/y, depth, interpolated attributes).
- **`d3d.py`**. D3D enum constants (`SHADER_STAGE_*`, primitive topology values).
- **`mesh_view.py`**. Optional `tkinter` GUI (`MeshView`) for visualizing input/output meshes and rasterized pixels; runs on a separate thread. Enabled via `mesh_view_enabled`.

## Two non-obvious gotchas (read before "fixing" VS output)

1. **3Dmigoto trailing-`float3` misalignment in golden data.** The `MeshOut_*_mesh.csv` golden capture shifts trailing `float3` VS outputs (e.g. WORLDPOS) by one float: `WORLDPOS.x` actually holds `o.y`, `WORLDPOS.y` holds `o.z`, and `WORLDPOS.z` is the *next vertex's* data (garbage). `load_vs_golden_from_mesh_csv` compensates by remapping to `[None, x, y]`, and `compare_vs_output_with_golden_params` skips `None` components. A correct interpreter output can therefore look "wrong" against a naive read of the golden CSV — verify the math before changing the interpreter. See `Prompts/hlsl-interpreter-prompt-ClaudeCode.md` §4 for a worked example.

2. **Unary vs. binary `+`/`-` in expression parsing.** `r0.xyz * -r2.xxx + r1.xyz` must not treat the `-` after `*` as subtraction. `_find_top_level_operator_cached` looks back at the previous non-space char; if it's an operator/delimiter (`+-*/%(,[|&!<>=`), the `+`/`-` is unary and skipped as a binary-split candidate. Bitwise `|`/`&` are also handled specially (and pre-empt the cast-pattern branch). Precedence bugs here surface as wrong colors/vectors, not crashes.

## Data file conventions (inside each `Cases/*.zip`)

The zip extracts to a single top-level folder containing (paths resolved in `_execute_pipeline`):
- `VS_shader.hlsl`, `PS_shader.hlsl` — shader source. `main()` declares inputs/outputs as **parameters with semantics** (`out float4 o0 : SV_POSITION0`), not structs. Parsed by `parse_main_params_with_semantics`.
- `VS_input_output_signature.csv`, `PS_input_output_signature.csv` — signatures used to map params ↔ register slots (`map_params_to_signature`). VS output / PS input are wired by matching semantics across these.
- `ia_vertex_data.csv` + `ia_input_layouts.csv` — input-assembler vertex data and its layout, mapped to VS inputs via the input signature.
- `VS_constant_buffers.csv`, `PS_constant_buffers.csv` — combined cbuffer dumps (`load_all_cbuffers_from_combined_csv`).
- `pipeline_state.csv` — rasterizer/blend/depth-stencil state **and** primitive topology (CSV topology wins unless `primitive_topology` is explicitly set in the JSON config).
- `MeshOut_*_mesh.csv` — golden VS output (subject to gotcha #1).
- Textures are BMP files; `.dxbc`/`_disasm.txt`/`.bin` files are reference artifacts, not consumed by the interpreter.

## Project docs & history

- `Prompts/` holds the prompt/spec history that drove development — `hlsl-interpreter-prompt-ClaudeCode.md` is the richest record of recent bug fixes and design decisions.
- `Sessions/` contains 80+ step-by-step session logs (`hlsl-stepN-*.md`) documenting how each feature was built. Useful for "why does this work this way" archaeology.
- `ReadMe.md` is currently empty.
