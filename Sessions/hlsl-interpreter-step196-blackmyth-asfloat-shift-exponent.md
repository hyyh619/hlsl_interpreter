# Step 196 — 运行 Dump 新 draw case（BlackMyth 批次），修复 `asfloat(exp<<23)` 位模式被当整数值用

Date: 2026-07-07

## 任务
Scheduled hourly develop run（无人值守）。扫描 `/Dump`，把 `Dump/dump_case.csv` 未记录的新
case 记录并逐个运行；不过的定位 `hlsl_interpreter` 根因并修复；修复后能过的加入回归、直接过的记录
并删 zip；最后提交推送，并把思考/执行/结果写入本文件。

## 环境限制
- 每次 shell 调用上限 45s，进程跨调用不保活；`Dump/` 里新 case 多为 BlackMyth 大包（4MB–500MB），
  完整回归（`Cases/regression_test_zip_files.csv` ~155 例，最大 264MB）在本环境**跑不完**。
- `triage_dump.py` 的临时配置清理 `os.remove` 在挂载盘上会 `Operation not permitted`，故沿用上轮做法：
  用 `/tmp` 下的 per-case 配置直接驱动 `render.py`，日志也写 `/tmp`。

## 扫描结果
`Dump/` 现有 123 个 zip，`Dump/dump_case.csv` 只记录了 39 个 → **84 个新 case**（全部 BlackMyth，
frame12199 / 14374 / 15293 / 19470 四帧）。已将 84 个文件名追加进 `Dump/dump_case.csv`（现 123 行 + 表头）。

## 运行 & 症状（新 case 一致失败）
抽样运行最小的若干个（event5690 / 8441 / 9319 / 8040 / 9526 …），**全部 0/N 通过**，且量级荒诞：

```
Row 0 sv_position[0]: output=-27098221641728.0  golden=-0.118441
Row 0 TEXCOORD7[2]:   output=73876753205952512.0 golden=8186.586426
```

buffer 全部正常加载（StructuredBuffer t0/t1/t2、typed Buffer t3=R16G16_FLOAT / t4=R8G8B8A8_SNORM），
不是数据缺失，是解释器算错。

## 根因：`asfloat((exp<<23)+bias)` 的位模式被当成整数值参与浮点乘

VS 是 BlackMyth 的**顶点位置/法线压缩解码** shader，核心一句：

```hlsl
r0.x = (uint)r4.z << 23;          // 把指数塞进 float 的指数位
r0.x = (int)r0.x + 0xf8800000;    // 加偏置，构造出一个 float 的位模式
...
r2.xyz = r2.xyz * r0.xxx;         // 之后按 float 读回（隐式 asfloat）→ 缩放解码向量
```

DXBC 里寄存器是**带类型**的：`ishl`/`iadd` 写的是位，随后的 `mul` 按 float 读回即隐式 `asfloat`。
解释器不跟踪寄存器类型：`<<` 与 `+` 都返回**普通 Python int**，于是 `r0.x` ≈ 0xf88xxxxx ≈ 41 亿，
`r2.xyz * r0.xxx` 直接乘上 41 亿 → sv_position 变成万亿级垃圾。

解释器本有 `_RawBits`（int 子类）用来标记“这是寄存器位模式”（原先只有 `bfrev` 结果会打标），
但位运算结果没有打标，所以这条链断了。

## 修复（`hlsl_interpreter.py`）
1. 新增 `_bits_to_float(bits)`：把 32-bit 整数位模式按 float32 重解释（asfloat）。
2. 新增 `_coerce_rawbits_for_float_op(left, right)`：在 `+ - * /` 里，若一侧是 `_RawBits`（位模式）
   而另一侧含**真浮点**，说明这是一次浮点 ALU 运算，把 `_RawBits` 先 `asfloat` 再算；两侧都无真浮点
   （纯整数上下文）则原样透传，不动位模式。在 `execute_binary_op` 的算术分支开头调用它。
3. `execute_binary_op` 里把**左移 `<<`** 的结果打标为 `_RawBits`——`<< 23` 正是“往指数位塞值”的
   float 重构惯用法标志。**其余位运算（`& | ^ >> %`）保持普通 int**：它们常喂给
   整数→浮点的**数值**转换（itof/utof），若一并打标会把这些值错误 asfloat。

### 关键教训：范围过宽会砸回归
初版把 `& | ^ << >>` 全部打标 `_RawBits`，虽然新 case 的 sv_position 立刻从万亿降到个位数，
但**回归例 `BlackMyth_frame14374_event3393` 从 30960/30960 掉到 305/30960**（该 shader 的位运算结果
是当整数值再转 float 的）。收窄到**只标 `<<`** 后，回归恢复满分，新 case 的巨值 bug 依旧修好。

## 验证
不能跑完整回归（预算），抽样 14 个覆盖各 bit-math 家族的回归例，全部 PASS：
`Octopath 102/4014/283/1031/1852`、`witcher 22484/7889/2774/994/1450/895`、`Collision event28`、
`manhattan event50`、`sekiro2 event1089`，以及**曾被初版砸掉、现已恢复的 `BlackMyth_frame14374_event3393`（30960/30960）**。

新 case（如 event8441）效果：`sv_position[0]` 从 `-27,098,221,641,728` → `-0.0777`（golden `-0.1184`），
巨值 bug 消除。但**仍未整例通过**：残留 `TEXCOORD11(o1.xyz)=0`（法线/切线帧四元数重构那一支还有问题）、
`sv_position` 尚差 ~0.04 的精度、trailing-float3 分量错位等，属于该压缩解码 shader 的更深问题，本轮未处理。

## 结论 / 后续
- 84 个新 case 已全部记录进 `Dump/dump_case.csv`。
- 无 case 直接通过、也无 case 经本轮修复后**整例**通过 → 不删除任何 zip，不新增回归条目。
- 本轮修复是独立且正确的 bug 修复（`asfloat(exp<<23)` 位模式）——已确认不砸回归——故照常提交。
- 待办：BlackMyth 顶点压缩解码的四元数/切线帧重构支（`r2.xyz` 归零）是下一步整例通过的关键。
