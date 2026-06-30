# Step 157 — 实现 Texture2DArray 切片采样

## 任务

> 修复 E：Texture2DArray 切片采样（两 loader 去掉 `arr!=0` 跳过 + `sample()` 收 slice index）。

step 154/156 把 witcher o2/o3 的根因之一定位为 Texture2DArray 采样缺口。本步实现切片采样。

## 关键发现：解释器侧的真正阻塞不止「只装 slice 0」

排查 witcher event16215（`Texture2DArray<float4> t4`，`r0.xz = t4.SampleLevel(s4_s, r3.xyz, 0).xy`）时
发现**更上游的 bug**：`texture_binding` 正则 `Texture2D(?:<[^>]+>)?\s+(\w+)...` **根本不匹配
`Texture2DArray` / `TextureCube` / `Texture3D` / `Texture1D`**（`Texture2D` 后跟 `Array` 既非 `<` 也非空格）。
于是 t4 从未被绑定 → `SampleLevel` 的 `_find_texture_binding('t4')` 返 None → 整项静默归 0。
即 step 154 在 DXBC VM 看到的「采样返 0」，在 HLSL 解释器侧的原因其实是**绑定都没解析到**。

## 实现（4 处改动）

1. **绑定正则泛化**（`hlsl_interpreter.py` ~427）：
   `(Texture(?:1D|2D|3D|Cube)(?:Array)?(?:MS)?)(?:<[^>]+>)?\s+(\w+)\s*:\s*register\(t(\d+)\)`，
   group(1)=维度种类。`TextureBinding` 加 `kind` 字段，`_parse_texture_and_sampler_bindings` 记录之。
2. **两个 loader 装全部切片**（`render.py`）：
   - 新增 `_collect_array_mip_paths`：扫 `arr0,arr1,...` 返回 `slices[slice]=[mip0,mip1,...]`。
   - `_load_stage_textures`：填 `ArrayMipDataPaths`。
   - `_discover_stage_textures_from_bmp`：去掉 `if arr!=0: continue`，按 `{slot:{arr:{mip:path}}}` 分组装全切片。
3. **TextureDesc + sample() 收 slice**（`texture.py`）：
   `TextureDesc.ArrayMipDataPaths`（slice→mip 链，slice0 = 既有 `MipDataPaths` 故非数组纹理零行为变化）；
   `_get_mip_levels(desc, array_slice=0)` 选对应切片；`sample(..., array_slice=0)` 透传。
4. **采样点传 slice**（`hlsl_interpreter.py`）：新增 `_array_slice_for(binding, coords)` —— 按 kind 取
   切片分量（Texture2DArray→coords[2]、Cube Array→coords[3]、1DArray→coords[1]、非数组→0）。
   `Sample`(函数式+方法式) 与 `SampleLevel` 三处都传 `array_slice`；数组纹理的第 3 坐标是切片而非显式 LOD，
   故数组时把传给 `sample` 的 `w` 置 0（让 derivative 决定 LOD），不再误当 LOD。

## 验证

- **回归 125/125 PASS（零回归）**：绑定正则现在也匹配 Cube/3D/1D（原先这些静默返 0），回归集无 case 因此改变。
- **witcher event16215**：t4 现在绑定、**装入 3 个 array 切片**、`SampleLevel` 返回**真实 texel**
  `[0.0, 0.027, 0.0, 1.0]`（原来恒 0）。3 个切片采样值各异（slice0/1/2 的 G = 0.032/0.189/0.125），
  按 `coords.z` 正确选片。o2 的 y 分量由 0.0289 → 0.0559 **向 golden 0.087 收敛**。

## 仍未 PASS 的原因（超出本任务范围）

t4 是 **R16G16_FLOAT**（2 通道）。需要 `sample.xy ≈ [-0.087, 0.058]`，但所有切片在该 UV 解出 **R≈0**
（`[0.0, G, 0, 1]`）——detail-normal 的 R 通道在计算出的 UV 处读到 ~0，而 golden 需要 -0.087。这属
**texel 取值精确性**（精确 UV / 采样器过滤 / R16G16 通道 / DXBC 的 `t4.xzyw` 资源 swizzle），与「实现切片采样」
是不同的更深问题；叠加 step 156 已记录的 o3 矩阵主序分量，witcher o2/o3 仍不入 0.01 容差。step 156 已预判
witcher 命中容差概率低，本步证实：切片采样缺口已补齐（真实数据、值收敛），但精确 PASS 受 texel 精确性限制。

## 结果

- **Texture2DArray / Cube / 3D / 1D 切片采样完整实现**（绑定解析 + 全切片加载 + slice-index 采样），零回归。
- 顺带修复**绑定正则漏匹配非-Texture2D 资源**这一更广的真实 bug（之前所有 array/cube/3D/1D 纹理采样静默返 0）。
- witcher o2/o3 采样值由「恒 0」变为「真实且向 golden 收敛」，但受 R16G16 detail-normal 的 texel 精确性
  （+o3 主序）限制未入容差。

## 后续
- texel 精确性：核对 R16G16_FLOAT 在精确 UV 的取值、采样器过滤/地址模式、以及 DXBC `t4.xzyw` 资源返回 swizzle
  在反编译 `.xy` 中的等价处理（step 153 记过 `_parse_method_call` 丢尾随 swizzle 的小 bug）。
- o3 矩阵主序仍需 DXBC 反汇编重建（同 C/E 墙）。
