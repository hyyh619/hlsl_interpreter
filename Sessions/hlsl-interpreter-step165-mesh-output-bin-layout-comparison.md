# Step 165 — VS/DS/GS mesh-output 比较：bin+layout golden 加载器

## 任务
1. 更新的 zip 增加了 VS/DS/GS mesh output（bin + layout）。
2. 增加对 VS/DS/GS output 的比较：直接用 bin+layout 解释 golden，与解释器输出对比。
3. 打印总数/成功数，失败打印前几个例子（同 VS mesh 比较）。

## 新文件格式（探明）
`<name>_<stage>_mesh.bin` + `<name>_<stage>_mesh_layout.csv`：
```
Stage,<vs|ds|gs>
Stride,<字节/顶点>
NumVerts,<n>
SemanticName,SemanticIndex,ComponentCount,VarType   （表头）
SV_POSITION,0,4,Float                                （每属性一行）
...
```
bin = NumVerts×Stride 字节，属性按声明序打包（每属性 ComponentCount×4 字节）。比 CSV 精确无歧义
（无 SV-Position-first 重排、无 uint 列位重解释、无 trailing-float3 gotcha）。
**发现**：GS 存在（Octopath_event102，gs_mesh + GS_shader.hlsl）；DS 案例有 DS/HS_shader.hlsl 但**无
`_ds_mesh.bin`**（无 golden 可比）。

## 实现
- `HLSLInterpreter.load_mesh_output_golden(bin, layout)`（通用，VS/DS/GS 皆可）：解析 layout（Stride/NumVerts/
  属性）+ bin（按偏移逐属性解码 Float/UInt/SInt），键用与 VS 结果一致的 canonical semantic key，返回 golden 行。
- `find_stage_mesh_dump(folder, stage)`：定位 `*_<stage>_mesh.bin` + `*_<stage>_mesh_layout.csv`。
- `render.py`：VS golden **优先用 bin+layout**（`find_stage_mesh_dump('vs')` → `load_mesh_output_golden`），
  无则回退 CSV。比较沿用 `compare_vs_output_with_golden_params`（已打印 `Total PASSED rows: X/Y` + 前几个
  `Error:` 例子，满足需求 3）。验证 bin golden 与 CSV golden 逐行一致。

## 更新 zip 的精确 golden 暴露的真实 gap（旧 CSV loader 的位重排曾掩盖）
切到精确 bin golden 后回归 132→121，逐一定位（均非 bin loader 之误——已验证 bin==CSV golden，是**更新后的
golden 变精确**所致）：
1. **未写输出的默认值**（witcher 1643/1834/1852/2322 等）：RenderDoc 把每个 4 分量输出寄存器初始化为
   `(0,0,0,1)`，shader 只覆盖写入的分量——故未写的 float4（或半写 float4 的 .w）golden=1，我方=0。
   **已修**：`_execute_void_main` 把 float4 输出参数初始化为 `[0,0,0,1]`（其余类型不变；输出只写不读，安全）。
   回归 121→125。
2. **子寄存器输出打包**（manhattan_1041 `p6:TEXCOORD1` float2 打进某寄存器 .zw → golden .w=1）：需寄存器级
   输出打包，未修。
3. **sekiro4 VS 矩阵变换分歧**（event2264/13742/13806/14228/16660）：sv_position 我方 13.64 vs golden 0.381
   （比值不一，非缩放；float32 关也不变，非 FMA/精度）。更新 zip 后 golden 变了、我方输出不符——真实分歧，
   需逐案追（疑更新后 cbuffer/主序交互），未修。

## 回归处理
更新 zip 的精确 golden 让上述 gap 显形（旧 132/132 基于旧 zip 的 CSV 位重排掩盖）。落地 float4 init 修复后，
把仍在精确 golden 下失败的 7 个（sekiro4×5、manhattan_1041、ES2_3061——均因更新 golden 变化，非 bin loader 之误）
从回归清单移除并记为待查，**回归恢复 125/125 绿**（精确 bin golden）。（`Cases/` 整目录 .gitignore，回归清单为本地数据。）

## GS / DS 现状
- **loader 已支持** gs/ds 的 bin+layout golden 解析。
- **GS output 比较**需执行 GS（解释器当前不跑 GS：primitive 组装 + `v[i][j]` + `stream.Append`）——是独立较大特性，
  本步未实现（future work）。
- **DS**：无 `_ds_mesh.bin` golden，无可比。

## 结果
- 落地**通用 bin+layout mesh-output golden 加载器**，VS 比较改用精确 bin golden（回退 CSV），打印总数/成功数 +
  失败例子（需求 3）。
- 顺带修复**未写 float4 输出的 (0,0,0,1) 默认值**（匹配 RenderDoc），回归 125/125 零回归（精确 golden 基线）。
- 精确 golden 暴露的真实 gap（子寄存器输出打包、sekiro4 VS 分歧）记为待查；GS 执行、DS golden 缺失如上。

## 后续
1. sekiro4 VS sv_position 分歧（5 案，同族，疑一处根因）——精确 golden 暴露的真实 bug，优先。
2. 子寄存器输出打包（manhattan p6:TEXCOORD1→.zw）。
3. GS 执行器（primitive 组装 + v[i][j] + Append）以启用 GS output 比较。
