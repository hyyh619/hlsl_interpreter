# Step 180 — 超越函数墙的数据驱动定量：墙成立，libm 即最优近似

## 任务

类 D 7 案：TombRaider 2848/7308（827/1548）、manhattan 87/124/161/198
（各差 1–2 行出生粒子）、sekiro2 3207/9493 残余 1235 行。按步 175 建议做
数据驱动拟合：批量反解 golden 隐含的硬件 sin 输出，与多种归约模型比对。

## 基建：可插拔 trig 归约模型

- `HLSLInterpreter(trig_model=...)` + config 键 `trig_model`（render.py 接线），
  默认 `'libm'`（= math.sin/cos，行为与历史逐位一致）。
- 模型库 `_TRIG_MODELS`：`libm`、`f32red`（朴素 f32 圈数归约）、
  `cw2`（Cody-Waite 双项 f32 逐步舍入）、`cw2fma`（双项 FMA 单舍入）。
- sin/cos/sincos 全部经 `self._sin/_cos` 路由。

## 拟合一：sekiro2_3207 全案逐模型（Parallels/Apple GPU）

| 模型 | Total PASSED |
|---|---|
| **libm** | **44341/45576** |
| f32red | 44338 |
| cw2 | 44338 |
| cw2fma | 44338 |

所有 f32 级归约模型都不如 libm——该 GPU 的归约精度高于任何 f32 模型。

## 拟合二：manhattan 出生粒子哈希反解（nVidia GPU，决定性）

出生行输出 = `emitter_worldmat·(aperture·(2·frac(sin(a)·k)−1))+T`，
线性可逆：golden → frac → 隐含硬件 sin = (n+frac)/k（n 由 libm 近似定整，
k≈43758/87362/12235 使隐含 sin 的精度达 ~2e-5）。4 案 14 个样本
（args 1.36e4 ~ 1.00e5 rad）：

- **隐含硬件 sin 与 libm 的偏差：max 2.4e-5、mean 7.5e-6**；
- 各归约模型残差：f32red max 5.6e-3、cw2 5.4e-3、cw2fma 3.4e-3——
  **全部比 libm 差 100–1000 倍**（nVidia 用宽位 Payne-Hanek 类 RRO）；
- 归约角偏移 dr 的符号无规律、dr/arg 非常数（6e-12 ~ 1.4e-9）——
  **不是解析可建模的常数偏置，是硬件内部定点量化噪声**（幅度 ≲2^-15.3 rad）。

## 结论：墙定量成立

1. **libm（double 归约）就是可用的最优近似**——任何 f32 归约"改进"都是倒退
   （步 174 的净负效实验由此得到解释）。
2. manhattan 的 `frac(sin·87362)` 把 ~1e-5 的硬件角噪声放大 87362 倍 → 出生行
   的 frac 输出与任何解析近似完全去相关。**匹配这些行需要比特级复刻 nVidia
   SFU 的 RRO+二次插值表**（未公开的内部定点常数），工程上不可行。
3. sekiro（Parallels）与 TombRaider（nVidia）同理：残差行都在
   放大链末端的容差边缘，模型扫描证实无解析模型可改进。
4. **决策：默认保持 `libm`，不放宽全局容差**；7 案按"已定量证实的墙"归档。
   `trig_model` 开关保留作未来 vendor 拟合的工具。

## 全量验证

**Cases 回归 148/148 全 PASS**——`trig_model` 接线默认 `libm`，行为与历史
逐位一致，零扰动。

## 方法论沉淀

哈希放大链是**反解硬件超越函数的天然探针**：`frac(sin(a)·k)` 的 k 越大，
golden 对 sin 的约束越精（本例 2e-5）；配合 libm 定整数部分，
无需任何硬件文档即可测得 vendor sin 的真实误差分布。
