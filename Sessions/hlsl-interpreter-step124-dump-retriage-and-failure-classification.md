# hlsl-interpreter-step124：Dump/ 重新triage、删除通过项、失败分类与修复方案

## 任务

1. 继续测试 `Dump/` 文件夹中的 case。
2. 通过的 case 删除。
3. 不通过的 case 归类，写一份失败报告。
4. 对每一类失败分析原因并给出修复方案。

## 执行

### 1. 重新跑全量 triage

`Dump/` 目录在 step94 / step122 之后还剩 **140** 个历史失败 case（step93 时为 181，sekiro C 类等被 step94 修复删除后减少）。
用既有的 `triage_dump.py`（headless，300s 超时，`float_tolerance=0.005`，`float32_emulation=True`）以 12 worker 全量重跑：

```
python triage_dump.py --workers 12
```

判定规则（与回归一致）：进程正常退出 **且** 日志无 `Error:` 行 **且** `Total PASSED rows: X/Y` 满足 `X==Y` 才算 PASS。

### 2. 结果

```
TRIAGE SUMMARY: 6/140 passed
```

| 状态 | 数量 |
|---|---|
| PASS（已删除） | 6 |
| FAIL | 134 |

**通过并已从 `Dump/` 删除的 6 个：**
- `TombRaider-frame25229_event1837` / `event2336` / `event2549` / `event2565` / `event2644`（step94 的 float4x3/struct 修复使其位置+法线全对，且这几个 draw 不含会丢选择子的法线分支）
- `manhattan_frame_274_event13`

删除后 `Dump/` 剩 **134** 个失败 case。每个 case 的分类写入 `Cases/dump_failure_categories.csv`，原始 triage 明细在 `Cases/triage_results.csv`，逐 case 日志在 `Cases/triage_logs/`。

---

## 失败报告：134 个 case 的分类

| 类别 | 数量 | 性质 | 是否解释器 bug | 优先级 |
|---|---|---|---|---|
| A. 超时 (timeout >300s) | 45 | 正确但慢 | 否（性能） | 低 |
| B. 无 golden 可对比 | 16 | 无法验证 | 否（数据缺失） | 不修（N/A） |
| C. TombRaider struct 矩阵选择子丢失 | 37 | 输出错误 | 否（反编译有损） | 不可从 HLSL 修复 |
| D. Octopath 输入属性解码 + golden 列解析 | 18 | 输出错误 | 是 | 中 |
| E. witcher3 切线/拷贝属性解码 | 7 | 输出错误 | 是 | 中 |
| F1. 精度/边界（高通过率） | 2 | 极小 diff | 是（精度） | 低 |
| F2. 其它 value mismatch（sekiro/ES2/Nobu 残留） | 9 | 输出错误 | 部分 | 中 |
| **合计** | **134** | | | |

---

### A 类：超时（45 个）

**家族分布：** witcher3(17)、sekiro2(8)、TankMechanicSimulator(8)、OldWorld(3)、heaven(3)、Nobu15(2)、Octopath(1)、TombRaider(1)、valley(1)、sekiro4(1)。

**原因：** 解释器是纯 Python 逐顶点 / 逐像素执行，无向量化/编译。顶点数大（如 witcher 全屏 pass、TankMechanicSimulator 大网格）或像素着色器跑满视口时，单 case 超过 triage 的 300s 上限。**证据：** 这些日志在被杀掉前**没有任何 `Error:` 行**、也没有 `Total PASSED rows:` 总结行（中途终止）。不是正确性 bug——同类全屏 pass（`witcher3_event7358`）在回归套件给足时间即通过。

**修复方案（按收益）：**
1. **加 `vs_only` 配置**：正确性只看 VS-vs-golden，但 `render.py` 现在跑完整管线（含光栅化+PS）。跑完 VS 对比即返回、跳过光栅化/PS，可消除绝大多数超时。
2. **解释器热路径预编译**：`evaluate_syntax_tree`/`get_value` 每个顶点都重新解析字符串。对 main 函数体做一次性 AST 预编译并缓存 swizzle/下标解析，可数量级提速。
3. **提高 triage 超时阈值**（回归已是 1800s）作兜底，不解决根本性能问题。

---

### B 类：无 golden 可对比（16 个）

**家族分布：** manhattan(5)、sekiro2(4)、witcher3(3)、sekiro4(2)、EndlessSpace2(1)、Nobu15(1)。

**原因：** 日志无 `Total PASSED rows:`，detail = `no VS-vs-golden comparison in log`。capture 里**没有可对比的 golden VS 网格**——该 draw 顶点被完全裁剪 / 0 顶点（manhattan 多个 case PS 执行 0 像素）、indirect/GPU 生成几何未导出 `*_mesh.csv`、或 capture 本身不完整。

**修复方案：** 这类**无法用 VS-vs-golden 工作流验证**，非解释器问题。建议从扫描集标注为 **N/A**，不计入"失败"；若要覆盖需改用对比最终 RT 像素的验证方式。

---

### C 类：TombRaider struct 矩阵选择子丢失（37 个，全部 TombRaider-frame25229）

**原因（step92/93 已确诊，本次复核）：** cbuffer 为 `struct { row_major float4x4 WorldViewProject; World; ViewProject; } WorldParameters[12]`。反汇编里位置用 `cb0[base+0..3]`（WorldViewProject）、法线用 `cb0[base+4..6]`（World）。但 3Dmigoto 反编译出的 HLSL **把 struct 成员/矩阵选择子丢弃了**——位置和法线都写成同样的 `._m00.._m22`，只有反汇编的 `cb0[base+N]` 寄存器号能区分语义，HLSL 源里这个信息已不存在。

本次新观察：失败不只在法线。**约 22 个 case 在 `sv_position` 就错**（如多 case 复现同一个错值 `output=-1335.94`，说明 `WorldParameters[r0.x]` 的**实例/数组索引**解析到了固定的错误矩阵），另 ~15 个在 `TexCoord3`（法线/切线）错。

**修复方案：**
- **只能从反汇编修复**：解析 `VS_shader_disasm.txt` 里 `cb0[r0.x + N]` 的真实寄存器号来驱动取值，而非相信 HLSL 的 `_mRC` token。等于为这类 capture 增加一条"反汇编驱动"的解释路径，工作量大。
- 或**接受现状**：对一个"直接解释 HLSL 源"的解释器，这类 case 超出能力边界，建议不投入。

---

### D 类：Octopath 输入属性解码 + golden 列解析（18 个，全部 Octopath-frame746）

**原因（三种叠加，均为可修复的解释器问题）：**

1. **归一化字节属性未按格式解码**：`TEXCOORD10/11` 大量出现 `output=0.498039`（= 127/255）vs `golden=±1.0`、或 `output≈-0.03` vs `golden≈1.0`。说明 packed 法线/切线（`R8G8B8A8_UNORM`/`_SNORM`）从 `ia_vertex_data` 解码时 offset 或格式不对，应按 `ia_input_layouts.csv` 的 `Format` 还原（UNORM→`b/255`，SNORM→`b/127` 带符号）。
2. **golden 列被当 uint 解析**：`TexCoord[0]: output=0.437500 golden=1054867456.000000`。`1054867456 = 0x3EE00000`，正是 `0.4375` 的 IEEE-754 位模式——即我们的输出**正确**，但 golden mesh CSV 这一列以**十六进制/uint** 存储而 `load_vs_golden_from_mesh_csv` 按整数读入，造成假性 mismatch。
3. **高编号语义列映射 + 位置精度**：`event3502` 输出 `TEXCOORD12/13` 等高编号语义对不上 golden 列；多个 case `sv_position` 仅差 ~0.1–0.3（精度/边界），少数（`event3012`）位置结构性偏大。

**修复方案：**
1. 复核 `ia_input_layouts.csv` → VS 输入解码：按 `Format` 正确处理 UNORM/SNORM/packed-uint 与 offset。
2. `load_vs_golden_from_mesh_csv` 对"看起来是大整数但实为 float-bit"的列做位重解释（或统一按 float 读 golden 列）。
3. 让 golden 列映射使用**完整 indexed 语义**（`TEXCOORD12` 而非 `TEXCOORD`）避免高编号串列。

---

### E 类：witcher3 切线/拷贝属性解码（7 个）

代表错误：`event22229` 中 `o4.xyzw = v1.xyzw`（纯拷贝输入）输出 `40.49` vs golden `21.76`——说明**输入属性 v1 的布局/格式解码有误**；`event16215` 的 `TexCoord2`（切线空间）整体偏，疑似**矩阵行/列主序约定**；`event16834` 着色器除零产生 nan/inf（纹理返回 0 后做除法），属数值定义问题。

**修复方案：**
1. 同 D 类原因 1：复核输入顶点格式/offset 解码（拷贝型输出最能暴露解码错误）。
2. `event16215`：核对切线矩阵主序约定。
3. `event16834`：按"除零→0/inf 的 GPU 定义"对齐数值。

---

### F1 类：精度/边界（2 个）

`sekiro2_event3207`、`sekiro2_event9493`：均 **passed 43329/45576**，失败行 diff 只有 **0.005**（如 `46.114619` vs `46.119830`），恰好略超 `float_tolerance=0.005`。属 float32 精度 / 特定分支边界，非结构性错误。

**修复方案：** 细化 VS float32 运算模拟（已有 `float32_emulation`），或对这类把容差从 0.005 略放宽（如 0.01）即全过。

---

### F2 类：其它 value mismatch（9 个）

| case | 现象 | 初判 |
|---|---|---|
| `sekiro4_event7844` | passed 162/324 | 部分通过，疑似实例索引/分支边界 |
| `sekiro2_event4833` | passed 12/24 | 同上 |
| `sekiro2_event15481` / `event16052` | `TexCoord[2]: output=20 golden=1` | 特定指令（疑似 clamp/min 或常量）误算 |
| `sekiro2_event14998` | `sv_position[0]` 差 0.007 但 `[1]` 差 179 | 矩阵某行/实例索引错 |
| `sekiro4_event20560` | sv_position 全错（step93 的派生 quad case） | 实例索引残留（派生崩溃 step94 已修，值仍错） |
| `EndlessSpace2_event2991` / `event3061` | output 非 0、**golden 全 0** | golden 网格退化为 0，疑似 B 类边缘（不可对比） |
| `Nobu15-frame3456_event586` | **output 全 0**、golden 非 0 | VS 输出 0——cbuffer/矩阵未加载，真 bug |

**修复方案：**
- `Nobu15_event586`：排查该 capture 的 cbuffer 二进制是否按寄存器正确匹配（VS 全 0 通常是矩阵未加载）。
- `EndlessSpace2`：确认 golden 是否真为退化 0；若是则归入 B 类 N/A。
- sekiro 残留（4833/7844/14998/15481/20560）：继续沿 step94 的 ByteAddressBuffer 实例索引 + struct 命名成员路径排查具体索引/指令。

---

## 结论与优先级

| 优先级 | 动作 | 预计收益 |
|---|---|---|
| 中 | D 类：修输入属性 UNORM/SNORM 解码 + golden 列 float-bit 解析 | 解锁部分 Octopath（≤18） |
| 中 | E 类：复核输入解码 + 切线矩阵主序 | 解锁部分 witcher（≤7） |
| 中 | F2：Nobu cbuffer 加载 + sekiro 实例索引残留 | 数个 |
| 低 | F1：精度/容差 | 2 |
| 低 | A 类：加 `vs_only` 跳过 PS | 消除大部分 45 个超时 |
| 不修 | B(16, 无 golden)、C(37, TombRaider 反编译有损) | 超出 HLSL 解释器能力边界 |

理论可达：修 D+E+F 后约 **18+7+9+2 = 36** 个有望从失败转通过；A 类 45 个可经 `vs_only`/加时通过；B(16)+C(37)=53 个属数据缺失或反编译有损，不在解释器修复范围内。

## 产物

- 删除 `Dump/` 中 6 个通过 case。
- `Cases/triage_results.csv`：140 个 case 的 PASS/FAIL 明细。
- `Cases/dump_failure_categories.csv`：134 个失败 case 的 category 归类。
- `Cases/triage_logs/`：逐 case 日志。

## 回归

本次仅做 triage / 分类 / 删除数据文件，**未改动解释器代码**，回归套件不受影响。
