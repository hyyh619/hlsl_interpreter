# Step 155 — 复检 7 类失败：哪几类还没修

## 任务

> 检查下列 7 类失败，还有几类没有修复的（A 超时 / B 无 golden / C TombRaider 矩阵选择子 /
> D Octopath 解码 / E witcher 切线 / F1 精度 / F2 其它）。

这是一次**审计**：跑全量 `Dump/` 三联，把当前失败重新归入 7 类，对比各类的「原始数 → 现状数」，
给出每类的修复状态与可修性判定。

## 方法

`python triage_dump.py --vs-only --workers 6 --timeout 240`（`vs_only` 跳过光栅化/PS，专验 VS 输出；
这也是 A 类超时的既有缓解手段）。结果落 `Cases/triage_results.csv` + `Cases/triage_logs/*.log`。
按 detail 签名（timeout / no-golden / passed X/Y）+ 套件前缀归类。

## 结果总表（Dump/ 现存 86 个失败）

| 类 | 现象 | 原始 | 现 Dump | 修复状态 | 可修性 |
|----|------|------|---------|----------|--------|
| **A 超时** | 纯 Python 太慢被 240s 杀，0 个 `Error:` | 45 | **2** | **大幅修复**（vs_only 解掉 43） | 残 2 仅性能问题（非正确性）|
| **B 无 golden** | capture 无可比 VS 网格 / 0 顶点 | 16 | **16** | 不变 | **结构性不可修**（N/A）|
| **C TombRaider 矩阵选择子** | 反编译丢 `WorldParameters[]` 矩阵成员选择子，取错矩阵 | 37 | **37** | 不变 | **超出 HLSL 源解释边界**（反编译有损）|
| **D Octopath 解码** | packed 法线/切线格式 + golden 列解析 | 18 | **9** | **部分修复**（10 个转 PASS 删除）| 残 9 为更深子 bug，可修 |
| **E witcher 切线/拷贝** | 切线帧 o2/o3、SH/雾、除零 | 7 | **10** | **已根因定位**，0 转 PASS | 残因 = Texture2DArray 采样缺口（共有，非反编译）|
| **F1 精度边界** | 失败行 diff 恰好略超 0.005 容差 | 2 | **2** | 不变 | 容差/精度策略，可修 |
| **F2 其它** | cbuffer 未加载 / golden 全 0 / 实例索引等 | 9 | **10** | 不变 | 混合，部分可修 |

> 计：2+16+37+9+10+2+10 = **86** ✓

## 逐类核验细节

### A 超时（2）— 已基本解决，残留纯性能
- `OldWorld_event1034`、`OldWorld_event2767`：日志显示 **Loaded 203328 vertices**，被杀前 **0 个 `Error:`**
  —— 输出正确，只是 20 万顶点纯 Python 跑不完 240s。属正确-但-慢。`vs_only` 已把原 45 个超时里的 43 个救回
  （step 504b3a3）；这 2 个顶点量过大，需热路径 AST 预编译 / 向量化才能进一步压。**非正确性问题。**

### B 无 golden（16）— 结构性不可验证
- 例 `witcher3_event16803`：`VS executed 4 vertices`，但无 golden mesh CSV → 日志无 VS-vs-golden 比较段。
- 例 `manhattan_frame_274_event124`：`Loaded 0 vertices from ia_vertex_data.csv` → 0 顶点执行。
- 名单：EndlessSpace2_event3093；Nobu15_event2894；manhattan ×5（50/87/124/161/198）；
  sekiro2 ×4（13554/13931/14130/14388）；sekiro4 ×2（19857/20244）；witcher ×3（16803/16817/21346）。
- **无可比对象，永远标 N/A。**

### C TombRaider 矩阵选择子（37）— 出界
- 全部 `passed 0/Y`。3Dmigoto 反编译丢弃了 `WorldParameters[]` 结构体的矩阵成员选择子，位置/法线取到错误矩阵。
  只能靠反汇编 `cb0[base+N]` 寄存器号重建，**超出「直接解释 HLSL 源」边界**（与 step 154 给 witcher 上 DXBC
  寄存器级 golden 同类问题——TombRaider 是下一个该上 `dxbc_diff` 的套件）。

### D Octopath（9）— 部分修复，残留更深
- fbf1313 修了 typed-buffer 格式推断 + golden uint 列解析，**10 个转 PASS 删除**。残 9：
  - `event2651/2682/576`：`passed 8/23064`，sv_position 数值溢出（极少数顶点对）。
  - `event2912/3012/3601/664`：蒙皮索引残留（0/Y）。
  - `event3502`：高编号 TEXCOORD 列映射。
  - `event2135`：四元数解码残留。
- **可修**，属 step 152 收尾。

### E witcher 切线/拷贝（10）— 已根因定位（step 153/154）
- step 153 修了 `Texture.SampleLevel`（VS 采样）；step 154 修了 **per-instance 顶点缓冲加载**
  + 新增 **DXBC 寄存器级 golden**。以 `event16215` 为例，o0/o1/o4/o5/o6 在 DXBC VM 已对齐 golden，
  **只剩 o2/o3 切线帧**——经寄存器级 golden 证明其根因是 `sample_l` 对 **Texture2DArray** 返回 `[0,0]`
  （细节法线采样缺口），**HLSL 解释器与 DXBC VM 共有，非反编译问题**。
- 仍 0 转 PASS（切线帧未收敛）。`event16834` 含除零 nan；`event21979` 是 sv_position 精度边界（近 F1）。
- **下一步**：实现 VS 端 Texture2DArray 切片采样（`sample_l` 取 `coords.z` 作 array index），同时惠及两解释器。

### F1 精度边界（2）— 容差策略
- `sekiro2_event3207`、`sekiro2_event9493`：均 `passed 43329/45576`，失败行 diff 恰好略超 0.005 容差。
  开 float32 模拟不消失。需统一精度/相对容差策略（同 step 153 的 21979）。

### F2 其它（10）— 混合
- `Nobu15_event586`：`0/2174`，VS 全 0（cbuffer 未加载）——**最像独立可修 bug**。
- `EndlessSpace2_event2991`(1464/1536)、`event3061`(1188/1536)：部分行错。
- `sekiro2_event14998`(0/4867)、`event15481`(0/3)、`event16052`(0/3)、`event4833`(12/24)；
  `sekiro4_event20560`(0/179)、`event7844`(162/324)：实例索引残留 / 局部解码。
- `OldWorld_event3338`：`23550/23814`，绝大多数过，少量行错（近 A，但能比对）。

## 结论：还有几类没修？

**7 类无一在 Dump/ 内被清零**，但按可修性分三档：

1. **结构性不可修（2 类，53 例）：B（16，无 golden）+ C（37，反编译丢选择子）。** 外加 E 的深层切线部分。
   这些靠「解释 HLSL 源 + 仅 VS 输出 golden」无解，需反汇编寄存器级 golden（C 是下一个目标套件）。
2. **已基本关闭（1 类）：A。** vs_only 解掉 43/45；残 2 是 20 万顶点的纯性能问题，非正确性。
3. **仍有可修残留（4 类，~31 例）：D（9）、E（10，其中浅层=纹理数组采样）、F1（2，容差）、F2（10，
   首推 Nobu586 cbuffer 未加载）。**

一句话：**真正还需写代码修的是 D / E / F1 / F2 这 4 类**；A 仅差性能优化；B、C 在当前架构下不可修
（要么无 golden，要么需要 DXBC 反汇编重建）。

## 本步产物
- 复检 86 例并重新归类，更新 `Cases/triage_results.csv`。
- 未改解释器代码（纯审计）；回归基线维持 step 154 的 125/125。
- 优先级建议（供后续步）：① E 类 Texture2DArray 切片采样（解释器+VM 双收益）；② F2 Nobu586 cbuffer 加载；
  ③ D 类 Octopath 蒙皮/溢出收尾；④ 给 C（TombRaider）上 `dxbc_diff` 寄存器级 golden；⑤ F1 统一容差策略。
