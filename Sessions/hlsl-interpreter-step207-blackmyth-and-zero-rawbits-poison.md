# Step 207 — BlackMyth 逐实例 + 多级 buffer 解码坍缩为零：AND 零结果被误标 `_RawBits` 毒化下游浮点加法

Date: 2026-07-08

## Prompts（本步任务）

> The single biggest remaining bug is Category 1 — ~14+ BlackMyth cases share one
> new root cause (per-instance + multi-level buffer indirection collapsing to
> zero). Fixing that would likely clear the largest chunk, same as steps
> 201/202/205 did for the earlier families.

即 step206 triage 里的 **类 1**（14 例）：`event4767 / 4812 / 4892 / 5219 / 5361 /
5945 / 6018 / 6836 / 7719 / 9999 / 10575 / 11070`，加部分通过的 `8750 / 9055`。
症状：`sv_position` / `TEXCOORD10/11` 输出**全 0**（或部分顶点为 0）。

## 定位过程（最小标本 event4812，144 顶点）

用 `printSyntaxTree + print_interpreter_result` 打开 `[STMT]` 逐语句轨迹，跑前 2 个顶点。

### 排除的假设

- **per-instance 输入**：`ATTRIBUTE13`（`v1`，`PerInstance=True`，slot 4 独立 VB，
  不在 `ia_vertex_data.csv` 里）已由 `load_per_instance_data` 正确装载：
  `v1=1842, v2(SV_InstanceID)=0`。**非此处 bug**。
- **多级 buffer 间接寻址**：`t3[1842] → r0.x=25360 → t1[25360] → r0.w=4489 → 进真分支`，
  再 `mad(r0.w*40+offset) → t0[179586]=20.4885…` 全部读到**真实数据**，间接链**解析正确**。
  triage 里"t3[idx]=0 / t1[idx]=0 走无数据零分支"的猜测**不成立**——它们读回的是
  denormal（如 `t3[1842]` float 显示 0.0000，但位模式 `(int)& mask = 25360`，非零）。

### 真正的根因（AND 零结果的 `_RawBits` 误标 → 毒化下游浮点加法）

逐语句对比 HEAD（未修）与工作区（已修）版本，分歧点精确定位到这条链：

```
r0.z = (int)r0.z & 0x3f800000;   // 字段提取：取出"1.0 的位" (0x3f800000) 或 0
...
r0.z = r2.w * r0.z;              // 浮点乘
r0.z = cb0[7].w + r0.z;          // 浮点加   （cb0[7].w = 1.0）
...
r4.z = r2.w * r0.z;              // r4.z 是淡入/饱和乘子
...
r6.xyzw = r6.xyzw * r4.zzzz;     // ★ 位置乘以 r4.z
o11.xyzw = ... (源自 r6) ...      // SV_POSITION
```

本例中 `(int)r0.z & 0x3f800000 => 0`（该位未置位）。关键在 `_and_lane`（`hlsl_interpreter.py`
`_eval_bitwise_operand` 的按位与实现）**旧逻辑**：

```python
if ri in (0, -1, 0xFFFFFFFF) or li in (0, -1, 0xFFFFFFFF):
    return _RawBits(self._wrap_i32(res))
```

左操作数 `(int)r0.z` 的位恰为 0（`li == 0`）→ 命中 → 把结果 **0 标记成 `_RawBits(0)`**。
`_RawBits` 表示"这是寄存器原始位模式"，会让**下游整数运算吃原始位、下游浮点运算 asfloat 重解释**。

于是 HEAD 轨迹：
```
r0.z = (int)r0.z & 0x3f800000 => r0.z = 0          （被标 _RawBits(0)）
r0.z = r2.w * r0.z           => r0.z = 0
r0.z = cb0[7].w + r0.z       => r0.z = 1065353216   ★ 错！
```
`cb0[7].w + _RawBits(0)`：因一侧是 `_RawBits`，`+` 走**整数 raw-bits 路径**（`evaluate_syntax_tree`
的 `+/-/*` 分支），把真正的浮点 `cb0[7].w = 1.0` 当成它的**整数位模式 `0x3f800000 = 1065353216`**，
再 `1065353216 + 0 = 1065353216`。本应是浮点 `1.0`。
结果 `r4.z = r2.w * 1065353216 => 0` → `r6 * r4.zzzz = 0` → `o11 = [0,0,0,0]`。**全零坍缩**。

修复版轨迹（正确）：
```
r0.z = (int)r0.z & 0x3f800000 => 0     （不标记）
r0.z = r2.w * r0.z           => 0.0
r0.z = cb0[7].w + r0.z       => 1.0    ✓ 纯浮点加
r4.z                         => 1.0    ✓
o11.xyzw                     => [7747.69, 7871.46, 10.0, 12710.19]  ✓
```

## 修复（`hlsl_interpreter.py` `_and_lane`）

```python
# Only a genuine bit-SELECT (mask = all-ones -1/0xFFFFFFFF that PRESERVES the
# other operand's float bits) needs the _RawBits tag. Require res != 0: a zero
# result is 0.0 as float and 0 as int alike — nothing to preserve — and (since
# x & 0 == 0) a non-zero result rules out an all-zeros mask, so this keys on the
# all-ones preserve case only. Tagging a zero result was a false positive for
# FIELD extraction like `(int)x & 0x3f800000` when x happens to be 0: the bogus
# _RawBits(0) then forced a downstream float add (`cb0[7].w + r0.z`) onto the
# integer path, collapsing a BlackMyth particle position to zero.
if res != 0 and (ri in (-1, 0xFFFFFFFF) or li in (-1, 0xFFFFFFFF)):
    return _RawBits(self._wrap_i32(res))
```

两处收紧：
1. 从判定集合里**移除 `0`**：`x & 0 == 0` 恒为零，标 raw-bits 无意义。
2. 增加 `res != 0` 守卫：**只有非零的 AND 结果**才可能承载需保留的浮点位（如 `& 0xFFFFFFFF`
   的恒等保留，或 `& 0x3f800000` 命中时的 `0x3f800000` → asfloat 1.0）；零结果无位可保留。

这是**通用正确性修复**，非 BlackMyth 专用补丁：把一个"零值不该被当作位模式"的类型推断误判去掉。

## 结果

### 类 1 家族（14 例，`float_tol=0.005`，全流水线 vs-golden）

triage 把 14 例归为"同一根因"，实测**分两支着色器**：

**A · event4812 变体**（`o0..o11`、`StructuredBuffer t4/t5`、含 `& 0x3f800000` 距离淡入块）
——本步 AND-zero 修复的直接受益者，**全部救活**：

| case | 结果 |
|---|---|
| event4767 | 627/627 ✅ |
| event4812 | 144/144 ✅ |
| event4892 | 1092/1092 ✅ |
| event5219 | 1950/1950 ✅ |
| event5361 | 744/744 ✅ |
| event5945 | 387/387 ✅ |
| event6018 | 522/522 ✅ |
| event6836 | 195/195 ✅ |
| event7719 | 876/876 ✅ |

（HEAD 版本对 event4812 为 **0/144**，修复后 **144/144**，单点确证该改动即为根因修复。）

**B · 仍未过的 5 例（不同/额外根因，本步不改，如实记录）**：

- `event8750`（1293/30960）、`event9055`（4158/58212）：同属 A 支着色器（有淡入块），但仅
  少数行通过；AND-zero 修复前后**均为此数**（`1293/1293`、`4158/4158` 不变）→ 其余行是**另一
  条数据相关路径**的错，非本 bug。
- `event9999 / event10575 / event11070`（各 12162 顶点，`0/N`）：**另一支着色器变体**
  （输出仅 `o0..o7`、无 `INSTANCE_LOCAL_TO_WORLD`；`t4/t5` 是 typed `Buffer<float4>` 而非
  StructuredBuffer；无距离淡入块）。逐语句轨迹：**`SV_POSITION`(o7) 正确**，但 `TEXCOORD10/11`
  (o0/o1，法线/切线) 坍缩为 0。定位到 `r4.xyz = t5.Load(i).xyz` 每次读回**单位四元数
  `[0,0,0,1]`**——`t5`（声明 `Buffer<float4>`）被按 `R16_UINT / 2B/elem / 131072` 装载，
  而其真实 SRV 应为逐顶点切线帧四元数（`buffer_4927.bin` 262144B）。这是**typed-buffer 视图
  格式推断**的独立 bug，改动面广、验证慢（12162 顶点/例 >150s），留作后续，不在本步冒险改。

### 全量回归

`python run_regression.py`（158 例，含本步新加的 `event4812`）：**155/158 PASS**。
3 个未过均为**与本改动无关的既有失败**（step205 同款基线）：
`witcher3_countryside_event16834`(3/30)、`OldWorld_event1034`(0/203328)、
`OldWorld_event2767`(0/203328)。
既有 BlackMyth 全绿：`event3393 30960/30960`、`event9319 1050/1050`、`event7117 6/6`、
`event8040 6/6`。→ **AND-zero 修复零回归。**

### 锁定

`event4812`（144 顶点、最快）已 `cp` 进 `Cases/` 并加入 `Cases/regression_test_zip_files.csv`
（该 CSV 与 `Cases/*.zip` 均 gitignore，为本地回归网）。

## 结论

step206 triage 判定的"当前最大一类"（类 1）里，**event4812 支（9 例）** 的根因是**通用的按位与
类型推断误判**：`_and_lane` 把值为 0 的 AND 结果误标 `_RawBits`，使下游一条浮点加法被拽上整数
raw-bits 路径，把 `cb0[7].w=1.0` 重解释成 `0x3f800000`，级联使淡入乘子 `r4.z→0`，最终
SV_POSITION 与全部解码输出坍缩为零。收紧标记条件（去掉 `0`、加 `res!=0` 守卫）后 9 例全部救活，
且全量回归零回归。

诚实记录：triage 把 14 例当作"同一根因"，实测**只有 9 例**是此 AND-zero bug；`8750/9055` 部分
通过是另一条数据相关路径的问题，`9999/10575/11070` 是**另一支着色器**（typed `Buffer<float4>`
的切线帧 `t5` 被按错误视图格式装载，法线/切线读成单位四元数）。后两支根因不同、改动面/验证成本更高，
本步不冒险改，已定位并留作 follow-up。本步净收益：**类 1 从 0/14 → 9/14 全过，零回归**。
