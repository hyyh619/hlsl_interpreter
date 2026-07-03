# Step 169：修复 GS 发射类失败（类③计数 0 / 类④值不匹配）—— 一路挖出 8 个独立真 bug

## 任务

修复 step 168 分类中的：
- **类③** GS 条件发射计数 0（当时归因“cbuffer 解码”）：manhattan_frame_274_event50、sekiro2_event14998、sekiro4_event20560
- **类④** GS 发射值不匹配：manhattan_frame_274_event87 / event124 / event161 / event198

## 思考与勘误

先验证既有假设：读取 manhattan event50 的 GS cbuffer 二进制（constant_1777.bin c12），`emitter_startBirthIdx=0, endBirthIdx=0` —— **cbuffer 解码本来就是对的**，“birth-index 解码错”的旧假设（step 166 记忆）不成立。逐案深挖后，实际是 8 个互相独立的 bug 叠加：

### 发现 1：manhattan 的「GS」根本不是 GS（5 案共因）

`GS_shader_disasm.txt` 首行是 **`vs_4_0`**；GS_shader.hlsl 与 VS_shader.hlsl **逐字节相同**。这是 VS + Stream-Output 的粒子模拟 draw（无几何着色器），导出工具把同一 VS 导了两份。golden `gs_mesh.bin`（3000 行）= SO buffer 全量：前 1000 行是本 draw 的 VS 输出，后 2000 行全零（未写容量区）。我们的 GS 执行器只在拦截到 `Append()` 时计数 → 发射 0。

**修复**（render.py `_run_gs_stage`）：源码无 `Stream<>` 且无 `.Append(` → 判定为 stream-out 直通，`emitted = vs_results`（重跑一遍会把粒子更新应用两次）；golden 超出发射数的**全零尾部**按未写容量裁剪。

### 发现 2：struct 成员被 `// Offset:` 注释杀死（sekiro 共因）

`_parse_structured_buffers` 按 `;` 切分成员后，RenderDoc 风格的行尾注释落在下一段开头，正则匹配失败——`GXStandardParticleVS`（14 个成员，真 stride 176）只剩 `float4 Position`（stride 16）。VS 读 `StructuredInstance[vid].*` 全错位。
**修复**：解析 struct 体前 `re.sub(r'//[^\n]*','')` 剥离注释。

### 发现 3：stride 采信捕获值

新 dump 的 `buffer_params.csv` 已记录真实 `ElementByteSize`（StructuredInstance=176、g_LightBuffer=208…）。`load_structured_buffer_data` 新增 `_captured_sb_strides`：按 Name（回退 Slot）用捕获值覆盖源码推导 stride。

### 发现 4：SB 成员访问的尾部 swizzle 被丢弃（广播之源）

`o0.y = StructuredInstance[v0.x].Position.y` —— 成员访问正则的 group(3)（`.y`）被忽略，返回整个 float4，赋值取 `[0]`，于是 **o0 = [P.x, P.x, P.x, 1]**（日志里 sv_position 三分量同值的直接原因；与 step 168 修的方法调用尾部 swizzle 同族）。
**修复**：get_value SB 分支对 trailer 应用 `apply_swizzle`。

### 发现 5：非索引 draw 的 SV_VertexID 不含 StartVertexLocation

sekiro 两案 `DrawInstancedIndirect` 的 `VertexOffset=1`。我们把 idx_list=[1..N] 直接当 SV_VertexID → 所有粒子错位一个。golden 证实 GPU 从 0 开始（golden 粒子 0 = StructuredInstance[0]）——D3D11 语义：**非索引 draw 的 SV_VertexID 从 0 起，StartVertexLocation 只作用于 VB 取数**（与 Vulkan/GL 相反）。
**修复**（render.py）：非索引 draw 时 SV_VertexID = idx - VertexOffset；VB 取数保持原 idx_list。

### 发现 6：RenderDoc post-GS golden 是 strip→list 展开

sekiro4 golden 1074 = **179 × 6**、sekiro2 golden 29148 = **4858 × 6**（4858 恰是我们门判定通过的粒子数——门逻辑本来就对！）。RenderDoc 把 4 顶点 trianglestrip 展开成 2 个三角形 = 6 行。
**修复**：`executeGS_with_params` 改为按 strip 收集（`RestartStrip`/调用结束封口），`expand_strips=True` 时按 D3D 规则展开：偶三角形 (i, i+1, i+2)、奇三角形 **(i, i+2, i+1)**（次序按 golden 逐角验证，最初写成 (i+1,i,i+2) 时 Row3/4/5 恰好错位）；`_run_gs_stage` 从 GS disasm 的 `dcl_outputtopology trianglestrip` 触发展开。

### 发现 7：`f32tof16` 必须向零舍入

打包色 uint 输出与 golden 恰差 65536（高半字 1 ULP）。D3D 的 `f32tof16` 指令语义是 **round-toward-zero**，Python `struct.pack('<e')` 是就近舍入。
**修复**：新增 `_f32_to_f16_rtz`（含 subnormal/溢出钳制/NaN 处理），f32tof16 改用之。TexCoord2 的 2148 条错误全消。

### 发现 8：原始类型 / 矩阵成员 SB + 反编译伪影

sekiro4 tile 光照读 `StructuredBuffer<uint>` / `<uint3>`（无 struct，`[i].x` 直接 swizzle 元素）和 `g_SpotLightBuffer[i].ViewToLightSpaceMatrix._m02`（float4x4 成员，列主序 `_mRC` = vals[c*4+r]）。另外 `and r16.x, r16.x, l(0x0000ffff)`（取光源索引低 16 位）被 3Dmigoto 反编译成无意义自三元式 `r16.x = r16.x ? 0.000000 : 0;`（掩码按 float 打印成 0）。
**修复**：`_SB_COMPONENTS` 增矩阵尺寸；`_structured_buffer_member` 支持原始元素 swizzle；访问路径支持 `._mRC`；`preprocess_hlsl` 将自三元式伪影重写回 `(uint)X & 0xffff`（真实编译器不会生成两分支全零的 movc，无误伤风险）。

## 结果

| Case | 修复前 | 修复后 |
|---|---|---|
| manhattan event50 | 发射 0 vs 3000 | **PASS 1000/1000**（已晋级回归表） |
| manhattan event87/161 | 发射 0，0/0 | 998/1000（仅 2 行 birth 粒子） |
| manhattan event124/198 | 发射 0，0/0 | 999/1000（仅 1 行 birth 粒子） |
| sekiro2_event14998 | 发射 0 vs 29148 | **28974/29148（99.4%）**，计数精确吻合 |
| sekiro4_event20560 | 发射 0 vs 1074 | 计数 1074=1074、VS 179/179；仅剩 TEXCOORD9 |

- **回归 130/130 全过**（129 + 晋级的 manhattan_frame_274_event50），Dump 侧逐案对比零退步。
- manhattan 残余 1~2 行 = birth 粒子的 `frac(87362×sin(80000+))` hash —— 失败行号与 cbuffer birth 范围 [646,648)/[411,412) 完全吻合，sin 大相位下 GPU 硬件近似与 math.sin 的偏差被 ×87362 放大 → **超越函数墙（并入类⑥）**，非解释器 bug。
- sekiro4_20560 残余 TEXCOORD9（每行 3 分量）：irradiance volume（Texture3D）RGBM 解码读到 **alpha=1.0** → `exp2(20*(1-0.5))=1024` 倍放大；实际是体纹理 alpha/切片解码问题 → **并入类⑧（VS/GS 纹理采样精确性）**。
- sekiro2_14998 残余 174 行 TexCoord4 差 ~0.13（相对 4e-4）→ 精度临界（类⑦）。

## 分类更新（对 step 168 的 9 类）

- **类③（GS 发射计数 0）：已消灭** —— 根因不是 cbuffer 解码，而是发现 1/2/4/5/6 的叠加。
- **类④（GS 发射值不匹配）：已消灭为墙残余** —— manhattan 4 案并入类⑥（超越函数），sekiro4_20560 并入类⑧，sekiro2_14998 并入类⑦。
- 旧记忆「GS cbuffer birth-index 解码错（411/412 vs all-spawn）」已被证伪并更正。

## 修改文件

- `hlsl_interpreter.py`：struct 注释剥离；`_captured_sb_strides` stride 覆盖；SB 成员尾部 swizzle / `._mRC` / 原始元素访问；`_SB_COMPONENTS` 矩阵尺寸；GS strip 收集 + strip→list 展开；`_f32_to_f16_rtz`；`preprocess_hlsl` and-mask 伪影修复。
- `render.py`：`_run_gs_stage` stream-out 直通 + golden 零尾裁剪 + 输出拓扑检测；非索引 draw SV_VertexID 去 VertexOffset。
- `Cases/regression_test_zip_files.csv`：+manhattan_frame_274_event50.zip（从 Dump/ 移入）。
