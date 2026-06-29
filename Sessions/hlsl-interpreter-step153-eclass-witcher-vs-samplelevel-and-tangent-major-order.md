# hlsl-interpreter-step153：E 类（witcher3）—— VS 内 `SampleLevel` 缺失修复 + 切线/SH 主序divergence诊断

## 任务

处理 step124/152 分类里的 **E 类（10 个 witcher3_countryside）**。E 类是一组独立失败，doc 命名其核心症状为
"切线矩阵主序"（tangent matrix major order）。逐条诊断、能修则修、跑回归。

## E 类 10 个 case（来自 `Cases/dump_failure_categories.csv`）

```
event16215  event16834  event21719  event21895  event21979
event22049  event22092  event22201  event22229  event22260
```

全部在 `Dump/` 下。判定规则同回归：进程正常退出 + 日志无 `Error:` 行 + `Total PASSED rows: X/Y` 满足 X==Y。

## 诊断

### 0. 先厘清比较列的"罐头键"错位（不是 bug，但极易误判）

witcher VS 输出 `TEXCOORD0..5 + SV_Position`。`_get_output_semantic_to_key_map` 把语义按 Collision
套件的约定折叠成 canonical key：`TEXCOORD0→TexCoord`、`TEXCOORD1→TexCoord2`、`TEXCOORD2→TexCoord3`、
`TEXCOORD3→TexCoord4`（注意**偏移一位**），`TEXCOORD4/5` 无映射回落到大写 `TEXCOORD4/5`。

后果：错误日志里的 **`TexCoord4` 实际是 `o3`（语义 TEXCOORD3）**，不是 `o4`。
step124 doc 把 `event22229` 写成 "`o4.xyzw = v1.xyzw` 纯拷贝输出 40.49 vs golden 21.76" 是被这个错位误导了——
经核对，**`o4 = v1`（COLOR）实际通过**，真正失败的是 `o3`。我方与 golden 列对齐都正确
（golden loader 的 "SV_Position 优先重排" 给出 `o4 = [1,1,1,0.87]` = COLOR，吻合）。

### 1. 失败输出分布（execute_count=12，修前）

| case | 失败 canonical key（→实际输出） | 备注 |
|---|---|---|
| 16215 | TexCoord2(o2), TexCoord3(o3) | 切线帧；half4 输入 + 实例化 |
| 16834 | TexCoord(o1),TexCoord2,TexCoord3,sv_position | 除零→nan/inf |
| 21719 | TEXCOORD4(o4),TEXCOORD6,TexCoord4(o3) | |
| 21895 | TEXCOORD5,TexCoord3(o2),TexCoord4(o3) | |
| 21979 | sv_position（仅 1/12 行） | 精度边界 |
| 22049 | TexCoord4(o3), sv_position | |
| 22092 | TexCoord4(o3) | **仅 o3** |
| 22201 | TexCoord4(o3) | **仅 o3** |
| 22229 | TexCoord4(o3) | **仅 o3** |
| 22260 | TexCoord4(o3) | **仅 o3** |

**主导失败是 `o3`（语义 TEXCOORD3）**，5 个 case 几乎只错它。

### 2. 根因 A（已修）：VS 内 `Texture.SampleLevel(...)` 完全未实现 → 返回 None

逐语句 trace `event22229` 顶点 0，命中：

```
[STMT] r6.xyz = t22.SampleLevel(s11_s, r6.xy, 0).xyz
[ERROR] Unknown method: SampleLevel
[STMT] ... => r6.xyz = None
```

`execute_method_call_node` 只实现了 `Sample`（2 参）和 `Load`，**没有 `SampleLevel`（3 参，显式 LOD）**。
witcher 的顶点着色器在 VS 阶段就采样纹理（环境光探针 t22、阴影级联 t19/t9/t10——VS 采样在游戏里少见），
未实现时一律返回 None，被下游当 0，静默丢掉采样项。

> 还发现一个潜在解析缺口：`_parse_method_call` 丢弃 `)` 之后的尾随 swizzle，
> 故 `tex.SampleLevel(...).xyz` 的 `.xyz` 没生效（返回完整 float4）。对本批 case 因
> `.xyz→.xyz` 赋值/`.x` 单分量赋值"碰巧取 value[0]"而无害，**未修**（避免扩大改动面）。

### 3. 根因 B（深层，未能可靠修复）：`o3` 的 SH 环境光 + 距离雾 blend

修 SampleLevel 后重跑 `event22229` 顶点 0：t22 采到真实 texel `[0.1412,0.1572,0.1605]`，
`o3` 从 `[26.5186,15.2060,5.4427]` → `[26.4220,15.2269,5.5004]`（**z 向 golden 5.5514 收敛**），
但 x/y 仍偏高，**误差未跨过容差**。

把 `o3` 链路完整 trace 出来，结构是 3 段天空梯度的双线性 blend：

```
o3.xyz ≈ r6 = lerp(cb13[1], lerp(cb13[52], cb13[51], r4.w), r3.w) / π · r5
```

- 核对原始常量缓冲：`cb13[1]=[82.895,47.686,17.109]` 加载**正确**，`o3 = cb13[1]/π = [26.38,15.18,5.44]`。
- 关键：最终 blend 的 `r3.w = saturate(dist·cb12[219].x + cb12[219].y)`，`dist=2.61`、
  `cb12[219]=[0.00267,-0.2896,…]` → `saturate(-0.283)=0`。即**距离雾权重=0**（近处顶点无雾，看似合理），
  于是 `o3` 退化为 `cb13[1]/π`。
- golden `o3=[21.76,13.21,5.55]`，比我方**整体偏低且各通道比例不一致**（0.825/0.870/1.020）。
  反解：要用单一 blend 权重凑出 golden，需 `r4.w=1.44`（saturate 输出∈[0,1]，**不可能**）。

**结论：** golden 的 `o3` 无法由"`r5=[1,1,1]` 的该 SH 公式 + 单权重"重建——说明我方在这条
**分支密集**（多层 `if`/`while` 阴影级联 + 雾）的链路上某处取了与 GPU 不同的分支/状态，或缺了一项贡献。
在**没有 golden 寄存器级 dump** 的前提下无法定位到具体指令——手工反解出现自相矛盾即是证据。
这类与 C 类（TombRaider 反编译有损）类似，超出"直接解释 HLSL 源 + 仅有 VS 输出 golden"的可验证边界。

### 4. 其余子类

- **16215（doc 点名的"切线矩阵主序"）**：`o2=r5`、`o3=r3` 是切线帧（binormal/tangent）。
  golden `o3≈[0.993,0.010,0.116]`（近 X 轴），我方 `[0.748,0.065,-0.661]`——**整帧被旋转/置换**。
  输入是 half4（已从 raw VB 精确解码，非 CSV 取整）+ 实例化（sv_position 通过 ⇒ 实例变换正确）。
  切线帧重建链同样分支密集（含 `(int3)&`、`log2/exp2`、条件 select），与根因 B 同性质，未能可靠修复。
- **16834**：着色器除零产生 nan/inf（纹理返回后做除法），数值定义问题，未修。
- **21979**：`sv_position[1]≈62.5` 系统性偏 `~0.01`（相对 `~1.6e-4`）、`TexCoord3[2]≈11.55` 偏 `~0.006`。
  开 `float32_emulation` 不消失 ⇒ 非纯舍入；当前相对容差 `_REL_TOLERANCE=2e-5` 太紧（需 ~3e-4 才过）。
  属精度/容差边界（F1 性质），未放宽容差（15× 放宽风险偏大，留待统一精度策略）。

## 修复（`hlsl_interpreter.py`）

`execute_method_call_node` 新增 **`SampleLevel`** 处理（紧邻 `Sample` 之后）：
- 形如 `tex.SampleLevel(sampler, coords, lod)`：`coords` 取 u,v（Texture2DArray/Cube 的第 3/4 分量
  做 slice-0 近似），`lod` 为尾随标量；以"显式 LOD = `w`、derivatives=None"调
  `self._texture_exec.sample(u, v, lod, desc, sampler, None, None)`——复用既有 SampleLevel 风格路径。

## 结果

- **SampleLevel 修复正确且必要**：VS 纹理采样此前完全失效（返回 None），现返回真实 texel，
  输出向 golden 收敛（如 22229 `o3.z` 5.44→5.50）。
- 但 **10 个 E 类 case 无一转为 PASS**：主导误差在 `o3` 的 SH/雾 blend 与切线帧重建链——
  深层、分支密集，无 golden 寄存器级数据无法可靠定位（手工反解自相矛盾）。
- 诚实结论：E 类不是单点 bug，SampleLevel 是其中一个真实可修子项；其余属
  "需寄存器级 golden / 超出 HLSL 源解释边界" 的同类，与 C 类定性一致。

## 回归

`python run_regression.py` → **125/125 PASS**（SampleLevel 改动零回归）。
未新增回归用例：E 类无通过 case 可作代表；现有回归集亦无 VS-`SampleLevel` 覆盖（witcher 特有）。

## 产物

- `hlsl_interpreter.py`：`execute_method_call_node` 新增 `SampleLevel` 分支。
- 本会话文档。
- 诊断脚本/配置在 scratchpad（`/tmp/cfg_*.json`、trace），非仓库产物。

## 后续（如要继续推进 E 类）

1. 给比较框架加 **VS 寄存器级 golden**（导出 DXBC 逐指令中间值）才能定位 `o3`/切线帧 divergence。
2. 修 `_parse_method_call` 的尾随 swizzle 丢弃（`tex.Sample(...).y` 这类会取错分量）——独立小 bug。
3. 统一精度策略：相对容差 + float32 模拟，专门收敛 21979 这类边界。
