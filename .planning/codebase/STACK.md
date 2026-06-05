# Technology Stack

**Analysis Date:** 2026-06-05

## Languages

**Primary:**
- Python 3 - All pipeline logic. Core modules: `render.py`, `hlsl_interpreter.py`, `hlsl_syntax_tree.py`, `rasterizer.py`, `output_merger.py`, `texture.py`, `pixel.py`, `d3d.py`, `mesh_view.py`.
- HLSL (High-Level Shader Language) - Interpreted as data, not executed natively. Shader source (`VS_shader.hlsl`, `PS_shader.hlsl`) is parsed and walked by `hlsl_interpreter.py`. The project's reason for existing.

**Secondary:**
- None. No JS/TS, no C/C++ build, no SQL.

## Runtime

**Environment:**
- CPython 3.14.0 (confirmed via `python --version`; `__pycache__/*.cpython-314.pyc` artifacts present). Uses modern syntax/typing features; treat 3.10+ as the practical floor, 3.14 as the developed-against version.
- Pure interpreter execution — no compiled extensions, no native bindings.

**Package Manager:**
- None used for dependencies (no third-party packages to install).
- `.vscode/settings.json` sets `python-envs.defaultPackageManager` to `ms-python.python:conda` (editor hint only; not a project requirement).
- Lockfile: absent (no `requirements.txt`, `Pipfile`, `poetry.lock`, `pyproject.toml`, `setup.py`, `setup.cfg`).

## Frameworks

**Core:**
- None. The project is built entirely on the Python standard library. There is no web/app framework.

**Standard-library modules in use (the de-facto "framework"):**
- `csv` - parsing all capture CSV data (`hlsl_interpreter.py`).
- `re` - HLSL tokenization / pattern matching (`hlsl_interpreter.py`, `hlsl_syntax_tree.py`).
- `math` - vector/matrix and shader intrinsic math (`hlsl_interpreter.py`, `rasterizer.py`, `texture.py`, `mesh_view.py`).
- `struct` - binary BMP header/pixel unpacking (`texture.py`).
- `json` - config and parameter file loading (`render.py`, `rasterizer.py`, `output_merger.py`, `texture.py`, `mesh_view.py`).
- `zipfile` + `tempfile` + `shutil` + `os` - capture-zip extraction workflow (`render.py`).
- `dataclasses` (`@dataclass`, `field`) - record types: `Pixel` (`pixel.py`), rasterizer/depth state, interpreter structs.
- `enum` (`Enum`) - state enums in `rasterizer.py`, `output_merger.py`.
- `typing` - type hints throughout.
- `functools.lru_cache` - memoized hot-path expression parsing in `hlsl_syntax_tree.py` (operator finding, arg splitting, ternary scanning).
- `concurrent.futures.ThreadPoolExecutor` - optional parallel vertex execution in `hlsl_interpreter.py` (gated by `max_workers` config).
- `threading` - background thread for the GUI in `mesh_view.py`.
- `sys`, `time` - CLI argv handling and timing in `render.py`.

**GUI Toolkit:**
- `tkinter` / `tkinter.ttk` - optional mesh viewer (`mesh_view.py`, `MeshView` class). Standard-library, but requires a Tk-enabled Python build. Only loaded when `mesh_view_enabled` is true. The pipeline runs headless without it.

**Testing:**
- No test framework. No `pytest`/`unittest` suite exists. Correctness is validated by the "verify-by-log" workflow: run the pipeline, then grep `Cases/output.log` for lines beginning with `Error:` (per-component VS-vs-golden mismatches). See `CLAUDE.md`.

**Build/Dev:**
- No build step. Source is run directly.
- VS Code debug configs in `.vscode/launch.json` ("Python hlsl_interpreter" → `render.py ./Cases/Default.json`, using the `debugpy` adapter).
- `.vscode/tasks.json` exists but its build tasks are stale leftovers from an unrelated Irrlicht project — ignore (per `CLAUDE.md`).

## Key Dependencies

**Critical:**
- None (third-party). Zero external dependencies is an explicit design constraint of the project.

**Infrastructure:**
- Tk runtime - implicit requirement for the optional GUI path only (`mesh_view.py`).

## Configuration

**Entry / CLI:**
- Single CLI argument: a JSON config file. `python render.py ./Cases/Default.json` (`render.py` `main()`, `sys.argv[1]`, lines ~444-465).
- `Cases/Default.json` is the canonical config.

**JSON config files (committed, repo-relative):**
- `Cases/Default.json` - canonical run config. Keys: `data_path` (capture zip to run), `log_file_path` (relative to the config file), `early_z`, `log_to_file`, `printSyntaxTree`, `print_interpreter_result`, `print_sequence`, `float_tolerance`, `execute_count` (`-1` = all vertices), `max_workers`, `primitive_topology`, `mesh_view_enabled`, `log_file_mode` (`"w"` truncates).
- `Cases/color-correct-ninjia-of-collision.json`, `Cases/specular_too_shining.json`, `Cases/wrong_constant_attenuation.json` - additional named scenario configs.
- `rasterizer_param.json` (repo root) - standalone rasterizer state fallback (`cull_mode`, `fill_mode`, `front_face`, `scissor_*`, `multisample_enable`, `depth_clip_enable`, `viewport`). Used when no `pipeline_state.csv` is present.
- `animation_config.json` (repo root) - `{ "interval_ms": 15 }`, mesh-viewer animation timing.

**Config precedence note:**
- `pipeline_state.csv` (inside each capture zip) supplies rasterizer/blend/depth-stencil state and primitive topology; CSV topology wins unless `primitive_topology` is explicitly set in the JSON config (`render.py`, `rasterizer.py`).

**Build:**
- No build config files.

## Platform Requirements

**Development:**
- Python 3.14 (CPython) with the standard library.
- A Tk-enabled build only if using the mesh viewer.
- OS-agnostic; developed on Windows 11 (paths and `__pycache__` confirm). No OS-specific APIs in use.

**Production:**
- No deployment target. This is a local developer/validation tool run from the command line; it is not a service and has no network surface.

---

*Stack analysis: 2026-06-05*
