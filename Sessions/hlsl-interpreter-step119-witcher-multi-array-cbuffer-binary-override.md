# Step 119 — witcher3_countryside triage + multi-array cbuffer binary-override fix

## Goal

Continue the RunDrawFromDump long-tail work. The witcher3_countryside batch (160
zips in `Dump/`) had only 31 events in the regression CSV; the other 129 were
never individually triaged. Triage them, find a fixable failure class, fix it,
keep the regression green, commit, and add a representative case to the suite.

## Triage method

Wrote a small headless triage runner (scratchpad `triage.py`) that points
`data_path` straight at a `Dump/*.zip`, runs `render.py` with the mesh viewer
off, and classifies each run by parsing its log:

- `PASS`  — exits clean, no `Error:` lines, `Total PASSED rows X/X`.
- `FAIL`  — produced `Error:` lines (per-component VS-vs-golden mismatch).
- `CRASH` — non-zero exit.
- `NOCMP` — no golden VS comparison in the log (nothing gradeable).
- `TIMEOUT` — exceeded the wall-clock budget (90s); pure-Python rasterization
  on big draws, same slow class as the Tank cases — not a correctness bug.

~25 s per run serial; ran 6 in parallel. CPU contention inflates per-run time,
so the final confirmation runs were done with a single worker.

### Triage outcome (129 untriaged witcher3 events)

Most PASS. The failing ones clustered into:

1. **`sv_position`/`TexCoord` reading 0** — `event23341`, `event23141`,
   `event23251`, `event23183`, `event23195`. **← fixed this session (one bug).**
2. **packed-uint octahedral normals** — `event16215`, `event16834`. Known
   class-11 long-tail (deeper octahedral normal decode), still unsolved.
3. **unsupported raw structured buffer** — `event22201` (and similar): the
   disasm literally carries `// No code for instruction (needs manual fix):
   ld_raw_indexable(raw_buffer) ... t21` plus `dcl_resource_raw t21`. The VS
   reads a raw buffer the capture never dumped — a feature/capture limitation,
   not a fixable interpreter bug. Deferred.
4. **slow draws (TIMEOUT)** — ~26 events. Correct-but-slow, same as Tank. Deferred.

## Root cause of class 1 (the fix)

Example `event23341`. Its VS cbuffer:

```hlsl
cbuffer Constants : register(b0) {
  float4 mvp[2]    : packoffset(c0);
  float4 texgen[2] : packoffset(c2);
}
```

`o2` (SV_Position) = `dot(v1, mvp[i])` was correct, but `o1` (TEXCOORD) =
`dot(v1, texgen[i])` came out 0 → all TexCoord errors.

Two layers conspired:

- The combined float CSV (`VS_constant_buffers.csv`) stores only **one
  representative value per array name** (`mvp`, `texgen`), never the per-element
  `mvp_v0`/`mvp_v1` keys the CSV loader looks for — so both arrays loaded as
  `field.data = [None, None]`.
- `override_cbuffers_from_binary` then re-loaded the real bytes from
  `constant_2860.bin`, but its fill loop did:

  ```python
  for field in cb_def.fields:
      if field.array_size > 0:
          field.data = decoded[:m]
          break          # ← only the FIRST array field, starting at register 0
  ```

  So `mvp` got `decoded[0:2]` (correct) and the loop **broke**, leaving
  `texgen.data = [None, None]` → `texgen[i]` evaluated to 0.

This is invisible whenever a cbuffer has a single array at c0 (the old
assumption), which is why every prior witcher case passed.

## Fix

`hlsl_interpreter.py`:

1. `FieldDefinition` gains `reg_offset: int = -1`.
2. `parse_cbuffer` parses `packoffset(cN)` into `reg_offset` (3Dmigoto always
   emits it).
3. New `_field_register_offsets(fields)` returns each field's 16-byte register
   offset: prefer the explicit packoffset, else replay HLSL constant-buffer
   packing (vectors never straddle a register; arrays/matrices register-align).
4. The binary-override fill loop now fills **every** array field from its own
   register window (`decoded[reg_off : reg_off + array_size]`) instead of only
   the first.

So `mvp` ← `decoded[0:2]`, `texgen` ← `decoded[2:4]`. Single-array cbuffers
(array at c0) are unchanged.

## Verification

- `event23341` 0→30/30; `event23141` 162/162; `event23251` 54/54;
  `event23183` 30/30; `event23195` 36/36. All five class-1 cases pass, no new errors.
- `event22201` still FAILs — confirmed unsupported raw-buffer class, deferred.
- Regression suite: 44/44 PASS (unchanged by the fix). `event23341` then added
  as the 45th case.

## Regression coverage added

Added `witcher3_countryside_event23341.zip` to
`Cases/regression_test_zip_files.csv` — the clearest two-array cbuffer case
(`mvp[2]@c0` + `texgen[2]@c2`).

## Still unfixed (long tail, unchanged from step 118)

- packed-uint octahedral normals (`event16215`/`16834`).
- quaternion typed-buffer SNORM/UNORM ambiguity (Octopath) — capture limitation.
- unsupported raw structured buffers (`event22201` …) — capture/feature limitation.
- float32 precision tail (`event1828`, `event2091`).
- slow pure-Python rasterization timeouts (Tank + ~26 witcher draws).
