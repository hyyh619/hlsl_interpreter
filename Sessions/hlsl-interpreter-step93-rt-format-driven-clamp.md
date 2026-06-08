# Step 93 — Drive the output-merger clamp from the render-target format

## Prompt

> pipeline_state.csv 增加了 render target / depth-stencil buffer format：
> ```
> RenderTarget,Target[0]_Format,R8G8B8A8_UNORM
> DepthStencil,Format,D24_UNORM_S8_UINT
> ```
> 请根据 RT 的格式来决定是否要做 `_clamp_output_colors`，以及 clamp 的边界。
> （UNORM→[0,1]，SNORM→[−1,1]，FLOAT→不 clamp）
> 把思考、执行、结果写入 Sessions 下的 md。

## 1. Background

Step 92 added `_clamp_output_colors` modelling the D3D11 output-merger write, but its
mode came from a static config key (`output_color_clamp`, default `[0,1]`). The capture
now exposes the actual RT0 format, so the clamp decision should be **data-driven** from
that format instead of assumed.

D3D clamps the PS output during format conversion on write:

| RT format class | Clamp |
|-----------------|-------|
| UNORM / UNORM_SRGB | [0, 1] |
| SNORM | [−1, 1] |
| FLOAT | none |
| UINT / SINT | none (integer target; float clamp doesn't apply) |

## 2. Confirmed the capture carries the rows

Both `event371` and `event104` `pipeline_state.csv` now contain:
```
RenderTarget,Target[0]_Format,R8G8B8A8_UNORM
DepthStencil,Format,D24_UNORM_S8_UINT
```
RT0 is `R8G8B8A8_UNORM` → correct clamp is **[0, 1]** (matches the k/255-quantised golden).
The depth-stencil format is not needed for the *color* clamp, so it is not consumed here.

## 3. Changes

**`rasterizer.py`** — `pipeline_state.csv` is already parsed here, so the format is read here:
- `RasterizerConfig.render_target_format: Optional[str]` new field.
- `load_config_from_pipeline_state_csv` now handles a `RenderTarget` section and stores
  `Target[0]_Format` (the RT the PS writes / the one dumped as `diff_ps_output_rt0.csv`).

**`render.py`**:
- New pure helper `_rt_format_to_clamp_mode(fmt)` maps a format string → `'unorm'` /
  `'snorm'` / `'float'` / `'none'`, or `None` for absent/typeless/unrecognised. Substring
  match so it is robust to channel layout (`R8G8B8A8_*`, `R16G16B16A16_*`, …). `SRGB`
  storage is treated as UNORM; `TYPELESS` returns `None` (no concrete view format).
- In `_execute_pipeline`, after the rasterizer parses the state:
  ```
  rt_format       = rast.config.render_target_format
  fmt_clamp_mode  = _rt_format_to_clamp_mode(rt_format)
  effective_clamp = fmt_clamp_mode if fmt_clamp_mode is not None else output_color_clamp
  ```
  i.e. **RT format decides**; `output_color_clamp` config is only the fallback for older
  captures with no format row (so prior behaviour is preserved). The decision is logged.
- `_clamp_output_colors(pixels, effective_clamp, …)` instead of the raw config value.

### Precedence
RT format (when present) → else `output_color_clamp` config → default `[0,1]`. A capture
with a FLOAT RT now correctly **disables** the clamp; a UNORM RT clamps to `[0,1]`; SNORM
to `[−1,1]`.

## 4. Verification

`_rt_format_to_clamp_mode` spot-checks:

| Input | Output |
|-------|--------|
| `R8G8B8A8_UNORM` | `unorm` |
| `R8G8B8A8_UNORM_SRGB` | `unorm` |
| `R16G16B16A16_FLOAT`, `R32G32B32A32_FLOAT` | `float` |
| `R8G8B8A8_SNORM` | `snorm` |
| `R32_UINT` | `none` |
| `*_TYPELESS`, `''`, `None` | `None` (config fallback) |

End-to-end (regression logs):
```
Render target format: R8G8B8A8_UNORM → output-merger clamp mode: unorm
event104:  Output-Merger write clamp [0.0,1.0]: 399126 pixel(s) ... (clamped)
event371:  Output-Merger write clamp [0.0,1.0]: 0 pixel(s) ... (clamped)
```
The mode is now sourced from the parsed format, and event104's `4 × lightmap × diffuse`
out-of-range pixels are still correctly clamped to [0,1].

**Regression suite — 6/6 PASS**, all golden rows matched (204/204, 1149/1149, 315/315,
6/6, 696/696, 3/3).

## 5. Notes / scope

- Only `Target[0]` is read — the interpreter writes a single RT and compares
  `diff_ps_output_rt0.csv`. MRT (Target[1+]) is out of scope.
- UINT/SINT integer targets map to "no float clamp"; correct integer range masking is a
  separate concern and not exercised by these captures.
- As in step 92, the clamp fixes the *source* color's range; full OM correctness still
  requires the (still-unmodelled) alpha-blend stage.
