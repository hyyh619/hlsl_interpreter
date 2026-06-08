# Step 92 — Does the PS output color need clamping? (output-merger write clamp)

## Prompt

> PS 执行完毕后的 Pixel color value 是否需要做 clamp？
> 把你思考、执行和结果都写入到一份 md 文件中，放到 Sessions 目录下。

## 1. Question restated

After the pixel shader runs, `Pixel.ps_output_color` holds the raw `SV_TARGET` value
(`hlsl_interpreter.py` sets it directly, no clamp). Should it be clamped before it
becomes the final framebuffer color?

## 2. D3D11 semantics — it's render-target-format dependent

The output merger converts the shader's float output to the render-target format **on
write**. Clamping happens there, and depends on the format class:

| RT format class | Clamp on write |
|-----------------|----------------|
| **UNORM** (e.g. R8G8B8A8_UNORM) | clamp to **[0, 1]** |
| **SNORM** | clamp to **[−1, 1]** |
| **FLOAT** (R16/R32 float) | **no clamp** (stores >1, negatives, even inf/nan) |

For fixed-point targets D3D also clamps the shader output to that range *before* blending.
So "do we clamp?" has a precise answer: **yes for UNORM/SNORM targets, no for float
targets.**

## 3. What this capture set actually uses

Inspected `Collision-…_event371`:

- `diff_ps_output_rt0.csv` golden values are quantized to k/255 (e.g. `0.498039 = 127/255`,
  `0.196078 = 50/255`) and all lie in **[0, 1]** → the render target is **R8G8B8A8_UNORM**.
- `pipeline_state.csv` shows blending **enabled** (`Target[0]` Src=ONE, Dst=INV_SRC_ALPHA, ADD).

So for these captures the correct OM-write behavior is **clamp to [0, 1]**.

## 4. Is it observable, or a no-op? — empirical check

Ran the regression set with a clamp pass instrumented to count clamped pixels:

| Case | PS shader (essence) | Pixels clamped to [0,1] |
|------|---------------------|--------------------------|
| event371 | `o0 = v1 * r0` (vertex color × diffuse) — both ∈ [0,1] | **0** |
| event104 | `o0 = float4(4,4,4,4) * (lightmap × diffuse)` | **399,126** |

event104 is the proof: the **×4 lightmap scale** drives the raw PS output well above 1.0
on ~400k pixels. A real GPU writing to the UNORM RT clamps every one of those to 1.0;
without the clamp the interpreter's stored colors are simply wrong (e.g. 1.8, 2.4 …).

**Conclusion: yes, the PS output color needs clamping.** It is not a cosmetic no-op — it
changes hundreds of thousands of pixels in event104. It is format-dependent, and for every
capture in this project (all UNORM) the correct clamp is **[0, 1]**.

## 5. Implementation

Modelled as the **output-merger write** step (after PS, after the depth test), `render.py`:

- New helper `_clamp_output_colors(pixels, mode, log)` — clamps each `ps_output_color`:
  - `True`/`"unorm"` → [0, 1] (default), `"snorm"` → [−1, 1], `False`/`"float"`/`"none"` → no clamp.
  - Logs how many pixels had an out-of-range channel.
- New config key **`output_color_clamp`** (default `True`), read in `_execute_pipeline`.
- Called once, right after the late-Z block, before the golden comparison and mesh view —
  so the clamped color is what gets compared and displayed.

The clamp is intentionally placed at the OM-write stage (not inside `executePS`) so the raw
shader output stays available for any future blend stage, matching the D3D order
(shader → clamp/blend → write).

## 6. Verification

- **Regression suite — 6/6 PASS** (`python run_regression.py`); all golden rows matched.
  The VS-vs-golden gate is independent of PS color, and event371's in-range output is
  untouched.
- Logs confirm the stage runs: event371 `clamp [0.0,1.0]: 0 pixel(s)`, event104
  `clamp [0.0,1.0]: 399126 pixel(s)`.

## 7. Caveat — clamp is necessary but not sufficient to match golden

The PS-vs-golden color diff noted in step 90 is **not** primarily a clamp problem — it is
**alpha blending**, which the interpreter does not model. The golden RT0 is
`src·1 + dst·(1−srcA)` composited over the pre-existing render target; we currently write
the (now-clamped) source color only. Clamping makes the *source* color correct for a UNORM
target; full output-merger correctness still needs a blend stage. That is a separate,
larger follow-up.
