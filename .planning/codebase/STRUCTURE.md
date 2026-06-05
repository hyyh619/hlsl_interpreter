# Codebase Structure

**Analysis Date:** 2026-06-05

## Directory Layout

```
hlsl_interpreter/
├── render.py                 # CLI entry point & pipeline orchestrator
├── hlsl_interpreter.py       # Core: HLSLInterpreter (~3300 lines), VertexPool/Vertex
├── hlsl_syntax_tree.py       # Recursive-descent expression parser (SyntaxTreeParser/Node)
├── rasterizer.py             # Rasterizer: triangle/line/point + barycentric interpolation
├── output_merger.py          # Depth: depth/stencil test (early-Z / late-Z)
├── texture.py                # Texture/TextureDesc/Sampler: BMP + mipmaps + sampling
├── pixel.py                  # Pixel dataclass (rasterizer→depth→PS interchange record)
├── d3d.py                    # D3D enum constants (topology, shader stage)
├── mesh_view.py              # Optional tkinter mesh-viewer GUI (~92 KB)
├── CLAUDE.md                 # Primary orientation doc (read first)
├── ReadMe.md                 # Empty
├── rasterizer_param.json     # Default rasterizer params (legacy workflow)
├── animation_config.json     # Misc config (small)
├── Cases/                    # Input zips + JSON run configs + sample output.log
│   ├── Default.json          # Canonical entry config (points data_path at a zip)
│   ├── *.json                # Per-scenario run configs
│   ├── *.zip                 # RenderDoc/3Dmigoto frame-capture dumps
│   ├── zip_files.csv         # Index of the case zips
│   └── color-correct-ninjia-of-collision/  # One pre-extracted legacy-format case
├── Prompts/                  # Prompt/spec history that drove development
│   ├── hlsl-interpreter-prompt-ClaudeCode.md   # Richest record of bug fixes/decisions
│   └── hlsl-interpreter-prompt-OpenCode.md
├── Sessions/                 # 90 step-by-step dev session logs (hlsl-stepN-*.md)
├── .vscode/                  # launch.json (use), tasks.json (STALE — ignore)
└── .planning/codebase/       # GSD codebase-map output (this folder)
```

## Directory Purposes

**Repo root (`.py` files):**
- Purpose: All production source. Flat module layout — one file per pipeline stage / concern.
- Contains: Orchestrator, interpreter, parser, fixed-function stages, support types.
- Key files: `render.py`, `hlsl_interpreter.py`, `hlsl_syntax_tree.py`.

**`Cases/`:**
- Purpose: Inputs and run configurations. Each `.zip` is a captured frame dump; each `.json` is a run config naming a zip via `data_path`.
- Contains: `*.zip` captures, `*.json` configs, `Default.json` (canonical), `zip_files.csv` (index), `output.log` (last run output), and one pre-extracted folder `color-correct-ninjia-of-collision/` in the legacy CSV-struct format.
- Key files: `Cases/Default.json`, `Cases/output.log`.

**Inside each `Cases/*.zip` (extracted to a single top-level folder):**
- `VS_shader.hlsl`, `PS_shader.hlsl` — shader source; `main()` declares IO as params with semantics.
- `VS_input_output_signature.csv`, `PS_input_output_signature.csv` — param↔slot mapping.
- `ia_vertex_data.csv`, `ia_input_layouts.csv` — input-assembler vertex data + layout.
- `VS_constant_buffers.csv`, `PS_constant_buffers.csv` — combined cbuffer dumps.
- `pipeline_state.csv` — rasterizer/blend/depth-stencil state + primitive topology.
- `MeshOut_*_mesh.csv` — golden VS output (subject to the 3Dmigoto float3 shift gotcha).
- BMP textures consumed by sampling; `.dxbc`/`_disasm.txt`/`.bin` are reference artifacts (not consumed).

**`Prompts/`:**
- Purpose: Development history — the prompts/specs that drove the build.
- Key files: `Prompts/hlsl-interpreter-prompt-ClaudeCode.md` (worked bug examples, design decisions; §4 documents the golden-CSV float3 shift).

**`Sessions/`:**
- Purpose: 90 step-by-step session logs documenting how each feature was built. Use for "why does this work this way" archaeology.
- Generated: Authored during development. Committed: Yes.
- Naming: `hlsl-stepN-<description>.md`, plus topic logs (e.g. `hlsl-optimization.md`, `hlsl-python-float-precision.md`).

**`.vscode/`:**
- `launch.json` ("Python hlsl_interpreter") and `settings.json` are usable. `tasks.json` is a **stale leftover from an unrelated Irrlicht project — ignore it.**

## Key File Locations

**Entry Points:**
- `render.py`: CLI + orchestrator. `main()` at `render.py:444`.

**Configuration:**
- `Cases/Default.json`: canonical run config.
- `rasterizer_param.json`, `animation_config.json`: root-level defaults (legacy workflow).
- Per-case `pipeline_state.csv` / `rasterizer_param.json` inside zips/extracted folders.

**Core Logic:**
- `hlsl_interpreter.py`: parsing + VS/PS execution (`HLSLInterpreter` at `:291`).
- `hlsl_syntax_tree.py`: expression parser; precedence table `_OPERATORS` at `:14`.
- `rasterizer.py`: `Rasterizer.rasterize` at `:212`.
- `output_merger.py`: `Depth.execute` at `:163`.
- `texture.py`: `Texture.sample` at `:439`.

**Testing / Verification:**
- No test files. Verification is by running the pipeline and grepping `Cases/output.log` for `Error:` lines (see TESTING.md / CLAUDE.md "verify-by-log workflow").

## Naming Conventions

**Files:**
- Python modules: lowercase, often `snake_case` (`hlsl_interpreter.py`, `output_merger.py`); short single-word where natural (`render.py`, `texture.py`, `pixel.py`, `d3d.py`).
- Run configs: descriptive lowercase with hyphens/underscores (`Cases/wrong_constant_attenuation.json`, `Cases/color-correct-ninjia-of-collision.json`).
- Session logs: `hlsl-stepN-<kebab-description>.md`.
- Capture zips: `<scenario>_event<N>.zip`.

**Directories:**
- Top-level domain folders are TitleCase (`Cases/`, `Prompts/`, `Sessions/`).

**Code symbols:**
- Classes: `PascalCase` (`HLSLInterpreter`, `SyntaxTreeNode`, `Rasterizer`, `TextureDesc`).
- Methods/functions: `snake_case`; module-level cached helpers prefixed `_` (`_find_top_level_operator_cached`).
- Public stage-execution methods retain D3D-style mixed case (`executeVS_with_params`, `executePS_with_params`).

## Where to Add New Code

**New HLSL intrinsic / language feature:**
- Expression parsing: extend `SyntaxTreeParser._parse_expression` / `_parse_function_call` in `hlsl_syntax_tree.py`.
- Precedence change: edit only `_OPERATORS` in `hlsl_syntax_tree.py:14`.
- Evaluation: add a branch in `evaluate_syntax_tree` (`hlsl_interpreter.py:1124`) or `execute_function_node` (`:1190`) / `execute_method_call_node` (`:1550`).

**New pipeline stage behavior:**
- Rasterization (new topology / interpolation): `rasterizer.py` (`rasterize` dispatch at `:212`).
- Depth/stencil rules: `output_merger.py` (`Depth`).
- Texture sampling / address modes: `texture.py`.

**New interchange field (rasterizer → PS):**
- Add to the `Pixel` dataclass in `pixel.py`, populate in `rasterizer.py` interpolation, and map it in `executePS_with_params`'s `sem_to_pixel` table (`hlsl_interpreter.py:3222`).

**New test case:**
- Drop the capture zip into `Cases/`, add it to `Cases/zip_files.csv`, and add a `Cases/<name>.json` config pointing `data_path` at it.

**New D3D constant:**
- `d3d.py`.

## Special Directories

**`Cases/color-correct-ninjia-of-collision/`:**
- Purpose: A pre-extracted case in the legacy CSV-struct format (`VS_INPUT.csv`, `VS_OUTPUT.csv`, separate buffer CSVs, `texture_desc.json`, `sampler_config.json`).
- Generated: From capture. Committed: Yes.

**`Sessions/`:**
- Purpose: Development archaeology (90 markdown logs). Not consumed at runtime.
- Generated: Hand-authored during development. Committed: Yes.

**`__pycache__/`:**
- Purpose: Python bytecode cache. Generated: Yes. Committed: No (build artifact).

**`.planning/codebase/`:**
- Purpose: GSD codebase-map documents. Generated: Yes.

---

*Structure analysis: 2026-06-05*
