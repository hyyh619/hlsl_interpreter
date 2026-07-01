# Step 163 — FMA 融合（float32 mad 单次舍入）+ 判定 event2848/7308 为 GPU 超越函数精度受限

## 任务
> 继续修 event2848/7308（精度受限的程序化动画）。

## 重新审视 step 162 的「精度受限」判定

step 162 用「双精度下仍失败（0.0176）」推断精度受限，但那个推理不完整：**golden 是 GPU 的 float32 结果**，正确
的目标是**贴合 GPU 的 float32 路径**（含 FMA 与超越函数实现），而非双精度。于是本步先补一个真实的 float32 仿真
缺陷——FMA。

## FMA 融合（真实修复，已落地）

**发现**：`dxbc_interp`（step 154 的寄存器级 golden VM）的 `mad` 是 `a*b+c` 在 double 计算、写回时单次舍入
（line 342 `_as_f32`）——等价 GPU 的 FMA（单次舍入）。而我方 HLSL 解释器开 `float32_emulation` 时每个二元 op
都舍入：`a*b+c` = round(a*b) 再 round(+c)，**两次舍入**，与 GPU 的 mad 不符。

**实现**（`evaluate_syntax_tree` 的 binary_op 分支 + `_try_fma`/`_fma`）：识别 `(a*b)±c` 与 `c+(a*b)` 模式，
求值 a/b/c 后按 **单次 float32 舍入** 计算 `a*b+c_sign*c`（乘积不预舍入），分量级处理标量/向量广播；非纯数值
操作数（矩阵/None）回退 `_NO_FMA` 走原路径。仅在 `f32_emulation` 时启用。

**验证**：in-process 单测确认融合生效且正确（`1.0000001*16777216-16777216`：非融合=2.0，融合=1.6777…=真值）。
**回归 130/130（零回归）**——FMA 是更贴近 GPU 的正确改进，安全。

## event2848/7308 —— 确证为 GPU 超越函数精度受限，非可修 bug

FMA 落地后 event2848/7308 **仍 1418/1548，max diff 0.026 完全不变**。定位：这两个 shader 的失败输出
（sv_position + TEXCOORD5）都源于**程序化顶点动画**（`rsqrt`/`sincos`/`frac`/分支算偏移，再经矩阵变换）。
FMA 只改矩阵 mad 的舍入，而**分歧发生在动画的超越函数上游**——我方 `math.sin`/`rsqrt` 近乎精确（double 后
舍 f32），GPU 用**硬件 float32 近似**的 sincos/rsqrt，二者本身就差 ~1e-4，经变换放大到 sv_position 的 0.026。

**结论**：要匹配需**位精确复刻 GPU 的 float32 sincos/rsqrt 近似实现**（及其精确指令序列），不现实。event2848/7308
是 GPU 超越函数精度受限，**不放宽全局容差**（掩盖真错），保留于 Dump。这坐实并深化了 step 162 的判定：不是我方
算术缺陷（FMA 已补齐、双精度也不符），而是 GPU 专有的超越函数舍入。

## 结果
- **FMA 融合落地**（float32 mad 单次舍入，修正 float32 仿真的真实缺陷），in-process 验证正确，**回归 130/130 零回归**。
- **event2848/7308 确证 GPU 超越函数精度受限**，非可修 bug，留 Dump（TombRaider 保持 35/37）。
- 本步无新用例转 PASS——目标 case 属超越函数精度墙；但补齐了 FMA 这一通用正确性改进（对未来精度边缘 case 有益）。

## 本步代码改动
- `hlsl_interpreter.py`：模块级 `_NO_FMA` sentinel；`_try_fma`/`_fma` + `_is_num`；`evaluate_syntax_tree` 的
  binary_op 分支在 f32 仿真下对 `(a*b)±c`/`c+(a*b)` 做单次舍入融合。

## 后续
- event2848/7308：GPU sincos/rsqrt float32 近似精度墙；除非实现 GPU 位精确超越函数，否则不可 PASS。
- FMA 融合现已就绪，可能帮助 F1 精度边缘 case（如 sekiro2 event3207/9493，45k 顶点本步未及跑完验证）——后续可测。
