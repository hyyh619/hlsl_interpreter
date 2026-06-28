# hlsl-interpreter-step152：修复 D 类（Octopath）—— typed buffer SNORM/UNORM/half 解码 + golden 列 uint 解析

## 任务

修复 step124/151 分类里的 **D 类（19 个 Octopath）**：packed 法线/切线 UNORM/SNORM 未按格式解码、golden 列被当 uint 解析。

## 诊断（以 `Octopath-frame746_event1828` 为样本）

D 类 Octopath 是**蒙皮/实例化 VS**，错的输出 `TEXCOORD10/11`（变换后的法线/切线）与 `TexCoord` 来自 **typed buffer**（`Buffer<float4> t1/t2`）的 `.Load()`，不是顶点属性。两个独立根因：

### Bug 1 — typed buffer 4 字节元素的视图格式被错误解码

`buffer_params.csv` 只记录 `ElementByteSize`，**不记录 DXGI 视图格式**；DXBC 反汇编一律声明 `dcl_resource_buffer (float,float,float,float)`（返回类型，恒为 float）。于是一个 4 字节元素的 `Buffer<float4>` 实际可能是：
- **R8G8B8A8_SNORM**（法线/切线/四元数，byte 127 → +1.0）
- **R8G8B8A8_UNORM**（颜色表，byte 255 → +1.0）
- **R16G16_FLOAT**（packed 纹理坐标，2 个 half）

旧 `_typed_buffer_load`：1 字节/分量一律按 **UNORM**（`b/255`）。于是 t2 的法线 byte 127 解出 `127/255=0.498039`，golden 期望 `1.0`（SNORM）——正是 `TEXCOORD10/11` 的错误来源。step118 曾确诊此 SNORM/UNORM 歧义但因"capture 无格式元数据"判为不可修。

**判别法（按字节分布，已对 Octopath 多个 capture 验证）：**
1. **先判 R16G16_FLOAT**：纹理坐标有限且范围温和，整个 buffer 每个元素都能解成两个有限、`|v|<1024` 的 half；而 R8G8B8A8 归一化数据的高字节常是 `0x7F/0xFF/0x80`（half 指数全 1 → Inf/NaN）。所以"**全 buffer 所有 half 都有限且在范围内**"是 R16G16_FLOAT 的可靠正信号（逐元素全检，几乎排除误判）。
2. **否则按 R8G8B8A8**：`#0xFF`（UNORM 1.0）多于 `#0x7F`（SNORM 1.0）→ unorm；否则 → snorm（含并列，蒙皮法线/切线/四元数的常见情况）。

> 关键修正：最初用"special 字节占比 ≥60% 才判 R8"做门控，但 event2384 的 SNORM 法线指向任意方向、special 占比仅 0.26，被错误回退到 UNORM 而**回归**。改为"half 有限性"作为唯一正信号、其余一律 R8（snorm/unorm 再分），event2384 恢复且新增多例通过。

验证字节分布：

| buffer | #0x7F | #0xFF | 全 half 有限 | 判定 |
|---|---|---|---|---|
| 1828 t2（法线） | 24 | 0 | 否(Inf) | **snorm** ✓ |
| 1320 t2（颜色） | 0 | 24 | 否(NaN) | **unorm** ✓ |
| 2135 t3（四元数） | 12 | 0 | 否(Inf) | **snorm** ✓ |
| 1357 t1（纹理坐标） | — | — | 是 | **half** ✓（解出 0.2344/0.4575 = golden） |

### Bug 2 — golden 列被当 uint 解析

golden mesh CSV 按 RenderDoc 的**物理列定位**布局：float 输出 `o2 : TEXCOORD0` 的数据物理落在 uint 类型的 `PRIMITIVE_ID.x` 列下，于是其 float `0.4375` 被打印成 uint 位模式 `1054867456`（=`0x3EE00000`）。`load_vs_golden_from_mesh_csv` 已有位重解释（`_golden_float`），但**只对 `sv_position` 生效**。RenderDoc 对真实 float 永远带小数点打印，故 float 列里的"裸整数"必是位模式——应对**所有 float 类型输出**都走 `_golden_float`，整型输出（uint/int，如 PRIMITIVE_ID 本身）保持原值。

## 修复（均在 `hlsl_interpreter.py`）

1. **`_infer_4byte_typed_buffer_fmt(data)`**（新）：按上面的判别法返回 `'snorm'/'unorm'/'half'`，结果缓存在 `tb['norm_fmt']`（每 buffer 只算一次）。
2. **`_typed_buffer_load`**：1 字节/分量分支按推断格式解码——`half`=2×`<e`；`snorm`=`max(c/127,-1)`（0x80→-1.0）；`unorm`=`b/255`。其余（8B→2×float32、16B→4×float32）不变。
3. **`load_vs_golden_from_mesh_csv`**：`param_cols` 增带 `is_float`，行解析对所有 float 输出用 `_golden_float`（对正常带小数/指数的文本是 no-op）。

## 结果

对 19 个 D 类 case 跑 `vs_only`：**10 PASS + 9 FAIL**（之前 0 PASS）。

**10 个通过并删除**：event1250、1357、1828、1922、2091、2384、2428、2513、3221、3542。

**9 个仍失败 → 属更深的其它子类（超出本次命名范围）**：
- `event2135`：sv_position 已对，`TEXCOORD10`（四元数解码残留）仍错。
- `event2651/2682/576`（各 2.3 万顶点）：sv_position 数值溢出（~1e13），exp2/pow 或索引溢出。
- `event2912/3012/3601/664`：sv_position 结构性错（蒙皮/索引）。
- `event3502`：`TEXCOORD12/13` 高编号语义列映射（golden=0，我方非 0）。

`Dump/` 失败集 **96 → 86**。分类刷新：B16 / C37 / **D9** / E10 / F1=3 / F2=9 / G2。

## 回归

- 改动均为 VS 侧（typed buffer 解码 + golden 加载），故用 `vs_only` 跑回归（更快且足以覆盖）：**123/123 通过**，其中 `Octopath-frame746_event1320`（UNORM 颜色）仍 6/6——确认 UNORM 推断未被破坏。
- 新增两个代表到回归套件（覆盖新格式路径）：`Octopath-frame746_event1828`（SNORM）、`event1357`（R16G16_FLOAT），均全管线复跑通过（6/6、696/696）。回归用例 123 → **125**。
- `run_regression.py` 的临时 `vs_only` 注入跑完已回退。

## 产物

- `hlsl_interpreter.py`：`_infer_4byte_typed_buffer_fmt` + `_typed_buffer_load` 格式感知解码；golden 加载对所有 float 输出位重解释。
- 删除 `Dump/` 中 10 个通过的 D 类；`Cases/dump_failure_categories.csv` 刷新为 86。
- 回归新增 `Octopath-frame746_event1828` / `event1357`（`Cases/` + `regression_test_zip_files.csv`）。
- `Cases/dclass_vsonly_results.csv`：19 个 D 类的 vs_only 明细。
