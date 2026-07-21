# Step 212 — 修复 Dump 剩余 63 fails：整型输出比较 + _RawBits 幅值 + 括号矩阵解析

Date: 2026-07-21

## Prompts（本步任务）

修复上一步遗留在 `Dump/` 的 63 个失败（37 PlanetCoaster 深变体、8 无 VS-golden、
manhattan/witcher/TombRaider 近过、EndlessSpace2 NaN、three.js 崩溃）。

---

## 思考 · 分诊

以当前代码重跑 63 例，按游戏前缀 + 失败组件聚合。57→37 PlanetCoaster 仍是主体。重点解剖
PlanetCoaster（GPU 驱动顶点拉取：SV_VertexID + StructuredBuffer + 位运算解包）。

**关键发现**：以 `event12358` 为例，逐组件比对后确认 **o2（TEXCOORD2）与 golden 逐位相等**——
核心顶点拉取/切线帧数学是**对的**。失败集中在个别组件，且暴露了三类独立的通用缺陷。

---

## 执行 · 三个通用修复

### Fix 1 · golden 比较：整型输出寄存器按位比较

`compare_vs_output_with_golden_params` 遇到 golden 分量是整数（UINT/SINT mesh 格式 → Python int）
而我方输出是 float（寄存器存的是打包 uint/索引，经 `float` 输出槽写出）时，走 `ov != gv` 直接判错
（`2.83e-37 != 46198815`）。但两者**同一 32 位**，RenderDoc 按整数 dump、我方按 float 读。新增
`_golden_component_match()`：整型 golden vs float 输出按 `asuint` 位比较（或整值相等）。

### Fix 2 · cbuffer 矩阵/向量 CSV 解析容忍括号（修 three.js 崩溃）

`_parse_float3x3/4x4/_vector` 用 `split(',')` 再 `float()`，遇到 three.js 3MF 把矩阵写成嵌套列表
`[[a,b,c],[d,e,f]]` 时 `float('[[0.707...')` 崩溃（exit=1）。改为正则 `_num_tokens()` 提取数字，
兼容裸 CSV 与嵌套列表。→ three.js 不再崩溃（余下 float3x3 变换约定问题另计）。

### Fix 3 · _RawBits 幅值判据：小整数值 vs 大浮点位型

`event12358` 颜色解包 `r3 = (uint)(r1.w & 255...)` 后 `r3 * (1/255)`：`& 255` 产出 `_RawBits(255)`，
向量乘 `float4(0.00392) * r3.xyzw` 走 `execute_binary_op` 元素级，经 `_coerce_rawbits_for_float_op`
把 `_RawBits(255)` **asfloat** 成非规格化数（≈0）→ 分量坍缩。而 step-210 的 raw-int×float 值运算修复
只在标量 `evaluate_syntax_tree` 路径，向量元素不覆盖。

根因：`_RawBits(255)` 是**整数值**（字节），不是浮点位型。判据：正常浮点的位型 ≥ 2²³
（`0x00800000`，指数非零）；小于该值 asfloat 只会得到近零非规格化数，几乎不可能是本意——它是整数值
（字节/计数/索引）。`_coerce_rawbits_for_float_op` 与 `_coerce_rawbits_list` 改为：`_RawBits` 无符号
< 2²³ 取整数值，≥ 2²³ 才 asfloat（保 BlackMyth 的 `asfloat(exp<<23)` 位置缩放不变）。

---

## 结果

三修复清掉 **8 个 PlanetCoaster**（颜色字节解包 + 整型输出比较类），加入 Cases/ + regression
（182 → **190**），删除其 Dump 副本。**全回归 190/190 全绿**（三修复对既有零回归，含 BlackMyth 的
asfloat 路径）。Dump 63 → **55**。

**剩余 55（诊断留档，深长尾）：**
- **29 PlanetCoaster**：真·逐顶点数学发散（部分行从某行起偏，如 event10418 row6+ TEXCOORD4
  15.75 vs 0.157；连续顶点输出相同值），属顶点拉取链上更深的数据相关分歧。非通用单点 bug。
- **8 无 VS-golden**（SingleDrawDump×4 / BlackMyth_event2063 / Nobu15 / EndlessSpace2_event3093 等）：
  capture 无 `*_vs_mesh` golden（compute/UAV/GS-only 或未 dump VS 输出）→ VS-golden 工作流**无法判定**，
  非可修复失败。
- **manhattan(5)/witcher3(7)/TombRaider(2)/sekiro2(1)**：多为逐顶点精度/边界近过（998~999/1000 等）。
- **three.js**：崩溃已修；余 float3x3（`_normalMatrix` 嵌套列表）行/列主序约定导致法线变换偏。
- **EndlessSpace2_event3061** NaN（step210 已诊断，packed 数据纹理解码发散）。

---

## 关键结论

- golden mesh 的整型输出分量须**按位比较**（我方 float 寄存器 vs RenderDoc 整数 dump 同位）。
- cbuffer CSV 数值解析要容忍**嵌套括号**格式（不同 capture 写法不一）。
- `_RawBits` 在浮点运算里的语义按**幅值**判定：小整数值取值、大位型 asfloat——修复标量与**向量元素级**
  两条路径的一致性。
- PlanetCoaster 余下失败与其它多为**逐顶点数学发散 / 无 golden 可比**，属需逐例深挖的长尾，未强行以
  投机改动追平（避免污染回归全绿）。
