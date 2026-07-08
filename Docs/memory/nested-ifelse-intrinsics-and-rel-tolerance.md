---
name: nested-ifelse-intrinsics-and-rel-tolerance
description: "interpreter now supports arbitrary-depth nested if/else, floor/frac/ceil/round/trunc, and abs+rel golden tolerance — recall when debugging zero/identical VS output or tiny-diff-on-large-value failures"
metadata: 
  node_type: memory
  type: project
  originSessionId: fc70bbd7-277b-4e5e-abe1-7b51b3121735
---

As of step116 (witcher3 countryside event1571~3191), four interpreter-core
capabilities exist that earlier sessions lacked:

- Intrinsics `floor`/`frac`/`ceil`/`round`/`trunc` are implemented
  (`frac(x)=x-floor(x)`). A missing intrinsic still returns None and poisons the
  whole expression → symptom is VS output all `0.000000`.
- Function bodies are extracted by **brace matching** in `parse_all_functions`
  (not the old one-level-nesting regex), so arbitrarily deep `if/else` parse fully.
  Old symptom: trace stops mid-function, trailing `o0` writes never run.
- `GenerateStmts` keeps `else` attached to its `if`; `execute_if_statement`
  brace-matches the then-block. Old symptom: condition-false branches silently
  dropped → identical output across all vertices (else-only per-vertex transform
  never ran, position collapsed to per-instance value).
- `compare_vs_output_with_golden_params` uses `max(float_tolerance, 2e-5*|golden|)`.
  So a "tiny absolute diff on a large-magnitude output" (e.g. screen-space
  `clip_xy * ~1024`) is float32-vs-float64 precision, not a logic bug.

**Why:** these were the 4 bugs behind every countryside VS failure.
**How to apply:** when a new capture gives all-zero or per-vertex-identical VS
output, suspect a *different* unimplemented intrinsic or parser gap, not these.
Related: [[witcher3-array-cbuffers-instanced-inputs]], [[structured-buffer-skinning-support]].
