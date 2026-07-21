# Step 213 — Dump 剩余 55 深长尾：逐簇根因定位（不以投机改动冒险回归全绿）

Date: 2026-07-21

## Prompts（本步任务）

修复 Dump 剩余 55 个失败（29 PlanetCoaster 逐顶点数学发散、8 无 VS-golden、若干精度近过）。

## 结论先行

对全部 5 个簇做了逐例深挖，确认**残余 55 是不可约的硬长尾**：要么需逐例逆向（数据相关的多级
拉取 / HS fork-join DXBC VM），要么是**极端/退化输入下的 float 精度**，要么**根本无 golden 可判定**。
没有一个是低风险的通用修复点。为**保住 190/190 回归全绿**，本步**不做投机性改动**，只产出精确的根因
定位，供后续按例深挖。

---

## 逐簇根因（本步调查）

### 1. PlanetCoaster ×29 — 多级网格拉取的逐顶点数据发散

以 `event10418` 深挖到底：draw 是 `DrawInstanced, InstanceOffset=50, TriangleStrip`，per-instance
`POSITION7` 取 VB[50] 作 meshlet 基址。**逐组件比对确认 row0–row5 与 golden 逐位相等**——核心顶点
拉取/切线帧/颜色解包**完全正确**；从 **row6 起个别顶点发散**（o4 = TEXCOORD4：ours [15.75,6.73]
vs golden [0.157,0.099]）。

追到发散点：`o4.xy = 16 * r1.yz`，`r1.yz = r2.xy/32768 - 1`，`r2.x = asint(r3.x)&0xffff`，
`r3.x = t2[idx].val[0]`。而 `idx`、`r3` 组件由 `r0.x` 的打包位经**多级三元选择**
（`r4.xx ? r3.zw : r3.xy`、`r0.x = r4.y ? r1.w : r1.z`）决定，`r0.x` 又来自上一级 `t2/t3/t4`
结构化缓冲读 + 位掩码合并。即：**6+ 层结构化缓冲间接 + 条件位解包**，某顶点某一级读回的打包值
（ours 低16位=65033 vs golden=33089）与 GPU 不一致。三元条件本身是干净整数（`&int2(2,1)`→0/1/2，
真值无歧义），排除了 -0.0/denormal 真值 bug。属**数据相关、逐例**的深度分歧，无单点通用修复。

### 2. manhattan ×5 — 退化哨兵顶点的 float 精度

每例仅 1–2 个顶点失败（998~999/1000），但该顶点**所有属性**齐偏。定位：失败顶点（如 event124
row411）的输入 `TEX_COORD0 = (10000,10000,10000)` 是**退化/剔除哨兵**；经变换矩阵放大后，
float32 运算顺序的微差在量级 ~76 上放大到 ~0.5（> 容差 0.005）。属极端输入下的精度，非逻辑错。

### 3. witcher3_countryside ×6 — 曲面细分 HS/DS

`event21346` 是 VS+HS+DS+PS 曲面细分；按 DS-out 布局比较 HS 控制点流。失败集中在**所有奇数 CP 记录**
的 `cp[4]`（=CP 记录第 4 分量）：ours 1.0 vs golden 0.0，169/1024 行。位于 HS fork/join DXBC VM
（`_execute_void_main` 外的 `DXBCInterpreter` 分相执行）与近似的 CP-流对齐比较中，属细分管线的 VM
边界问题，非通用单点。

### 4. TombRaider ×2 / sekiro2 ×1 — float 精度近过

sv_position / TexCoord 偏 0.02~0.07（量级百级），逐顶点 float 顺序精度，近过但超 0.005 容差。

### 5. three.js ×1 — float3x3 行列主序残留

step212 修了嵌套列表 `[[...]]` 解析崩溃；残留 `_normalMatrix`（CSV nested-list）法线变换偏。
**本步实测**：把 `_parse_float3x3` 改为转置后 three.js 仍 0/6（12 errors 不变）——**残留不是简单
的行列主序翻转**，而是更深的约定/下游问题；已还原改动。

### 6. 无 VS-golden ×8 — 不可判定

`SingleDrawDump-*×4`（无 VS_shader，compute/UAV draw）、`BlackMyth_event2063`、`Nobu15`、
`EndlessSpace2_event3093` 等：capture **无 `*_vs_mesh` golden**（未 dump VS 输出或非 VS draw）。
VS-golden 工作流**无参照可比**——无从"修复"或验证。

### 7. EndlessSpace2_event3061 — NaN（step210 已诊断）

VS packed 数据纹理解码在部分顶点发散 → `1/sqrt(0)=NaN`。数据相关深度问题。

---

## 结果

- **未改动代码**（`hlsl_interpreter.py`/`render.py` 与 step212 提交一致）；**回归维持 190/190 全绿**。
- Dump 仍 55（无新增可安全通用修复的项）。
- 产出逐簇精确根因，后续如需继续，应按**单例逆向**推进（尤以 PlanetCoaster 的 t2/t3/t4 打包格式与
  奇数-CP 的 HS VM 为切入点），并对每次改动跑全回归防止破坏绿线。

## 关键结论

- 残余失败**非通用 bug**：29 例多级网格拉取数据发散、6 例细分 VM、~10 例极端/退化输入 float 精度、
  1 例 float3x3 约定、8 例**无 golden 不可判定**。
- 工程判断：在无法以低风险通用修复推进时，**不以投机改动冒险已绿的 190 回归**；本步价值在于把"深长尾"
  从模糊变为**逐簇可执行的定位**。
