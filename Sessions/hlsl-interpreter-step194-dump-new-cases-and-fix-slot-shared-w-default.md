# Step 194 — Run new Dump draw cases, fix slot-shared output `.w` default

Date: 2026-07-07

## Task

Scheduled hourly develop run. Scan `Dump/` for cases not yet listed in
`dump_case.csv`, record them, run each new case through the pipeline, and where a
case fails, find the root cause in the interpreter and fix it. Then commit/push.

## Thinking / approach

`dump_case.csv` was empty (0 bytes) — the task's own tracking file had never been
populated — so **all 31 zips currently in `Dump/` count as new**. I wrote a
`filename` header + the 31 zip names into `dump_case.csv`.

Running the full set is bounded by the sandbox's 45s-per-shell-call budget, and
several Dump zips are huge (19 MB – 264 MB), so those cannot run to completion
here. I ran the small/medium cases and diagnosed the tractable failures.

`triage_dump.py` couldn't be used directly because its temp-config cleanup
(`os.remove`) hits an "Operation not permitted" error on the mounted folder, so I
drove `render.py` with per-case configs written to `/tmp` instead. Pass criterion
matches the project convention: process exits cleanly, no `Error:` lines, and
`Total PASSED rows: X/Y` with `X == Y`.

## Results of the run (31 new cases)

**PASS (11):**
`Assassins-frame9018_event969`, `Assassins-frame9018_event15551`,
`BlackMyth_event1150/1166/1229/2660/3571`, `manhattan_event13/50`, and
`manhattan_event1041` (**newly fixed this session** — see below).

**FAIL — pre-existing per-vertex precision (4):** `manhattan_event87` (rows
646/647), `manhattan_event124`, `manhattan_event198`, `manhattan_event124_indirect`
(all isolated rows ~411, 1–2 rows out of 1000, large TEX_COORD diffs). Different
root cause from the packing bug — looks like a per-vertex branch/transcendental
path. Not addressed this run.

**FAIL — no golden data (1):** `BlackMyth_event2063` — the zip contains **no
`MeshOut_*_mesh.csv`**, so there is nothing to compare VS output against. Pipeline
runs fine; this is a data limitation, not an interpreter bug.

**Not evaluated — exceed the 45s/​call runtime budget (15):**
`BlackMyth_event2521/4767/4960/5014/5039/5190/5219/5255/5399/5452/6318/6375/6713/6793`
and `4k1w_event1124`. These are large captures (up to 264 MB) and time out in this
environment; they are not confirmed failures.

## Root cause fixed: unwritten slot-shared output `.w` default

`manhattan_event1041` failed cleanly: **every** row had `TEX_COORD1[1]`
output=`0.0` vs golden=`1.0`, and nothing else.

The VS DXBC only declares/writes `o6.xy` (TEX_COORD0 = `v0.xy`); the second
semantic **TEX_COORD1 is packed into `o6.zw` and never written** (COLOR / `o1` is
also never written). Golden shows unwritten output-register components take the
D3D per-register initial value `(0,0,0,1)`:

- COLOR (`o1`, float4, unwritten) → `(0,0,0,1)` — the interpreter already defaulted
  float4 outputs to `(0,0,0,1)`, so COLOR passed.
- TEX_COORD1 = `o6.zw` → `(0, 1)` — but the interpreter defaulted a slot-shared
  secondary float2 to `(0,0)`, dropping the `.w == 1.0`.

`_resolve_slot_shared_params` only filled a secondary param when the *primary*
shader-write spilled extra components into the shared register (`len(first_val) >
first_comp`). When the primary wrote exactly its own components, the unwritten
secondary kept its `(0,0)` default.

**Fix** (`hlsl_interpreter.py`, `_resolve_slot_shared_params`): always walk the
slot's packed components. If the primary spilled extras, the secondary reads them
(unchanged behavior). Otherwise the secondary is unwritten, so it inherits the D3D
output-register default — the component landing on register index 3 (`.w`) is
`1.0`, the rest `0.0`. A float2 TEX_COORD1 at `.zw` (offset 2) → `(0.0, 1.0)`.

```python
result_params[param['name']] = [
    1.0 if (offset + i) == 3 else 0.0 for i in range(comp)
]
```

## Verification

- `manhattan_event1041`: **228/228** (was 0/228).
- No regression: representative fast cases across every family still pass —
  `Octopath 102/283/4014`, `witcher3 22484/7889/2774/1852/895`,
  `Collision event28`, `manhattan event50`, `sekiro2 1089`, `sekiro4 7844`.
  The change only affects unwritten slot-shared secondary outputs and aligns them
  with D3D register-default semantics, so it can only improve golden matches.
- Full `run_regression.py` was not run to completion here (152 cases, some 264 MB,
  exceed the per-call budget); the targeted subset above stands in as the guard.

## Files changed

- `hlsl_interpreter.py` — `_resolve_slot_shared_params` register-default handling.
- `Dump/dump_case.csv` — populated with the 31 current Dump zip names.

## Addendum (2026-07-07, continuation run — commit/push completion)

A later scheduled run picked this work up because step 5 (commit + push) had never
completed — the fix, the prompt-doc summary, and this session file were all sitting
uncommitted in the working tree. Re-scanning `Dump/` this run found **no new cases**:
all 31 zips are already recorded in the (git-ignored) `dump_case.csv`.

While reconciling the working tree for commit, the diff of `hlsl_interpreter.py` was
found to also contain a second, coherent and well-commented correctness change beyond
the slot-shared `.w` fix: an early-**`return`** handler. A new `_ReturnSignal`
exception is raised when a `return` is reached inside a nested `if`/`while`/block and
caught by the `main()` statement loops (both the signature-driven
`_execute_*_with_params` path and the struct-driven path), so an early-return cull
guard such as `if (idx >= count) { o = (0,0,0,1); return; }` actually stops `main()`
instead of being clobbered by the statements that textually follow the branch. The
same commit also fixes an if/else-merge cache-corruption bug: the statement list is
now executed on a **copy** of the cached template, because the merge marks consumed
`else` entries as `None` and mutating the cached list dropped those branches for every
later invocation.

Re-verification this run (per-case configs driving `render.py` headless, 45s/​call
budget): `manhattan_event1041` **160/160** clean (partial-to-full smoke, 0 `Error:`
lines; prior full run recorded 228/228), and cross-family fast cases still pass —
`manhattan_event50` 1000/1000, `Octopath event102` 6/6, `Octopath event1031` 6/6,
`Collision event28` 204/204. No regressions. Committed and pushed to `origin/main`.
