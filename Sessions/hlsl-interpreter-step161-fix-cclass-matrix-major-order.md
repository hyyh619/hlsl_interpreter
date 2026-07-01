# Step 161 — 修 C 类残余：普通矩阵主序（WorldToPSSM0 / ScreenMatrix）按声明的 row_major/column_major 分别加载

## 任务
> 继续修 step 160 剩下的 8 个 TombRaider：7 个 TEXCOORD6=WorldToPSSM0 的「矩阵主序」问题
> （golden 要 `mul(M,world)`，反编译 `r0.x*_m00+..` 是转置）；+ event1802（4 顶点 identity 边缘）。

## 关键发现：区分因素是**声明的主序**（row_major vs 缺省 column_major），不是加载路径

step 160 我误判「WorldToPSSM0 与正常工作的 ScreenMatrix 同路径」。实际对比两类 shader 的 `_mRC` swizzle：
- **Collision（需转置、现通过）**：`float4x4 WorldViewProj`（**无限定符=column_major**），访问
  `WorldViewProj._m00_m10_m20_m30`（**列** swizzle，第一下标变）。
- **TombRaider（需原始寄存器）**：`row_major float4x4 WorldToPSSM0`，访问
  `WorldToPSSM0._m00_m01_m02_m03`（**行** swizzle，第二下标变）。

3Dmigoto 对 **row_major** 矩阵发 `_m0R`（行 swizzle），对 **column_major** 矩阵发 `_mR0`（列 swizzle），
**两者都映射到 cbuffer 寄存器 R**（disasm `cb[base+R]`）。而 `override_cbuffers_from_binary` **无条件转置**
（假设一律 column_major：寄存器=列→转成逻辑行）。这对 column_major 正确（列 swizzle 读逻辑列=寄存器），
但对 **row_major 错误**（寄存器本就是行，转置后 `_m0R` 行 swizzle 读到逻辑列≠寄存器）。

验证根因：WorldToPSSM0 二进制寄存器 reg56=`[0,0,-1,1]`，GPU disasm `o2=..+cb[reg56]=[0,0,-1,1]`=golden；
但我方 field.data（转置后）`_m30_m31_m32_m33`=`[0,0,0,1]`≠reg56 → o2 错。

## 修复：按声明主序决定是否转置

1. `FieldDefinition` 加 `is_row_major`；`parse_cbuffer` 解析字段时用 `re.search(r'\brow_major\b', line)` 记录
   （step 156 曾把限定符 strip 掉用于取类型/名，这里从原始行再捕获主序）。
2. `override_cbuffers_from_binary` 的 float4x4 / float4x3 分支（非数组 + 数组两处）：
   - `is_row_major` → 存**原始寄存器**（`field.data = [reg0,reg1,reg2,reg3]`），`_m0R` 行 swizzle 直读寄存器 R。
   - 否则（column_major/缺省）→ 转置成逻辑行（保持原行为），`_mR0` 列 swizzle 读逻辑列=寄存器 R。
   这样 `_mRC` 访问器与 `mul_matrix_vector`（都算 `sum_i v[i]*field.data[i][j]`）在两种主序下都对齐 GPU 的
   `sum_i v[i]*reg_i[j]`。与 struct-array 路径（一直不转置、row_major、step 160 已通过）一致。

**先前的教训**：一开始我直接去掉所有 float4x4 转置 → 回归塌到 84/128（Collision/Frame/sekiro/heaven/valley 等
44 个 column_major 用例全崩）。证明转置对 column_major 必需；改为**按主序条件转置**才两全。

## 结果
- **TombRaider 29 → 34/37**：event1802（ScreenMatrix row_major，4/4）、event2201/2880/2892/2899
  （WorldToPSSM0 row_major）全部转通过。**回归 129/129（零回归）**——column_major 用例仍转置仍过；新增
  event1802 入回归守护 row_major 普通矩阵路径。
- 从 Dump 删 5 个转通过 zip（TombRaider 8→3 剩失败）。

### 仍失败的 3 个（**不同问题，非主序**）
- **event2848 / event7308**：`sv_position` 精度边缘（diff 0.0104 / 最大 0.026，容差 0.01）——float32 矩阵累加
  精度，F1 类，不放宽全局容差。event7308 另有个别 TEXCOORD5。
- **event2867**：`Color`(o3) 在**部分行**错（510/3372），o3=蒙皮矩阵 `_m30` 行的 blend（`SkinningParameters
  [r1.w/4]._m30..*v1.w + r0`）；row 0 通过、部分行错，属蒙皮/精度细节，非 WorldToPSSM0 主序。

## 本步代码改动
- `hlsl_interpreter.py`：`FieldDefinition.is_row_major`；`parse_cbuffer` 记录主序；`override_cbuffers_from_binary`
  的 float4x4/float4x3（数组+非数组）按 `is_row_major` 条件转置。
- 回归 +1 用例（event1802，129/129）。

## 结论
WorldToPSSM0「矩阵主序」问题**已修**（按声明 row_major/column_major 分别加载，这是通用且正确的模型，非 hack）：
TombRaider 29→34，零回归，event1802 亦顺带修好。剩 3 个属精度边缘（event2848/7308）与蒙皮 Color 细节
（event2867），非主序问题。

## 后续
- event2848/7308：sv_position float32 精度边缘（~0.01–0.026），需精确 FMA/累加顺序或相对容差策略（F1 通案）。
- event2867：蒙皮 o3/Color 部分行偏离，单独查（bone 索引/权重/精度）。
- 边缘：row_major 矩阵若仅有 CSV 无 bin（CSV 的 .row 是转置值），当前依赖 binary override 覆盖；TombRaider 均有
  bin，故不影响，留意即可。
