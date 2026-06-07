# Step 89 — Add a regression-test workflow

## Goal

The user asked to add a **Regression Test** item to `CLAUDE.md` requiring that every time Claude Code changes code, a set of regression tests is run. The list of regression cases is described by `Cases/regression_test_zip_files.csv`.

## Thinking

The project has **no unit tests** — correctness is validated by the "verify-by-log" workflow: run the pipeline against a capture zip and grep `output.log` for lines starting with `Error:` (per-component VS-vs-golden mismatches). A regression suite is just that workflow, repeated over a fixed list of zips, with a machine-checkable pass criterion.

Findings that shaped the design:

- `Cases/regression_test_zip_files.csv` already existed and listed 6 zips (a `filename` header + 6 `Collision-fix-constant-buffer-and-RdotV-zero_event*.zip` captures), all present in `Cases/`.
- `CLAUDE.md` already had an empty `## Regression Test` heading at the end — it just needed content.
- `render.py` takes a single JSON config and branches into `_run_zip_workflow` when `data_path` is set. The pass signal lives in the log as `Total PASSED rows: X/Y` (emitted by `compare_vs_output_with_golden_params`), and failures appear as `Error:` lines.
- `log_file_path` resolves relative to the config file's directory.
- `mesh_view_enabled: true` (as in `Default.json`) would pop a Tkinter GUI — unacceptable for automated runs.
- Topology: `Default.json` pins `primitive_topology: 4`, but per `CLAUDE.md` the CSV topology wins **unless** explicitly set in JSON. To respect each zip's own `pipeline_state.csv`, the runner must **omit** `primitive_topology`.

Design decision: rather than make the regression a manual "edit Default.json and eyeball the log" chore, I added a small data-driven runner, `run_regression.py`, so the `CLAUDE.md` instruction becomes a single reproducible command. Each case runs as a **subprocess** (`python render.py <tmp-config>`) so a `sys.exit(1)` or GUI thread inside one case can't abort the whole suite.

## Execution

1. **`run_regression.py`** (new). Reads `Cases/regression_test_zip_files.csv`, and for each zip:
   - builds a headless config (mesh viewer off, `log_to_file` on, topology omitted so the zip's `pipeline_state.csv` decides),
   - writes a throwaway `Cases/.regression_tmp_<stem>.json` and runs `render.py` on it as a subprocess,
   - writes the per-case log to `Cases/regression_logs/<stem>.log`,
   - parses the log for `Error:` lines and the `Total PASSED rows: X/Y` summary,
   - prints a PASS/FAIL table and **exits non-zero** if any case fails (so it can gate a change). Temp configs are deleted after each run.

   A case **passes** iff: `render.py` exits cleanly, the log has zero `Error:` lines, and `X == Y` in `Total PASSED rows`.

2. **`CLAUDE.md`** — filled in the `## Regression Test` section: when to run (after every code change), how (`python run_regression.py`), the data-driven CSV (edit the CSV to change coverage), the three-part pass criterion, and where to look on failure (`Cases/regression_logs/`).

## Results

Ran `python run_regression.py` — all 6 cases pass:

| Status | Case | Rows |
|--------|------|------|
| PASS | `...event28.zip`  | 204/204 |
| PASS | `...event104.zip` | 1149/1149 |
| PASS | `...event351.zip` | 315/315 |
| PASS | `...event371.zip` | 6/6 |
| PASS | `...event399.zip` | 696/696 |
| PASS | `...event516.zip` | 3/3 |

`6/6 passed`, exit code 0. The suite is now the standard post-change gate.

## Files changed

- `CLAUDE.md` — populated the `## Regression Test` section.
- `run_regression.py` — new CSV-driven, headless regression runner.

## Notes / follow-ups

- The runner leaves per-case logs in `Cases/regression_logs/` for inspection; consider adding `Cases/regression_logs/` and `Cases/.regression_tmp_*.json` to `.gitignore` if they shouldn't be committed.
- To extend coverage, add a zip filename to `Cases/regression_test_zip_files.csv` — no code change needed.
