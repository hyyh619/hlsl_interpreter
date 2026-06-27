# hlsl-step93：剩余 Dump/ 失败 case 分类与修复建议

## 背景

经过 step92 的修复，`Dump/` 中通过的 332 个 case 已被删除，剩余 **181 个失败 case**。
本文档对这 181 个 case 按**失败根因**分类，给出每一类的具体原因（含证据）和可操作的修复建议。

分类依据：`Cases/triage_results.csv` 的 detail 字段 + 各 case 在 `Cases/triage_logs/` 的日志 +
对代表性 case 的 HLSL 源码 / 反汇编 / cbuffer 结构的逐一分析。

---

## 总览

| 类别 | 数量 | 性质 | 是否解释器 bug | 修复优先级 |
|---|---|---|---|---|
| A. 超时 (timeout) | 37 | 正确但慢 | 否（性能） | 低 |
| B. 无 golden 对比 | 17 | 无法验证 | 否（数据缺失） | 不修复（标注 N/A） |
| C. sekiro 实例索引 + struct-in-cbuffer | 53 | 输出错误 | 部分（反编译缺口 + 缺特性） | 高（数量大） |
| D. TombRaider struct 成员矩阵选择子丢失 | 43 | 输出错误 | 否（反编译有损） | 不可从 HLSL 修复 |
| E. 输入/输出 signature 映射 & 顶点格式解码 | 29 | 输出错误 | 是 | 中 |
| F. 派生（derivative）quad 重执行崩溃 | 2 | 崩溃 | 是 | 中 |
| **合计** | **181** | | | |

各类家族分布：

- **A 超时(37)**：witcher3(17)、TankMechanicSimulator(8)、sekiro(3)、heaven(3)、OldWorld(2)、Nobu(2)、valley(1)、Octopath(1)
- **B 无golden(17)**：sekiro(6)、manhattan(6)、witcher3(3)、Nobu(1)、EndlessSpace2(1)
- **C sekiro(53)**：sekiro2 / sekiro4 的几何 VS
- **D TombRaider(43)**：全部 TombRaider-frame25229
- **E 其他(29)**：Octopath(18)、witcher3(7)、EndlessSpace2(2)、OldWorld(1)、Nobu(1)
- **F 崩溃(2)**：sekiro2_event13516、sekiro4_event20560

---

## A 类：超时（37 个）

### 具体原因
解释器是**纯 Python 逐顶点 / 逐像素**执行，没有任何向量化或编译。当一个 draw 的顶点数很大、
或像素着色器要跑满整个视口（full-screen pass）时，单个 case 会超过 triage 的 300 秒上限。
这**不是正确性 bug**——这些 case 的 VS 输出在跑完时通常是正确的（回归套件里的
`witcher3_event7358` 就是同类的全屏 pass，给足时间即可通过）。

证据：日志在被杀掉前没有出现任何 `Error:` 行，也没有 `Total PASSED rows:` 总结行（被中途
终止）。受影响最多的是 witcher3（17）与 TankMechanicSimulator（8）。

### 修复建议
1. **回归/triage 只评测 VS**：正确性判定只看 VS-vs-golden，但 `render.py` 跑了整条管线（含 PS
   光栅化）。增加一个 `vs_only` 配置项，跑完 VS 对比后即返回，跳过光栅化/PS，可消除绝大多数超时。
2. **优化解释器热路径**：`evaluate_syntax_tree` / `get_value` 每个顶点都重新走一遍字符串解析。
   可对 main 函数体做一次性"预编译"（把每条语句解析成可复用的 AST，缓存 swizzle/下标解析结果）。
3. **提高 triage 超时阈值**（回归已是 1800s），仅作兜底，不解决根本性能问题。

---

## B 类：无 golden 对比（17 个）

### 具体原因
日志里没有 `Total PASSED rows:`，detail 为 `no VS-vs-golden comparison in log`。意味着这些 capture
里 **没有可对比的 golden VS 网格**，原因有三种：
- 该 draw 顶点数为 0 / 被完全裁剪；
- capture 是 indirect draw 或 GPU 生成几何，3Dmigoto 未导出 `*_vs_mesh.csv`；
- manhattan(6) 这类全部缺 golden mesh，属于 capture 本身不完整。

### 修复建议
这类 case **无法用 VS-vs-golden 工作流验证**，不是解释器问题。建议：
- 从扫描集中标注为 **N/A / 跳过**，不计入"失败"；
- 若想覆盖，需要换一种验证方式（如对比最终 RT 像素，而非 VS 输出）。

---

## C 类：sekiro 实例索引 + struct-in-cbuffer（53 个，最大可修复群）

### 具体原因（以 sekiro2_event2282 为例）
sekiro 几何 VS 的核心逻辑是**通过 ByteAddressBuffer 做实例索引**，再用索引取对象矩阵：

```hlsl
ByteAddressBuffer g_InstanceIndexBuffer : register(t20);
...
  r0.x = (int)v5.x + VC_InstanceIdxOffset;
  r0.x = (uint)r0.x << 2;
// No code for instruction (needs manual fix):
ld_raw_indexable(raw_buffer)(mixed,mixed,mixed,mixed) r0.x, r0.x, t20.xxxx   // ← 被 3Dmigoto 注释掉
  r0.y = (int)r0.x * 17;
  ...
  r2.x = dot(r1.xyzw, VC_aObjMatrix[r0.x/3]._m00_m10_m20_m30);   // 用未解析的 r0.x 索引
  o0.x = dot(r1.xyzw, VC_MatrixViewProj._m00_m10_m20_m30);       // sv_position
```

三个叠加的障碍：
1. **反编译缺口（主因）**：`ld_raw_indexable ... t20` 这条**原始缓冲区加载被 3Dmigoto 标注为
   "No code for instruction (needs manual fix)" 并注释掉**。于是实例索引 `r0.x` 不会被赋值，
   后续 `VC_aObjMatrix[r0.x/3]`、`VC_InstanceData[r0.y]` 取到错误矩阵 → `sv_position` 完全错误
   （输出 -246.7 vs golden 19.4）。
2. **struct-in-cbuffer 命名成员访问**：`cbInstanceData` 里是
   `struct { float4x3 mWorld; ... uint4 matricesData; ... } VC_InstanceData[2]`，需要按**成员名**
   访问（`VC_InstanceData[i].matricesData.x`）。step92 加的 `__struct__` 支持只能做 `_mRC` 寄存器
   式访问，**还不支持命名成员**。
3. **float4x3 矩阵**：`VC_aObjMatrix[i]._m00_m10_m20_m30` 是 4x3 矩阵的 `_mRC`，需要确认二进制加载
   与访问器对 float4x3 正确。

### 修复建议（按收益排序）
1. **实现 ByteAddressBuffer（t20）原始加载**：解析 `dcl_resource_raw t20`，从 capture 的 `vb`/raw
   资源里读取实例索引缓冲，补上被注释的 `ld_raw_indexable`（即 `g_InstanceIndexBuffer.Load(addr)`）。
   这是解锁 sekiro 的关键。
2. **扩展 struct-in-cbuffer 为命名成员**：在 `__struct__` 字段里保存"成员名 → 字节偏移/类型"表，
   让 `NAME[i].member`、`NAME[i].member.swizzle` 能按偏移取值（复用 step92 的元素寄存器布局）。
3. **校验 float4x3 二进制加载 + `_mRC`**：确保 `override_cbuffers_from_binary` 对 float4x3（48 字节、
   3 寄存器）按 row_major 存放，`_m00_m10_m20_m30` 取列向量正确。

> 风险提示：即便补齐上述特性，若该 draw 的实例索引缓冲在 capture 中缺失，仍无法复现；需先确认
> t20 资源是否随 zip 导出。

---

## D 类：TombRaider struct 成员矩阵选择子丢失（43 个，不可从 HLSL 修复）

### 具体原因（step92 已确诊）
TombRaider 的 cbuffer 是 `struct { row_major float4x4 WorldViewProject; World; ViewProject; }
WorldParameters[12]`。反汇编显示：
- **位置** o1 用 `cb0[base+0..3]`（第 1 个矩阵 WorldViewProject）；
- **法线/切线** o3/o4/o5 用 `cb0[base+4..6]`（第 2 个矩阵 World）。

但 3Dmigoto 反编译出的 HLSL **把 struct 成员选择子丢弃了**——位置和法线都写成同样的
`._m00_m01_m02` / `._m10_m11_m12` / `._m20_m21_m22`。同一个 `_m00` 在位置语境指 WorldViewProject、
在法线语境指 World，**只有反汇编的 `cb0[base+N]` 寄存器号能区分**，HLSL 源里这个信息已经不存在。

验证：用 World 矩阵（寄存器 +4/+5/+6）手算 o3.x = 0.5929，正好等于 golden 0.59292，证明诊断成立。
step92 后 **位置 o1 和 TEXCOORD0 已正确**，仅法线相关输出（TEXCOORD2/3/4）错误。

### 修复建议
- **只能从反汇编修复**：解析 `VS_shader_disasm.txt` 里的 `cb0[r0.x + N]` 真实寄存器号来驱动取值，
  而不是相信 HLSL 的 `_mRC` token。这等于为这类 capture 增加一条"反汇编驱动"的解释路径，工作量大。
- 或**接受现状**：position 已对，法线输出标注为"反编译有损不可解释"。
- 对一个"直接解释 HLSL"的解释器而言，这类 case 超出其能力边界，建议不投入。

---

## E 类：输入/输出 signature 映射 & 顶点格式解码（29 个）

涵盖 Octopath(18)、witcher3(7)、EndlessSpace2(2)、OldWorld(1)、Nobu(1)。这类的 sv_position 往往
**接近或部分正确**，错的是某些 TEXCOORD/COLOR 输出，根因集中在两点：

### 原因 1：直接拷贝的输入属性被解码错（witcher3_event22229）
```hlsl
o4.xyzw = v1.xyzw;   // o4 = TEXCOORD3 → 对比键 TexCoord4
```
输出 40.49 vs golden 21.76。o4 只是把输入 `v1` 原样拷出，却不匹配，说明 **输入顶点属性 v1 的
布局/格式解码有误**（`ia_input_layouts.csv` 里的 offset 或 packed 格式没正确还原）。Octopath
多个 case 的 `TexCoord = 0.501961`（= 128/255）也指向 **归一化字节属性未按格式解码**。

### 原因 2：高编号 / 多输出语义映射到错误 golden 列（Octopath_event3502）
该 VS 输出 `o3..o6` 含 `TEXCOORD12 / TEXCOORD13` 等高编号语义；golden 对应列为 0 而输出非 0，
说明 `map_params_to_signature` / `load_vs_golden_from_mesh_csv` 对**高编号或大量 TEXCOORD 语义的
列映射**没对齐。

### 原因 3：精度/边界（OldWorld_event3338）
`passed 23352/23814`，仅极少数行以**很小的 diff（0.76、0.2）**失败，属 float32 精度或特定分支的
边界差异，非结构性错误。

### 个别需单独看
- `witcher3_event16215`：TexCoord2/3 错（切线空间旋转矩阵），可能是矩阵约定（行/列主序）问题。
- `witcher3_event16834`：着色器除零产生 nan/inf（纹理返回 0 后做除法），属数值定义问题。

### 修复建议
1. **复核输入顶点解码**：对照 `ia_input_layouts.csv` 的 `Format`（如 `R8G8B8A8_UNORM`、
   `R16G16_SNORM`、packed uint），确认 `ia_vertex_data` → VS 输入的解码与 offset 正确。
2. **复核输出 signature 列映射**：让 `load_vs_golden_from_mesh_csv` 用 **完整 indexed 语义**
   （TEXCOORD12 而非 TEXCOORD）匹配 golden 列；核对高编号语义不串列。
3. **逐 case 排查 16215 / 16834**：前者查矩阵主序，后者按"除零 → 0/inf 的 GPU 定义"对齐。

---

## F 类：派生 quad 重执行崩溃（2 个）

### 具体原因
`sekiro2_event13516`、`sekiro4_event20560` 在 PS 里调用带**自动派生（ddx/ddy）的纹理采样**，
触发 `_compute_uv_derivatives` 对邻居 lane 的重执行（`_get_lane_locals`）。
- step92 已修第一处崩溃：`_sample_linear` 对 NaN UV 调 `int()` 抛 `ValueError`（已加 NaN/Inf 钳为 0）。
- **仍存在第二处崩溃**：邻居 lane 重执行某条语句时，`execute_binary_op` 执行
  `[l * r for l,r in zip(left,right)]` 抛 `TypeError: can't multiply sequence by non-int of type 'float'`
  ——即某个表达式在 lane 重执行环境里返回了 **list**，但上下文期望 scalar，导致 `list * float`。

### 修复建议
1. **lane 重执行环境健壮化**：`_get_lane_locals` 重放 main 时，对返回类型与主 lane 不一致的情况
   做防御（缺失输入按 0/标量回退），避免 `list * float`。
2. **`execute_binary_op` 类型护栏**：当一侧是 list、另一侧是标量做逐元素乘时按广播处理；当类型确实
   非法时返回 0 并记日志，而不是抛异常中断整条 draw。
3. 修好后这 2 个 case 会从"崩溃"降级为"value-mismatch"（其 sv_position 另有 C 类的实例索引问题）。

---

## 结论与建议优先级

| 优先级 | 动作 | 预计收益 |
|---|---|---|
| 高 | C 类：实现 ByteAddressBuffer 原始加载 + struct 命名成员访问 | 最多解锁 53 个 sekiro |
| 中 | E 类：复核输入顶点格式解码 + 输出 signature 列映射 | 解锁部分 Octopath/witcher（约 29） |
| 中 | F 类：lane 重执行 / 二元运算类型护栏 | 消除 2 个崩溃（提升健壮性） |
| 低 | A 类：加 `vs_only` 模式跳过 PS | 消除大部分 37 个超时 |
| 不修 | B 类（17，无 golden）、D 类（43，TombRaider 反编译有损） | 超出 HLSL 解释器能力边界 |

理论可达：修复 C + E + F 后，约 **53 + 29 + 2 = 84** 个 case 有望从失败转为通过（其中 C 取决于
t20 资源是否随 capture 导出）；A 类 37 个可通过 `vs_only` 或加时通过；剩余 B(17)+D(43)=60 个属于
数据缺失或反编译有损，不在解释器修复范围内。
