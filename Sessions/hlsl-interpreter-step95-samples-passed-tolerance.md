# Step 95 — Configurable tolerance for the `SamplesPassed` comparison

## Task

For the `SamplesPassed` (pipeline statistics) comparison, add a tolerance value.
It must be configurable via the input JSON; if not configured, default to **500**.

Example of the residual mismatch the tolerance should absorb:

```
Error [PipelineStats]: SamplesPassed mismatch: output=42906 golden=42726 (mapped from pipeline_stats['depth_passed'])
```

Here `|42906 − 42726| = 180`, well within 500, so this should now report **OK**.

## Thinking

`SamplesPassed` maps to our `pipeline_stats['depth_passed']` in
`_PIPELINE_STAT_MAP`, and `_compare_pipeline_statistics` previously required an
**exact** match — any difference logged an error-level line. As established in
step 94, an exact match is unrealistic: our rasterizer's fragment count and
processing order differ slightly from the real GPU, so the depth-passed sample
count lands close to golden but not identical. A small tolerance band is the
right way to treat "close enough" as a pass while still catching gross errors.

Design decisions:

- **Scope:** the tolerance applies **only** to `SamplesPassed`, per the task.
  All other counters (`VSInvocations`, `IAPrimitives`, …) keep exact-match
  semantics.
- **Config key:** `samples_passed_tolerance`, read in `_execute_pipeline` with
  `config.get('samples_passed_tolerance', 500)` so the default is 500 when the
  JSON omits it.
- **Plumbing:** pass the value into `_compare_pipeline_statistics` as a keyword
  argument (default 500 there too, so the function is safe to call standalone).
- The OK line records the diff and the active tolerance for transparency.

## Implementation

### `render.py` — `_compare_pipeline_statistics`

New `samples_passed_tolerance: int = 500` parameter. In the per-counter loop,
after the exact-match check, a `SamplesPassed`-specific branch:

```python
if counter == 'SamplesPassed' and abs(our_val - golden_val) <= samples_passed_tolerance:
    log(f"  OK  {counter}: output={our_val} golden={golden_val} "
        f"(diff={abs(our_val - golden_val)} within tolerance {samples_passed_tolerance})")
    continue
```

Only if the diff exceeds the tolerance does it fall through to the existing
`Error [PipelineStats]:` line.

### `render.py` — `_execute_pipeline`

Read the config value alongside the other tolerances:

```python
samples_passed_tolerance = config.get('samples_passed_tolerance', 500)
```

and forward it at the call site:

```python
_compare_pipeline_statistics(golden_stats, pipeline_stats, vs_interp.log_output,
                             samples_passed_tolerance=samples_passed_tolerance)
```

## Results

### Unit check of the branch

```
--- diff=180, default tol=500 (expect OK) ---
OK  SamplesPassed: output=42906 golden=42726 (diff=180 within tolerance 500)
All compared pipeline statistics match golden.

--- diff=180, tol=100 from config (expect Error) ---
Error [PipelineStats]: SamplesPassed mismatch: output=42906 golden=42726 (mapped from pipeline_stats['depth_passed'])

--- exact match ---
OK  SamplesPassed: output=42726 golden=42726
```

The exact example from the task (output=42906, golden=42726) now reports OK
under the default tolerance, can be tightened via config to fail, and exact
matches are unaffected.

### Regression suite — 6/6 PASS

```
PASS  ...event28.zip   passed 204/204
PASS  ...event104.zip  passed 1149/1149
PASS  ...event351.zip  passed 315/315
PASS  ...event371.zip  passed 6/6
PASS  ...event399.zip  passed 696/696
PASS  ...event516.zip  passed 3/3
```

(The `[PipelineStats]` lines are diagnostics — they do not start with `Error:`
and never gated the runner — so the regression result is unchanged; this step
only changes whether a near-match is reported as OK vs error in the log.)

## Config

New optional key in the run JSON:

```json
"samples_passed_tolerance": 500
```

Omit it to use the default of 500.

## Files changed

- `render.py` — added the `samples_passed_tolerance` parameter and
  `SamplesPassed` tolerance branch in `_compare_pipeline_statistics`; read the
  `samples_passed_tolerance` config key in `_execute_pipeline` and forward it.
