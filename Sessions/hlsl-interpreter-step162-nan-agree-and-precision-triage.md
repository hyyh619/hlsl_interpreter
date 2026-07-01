# Step 162 — 修 event2867（NaN==NaN 视为一致）+ 判定 event2848/7308 为精度受限

## 任务
> 继续修：event2848 / event7308 —— sv_position float32 精度边缘（diff 0.0104 / 0.026 vs 容差 0.01，F1）；
> event2867 —— 蒙皮 Color（o3=`SkinningParameters[..]._m30` blend）部分行不符。

## event2867 —— 已修（比较语义：双方都 NaN 即一致）

排查发现 event2867 的 **全部 11346 个 Color 失败都是 `output=nan golden=nan`**（0 个非-nan、0 个非-Color）。
即 **GPU 对这些退化蒙皮顶点本就输出 NaN（golden=nan），我方也算出 NaN**——二者一致，但比较函数用
`not (|ov-gv| <= tol)` 判定：`|nan-nan|<=tol` 恒 False → 被误报为 mismatch。

**根因**：`compare_vs_output_with_golden_params` 的 `not (diff <= tol)` 写法（step 早期为「把我方 NaN/inf
正确判失败」而选）会把 **NaN==NaN 也判失败**。

**修复**：新增 `_num_agree(ov, gv, tol)`：
- 双方都 NaN → 一致（GPU 出 NaN 且我方复现，正确）；
- 双方都 ±inf 且相等 → 一致；
- **单侧** NaN/inf → 仍判失败（我方 NaN vs 真实 golden 的真 bug 照旧捕获）；
- 否则 `|ov-gv| <= tol`。
list 分量比较与标量比较两处都改用 `_num_agree`。

**结果**：event2867 **0→3372/3372 PASS**。回归 130/130（新增 event2867 入回归守护本修复 + 蒙皮扁平索引）。

## event2848 / event7308 —— 精度受限，不强修

两者同 shader（重复 draw，误差完全相同）：`sv_position` 150 行 + `TEXCOORD5` 14 行超差，最大 diff 0.026
（值 ~220，相对 ~1.2e-4）。

**关键实验**：开 `float32_emulation=false`（全双精度）后**仍失败**（1421/1548，max diff 0.0176）。说明这不是单纯
float32/FMA 舍入——即使「完美算术」也与 golden 差 0.0176。该 VS 是**程序化顶点动画**（`rsqrt`/`sincos`/`frac`/
分支 + MaterialParams/TimeVector 算偏移，再经 WorldParameters 变换），是**病态**计算（大中间量 ~1300 世界坐标相
减的抵消 + 超越函数），在敏感顶点上任何算术路径都会与 GPU 的**具体 float32 指令序列**（含 FMA 与 GPU 版
sincos/rsqrt 实现）分离 ~1e-4。

**判定**：要匹配需逐指令复刻 GPU 的 FMA + 超越函数位精确实现，不现实；**不放宽全局容差**（会掩盖真错）。属
F1 精度受限，保留于 Dump。

## 结果汇总
- **event2867 修复**（NaN==NaN 视为一致），**TombRaider 34→35/37**，回归 130/130 零回归，event2867 入回归。
- **event2848/7308 判为精度受限**（病态程序化动画，双精度下仍差 0.0176），非可修 bug，留 Dump。
- Dump TombRaider 3→2（删 event2867）。

## 本步代码改动
- `hlsl_interpreter.py`：新增 `_num_agree`（NaN==NaN / ±inf 相等视为一致，单侧 NaN/inf 仍失败），
  `compare_vs_output_with_golden_params` 两处比较改用它。
- 回归 +1（event2867，130/130）。

## 后续
- event2848/7308：程序化动画精度受限；若要推进需 GPU 位精确 FMA + sincos/rsqrt 复刻或按用例相对容差策略（F1 通案，高风险）。
- 提醒：`_num_agree` 的 NaN==NaN 放行仅当**双方**都 NaN；若将来出现「golden 因加载 bug 误成 NaN 而真实为有限」的
  情形，会被误放行——但那属 golden 加载问题，另行处理。
