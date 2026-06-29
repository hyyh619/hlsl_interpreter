# Step 154 — VS 寄存器级 golden（DXBC 逐指令解释器）+ 实例化输入加载修复

## 任务

> 先提交你的 fix，然后继续推进给比较框架加 **VS 寄存器级 golden**（导出 DXBC 逐指令中间值），
> 那才能定位 o3/切线帧的具体偏离点。

step 153 的诚实结论是：E 类（witcher）的 `o2/o3` 切线帧偏离在「只有 VS 输出 golden + 直接解释
HLSL 源」的可验证边界之外——手工反解自相矛盾，无法定位到具体指令。本步补上缺的那把尺子：一个
**独立执行 GPU 真实指令流（`VS_shader_disasm.txt`）的 DXBC 虚拟机**，逐指令 trace 中间寄存器，
与 golden `*_vs_mesh.csv` 对比，从而把「o3 偏离」分解成可定位的具体环节。

## 交付物

| 文件 | 作用 |
|------|------|
| `dxbc_interp.py` (新, ~700 行) | `DXBCInterpreter`：vs_5_0 反汇编解释器。ALU / int / bit / 控制流（if/else/loop/break）/ `sample_l` / `sample_c_lz` / `ld_structured` / `ld_raw`。寄存器是无类型 32-bit lane，整数/位运算时按位重解释（`f2u`/`f2i`/`u2f`/`i2f`）。`log_trace=True` 时记录每条指令的目的寄存器与值。 |
| `dxbc_diff.py` (新, ~370 行) | 驱动：解压 case → 复用 `render.py`/`HLSLInterpreter` 的加载器装 cbuffer、顶点输入（含二进制 VB + 实例缓冲）、纹理资源 → 跑 VM → 与 golden 逐 `oN` 对比（`SV_Position`-first 映射，匹配 HLSL 解释器的 golden 装载顺序）。 |
| `hlsl_interpreter.py` | **实例化（per-instance）顶点缓冲加载修复**（见下）。 |
| `hlsl_syntax_tree.py` | 科学计数法字面量修复：`_find_top_level_operator_cached` 不再把 `4.65661287e-10` 里指数的 `-` 当减法切开。 |
| `run_regression.py` | `BASE_CONFIG` 增加 `vs_only: True`（回归只验 VS 输出）。 |

## 执行与发现（以 witcher event16215「切线矩阵主序」为靶）

### 1. 第一次跑 DXBC VM：o0 = NaN

```
o0 = [nan, nan, nan, nan]      golden=[5.0447,-3.3938,0.1998,5.6931]
o3 = [1.0, 0.0, 0.0, 0.2874]   golden=[0.9932,0.0102,0.1158,0.2873]
```

逐指令 trace 定位首个 NaN：

```
11: dp2 r3.w = 0.0      # 某向量长度² = 0
12: rsq r3.w = inf      # 1/sqrt(0)
13: mul r0.xw = nan     # inf * 0 → nan，污染全部下游
```

即「对零长向量归一化」。再往上：该向量来自 `v1.zw` 的 half 解包（`ushr`/`f16tof32`）。

### 2. 根因：per-instance 输入 `v1` 被加载成全 0

- `ia_input_layouts.csv`：`TEXCOORD0`(=`v1`) 是 `R32G32B32A32_FLOAT`、**PerInstance=True**、
  来自 VB slot 1（`vb_slot1_res_119650.bin`，bind ByteOffset=336，stride=16）。shader 把
  `v1.z/v1.w` 的 32-bit 位再解释成 2×half（编码切线帧），`v1.xy` 是世界坐标。
- `ia_vertex_data.csv` **根本没有 TEXCOORD0 列**——RenderDoc 的 IA dump 不导出 per-instance slot。
- 而 `load_per_vertex_binary_data` 里 `if elem['per_instance']: continue` **直接跳过**所有 per-instance
  元素。于是 `v1` 既不来自 CSV 也不来自二进制 → 恒为 0。
- 验证：`vb_slot1_res_119650.bin` 偏移 336 处 = `(520.99, 79.48, 8212.01, 0.0014)`，正是 golden
  `o6`（世界坐标直拷）。golden mesh 只有 30 行 = instance 0，所以 per-instance 取 instance 0、
  bind offset 336 即可。

**修复**（`hlsl_interpreter.py::load_per_vertex_binary_data`）：

1. 不再跳过 per-instance 元素，job 里记 `per_instance` 标志；
2. per-instance 取偏移用 instance 索引 0（`base_off + 0*stride + elem.byte_offset`），对所有顶点恒定；
3. per-instance 绕过 CSV 一致性门（CSV 永远没有 per-instance 列，二进制是唯一真值，与 degenerate 同等待遇）。

修复后 DXBC VM：

```
[OK ] o0  dxbc=[5.0447,-3.3938,0.1998,5.6931] golden=[5.0447,-3.3938,0.1998,5.6931]
[OK ] o1  dxbc=[0.0,0.8691,0.6317,0.0]        golden=[0.0,0.8691,0.6317,0.0]
[OK ] o5  dxbc=[5.0449,-3.3938,0.1998,5.6932] golden=[5.0449,-3.3938,0.1998,5.6932]
[OK ] o6  dxbc=[520.99,79.48,8212.01,0.0014]  golden=[520.99,79.48,8212.01,0.0014]
[DIFF] o2 dxbc=[-0.0289,0.0289,0.9992,1.0]    golden=[-0.1163,0.087,0.9894,1.0]
[DIFF] o3 dxbc=[0.9996,0.0008,0.0289,0.2874]  golden=[0.9932,0.0102,0.1158,0.2873]
```

7 个输出中 **o0/o1/o4/o5/o6 全部对齐 golden**，只剩切线帧 o2/o3。

### 3. 定位 o2/o3：Texture2DArray 细节法线采样返回 0

trace 跟到 o2←r5、o3←r3 的最终装配（disasm 行 489–517，一段由世界坐标驱动的「细节法线/flow-map」
扰动）：

```
504: sample_l(texture2darray) r0.xz, r3.xyz, t4.xzyw, s4, l(0)   →  r0.xz = [0.0, 0.0]   ← 采样返回 0
506: mad r5.xy = r2.xz * cb11[83].x + r0.xz  = [-0.0289, 0.0289]  ← 全部来自 fallback 项
510: sqrt r5.z = 0.999163                                          ← 重建法线 z
```

`cb11[83].x = 0.15`。因为 `sample_l` 对 **Texture2DArray t4** 返回 `[0,0]`，r5.xy 完全由回退项构成，
切线被「欠扰动」——故 o2/o3 的小分量比 golden 小约 4×（z 分量接近 1 故仍接近）。**采样值就是杠杆**。

### 4. 与 HLSL 解释器交叉验证（关键结论）

跑真实 `render.py`（vs_only）于 event16215：

| | Total PASSED | 失败分量 |
|--|--|--|
| 修复前 | 0/30 | 仅 `TexCoord2`/`TexCoord3`（=o2/o3）|
| 修复后 | 0/30 | 仅 `TexCoord2`/`TexCoord3`（=o2/o3）|

即 **per-instance 修复不改变 HLSL 解释器的 witcher 结果**——因为反编译出的 HLSL 用
`POSITION(v2)+cbuffer` 算 `o0`，而 DXBC 指令流用 per-instance `v1` 算 `o0`：两条不同公式、输入正确时
都对 golden。HLSL 路径本就不依赖 `v1` 得到 o0/o5/o6，所以它一直「碰巧」对。

**这正是寄存器级 golden 的价值所在**：它独立执行 GPU 真实指令流，证明了
> o2/o3 的偏离 **不是** HLSL 反编译有损（step 153 的怀疑方向），而是 **HLSL 解释器与 DXBC VM 共有的
> Texture2DArray 细节法线采样缺口**——忠实执行的 DXBC 指令流在 o2/o3 上同样失败，且 trace 指到唯一根因
> 是 line 504 的 array 采样返回 0。

## 结果

- **新增 VS 寄存器级 golden 工具**（`dxbc_interp.py` + `dxbc_diff.py`），可逐指令 trace + 与 golden 对比。
- **修复 per-instance 顶点缓冲加载**（真实输入正确性 bug）：让 DXBC VM 在 event16215 上 o0/o1/o4/o5/o6
  对齐 golden；HLSL 解释器侧无回归无变化。
- **把 step 153 的「o3/切线帧」悬案分解定位**为两个互不相干的具体环节：
  ① per-instance 输入（已修，DXBC 侧验证）；② Texture2DArray 细节法线采样返回 0（已定位，二者共有，
  非反编译问题）。
- **回归 125/125 PASS（exit 0），零回归。** 科学计数法修复 + vs_only + per-instance 修复均不破坏既有用例。

## 后续可推进

1. 实现 VS 端 **Texture2DArray 切片采样**（`sample_l` 取 `coords.z` 作 array index），让 o2/o3 收敛——
   这同时惠及 HLSL 解释器与 DXBC VM（共有缺口）。
2. `dxbc_diff.py` 的 `_build_resources` 目前把 array 近似成 slice 0，应改为按 `r3.z=cb11[82].w` 选片，
   以便用 VM 确认「采样修好后 o2/o3 即对齐」。
3. 把 DXBC VM 作为 witcher 类的「第二 golden」纳入诊断流程：凡 HLSL 输出与 golden 不符，先跑 DXBC VM
   判定是反编译问题（VM 对、HLSL 错）还是共享基础设施问题（VM 也错）。
