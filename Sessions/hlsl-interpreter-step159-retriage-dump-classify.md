# Step 159 — 重跑 Dump 全量、删通过用例、失败分类（含全部 case 清单）

## 任务
1. 重新运行 Dump/ 所有 case；2. 通过的删除；3. 未通过的调查原因并分类；4. 每类详列所有相关 case。

## 执行

`python triage_dump.py --vs-only --workers 6 --timeout 240`（vs_only 跳过光栅化/PS，专验 VS 输出）。

**Dump 现存 85 个 zip**（step 158 把已通过的 `event3502` 移入 `Cases/` 回归后，Dump 由 86→85）。

**首轮发现一个新 CRASH 并就地修复**：`witcher event16834` 抛 `OverflowError: cannot convert float
infinity to integer`——上游除零产生 inf UV，流入 `texture.py::Sampler._wrap_address` 的
`math.floor(coord)`。**修复**：在 `transform_coordinates` 入口把非有限（NaN/inf）UV 钳为 0，采样退化为确定
texel 而非崩溃，坏值仍以正常 golden mismatch 暴露。修后 event16834 跑完（0/30，正常 mismatch）。
**回归 126/126 PASS（零回归）**。

## 结果：通过 0 / 85 → 无可删除用例

重跑（修复后权威态）：**0/85 通过**。step 156–158 的修复中唯一翻盘的 `event3502` 已在 step 158 移入
`Cases/` 回归（故不在本次 Dump 85 内），等同「通过即移出 Dump」。当前 85 个均未全通过，**本步无新增删除**。
（两个 OldWorld 超时是「正确但慢」，未完成不能判通过，保留于 A 类。）

最终桶：MISMATCH 67 / NO_GOLDEN 16 / TIMEOUT 2 / CRASH 0（已修）。

## 失败分类（7 类，共 85）

### A. 超时（2）— 纯 Python 太慢，非正确性
20 万顶点级，被 240s 杀前 0 个 `Error:`（step 155 已核「正确但慢」）。
- `OldWorld_event1034.zip`
- `OldWorld_event2767.zip`

### B. 无 golden（16）— 结构性不可验证
无可比 VS 网格 / 0 顶点执行（日志无 VS-vs-golden 比较段）。
- `EndlessSpace2_event3093.zip`
- `Nobu15-frame3456_event2894.zip`
- `manhattan_frame_274_event50.zip` / `event87` / `event124` / `event161` / `event198`
- `sekiro2_event13554.zip` / `event13931` / `event14130` / `event14388`
- `sekiro4_event19857.zip` / `event20244`
- `witcher3_countryside_event16803.zip` / `event16817` / `event21346`

### C. TombRaider 矩阵选择子丢失（37）— 反编译有损，超出 HLSL 源解释边界
3Dmigoto 丢弃 `WorldParameters[]` 矩阵成员选择子 → 位置/法线取错矩阵；全部 `passed 0/Y`。需 DXBC 反汇编
寄存器级 golden（`dxbc_diff`）才能定位。
- event67, 103, 225, 380, 396, 419, 429, 435, 447, 521, 626, 678, 692, 817, 832, 871, 931, 954,
  1002, 1018, 1802, 2113, 2129, 2153, 2164, 2171, 2201, 2252, 2527, 2605, 2848, 2867, 2880, 2892,
  2899, 7308, 7376（均 `TombRaider-frame25229_event*.zip`，共 37）。

### D. Octopath 解码（8）— 四元数/骨骼基置换 + sv_position + 地形退化
- `event3012.zip`（**passed 30/51**，step 156 half4 修复后大幅改善，残 sv_position）
- `event2135.zip`（0/6）/ `event2912.zip`（0/504）：`TEXCOORD10/11` **四元数/骨骼基分量置换**
  （x↔y 交换 / 3-cycle，解码顺序 bug，非列错位——step 158 已澄清）
- `event664.zip`（0/51）：sv_position 蒙皮
- `event3601.zip`（0/96）：procedural foliage 纹理采样精度
- `event576.zip` / `event2651.zip` / `event2682.zip`（各 8/23064）：地形 heightmap 退化（cb 全 0 → LOD/索引塌缩）

### E. witcher 切线/矩阵主序/纹理（10）— 主序反编译 + texel 精确性
step 157 已补 Texture2DArray 切片采样（采到真实 texel、值向 golden 收敛），但 o2/o3 受 texel 精确性
（R16G16_FLOAT 细节法线）+ 矩阵主序（反编译 `mul(world,M)` vs GPU `mul(M,world)`）双重限制未入容差。
- `event16215.zip`（0/30）/ `event16834.zip`（0/30，本步修崩溃）
- `event21719.zip`（0/1728）/ `event21895.zip`（0/6360）/ `event22049.zip`（0/840）
- `event22092.zip`（0/414）/ `event22201.zip`（0/18）/ `event22229.zip`（0/12）/ `event22260.zip`（0/108）
- `event21979.zip`（132/840）：sv_position ~1.6e-4 相对精度边界（近 F1）

### F1. 精度边界（2）— 失败行 diff 略超 0.005 容差
- `sekiro2_event3207.zip`（43329/45576）
- `sekiro2_event9493.zip`（43329/45576）

### F2. 其它（10）— 混合
- `Nobu15-frame3456_event586.zip`（0/2174）：矩阵主序（step 156 修了 cbuffer 矩阵限定符解析使其加载，
  但反编译 `mul(world,M)`=转置，与 C/E 同墙）
- `OldWorld_event3338.zip`（23550/23814，绝大多数过，少量行错，近 A 但可比对）
- `EndlessSpace2_event2991.zip`（1464/1536）/ `event3061.zip`（1188/1536）：部分行错
- `sekiro2_event14998.zip`（0/4867）/ `event15481.zip`（0/3）/ `event16052.zip`（0/3）/ `event4833.zip`（12/24）
- `sekiro4_event20560.zip`（0/179）/ `event7844.zip`（162/324）

## 汇总

| 类 | 数 | 性质 |
|----|----|------|
| A 超时 | 2 | 性能（正确但慢）|
| B 无 golden | 16 | 结构性不可验证 |
| C TombRaider 主序选择子 | 37 | 反编译有损，超 HLSL 源边界 |
| D Octopath 解码 | 8 | 四元数置换 / sv_position / 地形退化（部分可修）|
| E witcher 主序+纹理 | 10 | 主序反编译 + texel 精确性 |
| F1 精度 | 2 | 容差边界 |
| F2 其它 | 10 | 混合（含 Nobu586 主序）|
| **合计** | **85** | **0 通过 → 本步无删除** |

## 本步代码改动
- `texture.py`：`transform_coordinates` 钳制非有限 UV（修 event16834 崩溃）。回归 126/126，零回归。

## 后续可推进
- D：event2135/2912 四元数/骨骼基分量置换（解码顺序 bug，最具体可修）。
- C/E/F2-Nobu586：矩阵主序——需 `dxbc_diff` 寄存器级 golden 重建，超出直接解释 HLSL 源。
- A：热路径优化（20 万顶点）。F1：统一相对容差策略。
