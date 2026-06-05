# Testing

**Analysis Date:** 2026-06-05

> Written in-context as the sequential fallback: the parallel quality mapper hit a session limit after writing `CONVENTIONS.md` but before `TESTING.md`.

## Summary

There is **no automated test suite** — no `pytest`, `unittest`, `tox`, CI config, fixtures, or `test_*.py` files anywhere in the repo (`git ls-files | grep -iE "test|spec"` returns nothing). Correctness is validated by **running the full pipeline against captured GPU data and diffing the result against "golden" reference values written into a log file**. This is the "verify-by-log" workflow and it is the project's entire test strategy.

Treat this honestly when planning: any change to the interpreter, parser, rasterizer, or pipeline must be validated by re-running a case and inspecting the log — there is no green/red signal to lean on.

## The verify-by-log workflow

1. Run a case end-to-end:
   ```bash
   python render.py ./Cases/Default.json
   ```
   `Cases/Default.json` is the canonical case. It sets `log_file_mode: "w"` (truncate) and `log_file_path: "output.log"`, so each run produces a fresh `Cases/output.log`.

2. Inspect `Cases/output.log`. Search for lines beginning with `Error:` — each is a per-component mismatch between the interpreter's VS output and the golden capture:
   ```
   Error: Row 0 Color[0]: output=1.640544 golden=0.431490 diff=1.209054
   Error: Row 0 WorldPos[0]: output=-61.638200 golden=11.282900 diff=72.921100
   ```

3. Iterate on the fix until **no `Error:` lines remain** and the log reports:
   ```
   Comparison PASSED: All output data matches golden data within tolerance
   ```

The log is also the primary debugging surface: with `printSyntaxTree: true` it emits `[STMT]` (each interpreted statement and its result) and `[SYNTAX TREE]` (the parsed expression tree) traces, so an interpreter bug can be traced to the exact statement and parse.

## Golden-data comparison

The comparison engine is `compare_vs_output_with_golden_params` (`hlsl_interpreter.py:3270`); the legacy struct path uses `compare_vs_output_with_golden` (`hlsl_interpreter.py:2600`).

**How it works:**
- Golden VS output is loaded from `MeshOut_*_mesh.csv` inside the case zip via `load_vs_golden_from_mesh_csv` (`hlsl_interpreter.py:2998`). The legacy path loads `VS_OUTPUT.csv` via `load_vs_output_golden_from_csv` (`hlsl_interpreter.py:2539`).
- Comparison is keyed by canonical output key (per semantic), iterating component-by-component over vector outputs.
- Float components compare with `abs(output - golden) > float_tolerance` (`hlsl_interpreter.py:3298`). `float_tolerance` comes from the JSON config (default `0.0001`; `Cases/Default.json` loosens it to `0.005`).
- A component (or whole value) equal to `None` is **skipped** (`output_val is None or golden_val is None: continue`, and per-component `if gv is None: continue`). This `None`-sentinel is how known-unreliable golden components are excluded from the diff (see caveat below).
- `execute_count` (config; `-1` → all) bounds how many rows are compared.
- The method logs `Total PASSED rows: {passed}/{count}` and a final PASSED/FAILED line, and returns a bool.

**What is and isn't checked:** Only VS output is diffed against golden. The rasterizer, depth/stencil, and pixel-shader stages run but have **no golden comparison** — they are validated only indirectly (no crash, plausible pixel counts in the log, and the optional `mesh_view` GUI for visual inspection). Captured reference render targets exist in each zip (`post_draw_rt0_res_*.bmp`, `pre_draw_*.bmp`) but are **not** consumed by any automated check.

## Test data / fixtures

The de-facto fixtures are the RenderDoc/3Dmigoto capture zips in `Cases/` (e.g. `Collision-fix-constant-buffer-and-RdotV-zero_event399.zip`). Each zip is self-contained: VS/PS HLSL source, input-assembler vertex data + layout CSVs, constant-buffer CSVs, input/output signature CSVs, `pipeline_state.csv`, golden `MeshOut_*_mesh.csv`, and BMP textures/render targets. `render.py` extracts the zip to a temp dir per run and tears it down afterward (`_run_zip_workflow`, `render.py:89`). `Cases/zip_files.csv` enumerates the available capture cases.

## ⚠️ 3Dmigoto golden-data alignment caveat

The most important thing to know before "fixing" a VS mismatch: **the golden capture itself is not always trustworthy.** 3Dmigoto's `MeshOut_*_mesh.csv` shifts trailing `float3` VS outputs (e.g. `WORLDPOS`) by one float — `WORLDPOS.x` actually holds `o.y`, `WORLDPOS.y` holds `o.z`, and `WORLDPOS.z` is the *next vertex's* data (garbage). A mathematically correct interpreter output therefore shows up as an `Error:` against a naive read of the golden CSV.

`Prompts/hlsl-interpreter-prompt-ClaudeCode.md` §2–§4 documents this in depth, including a session that correctly *refused* to "fix" a correct computation to match misaligned golden data. CLAUDE.md describes a `[None, x, y]` compensating remap in `load_vs_golden_from_mesh_csv` — **note the documentation-drift concern flagged in `CONCERNS.md`: that remap is not present in the current `hlsl_interpreter.py:2998`.** Verify the actual loader behavior before relying on either claim.

**Rule of thumb:** when a VS component fails, first check whether two *reliably-aligned* outputs (e.g. `SV_POSITION` float4 and `NORMAL` float3) match. If they do, the inputs/matrices/mul logic are correct and the failing trailing-`float3` is almost certainly golden misalignment, not an interpreter bug.

## Recommendations (gaps)

- No regression harness: a thin wrapper that runs each `Cases/*.zip` and asserts `Comparison PASSED` would convert the manual log-grep into a repeatable check.
- The non-VS stages (rasterizer/depth/PS) have no golden oracle despite reference render targets being present in every zip — pixel-level comparison against `post_draw_rt0_res_*.bmp` is the obvious untapped validation.
- No coverage measurement exists or is meaningful without a test runner.
