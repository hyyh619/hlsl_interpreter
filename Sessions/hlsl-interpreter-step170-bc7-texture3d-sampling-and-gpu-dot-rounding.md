# Step 170：类⑧ irradiance volume（Texture3D/BC7）解码 + 类⑦精度残差（GPU 点积舍入链）

## 任务

1. **类⑧**：修复 sekiro4_20560 唯一残余的 irradiance volume（Texture3D）alpha 解码，并评估对 witcher 纹理采样家族的带动。
2. **类⑦**：精度临界 6 案（sekiro2_3207/9493/4833、sekiro4_7844、Octopath 664/3012），"需 bit-exact GPU 数学"。

## 类⑧：一路揭开四层纹理栈缺陷

从 sekiro4_20560 的 TEXCOORD9（RGBM 解码 `exp2(20*(α−0.5))` 放大 1024 倍）入手，逐层定位出 4 个独立缺陷：

### 1. BC7 解码器（新增，texture.py）

texture_params.csv 显示 irradiance volume 是 **Texture3D 40×8×76 BC7_UNORM**。原 `.img` 原始解码不支持 BC7 → 回退 24bpp BMP（**alpha 丢失恒 1.0** → exp2(10)=1024 倍放大，且只有 slice 0）。

实现纯 Python BC7 块解码器（8 模式全支持：分区表 2/3 子集、anchor 表、p-bit 端点扩展、2/3/4-bit 权重插值、mode 4/5 旋转与 idxMode 双索引流）。**验证方法**：对同 zip 内 RenderDoc 转换的 BMP（RGB 权威真值）逐纹素比对——首轮发现 PART2 表 16~63 行记错（mode7/partition21 块 8 纹素错），换规范表后：
- 40×8 volume 切片：**320/320 逐纹素精确**（×2 个资源）
- 256×256 PS 纹理：**65534/65536 精确**（2 纹素差 ≤8，可忽略）
- 2D `.img` 路径同步接入 BC7（`_decode_raw_texels`），BC7 纹理不再退化到无 alpha 的 BMP。

### 2. Texture3D 体加载与三线性采样（新增）

`TextureDesc` 增加 `Depth`/`Kind`（render.py 从 texture_params.csv 传入）；`_get_volume_slices` 把 `.img` 按 Depth 切片独立解码（BC7 每切片 (W/4)(H/4) 块）；`sample_volume` 做 z 轴 `w*D−0.5` 定位 + 两切片双线性 + z 线性（三线性），按采样器过滤/地址模式退化到点采样。解释器 Sample/SampleLevel 对 `Texture3D` 绑定路由到体采样（`_is_volume_texture`，绑定声明或 texture_params 的 Type 均可判定）。

### 3. 采样器地址模式名解析（潜伏面最广）

调试时发现 in-run 采样 u=1.0079 环绕到了纹理另一头——sampler_params.csv 用 RenderDoc 命名 **`ClampEdge`/`ClampBorder`**，`ADDRESS_NAME_MAP` 不认识 → **静默退化成 WRAP**。所有新 dump 的 clamp 采样器都受影响（含 witcher）。已加映射（CLAMPEDGE→CLAMP 等）。

### 4. 双线性采样约定（D3D 规范）

`_sample_linear` 用 `u*w` 取整——**比 D3D 规范 `u*w−0.5` 偏半个纹素**（纹素中心采样时会错误混合邻居）。改为规范约定，邻居索引按地址模式（wrap 取模 / clamp 夹边）。用 golden 隐含 α 值验证：修正后 α=0.3077 vs golden 隐含 0.3079 ✓。

**结果：sekiro4_event20560 从 0/1074 → PASS 1074/1074**（上一步遗留的最后一块）。

## 类⑦：GPU 点积舍入链

两个解释器（HLSL 解释器 + DXBC VM）的 `dot`/矩阵乘都是**双精度累加、最后一次舍入**；GPU 的 dpN/mul+mad 链是**每步一次 float32 舍入**。在 f32 仿真下改为 GPU 序列（`_gpu_dot`：f32 乘积在 double 中精确、每步 product+acc 一次舍入 = mad 语义）：

- `dot_product`、`mul_matrix_vector`（hlsl_interpreter.py，f32_emulation 门控）
- `dp2/dp3/dp4`（dxbc_interp.py），顺便补了 `umin`/`umax` 指令

**结果**：
- **Octopath event664：36/51 → PASS 51/51**
- **Octopath event3012：35/51 → PASS 51/51**（类⑦的两个 Octopath 蒙皮案被此修复消灭——此前判断"需 bit-exact GPU 数学"，实际 dpN 逐步舍入就是那个 bit-exact 语义）
- sekiro2_3207/9493：43329 → 43337/45576（+8 行，残余 diff ~0.005 恰在容差边缘）
- TombRaider event2848/7308（类⑥超越函数墙）顺带收敛：742 → **827/1548**（dpN 舍入链消除了变换侧的双重舍入，剩余纯为 sincos/rsqrt 硬件近似）
- sekiro2_4833、sekiro4_7844：无变化（DXBC VM 定位显示 o1 法线精确匹配、o0 位置差 1e-3 相对量级——骨骼数据/权重侧的问题，VM 尚不支持骨骼 buffer 资源加载，留待后续）

## 验证

- 回归（130 案）：**130/130 全过**——BC7/体采样/ClampEdge/双线性约定/点积舍入全部零回归（双线性半纹素修正没有破坏任何既有 case，说明旧 case 的纹理采样均在平滑区域或容差内）。
- Dump 全量：sekiro4_20560、Octopath664、Octopath3012 转 PASS（晋级回归表）；witcher 家族误差收敛但未过线（event16215 错误行 180→150）。

## witcher 家族状态（类⑧残余）

ClampEdge + 双线性修正使 witcher 各案误差普遍下降但均未过线；其 TexCoord2/4 链经 VS 内 `SampleLevel`（Texture2DArray 细节法线 R16G16_FLOAT 等），残余属纹素级精确性 + 一处 HS/DS 结构墙，留待后续步骤。

## 修改文件

- `texture.py`：BC7 解码器（分区/anchor/权重表 + `_bc7_decode_block`/`decode_bc7_image`）；`TextureDesc.Depth/Kind`；`_get_volume_slices`/`sample_volume`；`_decode_raw_texels` 接入 BC7；`ADDRESS_NAME_MAP` 增 RenderDoc 命名；`_sample_linear` 改 D3D `−0.5` 约定 + 地址模式邻居。
- `hlsl_interpreter.py`：`_is_volume_texture` + Sample/SampleLevel 体采样路由；`_gpu_dot` + `dot_product`/`mul_matrix_vector` GPU 舍入链。
- `render.py`：`_load_stage_textures` 传 `Depth`/`Kind`。
- `dxbc_interp.py`：dpN 逐步舍入；`umin`/`umax`。
- `Cases/regression_test_zip_files.csv`：+3（sekiro4_event20560、Octopath event664/event3012）。

## 最终状态

- 回归 **133/133 全过**（130 + 3 晋级）。
- 类⑦：6 案 → 2 案消灭（Octopath 664/3012），4 案残留（sekiro2×3 + sekiro4×1，精度临界/骨骼数据侧）。
- 类⑧：sekiro4_20560 消灭；witcher 9 案误差收敛中。
