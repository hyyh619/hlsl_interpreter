# Coding Conventions

**Analysis Date:** 2026-06-05

This is a pure-Python (standard-library-only) software emulation of the D3D11 pipeline. There is no linter, formatter, or style config in the repo, so conventions are inferred from the existing code and must be matched by hand. Indentation is **4 spaces**, files are UTF-8, and there is no shebang line on modules (entry is `python render.py`).

## Naming Patterns

**Files:**
- Module files are lowercase, single-word or `snake_case`: `hlsl_interpreter.py`, `hlsl_syntax_tree.py`, `output_merger.py`, `mesh_view.py`, `d3d.py`, `pixel.py`, `rasterizer.py`, `render.py`, `texture.py`.
- One major class per module, named for the file (`Rasterizer` in `rasterizer.py`, `HLSLInterpreter` in `hlsl_interpreter.py`).

**Functions / methods:**
- `snake_case` for functions and methods: `parse_main_params_with_semantics`, `load_all_cbuffers_from_combined_csv`, `compare_vs_output_with_golden_params`.
- Private/internal helpers are prefixed with a single underscore: `_execute_pipeline`, `_make_interpreter`, `_parse_texture_and_sampler_bindings`, `_find_top_level_operator_cached`, `_flush_log_cache`.
- HLSL-domain entry points keep their D3D-ish camelCase names: `executeVS`, `executePS`, `executeVS_with_params`, `executePS_with_params`. This is a deliberate exception to `snake_case` — match it for new pipeline-stage entry points, but use `snake_case` for everything else.

**Variables:**
- `snake_case` locals throughout (`vs_input_params`, `golden_vs_rows`, `float_tolerance`, `data_folder`).
- Config-mirroring booleans keep the config key name verbatim, including the few camelCase config keys (`printSyntaxTree`, `print_interpreter_result`). When reading config, preserve the existing key spelling.

**Types / classes:**
- `PascalCase`: `HLSLInterpreter`, `SyntaxTreeNode`, `SyntaxTreeParser`, `Rasterizer`, `Pixel`, `TextureDesc`, `RasterizerConfig`, `CullMode`.
- Enum members are `UPPER_SNAKE`: `CullMode.BACK`, `FillMode.SOLID`, `FrontFace.COUNTER_CLOCKWISE` (`rasterizer.py:19-33`).

**Module-level constants:**
- `UPPER_SNAKE`: `DATA_TYPE_LIST` (`hlsl_interpreter.py:25`), the D3D enum ints in `d3d.py` (`D3D_PRIMITIVE_TOPOLOGY_*`, `SHADER_STAGE_*`), and availability flags `TEXTURE_AVAILABLE` / `MESHVIEW_AVAILABLE`.

## Code Style

**Formatting:**
- No formatter (no Black/autopep8 config). Style is hand-maintained: 4-space indent, no trailing whitespace enforcement, generally `<120` col lines but long f-strings and signatures run wide.
- Multi-line call signatures align continuation args under the opening paren (see `HLSLInterpreter.__init__`, `hlsl_interpreter.py:297-309`).

**Linting:** None configured. Do not add lint config without being asked.

**Comments / docstrings — bilingual:**
- Docstrings and inline comments are predominantly **Simplified Chinese** in the core modules (`hlsl_interpreter.py`, `hlsl_syntax_tree.py`, `mesh_view.py`). Example: `SyntaxTreeNode` docstring (`hlsl_syntax_tree.py:122-133`), every `@dataclass` field comment in `hlsl_interpreter.py:44-99`.
- Newer / pipeline-glue code (`render.py`, `rasterizer.py`, `output_merger.py`, `pixel.py`, and the param-based methods in `hlsl_interpreter.py`) uses **English** docstrings.
- Convention: match the surrounding language. Editing a Chinese-commented method → keep Chinese; new English-commented files (`render.py`, dataclasses in `pixel.py`/`rasterizer.py`) → keep English. Do not mass-translate existing comments.

## Type Hints

- Type hints are used broadly but not enforced. `from typing import Any, Dict, List, Optional, Tuple, Union` is the standard import set.
- Public method signatures are annotated (`def compare_vs_output_with_golden_params(self, results: list, output_params: list, golden_rows: list, float_tolerance: float = 0.0001, execute_count: int = None) -> bool:`).
- Mixed convention: some signatures use lowercase builtins (`list`, `dict`) and others use `typing` generics (`List[...]`, `Dict[...]`) — both appear in the same file. Prefer matching the local method's existing style.

## Import Organization

Order observed across modules (e.g. `hlsl_interpreter.py:1-41`, `rasterizer.py:1-16`):
1. Stdlib imports (`csv`, `math`, `re`, `os`, `json`, `from dataclasses import ...`, `from enum import Enum`, `from typing import ...`, `from functools import lru_cache`, `from concurrent.futures import ThreadPoolExecutor`).
2. First-party module imports (`from hlsl_syntax_tree import ...`, `from pixel import Pixel`, `from d3d import (...)`).
3. **Optional dependencies guarded by try/except** with a module-level availability flag:
   ```python
   try:
       from texture import Texture, Sampler, TextureDesc, Sampler as SamplerClass
       TEXTURE_AVAILABLE = True
   except ImportError:
       TEXTURE_AVAILABLE = False
   ```
   (`hlsl_interpreter.py:11-22`). `mesh_view`/`tkinter` is guarded the same way (`MESHVIEW_AVAILABLE`). Always gate `tkinter`-dependent code behind `MESHVIEW_AVAILABLE` and texture code behind `TEXTURE_AVAILABLE`.
- No path aliases; flat module layout, all imports are top-level module names. A few imports are deferred into functions to avoid pulling `tkinter` at import time (e.g. `from texture import ...` inside `_run_legacy_workflow`, `render.py:339`).

## Structural Patterns

**Dataclasses (`@dataclass`) — the standard record type:**
- Used for all plain data records: `ShaderVariable`, `FieldDefinition`, `TextureBinding`, `SamplerBinding`, `Vertex`, `StructDefinition`, `CbufferDefinition` (`hlsl_interpreter.py:44-289`); `Pixel` (`pixel.py:5`); `Viewport`, `ScissorRect`, `RasterizerConfig`, `Triangle` (`rasterizer.py:36-99`); records in `output_merger.py:42+`.
- **Mutable-default idiom:** dataclasses use `= None` defaults for `Dict`/`List` fields and populate them in `__post_init__` rather than using `field(default_factory=...)`:
  ```python
  @dataclass
  class Pixel:
      attributes: Dict[str, Any]
      def __post_init__(self):
          if self.attributes is None:
              self.attributes = {}
  ```
  (`pixel.py:5-25`, `Vertex.__post_init__` `hlsl_interpreter.py:94-98`, `RasterizerConfig.__post_init__` `rasterizer.py:86-90`). Follow this pattern for new dataclasses with collection fields.
- `field` is imported (`from dataclasses import dataclass, field`) but the `default_factory` pattern is rarely used — the `None` + `__post_init__` idiom dominates.
- Field-level comments document units/meaning, one per line, aligned (`Pixel`, `Vertex`).

**Enums (`enum.Enum`):**
- Domain state enums live in the stage module that owns them: `CullMode`/`FillMode`/`FrontFace` (`rasterizer.py:19-33`), `ComparisonFunc`/`StencilOp`/`StencilFunc` (`output_merger.py:9-41`). Values are explicit ints matching D3D semantics.
- **Raw D3D constants** that mirror the native enum *values* (topology, shader stage) are plain module-level ints in `d3d.py`, NOT `Enum` subclasses, because they are compared directly against CSV/JSON integer values. Keep new D3D wire-value constants in `d3d.py` as ints; use `Enum` only for internal pipeline state.

**The `SyntaxTreeNode` pattern (`hlsl_syntax_tree.py:122-184`):**
- A single hand-written node class with a `node_type` discriminator string rather than a class hierarchy. Valid types: `'value'`, `'function'`, `'method_call'`, `'binary_op'`, `'unary_op'`, `'cast'`, `'ternary'`.
- Fixed child slots: `left`, `right`, `third_child` (ternary false-branch), plus `args: List[SyntaxTreeNode]` for calls, plus `value` (operator/name/literal) and `line_number`.
- `SyntaxTreeParser._parse_expression` is a recursive-descent parser that branches on `node_type` to build the tree; `HLSLInterpreter.evaluate_syntax_tree` consumes it by switching on `node_type`. When adding a construct, extend both the parser branch and the evaluator switch, and add a `_pretty()` case in `SyntaxTreeNode._pretty` (used for `[SYNTAX TREE]` debug output).
- **Operator precedence is data, not code:** the `_OPERATORS` dict (`hlsl_syntax_tree.py:14-21`) maps operator string → precedence int and is the single source of truth. `_find_top_level_operator_cached` picks the lowest-precedence, rightmost operator. Do not hard-code precedence elsewhere.

**`@lru_cache` for hot parser helpers:**
- Module-level pure functions in `hlsl_syntax_tree.py` are decorated `@lru_cache(maxsize=256)`: `_split_args_cached`, `_find_top_level_operator_cached`, `_is_proper_paren`, `_find_ternary_colon`. Because cached args must be hashable, these return **tuples** (e.g. `_split_args_cached` returns `Tuple[str, ...]`, and the instance method `_split_args` wraps it with `list(...)`, `hlsl_syntax_tree.py:330-331`).
- Rule for cached helpers: keep them module-level (not methods), take only hashable args (strings/ints), and return hashable results. Do not cache anything that depends on `self` or mutable interpreter state.
- `_COMPILED_PATTERNS` (`hlsl_syntax_tree.py:6-12`) and `HLSLInterpreter.patterns` (`hlsl_interpreter.py:347-389`) pre-compile every `re.Pattern` once into a dict keyed by purpose, with the purpose documented in an inline comment above each pattern. Add new regexes to these dicts rather than compiling inline.

## Error Handling

- **No custom exception classes and no `raise` in the pipeline.** The interpreter is designed to run to completion and report mismatches via the log, not to crash.
- Errors and warnings are reported as **log lines**, not exceptions. Two conventions:
  - User/setup errors in `render.py` use bare `print(f"Error: ...")` followed by `sys.exit(1)` or `return` (`render.py:99-101`, `156-158`, `183-184`, `451-453`).
  - Data/comparison errors inside the interpreter use `self.log_output(f"Error: ...")` and continue (`hlsl_interpreter.py:2545`, `2631`, `2665`, `3299-3309`). **These `Error:` lines are the test oracle** (see TESTING.md).
  - Recoverable issues use the `Warning:` prefix (`hlsl_interpreter.py:2332`, `2593`).
- `try/except` is used narrowly and defensively, swallowing the exception to keep the row/loop going:
  - `except ValueError` / `except (ValueError, IndexError)` around float parsing of CSV cells (`hlsl_interpreter.py:1634-1636`, `1706-1709`, `3048-3049`) — failures `pass` and leave the value unset.
  - `try/except ImportError` for optional deps (texture, mesh_view).
- Float comparisons always use a tolerance, never `==`: `abs(ov - gv) > float_tolerance` (`hlsl_interpreter.py:3298`). Degenerate divisors are guarded with epsilon checks (`abs(clip_w) < 1e-8`, `rasterizer.py:52`).
- Internal diagnostics use bracketed-tag debug lines via `debug_print`: `[ERROR] transpose requires 1 arg ...` (`hlsl_interpreter.py:1204`) — these are debug traces, distinct from the oracle `Error:` lines.

## Logging

- **Framework:** none — custom logging via `HLSLInterpreter.log_output` (`hlsl_interpreter.py:510-519`) and `HLSLInterpreter.debug_print` (`521-524`). No use of the stdlib `logging` module.
- `log_output(*args, **kwargs)` mirrors `print` semantics: it `print(*args)` to stdout **and** appends to an in-memory cache that is flushed to the log file. Use it like `print` (`self.log_output(f"...")`).
- **Buffered file writes:** lines are accumulated in `self._log_cache` and flushed when `self._log_cache_bytes` exceeds `self._log_cache_size` (default 10 MiB, `log_cache_size`) or on object destruction (`_flush_log_cache`, `__del__`, `hlsl_interpreter.py:394-400`, `502-508`). Do not assume a line is on disk immediately; the flush is size-triggered.
- **Log file config:** controlled by `log_to_file` (bool), `log_file_path` (resolved relative to the config JSON in `render.py`), and `log_file_mode` (`'w'` truncates, `'a'` appends). `Cases/Default.json` writes `Cases/output.log` with mode `"w"`. The file is opened once in `__init__` (`hlsl_interpreter.py:391-392`). `*.log` is gitignored.
- **`debug_print` gating:** only emits when `self.debug` is true AND `self._should_print` is true. `_should_print` is recomputed per evaluated row as `(self._eval_counter - 1) % self.print_sequence == 0` (`hlsl_interpreter.py:3093-3094`), so `print_sequence=100` logs every 100th row. Use `debug_print` for per-statement/per-row traces (`[STMT]`, `[SYNTAX TREE]`, `[FUNC]`, `[BINARY OP]` tags) and `log_output` for always-on pipeline milestones and the `Error:`/`PASSED`/`FAILED` oracle lines.

## Function Design

- Two parallel API styles in `HLSLInterpreter`, mirroring the two `render.py` workflows — keep them separate:
  - **Param/signature-driven (current zip workflow):** `executeVS_with_params`, `executePS_with_params`, `_execute_void_main`, `parse_main_params_with_semantics`, `map_params_to_signature`, `compare_vs_output_with_golden_params`, `load_vs_golden_from_mesh_csv`.
  - **Struct-driven (legacy workflow):** `executeVS`, `executePS`, `interpret`, `load_vs_output_golden_from_csv`, `compare_vs_output_with_golden`.
- Stage objects expose a single primary verb: `Rasterizer.rasterize(...)`, `Depth.execute(...)`. Construction is cheap and config is loaded via separate `load_config_from_*` methods.
- "Canonical key" maps centralize semantic→key translation (`_get_output_semantic_to_key_map`, `hlsl_interpreter.py:3054-3069`, and `sem_to_pixel` in `executePS_with_params`). When wiring a new semantic, add it to these dicts rather than scattering string literals.
- Config is read with `config.get(key, default)` everywhere (never `config[key]`) so missing keys fall back gracefully (`render.py:70-86`, `121-131`).

## Module Design

- **Exports:** no `__all__`, no barrel/`__init__.py` package — this is a flat collection of top-level modules imported by name.
- Each module is runnable-by-import; `render.py` is the only `if __name__ == '__main__':` entry point (`render.py:465-466`).
- Constructor factory: `render._make_interpreter(config, ...)` is the canonical way to build a configured `HLSLInterpreter`; prefer extending it over instantiating `HLSLInterpreter(...)` directly in new pipeline code.
- Class-construction-from-config uses `@classmethod ... from_config(cls, config_path, id) -> 'Type'`: `Sampler.from_config` (`texture.py:140-141`), `TextureDesc.from_config` (`texture.py:243-244`). Follow this for new config-backed value objects.

---

*Convention analysis: 2026-06-05*
