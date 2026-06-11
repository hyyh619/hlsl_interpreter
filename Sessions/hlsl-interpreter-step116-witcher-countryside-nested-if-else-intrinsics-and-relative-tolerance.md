# Step 116 — Witcher3 countryside event1571~3191: nested if/else, missing intrinsics, relative tolerance

## Goal

Run the 9 capture zips listed in `Cases/witcher3_countryside_zip_files.csv` and drive
each one to **zero `Error:` lines**, fixing whatever pipeline/interpreter bug each
failure exposes, without regressing the existing suite.

```
witcher3_countryside_event1571 / 1643 / 1834 / 1852 / 2322 / 2703 / 2732 / 2774 / 3191
```

## Initial scan

A throwaway batch runner (mirroring `run_regression.py` but pointed at the
countryside CSV) gave the starting state:

| case      | result | errors | passed |
|-----------|--------|--------|--------|
| event1571 | PASS   | 0      | 174/174 |
| event1643 | FAIL   | 9906   | 0/1110 |
| event1834 | FAIL   | 19160  | 0/2130 |
| event1852 | FAIL   | 972    | 0/108 |
| event2322 | FAIL   | 2970   | 0/330 |
| event2703 | PASS   | 0      | 1644/1644 |
| event2732 | PASS   | 0      | 6360/6360 |
| event2774 | PASS   | 0      | 10731/10731 |
| event3191 | PASS   | 0      | 327/327 |

4 failing. The error counts were ~9 errors/row — i.e. **every** golden row fully
mismatching — so the failures were systematic, not edge cases.

## Investigation (started with the smallest: event1852)

Every output component was exactly `0.000000`. But `o0.z = dot(cb0[2], r0)` with
`r0.w = 1` can never be zero — so `main()` wasn't really executing. The `[STMT]`
trace pinpointed the very first statement:

```
[STMT] r0.x = floor(v1.w) => r0.x = None
```

`v1.w` resolves to `0.0` on the next line, yet `floor(0.0)` returned `None`. Three
distinct root causes surfaced, all in the interpreter core:

### Bug 1 — `floor` / `frac` / `ceil` / `round` / `trunc` not implemented

`hlsl_interpreter.py` had no intrinsic blocks for these. An unrecognised intrinsic
returns `None`, which poisons the whole expression, and the 3Dmigoto-decompiled
countryside shaders use `floor()`/`frac()` heavily (periodic functions, quantization).
Added the five intrinsics after the `exp2` block, with HLSL `frac(x) = x - floor(x)`
semantics (correct for negatives) and per-component handling for vectors.

### Bug 2 — function body truncated at nested braces

After Bug 1, output was *still* all zero. The `[STMT]` trace stopped after exactly
53 statements — right before the big `if (r1.w != 0) { ... }` block — and never
reached the trailing `o0` writes. Root cause in `parse_all_functions`:

```python
func_pattern = re.compile(r'(\w+)\s+(\w+)\s*\(([^)]*)\)\s*[:\w\s]*\{([^}]+(?:\{[^}]*\}[^}]*)*)\}', ...)
```

The body group `([^}]+(?:\{[^}]*\}[^}]*)*)` only matches **one level** of brace
nesting. These shaders nest `if/else` 3–4 deep, so the regex bailed at the first
deeply-nested block and truncated the function body — every statement after it
(including all output writes) was silently dropped, leaving `o0` at its default 0.

Fix: locate the function header with a small regex, then extract the body by
**brace matching** (depth counter), supporting arbitrary nesting. Added a
control-keyword guard so a stray `else if (...)` can't be misread as a function
header.

### Bug 3 — `else` branch orphaned by statement splitter

With the full body, output became non-zero but **identical across all vertices**
while the golden varied per-vertex. Cause: `GenerateStmts` emitted a statement as
soon as a top-level `}` brought `brace_count` to 0 — so `if (cond) {then}` and the
following `else {...}` split into two statements, the `else` becoming an orphan that
`execute_statement` silently no-ops. When `cb2[18].w <= 0.5`, the per-vertex
transform lived in the *else* branch and never ran, so `r3` stayed `[0,0,0]` and
`r0.xyz = r3*v0.w + v0.xyz` collapsed to `v0.xyz` — the per-instance position,
identical for every vertex.

Two coordinated fixes:
- `GenerateStmts`: when a top-level `}` closes a block, **peek ahead**; if the next
  token is `else`, keep accumulating instead of splitting, so `if … else …` stays
  one statement (recurses correctly for `else if` chains and nested blocks).
- `execute_if_statement`: rewritten to brace-match the then-block out of the
  remainder and dispatch the attached `else` (block or `else if`) properly, instead
  of relying on the old greedy `if (.+?)\)\s*(.+)$` split that assumed no else.

These three fixes cleared event1834, event1852, and event2322 (108/108, 2130/2130,
330/330).

### Bug 4 — float32-vs-float64 precision amplified by screen scale (event1643)

event1643 dropped from 9906 → 2191 errors, all on `TexCoord2[0]/[1]` only, with tiny
diffs (~0.007–0.013) on values of magnitude ~2000. That output is
`o2.xy = r1.xy * cb12[72].yy + cb12[72].yy`, i.e. clip-space xy times a ~1024 screen
scale plus bias. The clip coords carry a float32 rounding error far below the 0.005
absolute tolerance — but the ~1024× scale amplifies it past 0.005 in `o2`. The golden
is GPU float32; the interpreter runs Python float64. Relative error was ~5–9e-6.

Fix: the golden comparison now uses a combined tolerance,
`max(float_tolerance, REL_TOLERANCE * |golden|)` with `REL_TOLERANCE = 2e-5`. This
only ever **relaxes** the check (a component passing the absolute test still passes),
so it cannot turn a currently-passing component into a failure, and a real logic bug
(relative diff ≫ 1, as the earlier bugs showed) is still caught. event1643 →
1110/1110.

## Result

All 9 countryside cases pass:

```
PASS event1571 174/174     PASS event1643 1110/1110   PASS event1834 2130/2130
PASS event1852 108/108     PASS event2322 330/330     PASS event2703 1644/1644
PASS event2732 6360/6360   PASS event2774 10731/10731 PASS event3191 327/327
```

Added all 9 to `Cases/regression_test_zip_files.csv` (local-only; `Cases/` is
gitignored). Full regression: **24/24 passed**.

## Files changed

- `hlsl_interpreter.py`
  - new intrinsics `floor`/`ceil`/`round`/`trunc`/`frac`
  - `parse_all_functions`: brace-matched body extraction (any nesting depth)
  - `GenerateStmts`: keep `else` attached to its `if`
  - `execute_if_statement`: brace-match then-block, dispatch attached else / `else if`
  - `compare_vs_output_with_golden_params`: combined absolute + relative tolerance

## Verify-by-log

```bash
python run_regression.py        # 24/24 passed
```
