# Codebase Concerns

**Analysis Date:** 2026-06-05

## Tech Debt

**Monolithic interpreter file (`hlsl_interpreter.py`, ~3323 lines):**
- Issue: A single file holds 9 classes, the largest being `HLSLInterpreter` which spans `hlsl_interpreter.py:291` to end-of-file (~3000 lines, ~114 methods). It mixes HLSL lexing/parsing, cbuffer/struct/signature loading, statement execution, expression evaluation, built-in-function implementations, golden-data loading, and golden comparison.
- Files: `hlsl_interpreter.py`
- Impact: Hard to navigate, hard to test in isolation, high merge-conflict surface, slow to load into an editor/agent context. Any change risks unrelated breakage because concerns are not separated.
- Fix approach: Split into modules along seams that already exist: `hlsl_lexer.py`/`hlsl_parser.py` (struct/cbuffer/signature/`main` param parsing), `hlsl_builtins.py` (the `func_name == 'transpose' … 'mul' … 'Sample'` dispatch at `hlsl_interpreter.py:1202`–`1525`), `hlsl_golden.py` (`load_vs_golden_from_mesh_csv` `hlsl_interpreter.py:2998`, `compare_vs_output_with_golden*` `hlsl_interpreter.py:2600`/`3270`), and a thin `HLSLInterpreter` orchestrator. Do this incrementally, validating via the verify-by-log workflow after each extraction.

**Hardcoded built-in function dispatch:**
- Issue: HLSL intrinsics are implemented as a long `if/elif func_name == …` chain (`hlsl_interpreter.py:1202`–`1525`) covering `transpose, normalize, length, dot, reflect, max, min, rsqrt, sqrt, log2, exp2, clamp, saturate, lerp, int/uint constructors, pow, abs, sin, cos, mul, float2/3/4, Sample`. The same intrinsic names are also duplicated as a hardcoded allow-list inside the parser (`hlsl_syntax_tree.py:301`/`304`).
- Files: `hlsl_interpreter.py:1202`, `hlsl_syntax_tree.py:301`
- Impact: Adding a new intrinsic requires edits in two places kept in sync by hand. Any shader using an unlisted intrinsic (e.g. `floor`, `fmod`, `cross`, `ddx`, `mad`, `atan2`) silently falls through with no diagnostic. The parser's special-casing of arg-arity by name is fragile.
- Fix approach: Move intrinsics to a single registry (name → callable + arity) consumed by both parser and evaluator.

**Hardcoded semantic→attribute key maps:**
- Issue: Semantic-to-key maps are hardcoded and duplicated: `_get_output_semantic_to_key_map` (`hlsl_interpreter.py:3054`), a near-identical map at `hlsl_interpreter.py:3228`, and the literal PS attribute dict in `executePS` (`hlsl_interpreter.py:2307`–`2313`). Only `COLOR, TEXCOORD0/1, NORMAL, WORLDPOS, SV_POSITION, SV_TARGET` are recognized.
- Files: `hlsl_interpreter.py:2307`, `hlsl_interpreter.py:3054`, `hlsl_interpreter.py:3228`
- Impact: Shaders using other semantics (e.g. `TANGENT`, `BINORMAL`, `TEXCOORD2+`, `COLOR1`) won't map to rasterizer/pixel attributes. The three copies can drift.
- Fix approach: Single canonical semantic map; derive PS interchange keys from it instead of hardcoded dict literals.

**Dual VS/legacy code paths:**
- Issue: Two parallel execution APIs exist — current signature-driven (`executeVS_with_params` `hlsl_interpreter.py:3173`, `executePS_with_params` `hlsl_interpreter.py:3215`, `_execute_void_main` `hlsl_interpreter.py:3076`) and legacy struct-driven (`interpret` `hlsl_interpreter.py:2153`, `executeVS` `hlsl_interpreter.py:2199`, `executePS` `hlsl_interpreter.py:2276`). `render.py:309` (`_run_legacy_workflow`) keeps the old path alive, selected when `data_path` is absent.
- Files: `hlsl_interpreter.py:2153`–`2323`, `render.py:309`
- Impact: ~doubles the maintenance surface for VS/PS execution; the legacy `executePS` carries the hardcoded attribute dict (`hlsl_interpreter.py:2307`) and a brittle regex for the PS signature (`func_signature_pattern` at `hlsl_interpreter.py:2295` assumes a single `(StructName input)` parameter). Bug fixes must be mirrored or the legacy path silently rots.
- Fix approach: Confirm whether any active `Cases/*.json` still uses the legacy path; if not, delete it. If still needed, route both paths through a shared core.

**Comment language is mixed Chinese/English:**
- Issue: `hlsl_interpreter.py` (371 lines with CJK chars) and `mesh_view.py` (121) carry Chinese comments/docstrings; `rasterizer.py`, `output_merger.py`, `texture.py`, `render.py` are English. CLAUDE.md and Sessions are English.
- Files: `hlsl_interpreter.py`, `mesh_view.py`
- Impact: Inconsistent readability for contributors/agents; mixed-encoding risk on some toolchains.
- Fix approach: Standardize on one comment language (English, matching the docs).

## Known Bugs / Documentation Drift

**CLAUDE.md gotcha #1 describes a `[None, x, y]` remap that is not present in the current code:**
- Symptoms: CLAUDE.md (lines 47–51) states `load_vs_golden_from_mesh_csv` "compensates by remapping to `[None, x, y]`" for trailing-`float3` 3Dmigoto misalignment, and that `compare_vs_output_with_golden_params` "skips `None` components." The current `load_vs_golden_from_mesh_csv` (`hlsl_interpreter.py:2998`–`3052`) contains no such remap — it reads `x,y,z(,w)` straight from the matched columns with no per-semantic shift. The comparison function (`hlsl_interpreter.py:3270`) does defensively skip `None` components (`hlsl_interpreter.py:3287`/`3295`), but nothing produces the `[None, x, y]` shape it guards against.
- Files: `hlsl_interpreter.py:2998`, `hlsl_interpreter.py:3270`, `CLAUDE.md:47`
- Trigger: Validating a shader whose VS output ends in a `float3` (e.g. `WORLDPOS`) against a 3Dmigoto `MeshOut_*_mesh.csv` capture.
- Workaround / risk: Either the misalignment compensation was removed/relocated (and the doc is stale), or trailing-`float3` golden values are currently read misaligned and silently produce false `Error:` lines / false passes. This must be reconciled before trusting golden comparisons on such shaders. The richest worked example is `Prompts/hlsl-interpreter-prompt-ClaudeCode.md` §4.

**Bare `except:` clauses swallow all errors (including `KeyboardInterrupt`/`SystemExit`):**
- Symptoms: Failures during type parsing and cbuffer lookup are silently absorbed, returning a fallback value instead of surfacing the problem.
- Files: `hlsl_interpreter.py:614` (`parse_value` falls back to returning the raw string on any error), `hlsl_interpreter.py:1793` (cbuffer swizzle resolution falls back to `0.0`), `mesh_view.py:131`, `mesh_view.py:1840`, `mesh_view.py:1848`.
- Trigger: Any malformed CSV value, unexpected type, or unhandled swizzle path.
- Workaround: None — errors become wrong numbers (`0.0` / raw string) rather than diagnostics, which is especially dangerous given the value at `hlsl_interpreter.py:1793` feeds shader math. Narrow these to `except (ValueError, IndexError, KeyError, TypeError)` and log the swallowed exception.

**Stale `.vscode/tasks.json` from an unrelated Irrlicht project:**
- Symptoms: `Build 01.HelloWorld` / `Build 02.Quake3Map` tasks run `make all_macos` in `examples/01.HelloWorld` / `examples/02.Quake3Map` — directories that do not exist in this repo.
- Files: `.vscode/tasks.json` (tracked by git per `git ls-files`)
- Trigger: Invoking the default build task in VS Code.
- Workaround: CLAUDE.md (line 19) warns to ignore it. Fix: delete or replace with a Python run task; it is committed and misleading to anyone opening the workspace.

**Empty `ReadMe.md`:**
- Symptoms: `ReadMe.md` is 0 bytes (the only empty tracked file). New contributors get no top-level orientation outside CLAUDE.md.
- Files: `ReadMe.md`
- Impact: Low, but it is the conventional first file a reader opens. (Note: contrary to the mapping brief, `Prompts/` and `Sessions/` are NOT empty placeholders — `Prompts/` has 280- and 1836-line specs, and `Sessions/` holds ~88 populated step logs. They are healthy documentation, not debt.)

## Security Considerations

**Zip extraction without path sanitization (zip-slip):**
- Risk: `render.py:106`–`107` calls `zipfile.ZipFile(...).extractall(temp_dir)` on arbitrary user-supplied zips (`data_path` from the JSON config). A malicious archive containing `../` entries or absolute paths could write outside `temp_dir`.
- Files: `render.py:104`–`118`
- Current mitigation: Extraction targets a fresh `tempfile.mkdtemp` and the dir is removed in `finally`. No member-path validation.
- Recommendations: Validate each member path resolves inside `temp_dir` before extracting (reject `..`/absolute members), or use a vetted safe-extract helper. Trust is currently implicit ("inputs are your own RenderDoc dumps"), so impact is moderate — but worth hardening since the path is user-config-driven.

**No `eval`/`exec` — positive note:**
- The interpreter genuinely interprets (no `eval`/`exec` found anywhere), consistent with CLAUDE.md. This avoids the worst code-injection risk and is a deliberate design strength worth preserving.

## Performance Bottlenecks

**Single-threaded interpretation is the default and the realistic mode:**
- Problem: VS execution is per-vertex Python interpretation. `max_workers` defaults to `1` (`hlsl_interpreter.py:304`, `render.py:81`/`327`/`357`), so by default every vertex is interpreted serially. For large captures (the `Cases/*.zip` event dumps are 1.5 MB each) this is the dominant cost.
- Files: `hlsl_interpreter.py:2241`–`2272`
- Cause: Tree-walking interpretation of re-parsed expression strings per vertex; no compilation/caching of per-shader statement plans across vertices beyond the module-level `@lru_cache` on parser helpers (`hlsl_syntax_tree.py:24`/`48`/`108`).
- Improvement path: Cache the parsed `SyntaxTreeNode` per statement once per shader (not per vertex), and vectorize/batch where possible. The `@lru_cache(maxsize=256)` caches in `hlsl_syntax_tree.py` are keyed on raw expression strings and capped at 256 — large shaders can thrash the cache.

**Multi-threaded VS path is unsafe AND limited by the GIL:**
- Problem: When `max_workers > 1`, `executeVS` submits `execute_row` tasks to a `ThreadPoolExecutor` (`hlsl_interpreter.py:2255`) that all share ONE `HLSLInterpreter` instance. Pure-Python interpretation holds the GIL, so threads give little CPU speedup; meanwhile they mutate shared instance state concurrently (see fragile-areas below).
- Files: `hlsl_interpreter.py:2241`–`2259`
- Cause: Threads (not processes) chosen for a CPU-bound interpreter.
- Improvement path: For real parallelism use `ProcessPoolExecutor` with per-worker interpreter instances, or keep it single-threaded and optimize the interpreter. As written, `max_workers > 1` adds risk without meaningful gain.

## Fragile Areas

**Hand-written recursive-descent expression parser (`hlsl_syntax_tree.py`):**
- Files: `hlsl_syntax_tree.py:48`–`92` (`_find_top_level_operator_cached`), `:203`–`270` (`_parse_expression`), `:272`–`331` (function/method-call parsing)
- Why fragile:
  - **Operator precedence** is a flat 8-level table in `_OPERATORS` (`hlsl_syntax_tree.py:14`). It lacks unary `!`/`~`, modulo `%` (referenced in the unary look-back set at `:77` but absent from `_OPERATORS`), shift `<<`/`>>`, and assignment operators. Bugs here surface as wrong colors/vectors, not crashes (CLAUDE.md gotcha #2).
  - **Unary vs binary `+`/`-`** relies on a single look-back at the previous non-space char (`hlsl_syntax_tree.py:73`–`79`). Multi-char operator boundaries and unary `+`/`-` after `]`/`)` whitespace edge cases are easy to get wrong.
  - **Cast vs paren-grouping vs bitwise** ordering is delicate: bitwise `|`/`&` are special-cased *before* the cast regex (`hlsl_syntax_tree.py:208`–`222`) precisely to avoid `(int2)a | (int2)b` mis-parsing — a non-obvious ordering constraint that any reordering will break.
  - **Swizzles** are parsed downstream in the interpreter (`hlsl_interpreter.py:1787`, `['x','y','z','w']`), limited to 4-component `.xyzw`; no `.rgba`, repeated, or write-mask swizzles in the value path.
  - **Method-call arg parsing** uses `expr.rfind(')')` (`hlsl_syntax_tree.py:325`), which breaks on trailing content after the call.
- Safe modification: Change `_OPERATORS` only as the single source of truth; never reorder the bitwise/cast/paren branches in `_parse_expression`; always re-run the verify-by-log workflow and grep for `Error:` after any parser edit. The cache decorators mean the parser must stay a pure function of the expression string (see `Sessions/hlsl-SyntaxTreeParser-cannot-cache.md`).
- Test coverage: None (see below).

**Shared mutable interpreter state under the threaded VS path:**
- Files: `hlsl_interpreter.py:321` (`self._eval_counter`), `:331` (`self.vertex_pool`), `hlsl_interpreter.py:2247`/`2250` (per-row `build_from_input` / `update_output` calls inside the threaded closure)
- Why fragile: `self._eval_counter` is reset and incremented during evaluation, and `self.vertex_pool` is written per row, all on a single shared instance while multiple threads run `execute_row` concurrently (`hlsl_interpreter.py:2255`). There is no lock. This is a latent data race that can corrupt `vertex_pool` contents or the eval counter when `max_workers > 1`.
- Safe modification: Treat `max_workers=1` as the only validated mode until per-worker isolation is implemented; do not rely on `_eval_counter` or `vertex_pool` correctness in the threaded branch.

**Exact float comparisons and float-precision sensitivity:**
- Files: ~74 float literals/casts in `hlsl_interpreter.py`; comparison tolerance is config-driven (`float_tolerance`, default `0.0001`, `render.py:125`).
- Why fragile: Python `float` is f64 while the GPU/golden data is f32; small reorderings of math change low bits. Golden comparison is tolerance-based (good), but intermediate exact comparisons (`== 0`, division) inside shader emulation can diverge. See `Sessions/hlsl-python-float-precision.md`.
- Safe modification: Keep all golden comparisons tolerance-based; be cautious adding any exact float equality in shader math.

**Regex-driven HLSL structure parsing:**
- Files: `hlsl_interpreter.py:2295` (PS signature regex assuming exactly `(Struct input)`), plus the param/semantic parsing for `main()`.
- Why fragile: Regex parsing of a C-like language breaks on formatting variations (multiple params, default values, line breaks inside the signature, comments). Failures are silent (`func_signature_match` falls through to `None`).
- Safe modification: Prefer the signature-driven `parse_main_params_with_semantics` path; treat the legacy regex as a known-brittle fallback.

## Test Coverage Gaps

**No automated test suite anywhere in the repo:**
- What's not tested: Everything. There are zero `test_*`/`*_test`/`*spec*` files (the only `*test*`-ish match is `Cases/specular_too_shining.json`, a config). Correctness relies entirely on the manual "verify-by-log" workflow (CLAUDE.md §"The verify-by-log workflow"): run `python render.py ./Cases/Default.json`, then grep `Cases/output.log` for `Error:` lines.
- Files: entire repo; the validation surface is `compare_vs_output_with_golden_params` (`hlsl_interpreter.py:3270`) writing `Error:` lines to the log.
- Risk: High. The most bug-prone components — the operator-precedence parser (`hlsl_syntax_tree.py`) and the built-in intrinsic implementations (`hlsl_interpreter.py:1202`–`1525`) — have no regression net. Any parser/precedence change can silently break previously-correct shaders, and the only signal is a human reading a 170 KB log. The documented golden-data misalignment (gotcha #1) is exactly the kind of subtle issue unit tests would catch.
- Priority: High. Recommended starting points (pure functions, easy to test): `SyntaxTreeParser.parse` / `_find_top_level_operator_cached` (precedence + unary/binary + cast + bitwise + ternary cases), each intrinsic in the `func_name ==` chain, and `load_vs_golden_from_mesh_csv` + `compare_vs_output_with_golden_params` against a small fixture CSV that exercises the trailing-`float3` alignment.

## Dependencies at Risk

**None — minimal-dependency design is a strength:**
- The project uses only the Python standard library plus `tkinter` (optional, mesh viewer). No `requirements.txt`, no third-party packages, no lockfile drift, no supply-chain surface.
- Files: `mesh_view.py:7` (`tkinter`/`threading`), `hlsl_interpreter.py:5` (`concurrent.futures`).
- Minor note: `tkinter` is an optional/heavy stdlib component not always present in headless/minimal Python installs; mesh-view is correctly gated behind `mesh_view_enabled` config so this is low risk.

## Missing Critical Features

**No structured/queryable validation output:**
- Problem: Pass/fail is emitted only as free-text `Error:`/`Total PASSED rows:` lines in `Cases/output.log` (`hlsl_interpreter.py:3299`–`3321`). There is no machine-readable result (exit code reflecting failures, JSON summary).
- Blocks: CI integration, automated regression gating, and any tooling that wants to assert "0 errors" programmatically. `main()` (`render.py:444`) does not appear to set a non-zero exit code on comparison failure.

**No diagnostics for unsupported HLSL constructs:**
- Problem: Unsupported intrinsics, semantics, swizzles, and operators fall through silently (returning fallback values) rather than emitting a "not supported" warning (see bare-except and hardcoded-dispatch items above).
- Blocks: Trusting results on any shader using features outside the implemented subset; users cannot tell "correct" from "silently unsupported."

---

*Concerns audit: 2026-06-05*
