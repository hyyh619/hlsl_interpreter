# Step 174 — 类⑦（精度临界，"需 bit-exact GPU 数学"）：多数并非精度问题

## 任务

修复类⑦（步 168 归类"精度临界（float32 仿真残差）需 bit-exact GPU 数学"）。
步 170 的 GPU dpN 逐步舍入已修掉 Octopath 664/3012；本步处理剩余：
sekiro2_3207/9493（43337/45576）、sekiro2_4833（12/24）、sekiro4_7844（162/324）、
sekiro2_14998（28974/29148，GS 案）。

## sekiro2_4833：根本不是精度问题——具名 cbuffer 嵌套 struct 的三层缺陷

trace 显示 `min((uint)g_forceParam.LoopNum.x, 8) = 0`——**整个风力 while 循环从未执行**
（且步 173 之前 while 本身就未实现），"精度残差"实为整个风力位移项缺失。
二进制里 LoopNum.x = 2（denormal 存储，浮点视图 0.0）。三层修复（hlsl_interpreter.py）：

1. **`override_cbuffers_from_binary` 对非数组 struct 实例不填数据**：
   `for j in range(array_size)` 在 array_size=0（`struct {...} g_forceParam`，
   非数组实例）时零迭代 → `field.data` 恒空。改 `range(max(1, array_size))`。
2. **get_value 的点号路径不识别非数组 struct 成员访问**：
   `g_forceParam.LoopNum.x` 落入通用浮点路径（denormal→0）。新增路由：
   base 名匹配 `__struct__` 非数组字段时走 `_struct_member_access(field, 0, rest)`
   ——uint/int 成员经 `struct_int_data` 读精确位。
3. **`_struct_member_access` 的成员数组下标用空局部环境求值**：
   `GustParam[r0.y]` 的 r0.y（循环变量）解析失败回退 0。签名加 `local_vars` 透传。

结果：**12/24 → 24/24 PASS**。

## sekiro4_7844：324/324 直接 PASS

同引擎（FromSoftware）风力 struct——纯搭 4833 修复（+步 173 的 while 循环）的车。
步 170 时"骨骼数据侧、DXBC VM 无法定位"的判断不成立：真因同上。

## sekiro2_3207 / 9493：又是"假精度"——(uint) 强转吞掉 denormal 位模式

同样的风力系统，但每顶点风力骨骼数来自 `Buffer<mixed4> t4`（tbForce）的
`.x`（uint 道）：t4[0].x = **uint 2**，浮点视图是 denormal 0.0 →
`min(8, (uint)0.0)` = 0 → 风力循环从未执行。用 golden o0 反解（4×4 求逆）
证实 golden 世界坐标含 ~0.004 的风力位移。

**修复（通用）**：int/uint 强转（`_INT_CAST_TYPES` 分支）遇 **denormal 范围**
（0 < |f| < 1.18e-38）浮点时按位重解释——GPU 是 FTZ 的，真实运算永远不产生
denormal；在 int 强转处出现的 denormal 只可能是原始整数位穿透浮点存储。
（与步 173 的 `_load_coord_to_int` 同一启发式，扩展到所有 int 强转。）

结果：43337 → **44341/45576**（+1004 行）。

**残余 1235 行 = 真正的精度墙**：全部 sv_position.x、diff 0.00500–0.0107
恰在 0.005 容差线上（相对 ~1e-4），失败顶点的风力相位 `sin(≈3889 rad)` 是
时间×频率大数。实验了 GPU 快速 sin 归约模型
（`sin(2π·frac(f32(x·f32(1/2π))))`）：局部有效（631 行检查 7 错→1 错）但
**全量净负效**（44341→44338，修一批坏一批）——本案捕获 GPU 是
Parallels/Apple 虚拟 GPU，其归约精度高于 f32 模型，math.sin 整体更近。
**已回滚**，维持 math.sin/cos；不放宽容差。

## sekiro2_14998

VS 段 **4867/4867 全过**；GS 段维持 28974/29148（TexCoord4 ~0.13，相对 5e-4）。
其 GS 的 `sincos` 角度非大相位，残余另有来源（billboard 角偏移链），非本步范围。

## 全量验证

- **Cases 回归 `run_regression.py` 142/142 全 PASS 零回归**（denormal int 强转、
  非数组 struct 三层修复均为共享路径改动）。
- **Dump triage 31 案：2 转 PASS（4833、7844），其余逐案与基线持平**。
  3207/9493 因风力循环开销在 6 并发下超 300s（独跑完成，44341/45576）——
  移入"慢案"观察，非正确性退步。
- **2 案晋级** Cases + 回归表（**144 案**）并从 Dump 移除（剩 **29 案**）。

## 结论：类⑦"需 bit-exact GPU 数学"的判断大部分不成立

6 案中 4 案（4833、7844、664、3012——后两个是步 170）实为**装载/解析 bug**
（denormal 位模式在三个不同入口被浮点路径吞掉：struct 成员、Load 坐标、int 强转），
修复后精确全过。真正的精度墙只剩 3207/9493 的 1235 行（sin(≈3889 rad) 大相位，
Parallels GPU）与 14998 的 GS 段（非 trig 来源）。

**方法论**：denormal 一族三入口（步 173 Load 坐标、本步 struct 成员 + int 强转）
的共同信号是"整数存进 cbuffer/buffer 后浮点视图 ≈0"——凡在 trace 里看到
`(uint)x = 0` 且行为像"循环没跑/索引恒 0"，先查原始位。GPU FTZ 保证运算不产生
denormal，故 int 消费点的 denormal 必为原始位模式，重解释是安全的。
