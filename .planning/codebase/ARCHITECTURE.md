<!-- refreshed: 2026-06-05 -->
# Architecture

**Analysis Date:** 2026-06-05

## System Overview

A pure-Python software emulation of the Direct3D 11 graphics pipeline. The orchestrator (`render.py`) drives data through a fixed sequence of stages, with the programmable stages (VS, PS) backed by a tree-walking HLSL interpreter that reads shader source directly — no compilation, no `eval`.

```text
┌─────────────────────────────────────────────────────────────┐
│                     Orchestration Layer                       │
│                       `render.py`                             │
│   main() → branch on config → _run_zip_workflow /             │
│            _run_legacy_workflow → _execute_pipeline           │
└───────────────────────────────┬───────────────────────────────┘
                                │ drives
         ┌──────────────┬───────┴───────┬───────────────┐
         ▼              ▼               ▼               ▼
┌────────────────┐ ┌──────────┐ ┌────────────┐ ┌──────────────┐
│ Vertex Shader  │ │Rasterizer│ │Depth/Stencil│ │ Pixel Shader │
│`hlsl_interp..` │ │`rasteriz.│ │`output_mer.│ │`hlsl_interp..`│
│ executeVS_*    │ │ .rasterize│ │ Depth.exec │ │ executePS_*  │
└───────┬────────┘ └────┬─────┘ └─────┬──────┘ └──────┬───────┘
        │ list[dict]    │ list[Pixel] │ list[Pixel]   │ pixel.ps_output_color
        └───────────────┴─────────────┴───────────────┘
                                │ uses
         ┌──────────────────────┼──────────────────────┐
         ▼                      ▼                      ▼
┌────────────────┐  ┌────────────────────┐  ┌──────────────────┐
│ Expression     │  │ Texture sampling   │  │ Interchange types │
│ parser         │  │ `texture.py`       │  │ `pixel.py`        │
│`hlsl_syntax_   │  │ Texture/Sampler/   │  │ `d3d.py` (enums)  │
│ tree.py`       │  │ TextureDesc        │  │                   │
└────────────────┘  └────────────────────┘  └──────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────┐
│  Optional output: `mesh_view.py` (tkinter GUI, separate thread)│
└─────────────────────────────────────────────────────────────┘
```

## Component Responsibilities

| Component | Responsibility | File |
|-----------|----------------|------|
| Orchestrator | Parse JSON config, branch workflows, sequence the pipeline stages, manage timing/logging | `render.py` |
| HLSL Interpreter | Parse HLSL (cbuffers/structs/functions/bindings), walk statements, evaluate expressions, run VS & PS | `hlsl_interpreter.py` |
| Expression parser | Recursive-descent parse of HLSL expressions into `SyntaxTreeNode` trees; owns operator precedence | `hlsl_syntax_tree.py` |
| Rasterizer | Primitive assembly + triangle/line/point rasterization, viewport transform, culling, barycentric interpolation | `rasterizer.py` |
| Depth/Stencil | Early-Z / late-Z depth and stencil testing, per `early_z` flag | `output_merger.py` |
| Texture unit | BMP parsing, mipmap generation, nearest/linear sampling, address modes; backs `Texture2D.Sample` | `texture.py` |
| Interchange record | `Pixel` dataclass: rasterizer → depth → PS payload | `pixel.py` |
| D3D constants | Primitive-topology and shader-stage enum values | `d3d.py` |
| Mesh viewer (optional) | tkinter GUI on a separate thread visualizing meshes/pixels | `mesh_view.py` |

## Pattern Overview

**Overall:** Staged pipeline (pipes-and-filters) orchestrated by a procedural driver, with a tree-walking interpreter as the programmable-stage engine.

**Key Characteristics:**
- Each stage is a discrete object/function with a typed input and output; `render.py` wires them in sequence and passes intermediate collections explicitly (no shared global pipeline state).
- The programmable stages (VS, PS) are the *same* `HLSLInterpreter` class instantiated twice — once per shader stage — never sharing interpreter state.
- Interpretation is two-phase: parse HLSL into structures + per-statement syntax trees (`GenerateStmts`, `SyntaxTreeParser.parse`), then evaluate trees per vertex/pixel (`evaluate_syntax_tree`).
- Configuration is data-driven: a single JSON config selects the workflow and tunes behavior; pipeline state (topology, raster/depth state) comes from CSV captured from the GPU.
- No third-party dependencies; standard library plus `tkinter` only.

## Layers

**Orchestration Layer:**
- Purpose: Decode config, choose workflow, sequence stages, own timing and final summary.
- Location: `render.py`
- Contains: `main`, `_run_zip_workflow`, `_run_legacy_workflow`, `_execute_pipeline`, `_make_interpreter`.
- Depends on: `HLSLInterpreter`, `Rasterizer`, `Depth`, `d3d`.
- Used by: CLI entry (`python render.py <config.json>`).

**Programmable-stage Layer (interpreter):**
- Purpose: Parse and execute HLSL shaders.
- Location: `hlsl_interpreter.py`, `hlsl_syntax_tree.py`
- Contains: `HLSLInterpreter`, `SyntaxTreeParser`, `SyntaxTreeNode`, and the parsing/evaluation/data-loading methods.
- Depends on: `texture.py` (for `Sample`), `pixel.py` (PS input), `d3d.py` (stage constants), `mesh_view.py` (optional).
- Used by: `render.py`.

**Fixed-function Layer:**
- Purpose: Rasterization and depth/stencil testing.
- Location: `rasterizer.py`, `output_merger.py`
- Contains: `Rasterizer`, `Triangle`, `Viewport`, `RasterizerConfig`; `Depth`, `DepthStencilOpDesc`.
- Depends on: `pixel.py` (produces/consumes `Pixel`), `d3d.py` (topology constants).
- Used by: `render.py`.

**Support Layer:**
- Purpose: Reusable value types and resource emulation.
- Location: `pixel.py`, `texture.py`, `d3d.py`
- Used by: all stages.

## Data Flow

### Primary Request Path (Zip Workflow — current)

1. CLI invokes `main()`; reads JSON config; presence of `data_path` selects the zip workflow (`render.py:444`).
2. `_run_zip_workflow` resolves the zip path relative to the config dir, extracts to a temp dir, locates the single top-level folder (`render.py:89`).
3. `_execute_pipeline` resolves the loose data files in that folder (`VS_shader.hlsl`, `*_signature.csv`, `ia_vertex_data.csv`, `*_constant_buffers.csv`, `pipeline_state.csv`, `MeshOut_vs_mesh.csv`) (`render.py:121`).
4. **VS setup:** preprocess + parse texture/sampler bindings, cbuffers, all functions; load cbuffer data; load VS signature; parse `main()` params with semantics; map params ↔ signature slots; load IA vertex data; load golden VS output (`render.py:154`).
5. **VS execute:** `executeVS_with_params` runs `main()` once per vertex via `_execute_void_main`, returns `list[dict]` keyed by canonical semantic names (`sv_position`, `Color`, `TexCoord`, …) (`hlsl_interpreter.py:3173`).
6. **VS verify:** `compare_vs_output_with_golden_params` logs `Error:` lines for per-component mismatches (`render.py:226`).
7. **Rasterize:** `Rasterizer.load_config_from_pipeline_state_csv` reads state + topology (CSV wins unless config overrides); `rasterize(vs_results, topology)` dispatches by topology and emits `list[Pixel]` with interpolated attributes (`render.py:237`, `rasterizer.py:212`).
8. **Early-Z (if `early_z`):** `Depth.execute(pixels, early_z=True)` filters pixels before PS (`render.py:252`, `output_merger.py:163`).
9. **PS setup/execute:** a *second* `HLSLInterpreter` parses `PS_shader.hlsl`; `executePS_with_params` maps each `Pixel`'s attributes to PS input params, runs `main()`, writes `pixel.ps_output_color` (`render.py:261`, `hlsl_interpreter.py:3215`).
10. **Late-Z (if not `early_z`):** `Depth.execute(pixels, early_z=False)` runs after PS (`render.py:297`).
11. Pipeline summary logged; temp dir removed (`render.py:117`, `301`).

### Per-statement interpretation (inside VS/PS)

1. `_execute_void_main` builds `local_vars` from inputs/cbuffers, collects the statements of `main` (`hlsl_interpreter.py:3076`).
2. `GenerateStmts` splits the function body into statements (`hlsl_interpreter.py:1974`).
3. `execute_statement` classifies each statement (declaration, assignment, if, return) (`hlsl_interpreter.py:1798`).
4. RHS expressions go through `evaluate_expression` → `SyntaxTreeParser.parse` → `evaluate_syntax_tree`, which recurses over `SyntaxTreeNode` (`hlsl_interpreter.py:1097`, `1124`).
5. Function/method nodes dispatch to `execute_function_node` / `execute_method_call_node` (intrinsics like `mul`, `dot`, `normalize`, and `Texture2D.Sample`) (`hlsl_interpreter.py:1190`, `1550`).
6. `[STMT]` / `[SYNTAX TREE]` traces are logged per statement — the primary interpreter-debugging surface.

### Legacy Flow (struct-based, back-compat)

1. Triggered when `data_path` is absent (`render.py:461`).
2. `_run_legacy_workflow` resolves `hlsl_file_path` / `csv_folder_path`, builds one interpreter, calls `interpret()` then `executeVS("vs_main", "VS_INPUT", …)` (struct-defined IO) (`render.py:309`).
3. Same rasterizer → depth → `executePS("ps_main", "PS_INPUT_BASIC", …)` sequence; supports the interactive mesh viewer loop.

**State Management:**
- No global mutable pipeline state; intermediate collections (`vs_results`, `pixels`) are passed explicitly between stages.
- Per-interpreter state: `cbuffers`, parsed `functions`, texture/sampler bindings, and `vertex_pool` live on the `HLSLInterpreter` instance and are populated during setup.
- `Rasterizer` holds a depth buffer / pixel list across a `rasterize` call; `Depth` holds depth/stencil buffers across `execute`.

## Key Abstractions

**SyntaxTreeNode / SyntaxTreeParser:**
- Purpose: Represent and parse an HLSL expression. Node types: `value`, `function`, `method_call`, `binary_op`, `unary_op`, `cast`, `ternary`.
- Examples: `hlsl_syntax_tree.py:122` (node), `:187` (parser).
- Pattern: Recursive descent. **Operator precedence is the `_OPERATORS` dict** (`hlsl_syntax_tree.py:14`) — the single source of truth.

**HLSLInterpreter:**
- Purpose: Whole shader-stage engine — parse + evaluate.
- Examples: `hlsl_interpreter.py:291`.
- Pattern: Tree-walking interpreter; two execution entry styles (param-driven vs. struct-driven) mirroring the two workflows.

**Pixel (dataclass):**
- Purpose: The rasterizer → depth → PS interchange record (screen x/y, depth, interpolated attributes, `ps_output_color`).
- Examples: `pixel.py:6`.

**VertexPool / Vertex:**
- Purpose: Per-vertex input/output storage for the mesh viewer.
- Examples: `hlsl_interpreter.py:78`, `:101`.

**Texture / TextureDesc / Sampler:**
- Purpose: Texture resource emulation backing `Texture2D.Sample`.
- Examples: `texture.py:264`, `:214`, `:115`.

## Entry Points

**CLI:**
- Location: `render.py:444` (`main`), guarded by `if __name__ == '__main__'` at `render.py:465`.
- Triggers: `python render.py <config.json>`.
- Responsibilities: Load config, branch to zip or legacy workflow.

**VS execution:**
- Location: `executeVS_with_params` (`hlsl_interpreter.py:3173`, current) / `executeVS` (`:2199`, legacy).

**PS execution:**
- Location: `executePS_with_params` (`hlsl_interpreter.py:3215`, current) / `executePS` (`:2276`, legacy).

## Architectural Constraints

- **Threading:** Single-threaded pipeline. The interpreter accepts a `max_workers` config (default 1) but the canonical path runs serially. The only other thread is the optional `mesh_view.py` tkinter GUI, which runs on its own thread.
- **Global state:** Module-level `@lru_cache`d helpers in `hlsl_syntax_tree.py` (`_split_args_cached`, `_find_top_level_operator_cached`, `_is_proper_paren`, `_find_ternary_colon`) cache by expression string. The precedence table `_OPERATORS` and `_COMPILED_PATTERNS` are module-level constants. No mutable cross-stage globals.
- **Two interpreter instances:** VS and PS use separate `HLSLInterpreter` objects (`render.py:154`, `:262`); they must not share parsed state.
- **No compilation / no `eval`:** All shader logic flows through the recursive-descent parser and tree walker. New HLSL constructs require interpreter support, not a library.
- **Config-vs-CSV precedence:** Primitive topology and raster/depth state come from `pipeline_state.csv` unless the JSON config explicitly overrides (`render.py:241`).

## Anti-Patterns

### Reading golden CSV naively when validating VS output

**What happens:** 3Dmigoto's `MeshOut_*_mesh.csv` shifts trailing `float3` VS outputs by one float (e.g. `WORLDPOS.x` actually holds `o.y`, `.z` holds the next vertex's garbage).
**Why it's wrong:** A correct interpreter output looks "wrong" against the raw CSV, tempting an incorrect interpreter "fix."
**Do this instead:** Trust `load_vs_golden_from_mesh_csv` (`hlsl_interpreter.py:2998`), which remaps to `[None, x, y]`; `compare_vs_output_with_golden_params` (`:3270`) skips `None` components. Verify the math before changing the interpreter. See `Prompts/hlsl-interpreter-prompt-ClaudeCode.md` §4.

### Treating a unary `+`/`-` as a binary split point

**What happens:** In `r0.xyz * -r2.xxx + r1.xyz`, naively splitting on `-` after `*` produces a wrong tree.
**Why it's wrong:** Precedence bugs here surface as wrong colors/vectors, not crashes — hard to spot.
**Do this instead:** `_find_top_level_operator_cached` (`hlsl_syntax_tree.py:49`) looks back at the previous non-space char; if it is in `+-*/%(,[|&!<>=`, the `+`/`-` is unary and skipped. Bitwise `|`/`&` are handled before the cast-pattern branch (`hlsl_syntax_tree.py:210`).

### Editing precedence anywhere but `_OPERATORS`

**What happens:** Precedence logic gets duplicated in the evaluator.
**Why it's wrong:** Divergence between parse-time and eval-time precedence yields silent wrong results.
**Do this instead:** Change only `_OPERATORS` in `hlsl_syntax_tree.py:14`.

## Error Handling

**Strategy:** Validate-and-log rather than raise. Missing optional files (golden CSV, PS shader, pipeline state) are tolerated; missing required files print an error and return/exit early.

**Patterns:**
- Fatal config/data problems: `print(...)` + `sys.exit(1)` in `render.py` (e.g. missing `data_path` at `:100`, missing VS shader at `:157`).
- Validation failures: emitted as `Error:` log lines (per-component VS-vs-golden mismatch) — these are the correctness signal, not exceptions.
- Optional stages guarded by `os.path.exists` checks before running.

## Cross-Cutting Concerns

**Logging:** `HLSLInterpreter.log_output` writes to the config-resolved `log_file_path` (default `Cases/output.log`) with a cache flush; per-statement `[STMT]`/`[SYNTAX TREE]` traces are the debugging surface. `log_file_mode: "w"` truncates per run.
**Validation:** Correctness is verified by grepping the log for `Error:` lines (no unit-test suite). Float comparisons use `float_tolerance` from config.
**Authentication:** Not applicable (offline CLI tool).
**Configuration:** Single JSON config drives everything; pipeline state additionally read from captured CSV.

---

*Architecture analysis: 2026-06-05*
