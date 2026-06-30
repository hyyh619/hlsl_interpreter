# Step 160 — 修 C 类（TombRaider）矩阵选择子：从反汇编恢复反编译丢失的成员

## 任务
> 修复 C 类：TombRaider「主序/选择子」反编译有损，超 HLSL 源边界。

前几步把 C 类（37 个 TombRaider）判为「反编译丢弃 `WorldParameters[]` 矩阵成员选择子，超出直接解释 HLSL 源
边界」。本步证明：**信息确实从 HLSL 源丢了，但能从权威反汇编 `VS_shader_disasm.txt` 恢复**，从而修好绝大多数。

## 两种反编译丢失模式

extract 一个 case 看清结构。`WorldBuffer` 是 `struct { row_major float4x4 WorldViewProject, World, ViewProject; } WorldParameters[12]`。
3Dmigoto 把 `WorldParameters[i].World._m10...` 反编译成 `WorldParameters[i]._m10...`——**丢了成员名**（哪个矩阵？
对 struct 直接 `._mRC` 本身非法）。disasm 不丢：`cb0[r0.x + N]` 精确给出元素内寄存器偏移。

发现实际有**两种**丢失：
1. **multi（多矩阵成员）**：`struct{float4x4 A,B,C;}Arr[N]` → `Arr[i]._mR..` 丢成员。disasm 的 `cb0[reg+N]`
   给出寄存器偏移 N，成员基址 = `N - R`（R = `_mR.` 行号）。
2. **flat（单个矩阵数组成员）**：`struct{float4x4 M[K];}Arr[N]`（蒙皮 `SkinningParameters[12].SkinMatrices[42]`）
   → `Arr[flat]._mR..` 把 `.M` 和内层下标一起折叠成一个**扁平矩阵下标**。K*N 个矩阵连续排布，故
   `Arr[flat] == Arr[flat/K].M[flat%K]`。

## 实现

### multi（源重写，disasm 驱动）— `recover_struct_array_matrix_selectors`
按程序顺序把 HLSL 的 member-less `Arr[idx]._mR..` 与 disasm 的 `cb<slot>[reg+N]` **位置对齐 1:1**（已在
event2848 多矩阵情形验证 17↔17），逐个算成员基址 `N-R`、查成员名，重写为 `Arr[idx].World._mR..`。之后既有
具名成员路径正确解析。带对齐数不符的 guard（数量不匹配则不改，避免误伤）。`render.py` 在 cbuffer 解析后、函数解析
前调用（读 `VS_shader_disasm.txt`）。

### flat（运行期，布局驱动）— get_value 派发处
源重写对 flat 不可行：`Arr[i].M[k]._mRC`（带内层下标）触发表达式解析器在 `.M` 边界断开。改为**运行期**处理：
struct-array 派发时若 struct 仅一个矩阵数组成员且 rest 是裸 `._m`，则 `elem=flat//K, inner=flat%K`，直接以字面
下标调 `_struct_member_access(field, elem, f'.{M}[{inner}]._mR..')`（字面下标避开解析器限制）。

## 结果

**TombRaider：0 → 29/37 PASS。**
- multi（WorldParameters 成员选择子）修复一批；flat（SkinningParameters 扁平蒙皮）再修一批
  （event2129 0→4407/4407）。
- **回归 128/128 PASS（零回归）**；新增 2 个代表用例入回归并拷入 `Cases/`：`event1018`（multi 选择子）、
  `event2129`（flat 蒙皮），守护两条恢复路径。
- 从 Dump 删除 29 个转通过的 TombRaider zip（Dump 85→56；TombRaider 37→8）。

### 仍失败的 8 个（**不同问题，非选择子**）
- **TEXCOORD6 = `WorldToPSSM0`（7 个：2201/2848/2867/2880/2892/2899/7308）**：这是 SceneBuffer 里一个**普通
  float4x4** 的「矩阵主序」问题（disasm `r0.x*reg0+..` 用寄存器行，与我方 `_m00..`=field.data 的取法不一致；
  golden `[0,0,-1,1]` 来自 reg56=`[0,0,-1,1]`）。与 step-158 的 Nobu586 `mul(world,M)` vs `mul(M,world)` 同类
  **结构性主序墙**，且与正常工作的 ScreenMatrix 走同一加载路径——**贸然改普通矩阵转置会危及 128 回归与刚修好的
  29 个**，故本步不动。
- **event1802（1 个）**：4 顶点 TriangleStrip 的 World=identity 边缘案，golden 为 SV-Position-first 的 uint 列
  位重解释布局，sv_position 数值边缘；非选择子问题。

## 本步代码改动
- `hlsl_interpreter.py`：新增 `recover_struct_array_matrix_selectors`（multi，disasm 驱动源重写）+ get_value
  派发处 flat 单矩阵数组运行期分解。
- `render.py`：cbuffer 解析后、函数解析前调用恢复（读 disasm）。
- 回归 +2 TombRaider 代表用例（128/128）。

## 结论
C 类「矩阵选择子」是**反编译丢失但可从 disasm 恢复**的真实可修问题，已修 **29/37**，零回归。剩 8 个属不同的
普通矩阵主序墙（WorldToPSSM0）+ 一个 identity 边缘，非选择子，留待主序专项（需 `dxbc_diff` 寄存器级 golden /
统一矩阵主序策略，风险高）。

## 后续
- 普通矩阵主序（WorldToPSSM0 / Nobu586 / 部分 E 类）：需统一「cbuffer 矩阵寄存器=行，按 disasm `cb[reg]`
  语义乘」的策略，但要保证 ScreenMatrix 等现通过用例不回归——是独立的高风险专项。
- event1802：SV-Position-first + uint 列的 4 顶点边缘，单独核查。
