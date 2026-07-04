# Step 172 — Octopath 地形系统性偏差（类⑨）：五个叠加缺陷的定位与修复

## 任务

调查并修复类⑨：Octopath event 576/2651/2682，长期停在 8/23064（sv_position 系统性 1–5% 偏差），
步 168 排除了「zero cbuffers」旧判断后真因未知。

## 调查路径

### 1. Shader 理解

`VS_shader.hlsl`（3Dmigoto 反编译）是 clipmap 地形 VS：
- `t0`/`t1`（`Buffer<float4>`，实为 `TypedBuffer R32_FLOAT`×25）存邻块/本块 LOD；
- 整数索引链（`asint(cb2[0])`/`asint(cb3[0])` + iadd/imad）算出 tile 索引去 Load LOD；
- 网格坐标 `v0.xy` 按 `rcp((64>>lod)-1)` 归一化，双候选 morph 因子按对角线选取；
- `t2`（128×128 **B8G8R8A8_UNORM**，8 mips）在两个 mip（`floor(lod)` 与 `+1`）各采一次高度，
  `dot(rg, float2(65280,255)) - 32768` 解码 16-bit 高度，morph 因子插值；
- 世界变换 + 厘米级 snap（`trunc(100x)*0.01`）+ 投影。
- 采样器 s0 是 **Point/Point/Point + ClampEdge**（无过滤，golden 的小数高度来自 morph 插值）。

### 2. DXBC 寄存器级 golden 的盲区

`dxbc_diff.py` 判定：VM 也偏离 golden → 貌似「共享基础设施 bug」。但检查发现
**VM 的 `ld`/`ld_indexable`（typed buffer Load）是「近似返回 0」的占位**——VM 在本案的
LOD 输入本身就是假的，其偏离不能定位问题（后续可补：给 VM 接入 typed-buffer ld）。

### 3. 独立复现脚本（replica576.py）→ GPU 语义全确认

写了逐指令 Python 复现（所有输入直接读 dump 二进制），**23064/23064 全部与 golden
精确一致**，从而钉死 GPU 语义组合：
- t2 `.img` 解码按 **BGRA 字节序**（R=byte2、G=byte1），高度=R*65280+G*255；
- `SampleLevel` point 过滤：mip = `floor(lod)`，texel = `floor(u*w)`，ClampEdge 夹边；
- morph/LOD 全整数链正确时，逐分量 f32 舍入即可比特级复现。

### 4. 用复现结果反照解释器 → 症结显形

解释器输出的 sv_position 对**所有顶点都等于 v0=[0,0,0,0]（0 号顶点）的答案**
（-7382.70/-1329.24/2445.27 = golden 第 0 行），8/23064 的"通过行"就是 idx=0 的 8 次出现。
v0 本身装载正确（二进制 VB 路径、逐行变化）——塌缩在执行层。`[STMT]` 逐语句 trace 揪出
三个叠加缺陷：

**Bug 1（元凶）：`asint(-cbN[i].sw)` 的求值顺序。**
disasm 原指令 `iadd r0.xyzw, r0.xyzw, -cb2[0].yxyx` 的 `-` 是**整数源修饰符**
（整数取负）；3Dmigoto 把它渲染成 `asint(-cb2[0].yxyx)`。按 HLSL 字面语义先做浮点取负：
`-0.0f`，再 asint → **0x80000000 = -2147483648**。索引链全部中毒
（`0 + (-2^31) = -2^31-1` → `t0.Load(-2147483649)` 越界 → LOD/morph 全垮 →
所有 v0 相关项被乘 0 或吞掉 → 输出恒为 0 号顶点的值）。
正确语义是**先位重解释、后整数取负**：`-asint(x)`。

**Bug 2：`rcp()` 内建函数未实现**——求值返回 None，赋值被跳过，寄存器保留旧值
（`r0.y` 保持 63 而非 1/63，网格坐标放大 3969 倍）。

**Bug 3：typed buffer `Load` 越界返回 None**——同样被"跳过赋值"吞掉，寄存器保留旧值。
D3D robust buffer access 语义：越界 typed-buffer 读**返回 0**。

三个 bug 的破坏面叠加，且因为 v0=0 顶点恰好全程免疫（乘 0），留下了 8/23064 这个
迷惑性极强的"部分通过"。

### 5. 修完前三个后 event576/2682 全过、event2651 仍 95/256 → 又挖出两个"被 576 侥幸掩盖"的 bug

event2651 与 576 同算法但 tile=(1,7)，其 t0[9]=**1.2318（小数 LOD）**——激活了 576 从未
走过的路径（576 的 tile 及邻块 LOD 全为 1.0 整数 → morph 小数部分恒 0）。给 2651 写第二个
复现脚本（golden 的 TEXCOORD0/2 恰好暴露 morph 坐标与高度中间值），23064/23064 精确匹配后
反照解释器：

**Bug 4：`asint(cbN[i].swizzle)` 多分量返回全 0。**
`_cbuffer_component_raw_int` 的 raw-bit 路径只匹配标量 `cbN[i].c`；swizzle
（`asint(cb3[0].yx)`）落入浮点求值，而 cbuffer 浮点视图来自 CSV——**小整数以 denormal
位模式存储，CSV 舍成 0.0**，asint 得 [0,0]（应为 [1,7]）。tile 索引全体坍缩到 t0[0]。
**event576 此前"全过"纯属侥幸**：t0[0]==t0[6]==1.0，错索引恰好读到相同值。
修复：raw 正则扩展为 `[xyzw]{1,4}`，多分量返回 int 列表；asint 分支的取负/回绕同步支持列表。

**Bug 5：显式 LOD 被采样器 AddressW 夹取（texture.py，破坏面大）。**
`sample()` 先 `tu,tv,tw = sampler.transform_coordinates(u,v,w)` 再 `lod = tw`——
而 2D 采样的 `w` 承载 SampleLevel 的**显式 LOD**，ClampEdge 的 AddressW 把 lod=2.0
夹成 1.0 → 所有 morphing 顶点的 coarse 采样读错 mip（fine 采样 lod=1 不受影响，
更添迷惑）。AddressW 只该作用于 Texture3D 的 z 坐标（`sample_volume` 已单独处理）。
修复：显式 LOD 直接用原始 `w`（加 isfinite 守护）。

## 修复汇总

hlsl_interpreter.py：
1. **asint/asuint 整数取负修饰符**（一元负号在语法树里是 `binary_op '-'`+空左子，
   不是 `unary_op`）：先位重解释、后整数取负（asuint 加 `& 0xFFFFFFFF` 回绕）。
2. **asint/asuint 的 cbuffer raw 路径支持 1–4 分量 swizzle**（正则 `[xyzw]{1,4}`）。
3. **新增 `rcp` 内建**：精确 `1/x` 按 f32 舍入；`rcp(±0)=±inf`、NaN 透传。
4. **`_typed_buffer_load` 越界返回 `[0,0,0,0]`**（D3D robust access；不再 None 静默保留旧值）。

texture.py：
5. **`sample()` 显式 LOD 不再经过 AddressW 变换**（用原始 `w`）。

## 结果

- **类⑨消灭：Octopath event576/2651/2682 全部 23064/23064 全过**（此前 8/23064）。
- **意外收获：witcher3_countryside_event22092 被 Bug 5（显式 LOD 被 AddressW 夹取）
  的修复带动，0/414 → 414/414 全过**——witcher 的 VS SampleLevel 同样用 ClampEdge
  采样器显式 LOD。witcher event21895 错误行 40868→40595 亦有收敛（未过线）。
- **Cases 回归 `run_regression.py` 134/134 全 PASS 零回归**（asint/rcp/OOB/LOD 五处
  共享基础设施改动无一破坏既有案例）。
- **Dump triage 39 案：4 转 PASS、其余逐案与步 168–171 基线持平零退步**。
- **4 案晋级** Cases + 回归表（**138 案**）并从 Dump 移除（剩 **35 案**）。

## 方法论沉淀

- **独立复现脚本**（所有输入直读 dump 二进制 + 每步 f32 舍入）是本步的决定性工具：
  两次都先证明「这组语义能 23064/23064 比特级复现 golden」，再反照解释器的 trace 找分歧，
  比对着 golden 猜要快得多。
- **「部分通过」可能是侥幸而非接近正确**：event576 曾在 tile 索引全错（全部读 t0[0]）的
  情况下依然 23064/23064 全过（数据巧合 t0[0]==t0[6]）；v0=0 顶点全程免疫三个 bug 留下
  8/23064 的迷惑基线。判断"已修好"必须换数据分布验证（2651 的小数 LOD 立即揭穿）。
- DXBC VM 的 `ld`/`ld_indexable`（typed buffer）仍是"近似返回 0"的占位——本步靠复现脚本
  绕过；后续可给 VM 接入 typed-buffer Load 使寄存器级 golden 对这类 shader 可用。
