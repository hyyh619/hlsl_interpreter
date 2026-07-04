# Step 175 — 剩余 29 案归类与修复意见

基准：step 174 收官后的 Dump 全量 triage（`triage_dump.py --vs-only`，容差 0.005）。
回归表已从步 168 的 129 案增至 **144 案**（全过）；Dump 从 44 案降至 **29 案**。

## 总览

| 类别 | 案数 | 性质 | 修复可行性 |
|---|---|---|---|
| A. 无 golden 可比 | 6 | 结构性不可验证 | 需导出侧补 golden |
| B. 超时/性能 | 2(+2 慢案) | 非正确性 | 可修（热路径优化） |
| C. HS/DS 结构墙 | 5 | 反编译丢失/惯例差异 | 部分可动 |
| D. GPU 超越函数墙 | 7 | 硬件近似差异 | 投入产出比低 |
| E. witcher 采样家族残余 | 6 | 深层链路缺陷 | **可修，优先** |
| F. 精度临界单案 | 3 | 单点残差 | 可试探 |

（E 类 22229 与 D 类 3207/9493 各有精度临界成分，计数按主要归因；共 29 个 zip。）

## A. 无 golden（6 案）

**案例**：EndlessSpace2_event3093、Nobu15_event2894、sekiro2_event13554/13931/14130/14388。

sekiro2 四案是 tessellation 案且无 `_ds_mesh.bin`；ES3093 是 DrawAuto/SO 案
（NumIndicesOrVertices=0）；Nobu2894 无 VS mesh golden。

**意见**：唯一出路在导出侧——dx_dump 补导 DS 阶段 mesh golden
（`_ds_mesh.bin` + `_layout.csv`）与 Stream-Output buffer 内容。解释器侧无事可做。

## B. 超时/性能（2 + 2 慢案）

**案例**：OldWorld_event1034/2767（203,328 顶点，纯 Python >300s）；
新增慢案 sekiro2_event3207/9493（步 174 修通风力循环后 45k 顶点 × 循环体，
并发 triage 下超 300s，独跑可完成，44341/45576）。

**意见**：
1. 语句级预编译——语法树已缓存，但赋值目标解析、swizzle 字符串处理仍每次重做，
   可预解析为闭包；
2. 确认 `max_workers` 顶点级并行在 vs_only 路径吃满；
3. 短期：triage 对慢案单独放宽超时即可先见分晓。非正确性问题。

## C. HS/DS 结构墙（5 案）

**witcher3_countryside_event16803/16817/21346**：DS 读 HS fork/join（patch 常量）
相位输出 `vpc0.y`，该相位被 3Dmigoto 反编译器整体丢弃，HLSL 源里不存在。
**意见**：给 DXBC VM 增加 HS fork/join 相位执行（disasm 里指令还在），由 VM 生成
`vpc*` 喂给 DS，绕过反编译。工作量大但路线清晰。

**sekiro4_event19857/20244**：两个独立小缺陷：
1. `StructuredBuffer<PointLight>` stride 被误判 16B —— **可修**：从 struct 成员布局
   推断真实 stride（与步 169 `_captured_sb_strides` 同思路）；
2. point-sprite golden 的 SV_POSITION xyz≡0 惯例 —— **可修**：比较器特判。

**建议先做 sekiro4 两案**，有望转正。

## D. GPU 超越函数墙（7 案）

**案例**：TombRaider_event2848/7308（827/1548，rsqrt/sincos 程序化顶点动画）；
manhattan_event87/124/161/198（各差 1–2 行，`frac(sin(≈80000))` 出生粒子哈希）；
sekiro2_event3207/9493 残余 1235 行（`sin(≈3889 rad)` 风力大相位，Parallels GPU）。

**共性**：大相位/深链条超越函数的硬件近似差异。步 174 已实证 **f32 归约模型净负效**
（44341→44338，已回滚）——该 GPU 归约精度高于朴素 f32 模型，math.sin 整体更近。

**意见**：若要继续，正确姿势是**数据驱动拟合**——批量反解失败顶点 golden 隐含的
sin/rsqrt 输出，与多种归约模型（Cody-Waite 双 f32、Payne-Hanek、查表+插值）比对
残差分布，确认 vendor 实际实现再落地。manhattan 每案只差 1–2 行、TombRaider 已
827/1548，按"已知墙"接受更划算。**不放宽全局容差**（会掩蔽真 bug）。

## E. witcher 采样家族残余（6 案）— 建议的下一步主攻

**案例**：21719（368/1728）、21895（1057/6360）、21979/22049（132/840）、
22260（54/108）、22229（10/12）。

步 173 十项修复后从全 0 大幅收敛，说明方向正确、还剩下一层。已掌握的线索：

- **21979 与 22049 通过数完全相同（132/840）** → 大概率同 shader 或同缺陷，
  修一得二。错误集中在 TexCoord4（探针/雾链）。
- **22260**：失败行 TexCoord3 输出是**常量**（0.6796/0.8352/0.9610 = 雾基色）而
  golden 逐顶点变化——`exp2(-14.6)≈0` 塌缩迹象，疑似雾衰减链系数错误（非精度），
  **低垂果实，建议第一个开刀**。
- **22229**：仅余 2 行 diff=0.005075（超容差 1.5%），灯光解析衰减 f32 细节；
  可检查该链的 FMA 融合覆盖（`_try_fma`）。

**方法**：沿用步 172/173 的"[STMT] trace 反照 + golden 中间值反解"。

## F. 精度临界单案（3 案）

- **EndlessSpace2_event2991（1464/1536）**：几何/SB 已证全对（golden TEXCOORD1
  与我们完全一致），残差锁定在曲线图集宽度因子——采样点 u·w=1832.5 **恰在纹素
  中心**，尾部 72 行差一个 texel。**意见**：验证 GPU point 采样在精确 .5 处的舍入
  方向，及 `r1.y*4+0.5` 链的 f32 舍入是否把 .5 推到另一侧；±1 texel 对照双跑即可判定。
- **sekiro2_event14998 GS 段（28974/29148）**：TexCoord4 相对差 5e-4，非 trig
  来源（billboard 角偏移链）。**意见**：用步 172 的独立逐指令复现脚本法对 GS 定位
  ——该方法已两次证明比对着 golden 猜快得多。

## 建议优先级

1. **E 类 witcher 家族**（22260 → 21979/22049 → 21895/21719）：方法成熟、线索在手，
   期望再转正 2–4 案；
2. **C 类 sekiro4 两案**（SB stride 推断 + point-sprite 惯例）：小改动、确定性高；
3. **F 类两单案**：各一个对照实验即可分晓；
4. **B 类性能**：一次热路径优化解锁 4 案结果；
5. **A/D 类**：分别等导出侧补 golden 与 vendor 数学拟合，暂记为墙。

## 附：29 案明细（最近一次全量 triage）

| 案例 | 状态 | 类别 |
|---|---|---|
| EndlessSpace2_event2991 | 1464/1536 | F |
| EndlessSpace2_event3093 | 无 golden | A |
| Nobu15-frame3456_event2894 | 无 golden | A |
| OldWorld_event1034 | timeout | B |
| OldWorld_event2767 | timeout | B |
| TombRaider_event2848 | 827/1548 | D |
| TombRaider_event7308 | 827/1548 | D |
| manhattan_event87/124/161/198 | 998~999/1000 | D |
| sekiro2_event13554/13931/14130/14388 | 无 golden | A |
| sekiro2_event14998 | VS 全过；GS 28974/29148 | F |
| sekiro2_event3207 | 44341/45576（慢） | D/B |
| sekiro2_event9493 | 44341/45576（慢） | D/B |
| sekiro4_event19857 | 0/5 | C |
| sekiro4_event20244 | 0/7 | C |
| witcher3_event16803 | 0/4 | C |
| witcher3_event16817 | 0/4 | C |
| witcher3_event21346 | 0/1024 | C |
| witcher3_event21719 | 368/1728 | E |
| witcher3_event21895 | 1057/6360 | E |
| witcher3_event21979 | 132/840 | E |
| witcher3_event22049 | 132/840 | E |
| witcher3_event22229 | 10/12 | E |
| witcher3_event22260 | 54/108 | E |
