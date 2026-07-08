# AI 辅助开发手册 — 以 HLSL 解释器项目为例

> 本手册用 **本项目（纯 Python 的 D3D11 管线 / HLSL 解释器）** 的真实开发记录，
> 系统说明"如何与 AI 协作完成一个真实工程"。所有例子都取自本仓库的提交、
> `Sessions/` 步骤日志与 `Prompts/` 提示词历史，可逐条核对。
>
> 本手册按**项目的开发周期 + 自动化运维**组织成四大部分：
> - **第一部分 · 项目总体介绍** —— 这是什么、怎么跑、怎么验证。
> - **第二部分 · 基于 OpenCode + minimax-m2.7 的开发（奠基期）** —— 从零搭骨架的协作方式。
> - **第三部分 · 基于 ClaudeCode 的开发（攻坚期）** —— 真机数据攻坚长尾的协作方式。
> - **第四部分 · 周期任务的创建和执行（自动化期）** —— 把开发/巡检交给定时任务无人值守闭环。

---

## 目录

**第一部分 · 项目总体介绍**

**第二部分 · 基于 OpenCode + minimax-m2.7 的开发（奠基期）**
1. 如何给 AI 提供提示词
2. 分析需求、生成开发计划
3. 如何按计划执行
4. 如何纠正 AI 的错误
5. 如何进行调试

**第三部分 · 基于 ClaudeCode 的开发（攻坚期）**
1. 如何给 AI 提供提示词
2. 分析需求、生成开发计划
3. 如何按计划执行
4. 输入 / 输出数据流
5. 什么输出能帮助调试
6. 回归测试的建立与应用
7. TDD 在本项目的应用
8. 如何纠正 AI 的错误
9. 记忆系统避免重复开发
10. b/c 两个开发部分的对比和优缺点总结

**第四部分 · 周期任务的创建和执行（自动化期）**
1. 什么是周期任务、它解决什么问题
2. 如何创建一个周期任务（调度 + 任务提示词 + 留痕）
3. 周期任务如何执行（无人值守闭环）
4. 例一：`hlsl-interpreter-hourly-develop`（每小时开发）
5. 例二：`daily-hlsl-status-report`（每日巡检）
6. 两个周期任务的对比与设计要点

> 每节都附本仓库 `Prompts/` 提示词历史与 `Sessions/` 步骤日志的超链接，可逐条核对。

---

# 第一部分 · 项目总体介绍

## 项目概述

### 0.0 项目的目的

**缘起（最初的目的）**：起因是一个非常具体的工程问题——作者当时在用 **AI Agent 实现 Irrlicht 游戏引擎的 DX11 video driver layer**，遇到 **VS（顶点着色器）计算不正确、导致渲染错误**。定位这类 bug 的常规做法是用 **RenderDoc 对 DXBC 反汇编指令**逐条调试，但反汇编**不直观、不方便看**；作者更想**直接运行 HLSL 高级语言指令**来查错。于是用 AI Agent 写了一个**简单的 HLSL 解释执行器**——这就是本项目的起点。

从这个具体的调试需求出发，"用 AI 写解释器"这件事本身也变成了**一次关于 AI coding 的研究**——它的目标随开发推进逐步演化：

1. **最初：一个 HLSL 调试器**：为看清 Irrlicht DX11 driver 的 VS 计算问题而写——用"直接解释 HLSL"取代"读 DXBC 反汇编"，把调试拉回高级语言层面。
2. **验证纯 AI coding 的能力边界**：核心问题是——**在几乎不动手写代码、全程由 AI 编程的前提下，到底能做到什么程度？** 整个仓库正是这个实验的产物。
3. **逐渐长成完整的 D3D 可编程图形管线**：在一步步推进中，它从最初的"解释一段样例 HLSL"逐渐演化成一个**完整的 Direct3D 可编程图形管线实现**（VS → 光栅化 → 深度/模板 → PS → 输出合并，外加纹理采样、cbuffer、回归体系）。
4. **最终成为 draw dump 的验证与分析工具**：项目最后落到一个实用目标上——**验证 draw dump 的正确性**，并能**逐语句、逐像素地分析某一个具体 Draw 的渲染过程**（哪一步的变换/光照/采样与真机 golden 对不上）。

> 换句话说，这份手册既是"如何实现一个 HLSL 解释器"的记录，也是"**纯 AI coding 能做到什么样**"的一份真实答卷——后续各章的方法论，都是在这个前提下总结出来的。

### 0.1 这是什么
本项目是一个**纯 Python 实现的 Direct3D 11 图形管线软件仿真器**，核心特点是**直接解释执行 HLSL 着色器源码**——不编译、不调用 `eval`，而是自己做词法/语法分析并逐语句解释。它把一帧真机捕获的 GPU 数据喂进这条仿真管线，跑完整的固定功能 + 可编程阶段：

```
Vertex Shader → Rasterizer → Depth/Stencil → Pixel Shader → Output Merger
```

然后把顶点着色器（VS）的输出与真机捕获的 **golden 参考数据**逐分量对比，以此验证解释器的正确性。

#### 演示视频

下面这段录屏展示了解释器跑完管线后、光栅化器把三角形逐像素填充并与 golden 对比的过程：

<video src="videos/hlsl_interpreter_rasterizer_show_20260524.mp4" controls width="100%" style="max-width:860px;border-radius:10px"></video>

> 若上方视频无法播放，可直接打开 [`Docs/videos/hlsl_interpreter_rasterizer_show_20260524.mp4`](videos/hlsl_interpreter_rasterizer_show_20260524.mp4)。

### 0.2 输入从哪来
输入是 **RenderDoc / 3Dmigoto 抓帧 dump**，打包成 `Cases/*.zip`。每个 zip 解压后是一组松散文件：

| 文件 | 作用 |
|---|---|
| `VS_shader.hlsl` / `PS_shader.hlsl` | 着色器源码（`main()` 用带语义的参数声明输入输出） |
| `*_input_output_signature.csv` | 参数 ↔ 寄存器槽位映射 |
| `ia_vertex_data.csv` + `ia_input_layouts.csv` + 二进制 VB/IB | 输入装配阶段的顶点/索引数据 |
| `*_constant_buffers.csv` + `constant_*.bin` | 常量缓冲区（文本布局 + 二进制精确值） |
| `pipeline_state.csv` | 光栅化/混合/深度模板状态 + 图元拓扑 |
| `output_merger.csv` + `pre_draw_ds_res_*.raw` | 输出合并阶段的 RTV/DSV 布局与 draw 前的深度模板内容 |
| `MeshOut_*_mesh.csv` | golden VS 输出（验证基准） |
| `.img` / BMP 纹理 | `Texture2D.Sample(...)` 的后备数据 |

### 0.3 怎么运行
唯一入口是一个 JSON 配置文件：
```bash
python render.py ./Cases/Default.json
```
`render.py` 是编排器，根据配置在 **zip 工作流**（当前主路径）与 **legacy struct 工作流**（旧配置后向兼容）之间分支。

### 0.4 模块构成
**零第三方依赖**——只用 Python 标准库，外加可选的 `tkinter`（网格查看器 GUI）。无 `requirements.txt`、无构建步骤、无独立单元测试框架。

| 模块 | 职责 |
|---|---|
| `hlsl_interpreter.py`（~3300 行，核心） | 解析 HLSL（cbuffer/struct/函数/纹理采样器绑定），遍历语句、求值表达式 |
| `hlsl_syntax_tree.py` | 手写递归下降表达式解析器，产出 `SyntaxTreeNode` 树；运算符优先级表在此 |
| `rasterizer.py` | 按图元拓扑光栅化；边函数 + 透视正确重心插值；视口变换与剔除 |
| `output_merger.py` | 深度/模板测试，支持 early-Z / late-Z |
| `texture.py` | 纹理解析、mipmap、采样与寻址模式 |
| `pixel.py` / `d3d.py` / `mesh_view.py` | 像素交换记录 / D3D 枚举常量 / 可选网格查看器 |

### 0.5 正确性如何保证
项目没有传统单元测试，正确性靠 **verify-by-log 工作流**与**数据驱动回归套件**：
- **verify-by-log**：跑管线 → 在 `output.log` 里 grep `Error:` 行（VS-vs-golden 的分量级不符）→ 迭代到无错。日志里的 `[STMT]` / `[BINARY OP]` 轨迹是调试主要面。
- **回归套件**：`run_regression.py` 读 `Cases/regression_test_zip_files.csv` 列出的 capture，逐个 headless 跑通，三条判据（干净退出、无 `Error:`、`Total PASSED rows: X/X`）全满足才算过。目前覆盖 **123 个真机 case**，是每次改动的提交闸门。

> 本手册后续各章，都是围绕"**如何与 AI 协作、在这样一个无单测、靠真机 golden 数据验证的解释器项目上持续修 bug**"展开的方法论总结。

---

# 第二部分 · 基于 OpenCode + minimax-m2.7 的开发（奠基期）

> 这一期（约 step1–step86，`Sessions/hlsl-step*.md`）用一个能力相对有限的模型，**从零搭出解释器骨架**：HLSL 词法/递归下降解析、语句解释、cbuffer/struct/函数解析、定点管线（rasterizer/depth/OM/texture）、golden 对比雏形。它的协作方式与第三部分截然不同——先用一节勾勒**人与 Agent 的开发流程闭环**，再从 5 个方面展开，每个都给 `Prompts/hlsl-interpreter-prompt-OpenCode.html` 里的真实步骤与超链接。

## 0 · 本期的开发载体：`Collision-…-RdotV-zero` case

这一期的解释器骨架，是围绕 `Cases/Collision-fix-constant-buffer-and-RdotV-zero_event*.zip` 这组真机 capture 来打磨的。下面以 `event399` 为例——它在屏幕上画出一件**带贴图、受光照的 3D 物体**（一柄战锤），要让纯 Python 解释器**逐语句重算出这件物体的每个像素**，需要把一整套 D3D11 顶点光照 + 像素贴图采样语义都实现对。下面用它的渲染结果和 HLSL 源码，说明项目目标与复杂度。

### 0.1 渲染结果：解释器输出 vs golden

项目目标是**不编译、不 `eval`，直接解释 HLSL 源码跑完整管线**，再把结果与真机 golden 逐像素/逐分量对比。下面是 `event399` 的三张图（取自 zip 内 capture，已转 PNG）：

| Golden（真机参考） | 解释器输出 | 深度缓冲 |
|---|---|---|
| ![golden](images/collision_event399_golden.png) | ![output](images/collision_event399_output.png) | ![depth](images/collision_event399_depth.png) |

左两张几乎完全一致——同一柄战锤、同样的金属贴图与高光走向，这正是"correct"的判据：解释器算出的颜色/位置/采样要和 GPU 真机一致（本帧 2548 个 golden 像素里约 2333 个落在容差内，余下是高光/光照边缘的细微差）。右图是同一帧写入的深度缓冲。**画面越丰富，要对齐的环节越多**：变换矩阵差一列、法线没归一化、贴图 UV 取错、光照衰减算偏，物体就会变形、错色或丢失高光。

### 0.2 输入顶点数据：数量、格式与样例

喂进管线的是一次 **`DrawIndexed`**（图元拓扑 `TriangleList`）：共 **696 个索引 → 232 个三角形**，背后是 **228 个不重复顶点**（索引缓冲为 16-bit）。每个顶点 **36 字节**、含 4 个属性（来自 `ia_input_layouts.csv`）：

| 语义 | 格式 | 分量 | 字节偏移 | 说明 |
|---|---|---|---|---|
| `POSITION` | `R32G32B32_FLOAT` | 3×float | 0 | 模型空间位置 |
| `NORMAL` | `R32G32B32_FLOAT` | 3×float | 12 | 法线 |
| `COLOR` | `R8G8B8A8_UNORM` | 4×u8→[0,1] | 24 | 顶点色（4 字节归一化） |
| `TEXCOORD` | `R32G32_FLOAT` | 2×float | 28 | UV |

> stride = 12 + 12 + 4 + 8 = **36 字节**；`COLOR` 以 `R8G8B8A8_UNORM` 存（4 字节），dump 时已归一化成浮点。

样例（`ia_vertex_data.csv` 前几行，已对齐排版）：

```
VTX IDX  POSITION(x, y, z)            NORMAL(x, y, z)            COLOR(r,g,b,a)     TEXCOORD(u, v)
 0   0   20.0289  21.6879 -23.3845    0.0224  0.0452  0.9987     0.8 0.8 0.8 1.0   0.2256 0.9067
 1   1   18.8732  23.9455 -27.7856   -0.5958  0.4614  0.6574     0.8 0.8 0.8 1.0   0.2571 0.7704
 2   2   18.8230  23.0896 -31.3426   -0.9792 -0.1951  0.0560     0.8 0.8 0.8 1.0   0.1968 0.6829
 3   3   19.9886  19.6074 -29.7759    0.0888 -0.9842  0.1530     0.8 0.8 0.8 1.0   0.1002 0.7539
 …  (共 696 行 = 按索引展开的绘制顺序；IDX 为原始顶点号，有重复，去重后 228 个)
```

注意这柄战锤所有顶点的 `COLOR` 都是统一的灰色 `0.8,0.8,0.8,1.0`——它是金属底色，最终颜色靠 PS 把它和漫反射贴图相乘得到（见 0.4）。这份逐顶点数据，正是 0.3 的 VS 要逐条变换、打光的输入。

### 0.3 Vertex Shader：把整套光照模型搬进解释器

这是该 case 的 `VS_shader.hlsl`（3Dmigoto 反汇编风格）。与一个只做坐标变换的简单 VS 不同，它在顶点级算完了**环境光 + 漫反射 + 镜面高光 + 距离衰减 + 聚光锥**，还带 `ColorMaterialMode` 分支选色：

```hlsl
cbuffer MatrixBuffer  : register(b0) { float4x4 WorldViewProj; float4x4 World; }
cbuffer LightBuffer   : register(b1) { float4 Ambient,Diffuse,Specular; float3 LightPos;
                                       float LightRadius; float3 LightDir; /*…*/ float3 Attenuation; /*…*/ }
cbuffer MaterialBuffer: register(b2) { float4 MatDiffuse,MatAmbient,MatSpecular,MatEmissive;
                                       float Shininess; uint ColorMaterialMode; /*…*/ }
cbuffer CameraBuffer  : register(b3) { float3 cameraPos; }

void main(float3 v0:POSITION0, float3 v1:NORMAL0, float4 v2:COLOR0, float2 v3:TEXCOORD0,
          out float4 o0:SV_POSITION0, out float4 o1:COLOR0, out float2 o2:TEXCOORD0,
          out float2 p2:TEXCOORD1,   out float3 o3:NORMAL0, out float3 o4:WORLDPOS0)
{
  float4 r0,r1,r2;
  // —— 裁剪空间位置：手工展开的 mul(v0, WorldViewProj)，按列 swizzle + MAD 链 ——
  r0 = WorldViewProj._m01_m11_m21_m31 * v0.yyyy;
  r0 = v0.xxxx * WorldViewProj._m00_m10_m20_m30 + r0;
  r0 = v0.zzzz * WorldViewProj._m02_m12_m22_m32 + r0;
  o0 = WorldViewProj._m03_m13_m23_m33 + r0;
  // —— 归一化法线 → 变换到世界空间 → 再归一化（dot/rsqrt 自展开）——
  r0.x = rsqrt(dot(v1.xyz, v1.xyz));  r0.xyz = v1.xyz * r0.xxx;
  r1.x = dot(r0.xyz, World._m00_m10_m20);
  r1.y = dot(r0.xyz, World._m01_m11_m21);
  r1.z = dot(r0.xyz, World._m02_m12_m22);
  r0.x = rsqrt(dot(r1.xyz, r1.xyz));  r0.xyz = r1.xyz * r0.xxx;  o3.xyz = r0.xyz;
  // —— 世界坐标、光向量、反射向量（R·V 高光的核心）——
  r1.xyz = World._m01_m11_m21*v0.yyy; r1.xyz = v0.xxx*World._m00_m10_m20+r1.xyz;
  r1.xyz = v0.zzz*World._m02_m12_m22+r1.xyz; r1.xyz = World._m03_m13_m23+r1.xyz; o4.xyz = r1.xyz;
  r2.xyz = LightPos.xyz + -r1.xyz;           // 一元负号：不能当成减法（见陷阱#2）
  r1.x = rsqrt(dot(r2.xyz,r2.xyz)); r1.xyz = r2.xyz*r1.xxx;
  r1.w = dot(r0.xyz, r1.xyz); r2.x = r1.w+r1.w; r1.w = max(0, r1.w);
  r1.xyz = r0.xyz * -r2.xxx + r1.xyz;        // reflect()
  // —— 高光：pow 用 log2/exp2 展开 + ColorMaterialMode 选色分支 ——
  r0.x = max(0, dot(r1.xyz, cameraPos.xyz)); r0.y = exp2(Shininess*log2(r0.x));
  r2 = cmp(ColorMaterialMode == int4(1,5,2,3));
  r1.xyz = r2.www ? v2.xyz : MatSpecular.xyz;
  r1.xyz = Specular.xyz*r1.xyz*r0.yyy;  r0.xyz = (0<r0.x) ? r1.xyz : 0;
  // —— 漫反射 + 距离衰减 + LightRadius 截断 + 环境光 + 自发光，最后 ×顶点alpha ——
  /* …Attenuation/1-over-d²、AmbientColor、MaterialEmissive 累加… */
  o1.xyz = (/*累加结果*/) * r1.www;  o1.w = 1;
  o2 = v3.xyxy;  return;
}
```

仅这一段，解释器就必须正确支持：**4 个带 `packoffset` 的 cbuffer**（`b0`–`b3`，含 `uint`/标量混排）、**矩阵按列 swizzle**（`_m01_m11_m21_m31`）与**任意 swizzle/写掩码**、**MAD 累加链顺序**、`dot`/`rsqrt`/`log2`/`exp2`/`max` 等 intrinsic、**`cmp(...)` 向量比较 + `?:` 三元选择**、**`int4` 比较与位运算**、**一元负号（`-r1.xyz`、`-r2.xxx`）不能误判成减法**（CLAUDE.md 陷阱#2），以及**多个带语义的 `out` 参数到寄存器槽位的映射**。这套语义，正是本期 §1–§5 里被用户一步步切碎、逐个驱动 AI 实现的（如 §3 的 `transpose`/`float3x3` cast）。

### 0.4 Pixel Shader：贴图采样 × 顶点色

配套的 `PS_shader.hlsl` 短，但引入了**纹理采样**这一新维度——它把 VS 算好的逐顶点光照色，与漫反射贴图的采样结果相乘：

```hlsl
SamplerState     LinearSampler_s : register(s0);
Texture2D<float4> DiffuseTexture : register(t0);

void main(float4 v0:SV_POSITION0, float4 v1:COLOR0, float2 v2:TEXCOORD0, /* … */
          out float4 o0:SV_TARGET0)
{
  float4 r0 = DiffuseTexture.Sample(LinearSampler_s, v2.xy);  // 线性采样漫反射贴图
  o0 = v1 * r0;                                               // 顶点光照色 × 贴图
}
```

这意味着最终颜色由 **VS 的光照** 和 **PS 的贴图采样** 共同决定——解释器不仅要把上面那串矩阵/swizzle/光照算对，还要正确实现 `Texture2D.Sample`（绑定解析、线性过滤、UV 寻址）。任何一环算偏，战锤的高光、底色或纹理走向就会和真机对不上——这也是这一期把它当作"标尺 case"的原因：它把验证压力同时压在 VS 光照与 PS 采样上。

## ⟳ 开发流程：人与 Agent 的交互闭环

这一期没有自动回归、没有成熟的 golden 自动对比，开发推进靠的是**人与 Agent 之间一圈圈的小步对话循环**。每一圈都很短、只动一个点，由人主导节奏、Agent 负责执行。整个流程可以概括成下面这条闭环：

```
①需求发起(人) → ②coding(Agent) → ③调试·跑+看打印(人) → ④反馈·指出现象/根因(人)
       ↑                                                                  │
       └──────────────  ⑥再调试验证(人)  ←  ⑤bug 修复(Agent/人手改)  ←──┘
```

**每一环具体是谁、做什么：**

1. **①需求发起（人）**：用户把目标切成一个**极小、可独立验证的步骤**，并尽量说全——贴样例 HLSL、给具体语句、写清期望值（见 §1）。这一期模型对信息密度敏感，留白就会被脑补，所以"发起"本身就是质量关口。
2. **②coding（Agent）**：Agent 针对这一个点写/改代码——通常是加一个 intrinsic、补一种 cast、修一处解析（如 §3 里 `transpose`、`float3x3` cast）。一次只动一个点，便于下一步定位。
3. **③调试（人）**：跑 `render.py`，**人工读打印**——`[STMT]` / 求值分支打印里每一步的操作数、操作符、结果（这套"会自述"的打印正是本期最有长远价值的产出，见 §5）。没有自动断言，对错靠人眼。
4. **④反馈（人）**：用户根据打印**描述现象、甚至直接点出根因**。这一期 Agent 常找不到深层根因（如 [#35](Prompts/hlsl-interpreter-prompt-OpenCode.html#step-35) 的"去括弧"问题由用户点破），所以反馈往往不是"这里错了"而是"**错在哪、为什么错**"。
5. **⑤bug 修复（Agent / 人手改）**：Agent 据反馈定点修正；当 Agent 修不彻底或误删代码时（如 [#37](Prompts/hlsl-interpreter-prompt-OpenCode.html#step-37) "I have to fix it by my hand"、[#42](Prompts/hlsl-interpreter-prompt-OpenCode.html#step-42) 误删 `return`），用户会**亲自动手兜底**。
6. **⑥再调试（人）**：改完重新跑、再读打印——若现象消失就推进到下一个需求，否则带着新的观察**回到 ④** 继续这一圈，直到这一小步绿掉。

> **这一期闭环的特征**：环短、频繁、**质量把关几乎全在人**——Agent 更像"快速打字员 + 局部修补匠"，需求拆分、根因判断、最终正确性都靠人兜底。这也正是第三部分要解决的痛点：把"③调试 + ④反馈 + ⑥再调试"从人眼读打印，升级为**回归套件当裁判、AI 自我纠错**的自动闭环（见第三部分 §6）。

下面 §1–§5，就是把这条闭环的关键环节——提示词、需求拆分、执行、纠错、调试——逐个展开。

## 1 · 如何给 AI 提供提示词

这一期能力有限的模型对**信息密度**格外敏感：你给得越具体（样例、字段名、期望值），它越不容易跑偏；一旦留白，它就**自己脑补**。

- **✅ 好例 —— 把"要做什么"说到可运行**：项目第一条提示词 [OpenCode #1](Prompts/hlsl-interpreter-prompt-OpenCode.html#step-1) 没有说"写个 HLSL 解释器"这种空话，而是**贴出完整样例 HLSL（`vs_main` 全文）**，并明确"先帮我实现对下列样例代码的解释执行"。给的是一个**具体、可运行的目标**，AI 第一步就有明确靶子。
- **❌ 反例 —— 留白被脑补**：[OpenCode #97](Prompts/hlsl-interpreter-prompt-OpenCode.html#step-97) 用户事后批注："**没有给具体的信息，导致解析 config 文件的字段名错误，AI 自己脑补的字段名**"。提示词没给出 depth/stencil config 的真实字段名，AI 就臆造了字段名 → 解析错误。
- **小结**：与第三部分的"可机判成功标准 + 不要做什么"相比，这一期更基础的功课是**把输入说全**——尤其是样例、字段、数据格式这类 AI 无从猜起的事实。

## 2 · 分析需求、生成开发计划

这一期的"计划"不是 AI 自己 triage，而是**用户把大目标切成极小的、可独立验证的步骤**，喂给 AI 一步步做。"找杠杆点"由人来做，AI 负责执行单步。

- **典型证据**：表达式求值器不是一次写成，而是沿一串小步**逐步长出来**——[#12 给求值每个分支加可开关打印](Prompts/hlsl-interpreter-prompt-OpenCode.html#step-12) → [#15 修 `transpose` 只执行一半](Prompts/hlsl-interpreter-prompt-OpenCode.html#step-15) → [#16 打印操作数/操作符/结果](Prompts/hlsl-interpreter-prompt-OpenCode.html#step-16) → [#19 加 `float3x3` cast](Prompts/hlsl-interpreter-prompt-OpenCode.html#step-19)。每步只动一个点。
- **为什么这样**：模型一次只能可靠地推进一小步；把需求切碎、每步产出可立刻肉眼验证，是对其能力的**适配**。这与第三部分"一步覆盖 triage→修复→回归→提交→文档整条闭环"形成鲜明对比。

## 3 · 如何按计划执行

执行单元 = "**描述一个具体现象 → 让 AI 改一个点 → 看打印验证 → 下一步**"。

- **例**：[#15](Prompts/hlsl-interpreter-prompt-OpenCode.html#step-15) 用户指出"`execute_statement` 执行 `transpose(WorldViewProj)` 时只执行了一部分"，AI 据此定点修正；[#19](Prompts/hlsl-interpreter-prompt-OpenCode.html#step-19) 用户给出具体语句 `normalize(mul(nor, (float3x3)World))` 驱动加 `float3x3` cast。
- **验证方式**：这一期还没有回归套件、没有成熟的 golden 自动对比，**验证靠人看打印**——所以每步都很小、便于肉眼判断对错。慢，但可控。

## 4 · 如何纠正 AI 的错误

这一期纠错以**重度人工介入**为特征：用户常常要直接给出真因，甚至亲自动手。下面三个真实案例，每个都拆成 **AI 的错误判断 → 用户点出的真因 → 对应代码改动** 三段——正好说明"为什么这一期离不开人"。

#### 案例一 · 找不到根因 · [#35](Prompts/hlsl-interpreter-prompt-OpenCode.html#step-35)

> 用户原话："MiniMax-M2.7 cannot find the root cause. 实际问题是 body 没有正常去除大括弧，导致无法识别语句。"

- **现象**：整段 HLSL 函数体被当成**一条语句**执行 → 执行失败。
- **AI 的错误判断**：MiniMax 认定是 `execute_main_function` 里那段**按 `;` 切分语句的字符循环写错了**（`brace_count` / `;` 判断有 bug），围着这个循环反复改，始终找不到根因。
- **用户点出的真因**：喂给循环的 `body` 字符串**没有去掉最外层的一对 `{ }`**。`body` 以 `{` 开头——循环第一个字符就让 `brace_count = 1`，直到末尾的 `}` 才归 0，中间所有顶层 `;` 都处在 `brace_count ≥ 1`，永远不触发切分，于是整段被并成一条语句。循环逻辑本身没错，**错在喂进去的数据**。
- **代码改动**：在进入切分循环**之前，先剥掉 `body` 首尾的花括号**：

```python
body = body.strip()
if body.startswith('{') and body.endswith('}'):
    body = body[1:-1]        # 去掉最外层 { }，再按 ; 切分
# 之后才是原来的 for char in body: ... 切分循环
```

#### 案例二 · 修复不彻底 · [#37](Prompts/hlsl-interpreter-prompt-OpenCode.html#step-37)

> 用户原话："MiniMax-M2.7 并不能完全修复……没有考虑到检测到 `<` 后，取 `[i-1:i+1]` 实际取出的是 ` <`，导致错误依旧。" 提交信息直接记成 *"I have to fix it by my hand"*（用户手改）。

- **现象**：`float cond = dist <= LightRadius ? 1.0 : 0.0` 里的 `<=` 被识别成 `<`，右操作数被切成 `= LightRadius`（见语法树 `Value(= LightRadius)`）→ 取不到常量、条件恒错。
- **AI 的错误判断（修了但没修对）**：MiniMax 加了"两字符运算符"判断，但**取的切片窗口错了**——用 `expr[i-1:i+1]`。当 `i` 指向 `<` 时，`[i-1:i+1]` 取出的是 `" <"`（空格 + `<`），不在运算符表里，于是仍退回把单字符 `<` 当运算符 → `<=` 没被识别，错误照旧。
- **用户点出的真因 + 手改**：两字符匹配必须**向前看** `expr[i:i+2]`（=`"<="`），而不是向后取 `[i-1:i+1]`；同时给单字符判断加护栏——**当 `expr[i:i+2]` 已是二字符运算符时，就不把单字符 `<` 计入候选**。修正后的 `_find_top_level_operator` 逻辑（见 [#39](Prompts/hlsl-interpreter-prompt-OpenCode.html#step-39) 的加注版）：

```python
if depth == 0:
    if i >= 1:
        two_char = expr[i-1:i+1]          # 以 i 结尾的两字符，如 "<="
        if two_char in self.operators:
            candidates.append((i-1, two_char, self.operators[two_char]))
            i += 1
            continue
    two_char = expr[i:i+2]                 # 向前看：i 处若是 "<=" 的开头
    if char in self.operators and not (i >= 1 and two_char in self.operators):
        candidates.append((i, char, self.operators[char]))   # 单字符，但 <= 时不计 <
```

#### 案例三 · 误删代码 · [#42](Prompts/hlsl-interpreter-prompt-OpenCode.html#step-42)

> 用户原话："MiniMax-M2.7 **错误地删除**以下语句导致 `execute_main_function` 没有返回执行结果。"

- **任务**：把 if/else 的执行改成"执行 `if` 前先检查有无 `else`，有就先合并成完整 if-else 再执行一次"。
- **AI 的错误判断**：MiniMax 在重构 if-else 合并逻辑时，**顺手误删了 `execute_main_function` 里处理 `return` 的那段**——它以为那段与本次重构无关，实则是取返回值的关键：

```python
# 被 AI 误删的语句：
if 'return' in stmt and 'output' in stmt:
    ret_val = local_vars.get('output')   # 把 output 取作返回值
    i += 1
    continue
```

- **后果 + 修复**：删掉后 `execute_main_function` 再不把 `output` 取作返回值 → **函数没有返回执行结果**。修复就是把这段 `return` 处理**原样恢复**回去。这类"改 A 却顺手删了不相关的 B"，是这一期必须由人逐行 review 兜底的典型。

#### 也有亮点

- [#45](Prompts/hlsl-interpreter-prompt-OpenCode.html#step-45) —— AI "找出 GIL 是多线程无法提速的根本，主动增加 function cache，把 executeVS 从 9 秒降到 7 秒"（用户批注"这次做得比较好"）。
- **小结**：纠错回路短而频繁，**质量把关几乎全在人**——AI 更像"快速打字员 + 局部修补匠"，深层根因与最终正确性靠人兜底。

## 5 · 如何进行调试

这一期最有**长远价值**的产出，是把解释器改造成"**会自述**"的——给求值过程加可开关的打印。正是这套打印，后来演化成第三部分赖以"沿数据流回溯根因"的 `[STMT]`/`[BINARY OP]` 轨迹（见**第三部分 · 5 · 什么输出能帮助调试**）。

- **证据**：[#12 每个求值分支加可开关打印](Prompts/hlsl-interpreter-prompt-OpenCode.html#step-12)、[#16 打印操作数/操作符/结果](Prompts/hlsl-interpreter-prompt-OpenCode.html#step-16)、[#17 用一个 bool 统一控制打印开关](Prompts/hlsl-interpreter-prompt-OpenCode.html#step-17)、[#31 优化 `log_output` 写文件（避免每条都开关文件）](Prompts/hlsl-interpreter-prompt-OpenCode.html#step-31)。
- **调试方式**：开打印 → 跑 → **人工读打印**定位哪一步的操作数/结果不对 → 改。没有自动断言，全靠"可读的中间状态 + 人眼"。这一期为整个项目的**可观测性打了地基**。

---

# 第三部分 · 基于 ClaudeCode 的开发（攻坚期）

> 这一期（约 step87 起，`Sessions/hlsl-interpreter-step*.md`）引入 zip/RunDrawFromDump 真机 capture 工作流，建立数据驱动回归套件，并系统化啃长尾。下面 10 个方面，例子取自 `Prompts/hlsl-interpreter-prompt-ClaudeCode.html` 与 `Sessions/`。

## 1 · 如何给 AI 提供提示词

提示词不是"一句话指令"，而是一个**分层的上下文系统**。本项目里有效的提示词由四层组成：

### 1.1 常驻项目说明（`CLAUDE.md`）
最重要的一层。它在每次会话开始时自动注入，相当于"团队 onboarding 文档 + 不可逾越的规则"。本项目的 `CLAUDE.md` 写清了：
- **这是什么**：纯 Python 解释 HLSL 源码、模拟 D3D11 管线、对比 golden 数据。
- **怎么运行**：`python render.py ./Cases/Default.json`。
- **核心工作流**：verify-by-log —— 跑管线、grep 日志里的 `Error:` 行、迭代到无错。
- **两个"反直觉陷阱"**：3Dmigoto 尾部 `float3` 错位、表达式里一元 vs 二元 `+/-`。
- **硬性规则**：每次改动后必须跑回归测试 `python run_regression.py`。

**关于回归测试的约束，`CLAUDE.md` 写得非常死，一字不让**——这是全篇最关键的一条纪律，原文（节译）：

> **每次代码改动后，都必须在"认为完成"之前跑回归套件。** 本项目没有单元测试，回归套件就是防止解释器/管线被改坏的安全网。
> 套件是数据驱动的：`Cases/regression_test_zip_files.csv` 逐行列出必须持续通过的 capture zip；增删覆盖面就改这个 CSV，**不要在 runner 里硬编码 case 名**。
> 一个 case **通过**当且仅当三条同时成立：① `render.py` 干净退出；② 日志里**没有以 `Error:` 开头的行**（VS-vs-golden 的分量级不符）；③ 日志的 `Total PASSED rows: X/Y` 汇总满足 **X == Y**（每一行 golden 都匹配）。
> 若某个 case 失败，打开 `Cases/regression_logs/<zip-stem>.log`、grep `Error:` 找到不符分量，**先修解释器再往下走**。

**`Total PASSED rows: X/Y` 到底是什么？用实际日志说清 X == Y**。它由 `compare_vs_output_with_golden` 在跑完 VS 后打印（`hlsl_interpreter.py`）：

- **Y = golden 的总行数**，即这次 Draw 的**顶点数**（golden VS 输出每个顶点一行）。
- **X = "整行都对"的行数**：某一行里，**每个语义字段（`SV_POSITION`/`COLOR`/`WORLDPOS`…）的每个分量**都与 golden 在 `float_tolerance` 内相等，这一行才计入 X。**任何一个分量超差，整行判失败**（并打一条 `Error:` 行），X 不加一。

一个**全通过**的真实 case（`event371`，6 个顶点）日志尾部长这样：

```
VS executed 6 vertices in 0.0028s
Comparing VS output with golden data...
Total PASSED rows: 6/6
Comparison PASSED: All output data matches golden data within tolerance
```

`6/6` → **X == Y**，6 行 golden 全部逐分量匹配 = 这个 case 绿。

反过来，一个**没修好**的真实 case（`event399` 某次修复前，696 个顶点）日志是：

```
Error: Row 0 Color[0]: output=0.000000 golden=0.364710 diff=0.364710
Error: Row 0 Color[1]: output=0.000000 golden=0.364710 diff=0.364710
Error: Row 1 Color[0]: output=0.000000 golden=1.121450 diff=1.121450
   … (每个出错分量一条 Error 行) …
Total PASSED rows: 0/696
Comparison FAILED: Some output data does not match golden data
```

`0/696` → **X ≠ Y**（这里 X=0，一行都没对上，Color 全算成 0）= 这个 case 红。回归判据要的就是 **X == Y**：**Y 是应对上的行数、X 是真正对上的行数，两者相等才说明"每一个顶点、每一个分量都和真机一致"**——差一行、甚至一行里差一个分量，`X < Y`，回归立即判红。这也和判据②（无 `Error:` 行）互为印证：只要 `X < Y`，日志里必然有对应的 `Error:` 行指出是哪一行、哪个字段、哪个分量、差多少。

这段约束把"完成"变成了**机器可判、不可赖账**的三条硬指标——AI 不能自称"修好了"，必须让 `run_regression.py` 亮绿。它还顺带堵死了两条捷径：**不许改 runner 硬编码绕过**、**失败必须回到解释器侧修**（而不是去动 golden 或放宽判据）。

> **为什么要"机器判绿"，看第二部分一个反面教训**：那一期还没有回归套件，AI 完全靠"自称"。步骤 [OpenCode #37](Prompts/hlsl-interpreter-prompt-OpenCode.html#step-37) 里，MiniMax-M2.7 **把两字符运算符的修复当成"已修好"提交了**——提交信息记的是修复 `<=` 识别问题。但它其实**没修对**：检测到 `<` 时取 `expr[i-1:i+1]` 得到的是 `" <"`（空格+`<`），`<=` 依旧被切成 `<`，`dist <= LightRadius` 照错不误。因为**没有任何机器判据拦住这次"假修复"**，错误一路溜过，直到用户亲自复跑、读打印才发现，最后提交信息只能如实记成 *"MiniMax-M2.7 fixes two char operator issue failed. I have to fix it by my hand."*（详见第二部分 §4 案例二）。
>
> **这正是第三部分立下回归铁律的原因**：把"修好了"从**AI 的一句话**，变成 `run_regression.py` 的**退出码 + `X==Y`**。同样一次"我觉得修好了"，在第二部分能蒙混过关、在第三部分会被回归当场判红——"假绿"再也过不了闸门。

> **要点**：把"踩过的坑"（两个陷阱）和"必须遵守的纪律"（回归三判据）写进常驻说明，AI 才不会反复犯同样的错、也不会自欺欺人地"假绿"。例如 C1 修复时，AI 无需被提醒就知道：去看 golden 对比日志定位分量、改完跑 `run_regression.py`、`X==Y` 才算过——这些全部来自 `CLAUDE.md`。

### 1.2 任务指令（本轮的编号清单）
用户本轮给出的提示词是一个**可执行的循环规范**，而不是模糊愿望：
```
1. 挨个修复上面列出的 issue
2. zip file 在 .../RunDrawFromDump/Dump 里
3. 如果运行的 zip 没有问题，直接运行下一个
4. 如果在 output.log 里发现 error，分析整个渲染管线，找出问题
5. 修复该问题，保证该 error 消失且无新增 error
6. 修复后执行回归测试保证通过
7. 通过后直接提交 fix，并把同类出错的 zip 选一个加入回归
8. 把思考/执行/结果写入 Sessions/hlsl-interpreter-stepN-*.md
```
这段提示词的优点：**可验证的完成标准**（无 error、回归通过）、**明确的循环边界**（一个 case 一轮）、**产出要求**（提交 + 文档 + 回归用例）。〔提示词原文：[ClaudeCode #42 继续修复](Prompts/hlsl-interpreter-prompt-ClaudeCode.html#step-42)〕

**这条"可执行循环"落到一个真实 case（C1 多数组 cbuffer）上，一轮是这样跑完的：**

```
[挑 case] 取 Dump 里下一个 zip → 跑 render.py
   ↓
[找 Error] output.log 出现:
   Error: Row0 texgen[2].x output=0.0000 golden=0.3125 diff=0.3125   ← texgen[2] 读成 0
   ↓
[定位] 沿数据流回溯:整个 texgen[] 只有 [0] 有值,[1]/[2] 全 0
        → cbuffer 数组字段的二进制重载只填了首个元素
   ↓
[修] load_all_cbuffers_from_combined_csv:按数组步长逐元素填充,而非只填 texgen[0]
   ↓
[验证] 重跑该 case → 无 Error、Total PASSED rows: N/N
   ↓
[回归] python run_regression.py → 全绿(一次修复顺带绿了 5 个同类 case)
   ↓
[提交] git commit 该 fix
   ↓
[加回归] 从这 5 个同类 zip 里挑一个,加进 regression_test_zip_files.csv
   ↓
[写文档] Sessions/hlsl-interpreter-stepN-*.md 记下现象/根因/改动/回归结果
   ↓
[下一轮] 回到 [挑 case]
```

关键在于**每一步都有机器可判的出口**：找 Error 靠 grep、验证靠 `X==Y`、回归靠退出码——AI 能自己判断这一轮是否真的完成，而不是"感觉修好了"。

### 1.3 现状/约束清单
用户把"已知未修复的长尾"列在提示词前面，并**逐条标注哪些是"capture 限制、无解"**，这样 AI **不会在死路上空转**——把精力放在可解项（C1/C2/C3），对已判定不可解的项直接跳过。这份清单摘录（原文见 [ClaudeCode #42](Prompts/hlsl-interpreter-prompt-ClaudeCode.html#step-42)）：

| 长尾项 | 状态 | 是否可解 |
|---|---|---|
| 打包 uint 八面体法线（event16215/16834） | sv_position 已修，残余 TexCoord 是更深的八面体解码 | ⏳ 未解，暂缓 |
| 四元数 typed-buffer（event2135/3542…） | 位运算已修；残余因 t3 是 `R8G8B8A8_SNORM`、t2 是 `UNORM` | ❌ **capture 未记录 view 格式，UNORM/SNORM 无法区分 → 无解** |
| raw structured buffer（event22201） | capture 根本没 dump 该 raw buffer | ❌ **能力/capture 限制 → 无解** |
| Tank 18 个超时 | 纯 Python 光栅化 ~62k 像素慢 | ⚠️ **非 bug，暂缓** |

标注"无解/非 bug"的价值在于：**它给了 AI 一个合法的"放弃"理由**。没有这份清单，AI 会对着 `R8G8B8A8_SNORM/UNORM` 这种 capture 里根本无从区分的信息反复试探、来回改代码却永远绿不了；有了它，AI 会直接记一句"capture 限制、跳过"，把预算投到真正能修的地方。

### 1.4 留痕要求（写 `Sessions/` 文档 + 填提示词历史）
任务指令的最后一条永远是"**把思考/执行/结果写入 `Sessions/hlsl-interpreter-stepN-*.md`**"。这不是走形式——留痕在本项目里同时扮演三个角色：

- **① 交付物**：每一轮修复都留下一份可核对的记录。Session 文档有固定骨架，AI 按它填：
  ```
  # Step N — <这一轮主题>
  ## Goal            这一轮要修的 case / issue
  ## Results summary  修了几个、绿了几个、剩几个
  ## Fix 1 … Fix k    每个 fix:现象 → 根因 → 代码改动 → 验证
  ## Blocked classes (honest assessment)   本轮判定无解/暂缓的项 + 为什么
  ## Regression       回归结果(X/Y)
  ```
- **② 下一轮的记忆（闭环的关键）**：本轮文档里的 **`## Blocked classes（honest assessment）`** 一节，下一轮会**原样变成 §1.3 的"现状/约束清单"**——上一轮判定"四元数 SNORM/UNORM 是 capture 限制、无解"，下一轮 AI 读到就直接跳过，不再重蹈覆辙。留痕把"这条路走不通"的结论**沉淀成跨会话的长期记忆**，否则每开一个新会话，AI 又会从零撞一遍南墙。
- **③ 提示词历史**：除了 Sessions，用户还把每轮的提示词原文按编号填进 `Prompts/hlsl-interpreter-prompt-ClaudeCode.md`（就是本手册通篇 `#N` 超链接指向的东西）。它让"当初为什么这么改"可回溯——本手册 §4/§8 的一整条 `#2→#3→#4` 证据链，就是靠它才能逐条还原。

> **一句话**：留痕文档既是这一轮的**收尾**，也是下一轮的**开局输入**——`Blocked classes` → 下轮 `现状/约束`，`Fix 根因` → 下轮遇到同类现象时的先验。写文档不是额外负担，而是让 AI **越修越聪明**的记忆机制。

### 1.5 给提示词的实操建议
- **给可验证的成功标准**，而不是"修好它"。本项目的标准是机器可判的：日志无 `Error:` 且 `Total PASSED rows: X/X`。
- **指明数据/代码在哪**（zip 目录、配置文件），省去 AI 猜测。
- **把"不要做什么"写出来**（已知无解的类别、不可改动的基准），避免无效探索与改错地方。
- **要求留痕**（写 session 文档 + 填提示词历史），既是交付物，也是下一轮的记忆（详见 §1.4）。

> **反例（缺"不要做什么"→ AI 改错了地方）**：步骤 [#2 修复前序提交引入的 bug](Prompts/hlsl-interpreter-prompt-ClaudeCode.html#step-2) 的提示词只说"请修复 Color/WorldPos 的 Error"，**没有声明"golden 数据是基准、不要动它"**。而 WorldPos 的 Error 其实源于 3Dmigoto golden CSV 的**尾部 `float3` 错位**陷阱（golden 列看着"不对"，但它就是权威基准，该修的是解释器侧的对齐）。由于提示词没有禁止改基准，AI 把矛头指向了 **golden 比较侧**——`load_vs_golden_from_mesh_csv` 里的 WorldPos 重映射一度被**注释掉（禁用）**，直到步骤 [#3](Prompts/hlsl-interpreter-prompt-ClaudeCode.html#step-3) 才不得不"**恢复被注释掉的 WorldPos golden 重映射……否则对比无法通过**"。正因为吃过这个亏，紧接着的步骤 [#4](Prompts/hlsl-interpreter-prompt-ClaudeCode.html#step-4) 才在提示词里补上明确禁令——"**不要更改 golden data 的加载函数，golden data 加载数据是正确的**"（即下文 §1.6 的好例 G1）。
>
> **教训**：当目标是"让 output 等于 golden"时，AI 天然有两条路——改解释器，或改 golden 比较侧让它"凑上"。一句"**基准是对的，只许改解释器**"就能堵死后一条错路；漏掉它，就要付出 #2→#3 这样的来回与返工。〔证据链：[#2 缺禁令](Prompts/hlsl-interpreter-prompt-ClaudeCode.html#step-2) → [#3 恢复被注释的重映射](Prompts/hlsl-interpreter-prompt-ClaudeCode.html#step-3) → [#4 补上禁令](Prompts/hlsl-interpreter-prompt-ClaudeCode.html#step-4)〕

### 1.6 好提示词 vs 差提示词：本项目的真实对照

下面三组例子全部摘自本仓库 `Prompts/hlsl-interpreter-prompt-ClaudeCode.md` 的真实提示词，可逐条核对（每例均附"提示词原文"超链接，指向对应步骤）。

#### ✅ 好例 G1 —— 贴精确证据 + 给期望值 + 限定问题域 + 排除错误路径
〔提示词原文：[ClaudeCode #4 修复执行 HLSL 得到的 WorldPos.xyz 数据不正确问题](Prompts/hlsl-interpreter-prompt-ClaudeCode.html#step-4)〕
> output.log 的打印如下：
> `Error: Row 0 WorldPos[0]: output=-61.638200 golden=11.282900 diff=72.921100`
> `[STMT] o4.xyz = r1.xyz => o4.xyz = ['-61.6382', '11.2829', '-88.1203']`
> 从语法树执行结果看 o4.xyz 错了。正确值应该是 `o4.xyz = ['11.2829', '-88.1203', '-58.05407']`。请修复，注意：
> 1. 问题看起来是解释执行 HLSL 中出现的，请修复；
> 2. **不要更改 golden data 的加载函数，golden 加载是正确的**；
> 3. 修复后跑 `python render.py ./Cases/Default.json`，读 output.log 确认还有没有 Error，有就继续修直到 VS 输出正确。

**为什么好**：① 贴出实际值**和**期望值，精确到分量（AI 一眼知道差在哪）；② 把问题域圈定在"解释执行"；③ 预先排除一条诱人但错误的修法（"别去改 golden 加载"——否则 AI 很可能为了让数字对上而去改基准，制造假绿）；④ 给出可机判的验证回路。这条提示词直接命中了 `CLAUDE.md` 里记载的"3Dmigoto 尾部 float3 错位"陷阱。

#### ❌ 差例 B1 —— 漏掉关键约束，导致 AI 猜错、用户被迫返工
第一条提示词（信息不足）〔原文：[ClaudeCode #12 实现 wireframe 绘制](Prompts/hlsl-interpreter-prompt-ClaudeCode.html#step-12)〕：
> rasterizer 只支持 solid 绘制，请根据 FillMode 实现 wireframe

AI 没有被告知"本项目 `pipeline_state.csv` 里的 FillMode 到底有哪些取值"，只能猜，于是写出：
```python
fill_mode_map = {
    'point': FillMode.POINT, 'line': FillMode.LINE, 'solid': FillMode.SOLID,
    '0': FillMode.POINT, '1': FillMode.LINE, '2': FillMode.SOLID,
}
```
用户不得不再发**一条纠正提示词**返工〔原文：[ClaudeCode #14 fill mode map 错误](Prompts/hlsl-interpreter-prompt-ClaudeCode.html#step-14)〕：
> fill_mode_map 不应该是这些选项，而应该是 Wireframe 和 Solid，相应的 rasterizer 也要按这两种 fill mode 光栅化。

**代价**：多一轮往返 + 一次返工。**改进版（一次说清）**：
> 请实现 wireframe 光栅化。注意 D3D11 的 FillMode 只有 `Wireframe` 和 `Solid` 两种取值（`pipeline_state.csv` 里就是这两个字符串），按这两种分派即可。

> **要点**：差不在"语气"而在"信息密度"。B1 缺的是**领域约束**（取值集合）——凡是 AI 需要靠猜才能填的空，就该在提示词里直接给死。

#### ✅ 好例 G2 vs ❌ 差例 B2 —— 同一个 bug，两种问法
| | 提示词 | 结果 |
|---|---|---|
| ❌ B2（设想的差版） | "颜色不对，请修一下 VS。" | AI 不知道哪个 case、错多少、对照什么；只能反复试探，极可能改错地方。 |
| ✅ G2（真实采用版，[原文 ClaudeCode #2](Prompts/hlsl-interpreter-prompt-ClaudeCode.html#step-2)） | 贴出 6 行精确报错（`Error: Row 0 Color[0]: output=1.640544 golden=0.431490 diff=1.209054` …），并写明"跑 `python render.py ./Cases/Default.json` 后读 `Cases/output.log`，确认还有没有 Error，有就继续修直到 VS 输出正确"。 | AI 立刻定位到 Color/WorldPos 分量，沿数据流回溯根因，一轮收敛。 |

> G2 的精髓是把"对不对"变成**机器可判的布尔**（无 `Error:` 行）——既是给 AI 的目标，也是 AI 自查的依据，无需人来回判读。

#### 一句话总结
**好提示词 = 精确证据（实际值+期望值/报错原文）+ 可机判成功标准 + 数据/代码位置 + 明确的"不要做什么"。** 差提示词的通病不是"太短"，而是**把需要确定性的地方留给 AI 去猜**（缺取值集合、缺基准、缺验证方式）——猜错的代价就是额外的返工轮次。

---

## 2 · 分析需求、生成开发计划

面对"挨个修复 issue"这种开放任务，AI 不会盲目动手，而是先**侦察（triage）→ 聚类 → 排序 → 立计划**。（对比第二部分：那里"切碎需求、找杠杆点"由人做；这里 AI 自己就能 triage 数百个 case 并按根因聚类。）

### 2.1 侦察：把"未知"变"已知"
本轮 Dump 目录有 355 个 zip，其中 witcher3_countryside 有 160 个、仅 31 个在回归里、**129 个从未单独验证**。AI 写了一个并行 triage 脚本（`scratchpad/triage.py`）：把每个 zip 跑成 headless 管线、按日志分类为 `PASS / FAIL / CRASH / NOCMP / TIMEOUT`。

### 2.2 聚类：把 129 个失败归成"问题类"
triage 结果不是 129 个独立 bug，而是几个**根因类**：
| 类别 | 代表 case | 处置 |
|---|---|---|
| sv_position/TexCoord 读成 0 | 23341/23141/23251/23183/23195 | **一个 bug（C1）一次修 5 个** |
| 大气输出错 | 20899 | C2 |
| (int) 强转 / 顶点纹理取样 | 16834/16215 | C3 + 受阻 |
| 打包八面体法线 | 16215/16834 | 受阻（纹理基础设施） |
| raw buffer 未 dump | 22201 | 受阻（capture 限制） |
| 慢绘制 | ~26 + Tank | perf，非 bug |

> **关键洞察**：聚类让"129 个失败"坍缩成"少数几个根因"。修一个根因（C1）就让 5 个 case 同时变绿。这是 AI 制定计划时最有价值的一步——**先找杠杆点**。

### 2.3 排序：先做"高确定性、低风险"的
AI 用 `TodoWrite` 把计划落成可勾选清单，并按 ROI 排序：
1. 多数组 cbuffer（C1）——根因清晰、影响 5 个 case、低风险 → **先做**。
2. FTZ（C2）——根因清晰、影响 1 个 case → 做。
3. ftoi（C3）——正确性修复、需谨慎验证不破坏既有 → 做。
4. 八面体/raw buffer/SNORM/超时——高成本或不可解 → **诚实评估后暂缓**。

这套"triage → 聚类 → 按杠杆排序 → 留痕"就是 AI 的开发计划生成法。

---

## 3 · 如何按计划执行

每个 case 走同一个**闭环**（与用户提示词第 3–8 步对应）：

```
运行 → 读日志找 Error → 顺着管线定位根因 → 改代码 →
单 case 验证 → 跑回归 → 提交 → 把代表 case 加进回归 → 写文档 → 下一个
```

### 以 C1（多数组 cbuffer）为例逐步拆解
1. **运行 + 发现**：event23341 报 `Error: Row 0 TexCoord[0]: output=0.000000 golden=0.129650`。输出恒为 0 → 某个量没读进来。
2. **顺管线定位**：读 `VS_shader.hlsl` 发现 `o1.x = dot(v1, texgen[0])`；`texgen` 是 `float4 texgen[2] : packoffset(c2)`。再看 `VS_constant_buffers.csv` —— 组合 CSV 每个数组名只存一个代表值，没有 `texgen_v0/_v1` 键 → CSV 把数组加载成 `[None,None]`。再看二进制重载 `override_cbuffers_from_binary`：
   ```python
   for field in cb_def.fields:
       if field.array_size > 0:
           field.data = decoded[:m]
           break   # ← 只填第一个数组就退出！
   ```
   `mvp[2]@c0` 拿到 `decoded[0:2]`（对），循环 break，`texgen[2]@c2` 仍是 `[None,None]` → 读成 0。**根因锁定**。
3. **改代码**：给 `FieldDefinition` 加 `reg_offset`、解析 `packoffset(cN)`、新增 `_field_register_offsets()`，重载循环改为给**每个**数组按各自寄存器窗口 `decoded[reg_off:reg_off+size]` 填充。
4. **单 case 验证**：event23341 `0 → 30/30`，再验 23141/23251/23183/23195 全过。
5. **跑回归**：44/44，确认未破坏既有。
6. **提交 + 扩充回归**：把 event23341 拷进 `Cases/` 并加入回归 CSV，提交 `1716ee5`。
7. **写文档**：`Sessions/hlsl-interpreter-step119-*.md`。

> 执行的精髓是**"沿数据流回溯"**：从错误的输出值（0），逆着 `o1 ← texgen ← cbuffer 加载 ← 二进制重载` 一路找到那行 `break`。这要求 AI 能读懂整条管线，而不是只盯着报错那一行。

---

## 4 · 输入 / 输出数据流

### 4.1 输入数据流（AI 看到什么）
```
┌─────────────────────────────────────────────┐
│  常驻上下文：CLAUDE.md（项目规则、陷阱、纪律）   │
│  记忆：MEMORY.md 索引 + 召回的 memory 文件      │
│  用户提示词：任务清单 + 约束 + 现状             │
└───────────────┬─────────────────────────────┘
                │ 每一轮工具调用后追加：
                ▼
┌─────────────────────────────────────────────┐
│  工具结果：文件内容(Read)、搜索命中(Grep)、     │
│  命令输出(Bash：render.py 日志、回归摘要)、     │
│  system-reminder（后台任务完成、文件被改）      │
└─────────────────────────────────────────────┘
```
AI 的"输入"是**累积的对话上下文**：项目说明、记忆、用户指令打底，之后每次工具调用的返回值（源码、日志、命令输出）不断追加。上下文过长时会被**摘要**后进入下一窗口。

上图最顶端的两层——**`CLAUDE.md`（常驻规则）**与 **`MEMORY.md`（跨会话记忆）**——是每轮开发都会被自动注入的"打底上下文"。它们具体装了什么、如何影响 AI 的行为，值得单独拆开看（这也是本项目让 AI"越修越对、越修越聪明"的两块基石）。

#### 4.1.1 `CLAUDE.md` 具体写了什么：项目规则 / 陷阱 / 纪律

`CLAUDE.md` 在**每次会话开始时整篇注入**，且带有最高优先级——它明确声明"这些指令覆盖任何默认行为，必须逐字遵守"。它的内容可归成三类：

| 类别 | 常驻内容（本项目 `CLAUDE.md` 实录） | 对 AI 行为的约束 |
|---|---|---|
| **① 项目规则（是什么 / 怎么跑 / 怎么组织）** | · **定位**：纯 Python 解释 HLSL 源码、模拟 D3D11 管线、与 golden 逐分量对比；**零第三方依赖**、无 `requirements.txt`/构建/lint/单测。<br>· **入口**：`python render.py ./Cases/Default.json`（单个 JSON 配置驱动，zip 工作流为主路径、legacy struct 工作流仅后向兼容）。<br>· **模块分工**：`render.py` 编排；`hlsl_interpreter.py`(~3300 行核心)；`hlsl_syntax_tree.py`(**运算符优先级表 `_OPERATORS` 是唯一真源**)；`rasterizer.py`/`output_merger.py`/`texture.py`。<br>· **数据文件约定**：zip 内每个 CSV/HLSL/BMP/bin 的作用与解析函数。<br>· **verify-by-log 工作流**：跑管线 → grep 日志里 `Error:` 行 → 迭代到无错；`[STMT]`/`[SYNTAX TREE]` 轨迹是主调试面。 | AI 无需重新摸索项目结构，改哪个文件、看哪个日志、动优先级要去哪张表，开局即知 |
| **② 陷阱（反直觉、极易改错的点）** | · **陷阱#1 · 3Dmigoto 尾部 `float3` 错位**：`MeshOut_*_mesh.csv` golden 把尾部 `float3`（如 WORLDPOS）整体偏移一个 float，`WORLDPOS.x` 实际存的是 `o.y`……**golden 看着"不对"但它就是权威基准**，该改的是解释器侧对齐。<br>· **陷阱#2 · 一元 vs 二元 `+`/`-`**：`r0.xyz * -r2.xxx + r1.xyz` 里 `*` 后的 `-` 是一元负号、不能当减法；bitwise `\|`/`&` 也有特判。**precedence 错在这里表现为错色/错向量而非崩溃**。<br>· 附带提醒：`.vscode/tasks.json` 是无关旧项目的残留、要忽略；CSV 里的 topology 覆盖 JSON（除非显式设置）。 | AI 面对"golden 看起来错了"的诱惑时，**不会去改基准凑数字**；解析一元负号时**不会误判成减法**——这两个坑在第二/三部分都真实反复出现过 |
| **③ 纪律（不可逾越的硬规则）** | · **每次代码改动后、"认为完成"之前必须跑 `python run_regression.py`**——无单测，回归套件就是唯一安全网。<br>· **通过 = 三条同时成立**：① `render.py` 干净退出；② 日志无 `Error:` 行；③ `Total PASSED rows: X/Y` 满足 `X==Y`。<br>· **数据驱动**：增删覆盖面只改 `Cases/regression_test_zip_files.csv`，**不许在 runner 里硬编码 case 名**。<br>· **失败必须回到解释器侧修**，不许动 golden 或放宽判据。 | 把"修好了"从 AI 的**一句话**，变成 `run_regression.py` 的**退出码 + `X==Y`**——AI 不能自称完成、也堵死了"改基准/绕 runner"两条捷径（详见 §1.1） |

> **一句话**：`CLAUDE.md` = "团队 onboarding 文档 + 不可逾越的规则"。规则让 AI **少猜**，陷阱让 AI **少犯同一个错**，纪律让 AI **不能自欺欺人地"假绿"**。

**举例 · "模拟 D3D11 管线"这条项目规则如何直接指导修 bug** —— 因为 `CLAUDE.md` 开宗明义写明"本项目是 **D3D11 图形管线的软件仿真**"，AI 修 bug 时就有了一个**明确的"正确性权威"：D3D11 官方规范本身**。"什么才算对"不靠猜、不靠拼凑，而是**去查 D3D11 规范里对应算法的硬性要求**，让代码逐条对齐。[step101](Sessions/hlsl-interpreter-step101-fix-rasterizer-coordinate-snapping-and-top-left-rule.md) 就是典型：

> **现象**：光栅化时相邻三角形的**共享边被两个三角形重复绘制**（overdraw），画面在三角形接缝处出现瑕疵。
>
> **依 D3D11 规范定位根因**：AI 把 `rasterizer.py` 的三角形遍历**逐条对照 D3D11 光栅化规范**，发现两处不符合：
> - **违反 §3.4.1 坐标吸附（Coordinate Snapping）**：规范要求顶点 x/y 在透视除法 + 视口变换后吸附到 `n.8` 定点（8 位小数 = 1/256 子像素栅格），且覆盖测试要取**像素中心** `(x+0.5, y+0.5)` 采样；而原代码用 `int(...)` 直接截断到整像素、并在**像素角点**求边函数——等于完全没有子像素精度。
> - **违反 §3.4.2.1 左上填充规则（Top-Left Rule）**：规范规定，采样点**正好落在边上**时，只有当该边是**上边或左边**才算被覆盖，以此保证相邻三角形不重复画共享边；而原代码三条边函数一律用 `>= 0`（inclusive），共享边对两个三角形都通过 → 正是该规则要防的 overdraw。
>
> **依规范修复**：新增 `transform_to_screen_subpixel`（`round(v*256)/256` 做 1/256 吸附，替代截断）；覆盖测试改为在像素中心采样；实现一个**与绕序无关**的 Top-Left 判定（按有符号面积归一化后，`is_top = D.y==0 and D.x<0`、`is_left = D.y>0`）。
>
> **依规范验证**：把一个 8×8 quad 沿对角线拆成两个三角形分别光栅化，比较覆盖像素集——**共享边重复覆盖 = 0，并集 = 64 = 8×8**，证明共享对角线恰好由一个三角形绘制、无缝无叠。回归 6/6 仍全绿。

> **要点**：这正是"项目定位"写进 `CLAUDE.md` 的价值——它把一个模糊问题（"接缝为什么有瑕疵"）转成一个**有权威答案的问题**（"D3D11 §3.4.2.1 是怎么规定边覆盖的"）。凡是本项目在仿真的 D3D11 行为（光栅化边规则、透视正确插值、深度裁剪、float→int 的舍入、纹理寻址模式、FTZ 反规格化……），bug 的判据都不是"看起来对不对"，而是"**和 D3D11 规范/真机一致不一致**"。

#### 4.1.2 `MEMORY.md` 记忆了什么：帮助 AI 开发的经验

如果说 `CLAUDE.md` 是"写死的规则"，`MEMORY.md` 就是**会生长的经验库**——它是 `~/.claude/projects/.../memory/` 下"**一事一文件 + 一个索引**"结构的索引层，会话开始时**全量载入索引**，相关经验在需要时被**召回**（recall）。它记的是那些**"非显而易见、且未来还会再用到"的事实**，典型有四类：

| 记忆类型（frontmatter `type`） | 记的是什么 | 举例（本项目已有 / 可有的记忆） | 帮 AI 省掉的重复劳动 |
|---|---|---|---|
| **project（项目知识）** | 从代码/git 看不出的领域坑与数据格式约定 | `golden-vs-mesh-sv-position-first`（golden CSV 把 SV_Position 排最前、按位置而非表头映射）；`witcher3-array-cbuffers-instanced-inputs`（数组 cbuffer + 实例化输入的加载坑） | 下次遇到同类 case 不必重新推导列序 / 逆向格式 |
| **reference（外部资源指针）** | 特定格式的解码方式、外部资料 | `per-vertex-binary-vb-decode-r10a2`（NORMAL/TANGENT 在二进制 VB 里是 R10G10B10A2）；`raw-img-texture-loading`（纹理按 DXGI 格式从 `.img` 读、BC7 回退 BMP） | 直接复用解码路径，不重造轮子 |
| **feedback（用户的工作方式纠偏）** | 用户给过的"该怎么做"的指导 + **为什么** | 例如"golden 是基准、只许改解释器"（§1.5 教训沉淀）；"已知 capture 限制的类别别再试探" | 不再重蹈"改错地方"的返工（如 §1.5 的 #2→#3） |
| **user（用户是谁）** | 角色、偏好、专长 | 用户关注 VS 计算正确性、验证靠 golden、偏好 verify-by-log | 交流与产出对齐用户习惯 |

每条记忆的正文遵循固定骨架：**事实本身 + `**Why:**`（为什么重要）+ `**How to apply:**`（下次怎么用）**，并用 `[[其它记忆名]]` 相互链接成网。

上面表格里的名字太浓缩、单看容易不知所云。下面把 **project** 和 **reference** 各挑一条，按"踩到什么坑 → 记忆里到底写了什么 → 下次怎么帮上忙"完整展开，就能看清一条记忆长什么样、怎么省事：

**例① · project 类 · `golden-vs-mesh-sv-position-first`**

- **踩到什么坑**：验证 VS 输出要拿解释器的结果去和 golden CSV（`MeshOut_*_mesh.csv`）逐列对比。直觉做法是"按 CSV 表头名字对齐列"——但 3Dmigoto 导出的 golden CSV **会把 `SV_Position` 列强制排到最前面**，不管 shader 里 `out` 参数原本声明成什么顺序。若按"表头顺序 = shader 声明顺序"去映射，整张表**列序就整体错位**，每个分量都对不上、满屏 `Error:`，还很容易误以为是解释器算错了而去乱改数学。
- **记忆里写了什么**（按骨架）：
  ```
  事实：golden mesh CSV 把 SV_Position 列排在最前，与 shader 声明顺序无关。
  Why ：按表头顺序映射会整体错位 → 假 Error，误导去改本来正确的 VS 数学。
  How to apply：对齐 golden 时按【语义/位置】映射，先把 SV_Position 认到第 0 列，
                 其余列再顺次对上；参见 [[trailing-float3-golden-misalignment]]。
  ```
- **下次怎么帮上忙**：再遇到任何 witcher / Octopath 等 case，AI 一召回这条记忆，就**直接按位置映射**、不再重新推导列序，也不会把"列序错位"误诊成"数学错误"去改解释器。

**例② · reference 类 · `per-vertex-binary-vb-decode-r10a2`**

- **踩到什么坑**：某些 case 的法线/切线在 CSV 里读出来是 0 或明显不对。根因是这些属性在**二进制顶点缓冲**里不是普通的 3×float，而是被打包成 **`R10G10B10A2` 格式**——一个 32 位里塞 R/G/B 各 10 位、A 占 2 位的归一化整数。不知道这个格式，就会按 float 去读那 4/12 个字节，解出一堆垃圾；而 `ia_vertex_data.csv` 对这种格式往往直接存成 0（有损），光看 CSV 根本发现不了。
- **记忆里写了什么**（按骨架）：
  ```
  事实：这批 case 的 NORMAL/TANGENT 在二进制 VB 里是 R10G10B10A2 打包格式（非 3×float）。
  Why ：按 float 解会得到垃圾；CSV 对该格式存 0，只有 raw VB 才有真值。
  How to apply：从 vb_slot*.bin 按 R10G10B10A2 拆位解码（10/10/10/2 → 归一化），
                 再喂给 VS；raw 优先、CSV 仅回退。参见 [[binary-vb-raw-precision]]。
  ```
- **下次帮上忙**：这条记忆记的是**一个可复用的解码配方**——下次再碰到同格式的顶点属性，AI 直接套这套"按位拆 10/10/10/2 再归一化"的解码，不必重新逆向一遍字节布局（reference 类的价值就在于"配方可直接复用、不重造轮子"）。

> **两个例子的区别**：`golden-...` 记的是**本项目特有的数据约定**（列序陷阱），属 **project**；`...decode-r10a2` 记的是**一种通用格式的解码方法**（换个项目也能用），属 **reference**。前者防"踩同一个坑"，后者省"重写同一段解码"。

> **记忆如何帮助开发（闭环）**：`Sessions/` 文档里每轮的 `## Blocked classes` 与 `Fix 根因`（见 §1.4）是"这一轮的收尾"，而把其中**跨会话仍成立的结论**提炼成一条 `MEMORY.md` 记忆，就成了"未来所有轮次的开局先验"。这样**同一个坑只踩一次**——golden 列序错位、SNORM/UNORM capture 无解这类反直觉点，一旦入记忆，后续 case 直接命中、不再从零困惑。
>
> **记忆的三条纪律**（与 §9 一致）：① 不记录代码/git 已经记录的东西（结构、历史、CLAUDE.md 已写的）；② 先查重再写，宁可更新已有文件、错的要删；③ 召回的记忆是"**当时为真**"的背景，引用到具体文件/函数时**要先核实其仍存在**。

> **两者的分工**：`CLAUDE.md` 管**"绝不能违反的"**（规则/陷阱/纪律，人工维护、整篇注入）；`MEMORY.md` 管**"最好别忘的"**（经验事实，随开发生长、按需召回）。前者防 AI 越界，后者防 AI 重复劳动——合起来构成 §4.1 输入数据流最顶端那层"打底上下文"。§9 会从"避免重复开发"的角度再展开记忆系统。

### 4.2 输出数据流（AI 产出什么）
AI 的每条输出要么是**给用户的文字**，要么是**工具调用**：
- `Read/Grep/Glob` —— 获取信息（无副作用）。
- `Edit/Write` —— 修改源码 / 写文档（有副作用）。
- `Bash` —— 跑 `render.py`、`run_regression.py`、git。
- `TodoWrite` —— 维护计划清单。

### 4.3 项目自身的数据流（被解释的对象）
AI 操作的"被测系统"也有自己的数据流，理解它才能定位 bug：
```
zip(capture) → 解压 → CSV/HLSL/BMP/bin
   ├─ ia_vertex_data + 二进制 VB ─┐
   ├─ cbuffers(CSV) + constant_*.bin ─┤
   └─ VS_shader.hlsl ──────────────┤
                                    ▼
        VertexShader 解释执行 ──→ VS 输出 ──→ 与 golden(*_vs_mesh.csv) 对比
                                    │
                                    ▼
        Rasterizer → Depth/Stencil → PixelShader → OutputMerger
```
C1 的 bug 就在"cbuffers + constant_*.bin → VS 执行"这一段；C2 在"constant_*.bin 解码"；C3 在"二进制 VB → VS 执行的强转"。

### 4.4 逐步加厚 capture：用更多 draw dump 攻克长尾 bug

§5 讲的是"让**被测系统**多吐中间状态"；这一节讲的是它的**输入侧对偶**——**让 capture 多 dump 一层真机数据**。最早的 capture 只有"四舍五入的 CSV + 转码后的 BMP"，很多长尾 bug 在这层有损表示下**根本看不见**，只能标成"capture 限制、无解"。攻坚期的关键一招，就是**逐步把整个 draw 的原始数据全都 dump 下来**——每加一类数据，就把一类"无解"变成"可修"。

> **核心洞见**：CSV / BMP 是 GPU 真正吃进去的数据的**有损重编码**。凡是只在**位级**才暴露的 bug（反规格化数、精确整数格式、精确纹素），你不把 GPU 当初读到的**同一份原始字节**喂给解释器，就永远看不到它。所以 `render.py` 的策略是——**同名数据里 raw 优先、CSV/BMP 仅作旧 capture 的回退**（源码注释：*"prefer raw binary captures for maximum precision … ia_vertex_data.csv uses rounded floats and may store some formats as zeros"*）。

| dump 的数据 | 文件（capture 内） | 取代了什么有损来源 | 解锁的修复 / 用途 | 步骤 |
|---|---|---|---|---|
| **Pipeline statistics** | `pipeline_statistics.csv`（`RawCounter,Value`） | —（新增的真机每阶段计数基准） | ① 可机判地对比 `VSInvocations` / `SamplesPassed`（后者带容差）；② **用真机 `IAPrimitives` 判定三角形 LIST vs STRIP**——`pipeline_state.csv` 里的 topology 枚举不可靠（把 list 谎报成 STRIP），真机图元数才是 ground truth | [step90](Sessions/hlsl-interpreter-step90-compare-pipeline-statistics-and-ps-output.md) · [step95](Sessions/hlsl-interpreter-step95-samples-passed-tolerance.md) · [step100](Sessions/hlsl-interpreter-step100-fix-triangle-topology-list-vs-strip.md) |
| **VS / GS / DS mesh-out** | `*_vs_mesh.csv`，及更精确的 `*_vs_mesh.bin` + `*_vs_mesh_layout.csv`（`find_stage_mesh_dump` 同样支持 `gs`/`ds`） | 取代 CSV golden 的**尾部 `float3` 错位**陷阱（CLAUDE.md 陷阱#1） | bin+layout 形式**显式 stride/type/order**，无 SV-Position 重排、无 uint 列位重解释、无尾部 `float3` 错位——是整个"verify-by-log"工作流的**权威基准（oracle）** | 见 CLAUDE.md 陷阱#1 |
| **原始格式 texture/image** | `*.img`（raw DXGI）伴随 `*.bmp` | 取代有损转码的 BMP（丢精度/通道/格式信息） | 按真机 **DXGI 格式精确解码纹素**，修复 `Texture2D.Sample` 采样不匹配（含 event1399 的 vector-load hazard） | [step113](Sessions/hlsl-interpreter-step113-raw-img-texture-loading.md) · [step114](Sessions/hlsl-interpreter-step114-texture-load-and-dxbc-vector-load-hazard-event1399.md) |
| **Raw 顶点缓冲（VB）** | `vb_slot*.bin` | 取代 `ia_vertex_data.csv`（浮点已四舍五入，某些格式甚至存成 0） | 拿到**精确位型**的逐顶点属性 + 逐实例输入，修复精度长尾（Witcher countryside） | [step117](Sessions/hlsl-interpreter-step117-witcher-countryside-event6977-8775-binary-vb-and-multioutput.md) · [step121](Sessions/hlsl-interpreter-step121-binary-input-data-precision.md) |
| **Raw 索引缓冲（IB）** | `ib_res_*.bin` | 取代 CSV 的 `IDX` 列 | 得到**真实 draw 索引序列**，并据此还原 `SV_VertexID`（否则每个顶点都读元素 0，如 Octopath 的 `Buffer<float4>` texcoord 表） | [step118](Sessions/hlsl-interpreter-step118-rundrawfromdump-octopath-tank-triage-and-fixes.md) |
| **Raw 常量缓冲（CB）** | `constant_*.bin` | 取代 CSV 里已四舍五入的常量 | 揭示被 CSV 抹平的 **subnormal**（event20899：`cb12[271].z` 的字节是 `0x00000001` = `1.4e-45`），根因是 GPU 的 FTZ 让三元分支走 false、解释器却因保留 denormal 走错分支（**C2**）；另支持 Witcher 多数组 cbuffer 的二进制覆盖 | [step119](Sessions/hlsl-interpreter-step119-witcher-multi-array-cbuffer-binary-override.md) · [step120](Sessions/hlsl-interpreter-step120-ftz-denormal-and-ftoi-cast-and-longtail-assessment.md) |
| **pre/post-draw 渲染目标 & 深度模板** | `*_res_*_<W>x<H>_<FORMAT>.raw`（+ `.png` 预览）、`diff_ps_output_rt0.csv` | 取代无字节级基准的状态 | 加载真机 **pre-draw 深度缓冲**做 early-Z / late-Z 对照，并对**输出合并像素（颜色+深度）逐像素比对** golden RT0 | [step94](Sessions/hlsl-interpreter-step94-load-pre-draw-depth-buffer.md) · [step122](Sessions/hlsl-interpreter-step122-load-pre-draw-depth-stencil-from-raw.md) |

**一条把它讲透的证据链——C2 / event20899。** 这个 bug 在 [step119](Sessions/hlsl-interpreter-step119-witcher-multi-array-cbuffer-binary-override.md) 一度被判成"大气数学的精度长尾、不可解"。真相是：常量 `cb12[271].z` 是个反规格化数（denormal），GPU 按 **FTZ** 当 `0`、三元 `cb12[271].z ? r2.x : 1` 走 false 分支；解释器保留了非零 denormal，走了 true 分支，污染大气散射的 `o0/o1`。**但这个根因在 CSV 里是看不见的**——CSV 把常量四舍五入后，`1.4e-45` 和 `0` 长得一模一样。只有 dump 出 `constant_*.bin`、把 `cb12[271].z` 的**原始字节**解码成 `0x00000001`，才第一次看见"它其实是个非零 subnormal"。加厚 dump（拿到 raw CB）**+** 加厚被测系统的自述（逐分支轨迹，§5.4）两件事叠加，才把 [step120](Sessions/hlsl-interpreter-step120-ftz-denormal-and-ftoi-cast-and-longtail-assessment.md) 的"一行修复"变成可能。

> **方法论**：当一个 bug 反复被判"精度/capture 限制、无解"时，先别信——**问一句"我喂给解释器的，是不是 GPU 当初读到的同一份原始字节？"** 很多时候答案是"不是，是被 CSV/BMP 有损重编码过的"。把那一层 dump 成 raw，"无解"往往就变成"可修"。这也正是 §1.3"现状/约束清单"里那些"无解项"需要**定期复核**的原因——它们中的一部分，只是**当时的 dump 还不够厚**。

---

## 5 · 什么输出能帮助调试

**结论：能把"中间状态"暴露成可 grep 文本的输出，最有价值。** 本项目刻意把解释器做成"会自述"的：

### 5.1 逐语句 / 逐运算的执行轨迹
开启 `print_interpreter_result` 后，日志逐条打印：
```
[STMT] Executing: r0.xy = (int2)v1.zw
[STMT] r0.xy = (int2)v1.zw => r0.xy = [1073741824, 1073741824]   ← C3 的 smoking gun
[METHOD] t1.Load((1073741824, 0, 0)) = ['0.0000','0.0000','0.0000','0.0000']  ← 越界读 0
[BINARY OP] left=['312.5','500.0'], right=['0','0'], op=/, result=['inf','inf'] ← 0/0
```
**正是这条轨迹**让 C3 一眼可见：`(int2)v1.zw` 把 `2.0` 变成 `1073741824`（=2.0 的位型），而不是 `2`。没有这条输出，只看到最终 `sv_position = nan` 是无从下手的。

### 5.2 分量级的对比报错
```
Error: Row 0 TexCoord2[0]: output=2.466900 golden=0.679580 diff=1.787320
```
精确到"第几行、哪个语义、哪个分量、输出值 vs golden 值 vs 差值"。C1/C2 的定位都从这里起步。

### 5.3 可机判的汇总
```
Total PASSED rows: 0/2160   →（修复后）2160/2160
```
让"是否修好"成为一个布尔判断，供回归脚本直接 gate。

### 5.4 C2 的调试全靠轨迹
event20899 一开始被（step119）误判为"精度/不可解"。step120 重新打开逐语句轨迹，把 o0/o1 一路回溯到 `cb12[271].z ? r2.x : 1` 这个分支选错，再发现 `cb12[271].z` 的字节是 `0x00000001`（反规格化数）。**没有逐语句 + 逐分支的轨迹输出，这个根因不可能被发现。**

> **设计准则**：让被测系统打印"每一步的输入、运算、输出"，并保证这些行可被 `grep Error:` / `grep '\[STMT\]'` 提取。可观测性 = 可调试性。

---

## 6 · 回归测试的建立与应用

### 6.1 建立
本项目**没有单元测试**，回归套件就是安全网。它是**数据驱动**的：
- [`Cases/regression_test_zip_files.csv`](../Cases/regression_test_zip_files.csv) 列出必须保持通过的 capture zip（一行一个）。
- `run_regression.py` 读这个 CSV，逐个 headless 跑 `render.py`，每个写一份 `Cases/regression_logs/<name>.log`，最后打印 PASS/FAIL 汇总，任一失败则退出码非零（可 gate CI）。
- **通过判据**（三条全满足）：① `render.py` 干净退出；② 日志无 `Error:` 行；③ `Total PASSED rows: X/X`（X==Y）。

### 6.2 在开发流程中的应用
- **每次改动后必跑**（`CLAUDE.md` 硬性规则）。C1 改完跑出 44/44、C2 跑出 45/45（含新加的 20899）、C3 跑出 46/46。
- **每修好一类，就把代表 case 加进回归**。本轮新增 `event23341`（多数组代表）和 `event20899`（FTZ 代表），让这两类**永不回退**。
- **回归是"提交闸门"**：只有回归绿了才提交。C3 是个"改变既有强转语义"的高风险改动——正是回归 46/46 才让它敢提交。

> **FTZ = Flush-To-Zero（反规格化数刷零）**：GPU 做浮点运算时把极小的非零浮点（denormal，如 `0x00000001` 按 float 解释成 `1.4e-45`）直接当 `0`。`event20899` 里常量 `cb12[271].z` 正是这种值，被用作三元条件 `cb12[271].z ? r2.x : 1`——GPU 视其为 0 走 false 分支，解释器却因保留了非零 denormal 走错分支、污染大气散射 o0/o1。修复：解码 cbuffer 时把 subnormal 刷成 `0.0`（`asint`/`asuint` 不受影响，仍读精确整数位）。详见 §5 / §8。

> 注意：回归 CSV 与 zip 本身是 **gitignore** 的（本地基础设施），所以提交里只含代码 + 文档；回归集在本地累积。

---

## 7 · TDD 在本项目的应用

经典 TDD 是"先写失败测试，再写实现使其通过"。本项目是 **TDD 的一种变体——"golden 即测试"**：

- **测试先于实现存在**：每个 capture zip 自带 GPU 真机跑出的 `*_vs_mesh.csv`（golden）。它就是"期望输出"，在 AI 动手前就已存在且必然正确。
- **红 → 绿循环**：
  - **红**：跑 event20899 → `Total PASSED rows: 0/2160` + 一堆 `Error:`。
  - **绿**：实现 FTZ 修复 → `2160/2160`，无 `Error:`。
  - **重构/防回归**：把该 case 加进回归集，确保未来任何改动都得让它保持绿。
- **失败先驱动定位**：和 TDD 一样，**先让失败可复现、可量化**（哪一行哪一分量差多少），再写最小修复。C1 就是先确认 `texgen` 读成 0（红），再精确改二进制重载循环（绿）。

> 与单元 TDD 的差别：这里的"测试用例"不是手写断言，而是**真机 golden 数据**；"测试框架"是 verify-by-log + 回归脚本。但红绿节奏、"测试驱动最小实现"、"测试防回归"三大要义完全一致。

### 7.1 一个"AI 自己写测试"的例外：`depth clip`（step91）

绝大多数轮次都靠 golden + 回归当裁判。但有一个**回归和 golden 都盖不到**的角落，AI **主动手写了自己的 TDD 用例**——[step91 实现完整 D3D depth clip](../Sessions/hlsl-interpreter-step91-implement-depth-clip.md)〔提示词原文：[ClaudeCode #20 实现 depth clip](Prompts/hlsl-interpreter-prompt-ClaudeCode.html#step-20)〕。

- **为什么 golden 失灵**：回归集里的每个真机 capture **都整幅落在屏幕内**，永远不会跨近/远平面，也没有相机背后（`w < 0`）的顶点。于是新写的几何裁剪代码（三角形切分、`w<0` 剔除、点/线深度重映射）**无论对错，golden 都是全绿**——真机数据对这段新逻辑完全"沉默"。回归 6/6 PASS 只能证明"没改坏既有路径"，**证明不了新功能是对的**。
- **AI 的应对**：既然没有现成的"期望输出"，AI 就**自己造输入、自己定期望值**，写了一个**一次性 ad-hoc 脚本**（跑完即删，不进回归集），断言五条：

  | 用例 | AI 自定的期望 |
  |------|------|
  | A. 完全在内的三角形 | `_clip_triangle` 原样返回 1 个子三角形（**同一批顶点对象**，恒等、零浮点扰动） |
  | B. 跨近平面的三角形 | 裁开成 2 个子三角形，像素比关闭时少，所有深度 ∈ [0,1]；关闭时多渲染那块、深度被钳制 |
  | C. 相机背后顶点（`w < 0`） | 几何上被剔除，无垃圾坐标，深度 ∈ [0,1] |
  | D. 点列 | 开启保留 1/3（近/远越界丢弃）；关闭保留 3/3、深度钳到 `[0, 0.5, 1.0]` |
  | E. `pipeline_state.csv` 里 `DepthClip,False` | 被正确解析成 `depth_clip_enable = False` |

- **这才是"经典 TDD"的形态**：用例是**手写断言**（不是 golden CSV），输入是**合成的**（不是真机 capture），期望值由 AI 依据 D3D 规范**自己推导**。它和 §7 主线的"golden 即测试"互补——**golden 覆盖不到的新行为，AI 自己补一层测试兜底**。
- **和回归的分工**：ad-hoc 脚本验证"**新功能对不对**"（用完即删）；回归集守"**别把老功能改坏**"（6/6 常驻）。两者叠加，才让这个"重写图元裁剪"的高风险改动敢落地。为什么脚本用完就删而不进回归？因为它依赖手工构造的合成图元，不是真机 capture，进回归会偏离"数据驱动、真机为准"的套件定位——它的价值在**实现当下的一次性验证**。

---

## 8 · 如何纠正 AI 的错误

AI 会犯错，纠错来自**四个机制**，本轮都出现了（与第二部分"靠人手改兜底"相比，这一期 AI 更多能**自我纠错**，人退到"裁判 + 推动者"）：

### 8.1 让 AI 自我纠错（更深的证据）
step119 时 AI 把 event20899 判为"大气数学的精度长尾、暂缓"。这其实是**误判**。step120 用户要求继续后，AI 重新用逐语句轨迹深挖，发现真因是反规格化数分支选错（C2），把它从"不可解"变成"一行修复"。**纠错的关键是回到数据流、要更细的证据，而不是停在第一个看似合理的结论。**

### 8.2 用回归/验证当"客观裁判"
C3 改动会改变 `(int)floatattr` 的语义，AI 担心破坏既有的打包-uint case。于是**先查证**：把 event1031、event1399 的 `v1` 声明翻出来，确认它们是 `uint` 声明（不在 float-only 顶点集，根本不受影响），再动手；改完再用回归 46/46 复核。**"先证伪自己的担忧再提交"**。

### 8.3 把陷阱写进常驻规则（防止重犯）
`CLAUDE.md` 的"两个非显然陷阱"（golden 尾部 float3 错位、一元/二元 `+/-`）就是历史错误固化成的护栏。AI 读到后不会再在这些点上犯错。

### 8.4 用户直接纠偏
用户在提示词里预先标注"哪些是 capture 限制、无解"，等于提前纠正 AI"什么都想修"的倾向，避免在不可解问题上空转。

---

## 9 · 记忆系统避免重复开发

记忆系统是 `~/.claude/projects/.../memory/` 下的**一事一文件 + 一个索引**：
- `MEMORY.md` —— 索引，每条一行，会话开始时全量载入。
- 每个 `*.md` —— 一条经验事实，带 `type`（user/feedback/project/reference）。

### 9.1 Agent 记忆系统：是什么、为何重要、与个人开发的关系

**是什么。** 一次对话（会话）结束，Agent 的上下文就清零了——下一次会话它对"上回啃明白的坑"一无所知，这是大模型的**天然失忆**。记忆系统就是补给 Agent 的一层**跨会话长期存储**：在会话里发现一个"非显而易见、未来还会再用到"的事实时，把它写成一条记忆（**write-on-discovery**）；下次会话开始载入索引、在需要时召回（**recall-on-need**）。它与 `CLAUDE.md` 的分工见 §4.1.2——后者是人工写死的规则（整篇注入），前者是**随开发生长、按需召回**的经验库。

**为何重要。** 没有记忆，每个会话都从零开始：同一个根因反复重新推导、同一条死路反复重新试探、同样的 token 与时间反复重新烧掉。§9.2 表里每一条，都是**某一轮啃了很久**的结论——`denormal 分支选错（FTZ）`、`拓扑枚举不可信、要看 IAPrimitives`、`golden CSV 把 SV_Position 排最前`……这些反直觉的点若不沉淀，下个同类 case 又得从头困惑一遍。记忆的价值，是把**一次性的高成本洞见**变成**永久资产**：让 Agent 的能力**随项目累积而复利**，而不是每次会话都归零重来——这正是本节标题"避免重复开发"的字面含义。

**与个人开发的关系。** 传统上，一个长期项目的"部落知识"（哪里有坑、这个数据格式怎么解、哪类 case 是死路）只活在**资深开发者的脑子里**——难传承、易遗忘、换人即断。Agent 记忆系统把这层知识**外化成持久、可读、可审的文件**，并顺带改变了个人开发者的角色：

| 维度 | 没有记忆系统 | 有记忆系统 |
|---|---|---|
| 知识载体 | 开发者的**大脑** + 零散笔记 | 版本化的 `memory/*.md`，人机共读 |
| 谁来记 | 靠人自觉写文档（常常不写） | Agent **每次发现即落笔**（纪律由工具保证） |
| 谁能用 | 基本只有作者本人查得动 | 后续任意会话（含换人接手）自动召回 |
| 开发者的活 | 既当知识的**唯一作者**又当**唯一读者** | 转为**审阅/校正**：定哪条值得记、错的删掉 |
| bus-factor（断档风险） | 高（知识锁在一人脑中） | 低（记忆 + `Sessions/` + `CLAUDE.md` 可交接） |

> **人机分工的本质**：开发者提供**判断力**（什么值得记、哪条已过时或写错了要删——见 §9.4 纪律 ②③）；Agent 提供**纪律**（每次都真的写下来、并可靠地召回）——恰好补上人类"知道该记却懒得记、记了又找不回"的短板。对**个人／小团队开发**尤其关键：一个人 + 一个带记忆的 Agent，就能维持住原本需要团队或大量人工文档才撑得起的**项目级知识沉淀**，让长尾攻坚不再因"上次是谁、当时怎么想的"而反复归零。

### 9.2 更多记忆示例（均可回溯到对应 Session）

§4.1.2 已经从"输入数据流"的角度介绍过这套机制，并**完整展开**了两条示例（`golden-vs-mesh-sv-position-first`、`per-vertex-binary-vb-decode-r10a2`，另提及 `raw-img-texture-loading`、`witcher3-array-cbuffers-instanced-inputs`）。本节换一个角度——**"避免重复开发"**——再给一批**与 §4.1.2 不重复**的记忆，每条都能回溯到本仓库对应的 `Sessions/` 步骤（记忆的实质就是把那一轮的根因/结论沉淀下来，供后续会话召回）。

这些都是**"当时啃了很久、且后续同类 case 还会再遇到"**的事实——正是最值得写进记忆、避免下次从零再啃一遍的东西：

| 记忆文件（`type`） | 记的是什么（事实） | 帮 AI 省掉的重复劳动 | 来源 Session |
|---|---|---|---|
| `gpu-ftz-denormal-flush-to-zero`（project） | GPU 对反规格化数（denormal）做 **FTZ**（flush-to-zero）：`cb12[271].z` 的字节是 `0x00000001`（`1.4e-45`），GPU 当 `0`，三元 `denormal ? a : b` 走 **false** 分支；解释器保留非零 denormal 会走错分支 | 再遇到"大气散射/光照数学差一点点、疑似精度长尾"时，**先怀疑 denormal 分支**，不再误判成"不可解的精度长尾" | [step120](Sessions/hlsl-interpreter-step120-ftz-denormal-and-ftoi-cast-and-longtail-assessment.md) |
| `dxbc-asfloat-bitpattern-through-fma`（project） | DXBC 寄存器**带类型**：`ishl`/`+` 构造的是 float 的**位模式**，随后按 float 读回（隐式 `asfloat`）；解释器的 `<<`/`+` 返回普通 int，会把 41 亿这种整数直接乘进向量 → 万亿级垃圾。位选择 `&` 的结果也要**穿透 FMA 路径**再当 float 用 | 每个 BlackMyth / EndlessSpace2 顶点**压缩解码** shader 不必重新逆向"这个数到底是整数值还是 float 位模式" | [step196](Sessions/hlsl-interpreter-step196-blackmyth-asfloat-shift-exponent.md) · [step198](Sessions/hlsl-interpreter-step198-endlessspace2-bit-select-fma-rawbits.md) |
| `topology-list-vs-strip-trust-iaprimitives`（project） | `pipeline_state.csv` 里的图元拓扑枚举**不可靠**（会把 triangle **LIST 谎报成 STRIP**）；真机 `pipeline_statistics.csv` 的 `IAPrimitives` 才是 ground truth | 遇到"颜色偏亮/整片错色但 VS 数学没错"时，**先核对真机图元数**，不再去 PS 数学里瞎找 | [step100](Sessions/hlsl-interpreter-step100-fix-triangle-topology-list-vs-strip.md) |
| `sv-vertexid-from-raw-ib`（project） | 必须从 **raw 索引缓冲**（`ib_res_*.bin`）还原真实 `SV_VertexID`；否则每个顶点都读 `Buffer<float4>` texcoord 表的**元素 0**（Octopath 就栽在这） | 再遇到用 `SV_VertexID` 索引 typed-buffer 的 case，直接接 raw IB，不再从 CSV 的 `IDX` 列绕 | [step118](Sessions/hlsl-interpreter-step118-rundrawfromdump-octopath-tank-triage-and-fixes.md) |
| `reused-output-register-genuine-zero`（project） | 交叉复用的输出寄存器上，一次**真实写入 `0.0`** 不能被当成"未写入的默认值"——否则 `.w` 等分量会被错误地填默认值 | 再遇到多个 `out` 参数复用同一寄存器槽位的 case，直接按"写过就是写过（含 0）"处理 | [step194](Sessions/hlsl-interpreter-step194-dump-new-cases-and-fix-slot-shared-w-default.md) · [step195](Sessions/hlsl-interpreter-step195-fix-slot-shared-genuine-zero-write.md) |
| `rasterizer-d3d11-top-left-subpixel-snap`（reference） | D3D11 光栅化规范：**§3.4.1** 顶点 x/y 吸附到 `n.8`（1/256 子像素）、覆盖测试取像素中心 `(x+0.5,y+0.5)`；**§3.4.2.1** 左上填充规则（采样落在边上时只有上/左边算覆盖） | 任何"共享边 overdraw / 接缝瑕疵"直接套这份规范配方，不用重查 spec | [step101](Sessions/hlsl-interpreter-step101-fix-rasterizer-coordinate-snapping-and-top-left-rule.md) |
| `autonomous-cron-env-constraints`（feedback） | 无人值守 cron 环境的硬约束：**每次 shell ≤45s**、**后台进程跨调用不保活**（VM 只持久化文件系统）、**挂载目录禁止删除文件**（`rm`→Operation not permitted）、`/tmp` 仅 ~4GB（大 case 要把 `TMPDIR` 指到挂载盘） | 每个每小时轮次不必重新撞这些墙——回归改**前台分片**跑、已通过的 case 写进 `dump_case.csv` 而非删 zip、大 case 先改 `TMPDIR` | [step195](Sessions/hlsl-interpreter-step195-fix-slot-shared-genuine-zero-write.md) |

### 9.3 记忆的两面价值：既"跳过死路"，也要"复核暂缓"

记忆最典型的用法是把一轮的 **`## Blocked classes`**（§1.4）沉淀成"别再试"的先验，让下轮直接跳过——但**"无解"分两种，记忆要区别对待**：

- **真死路（记下就长期跳过）**：如"**四元数 typed-buffer 的 SNORM/UNORM 无法区分**"——`capture 根本没记录 view 的格式`，`R8G8B8A8_SNORM` 与 `UNORM` 在 dump 里字节一样、无从判别（§1.3 表里的 ❌ 项）。这类是**信息层面的死路**，写成一条 project/feedback 记忆后，后续会话读到就**合法跳过**，不在死路上空转。
- **暂缓（记下但必须定期复核）**：如 event20899 一度被 [step119](Sessions/hlsl-interpreter-step119-witcher-multi-array-cbuffer-binary-override.md) 判成"大气数学精度长尾、不可解"——但这其实是**误判**。[step120](Sessions/hlsl-interpreter-step120-ftz-denormal-and-ftoi-cast-and-longtail-assessment.md) 重新用逐语句轨迹深挖，发现真因是 denormal 分支选错（FTZ），一行修复即绿（详见 §8.1）。

> **要点**：所以记忆里"无解"的条目要**带上"为什么无解"**——是**信息缺失**（capture 没 dump 那层数据 → 真死路），还是**当时没查到根因**（→ 暂缓、待更厚的 dump 或更细的轨迹再复核，见 §4.4）。区分这两者，记忆才既能"止损"又不会"把可修的 bug 永久判死"。

### 9.4 如何避免重复开发（机制 + 纪律）

> **机制**：当一个"非显而易见、且未来还会用到"的事实被发现，就写成一条记忆（带 `**Why:**` 和 `**How to apply:**`），并在 `MEMORY.md` 留一行指针。下次会话开始即载入索引，相关记忆在需要时被召回。这样**同一个坑只踩一次**——上表每一条，都是某一轮啃了很久的结论，一旦入记忆，后续所有同类 case 直接命中、不再从零困惑。
>
> **纪律**：① 不记录代码/git 已经记录的东西（结构、历史、`CLAUDE.md` 已写的）；② 先查重再写，宁可更新已有文件、错的要删；③ 召回的记忆是"**当时为真**"的背景，引用到具体文件/函数时**要先核实其仍存在**；④ "无解"条目必须写清是**信息死路**还是**暂缓待复核**（见 §9.3）。

---

## 10 · b/c 两个开发部分的对比和优缺点总结

本项目历经两个 agent 周期（约从 step1 到 step123、2026-06-05 起）。以下特征基于 `Sessions/` 日志与提交历史的可见证据归纳。

### 10.1 前期：opencode + minimax-m2.7（第二部分 / 奠基期）
- **做的事**：从 0 搭起解释器骨架——HLSL 词法/递归下降表达式解析（`hlsl_syntax_tree.py`）、语句解释、cbuffer/struct/函数解析、定点管线（rasterizer/depth/OM/texture）、legacy struct 工作流、golden 对比的雏形。
- **节奏特征**：**小步快跑**。step1 init、step2 打印 cbuffer、step10 给语法树加 eval 打印、step11 修向量×矩阵、step12 格式化矩阵打印……每步一个很小的功能或修复。
- **适配点**：在能力相对有限的模型上，把任务切得很碎、每步可见即可验证，是稳妥策略；大量"加打印/加注释/修一个算子"的步骤为后期可观测性打了底（第三部分 §5 的轨迹输出就源于此期）。
- **优点**：步幅小、易回退、每步可肉眼验证、成本低；为整个项目的**可观测性**（`[STMT]`/`[BINARY OP]` 打印）打了地基。
- **缺点**：找不到深层根因（[#35](Prompts/hlsl-interpreter-prompt-OpenCode.html#step-35)）、修复不彻底（[#37](Prompts/hlsl-interpreter-prompt-OpenCode.html#step-37)）、会误删代码（[#42](Prompts/hlsl-interpreter-prompt-OpenCode.html#step-42)）；**重度依赖人工兜底**，无回归网、推进慢。

### 10.2 后期：claude code + opus（第三部分 / 攻坚期）
- **做的事**：引入 zip/RunDrawFromDump 真机 capture 工作流、建立**数据驱动的回归套件**、并系统化啃长尾——批量 triage 280→355 个 capture、按根因聚类、逐类深挖修复（C1/C2/C3、structured buffer skinning、raw .img 纹理、R10A2 解码……）。
- **节奏特征**：**根因导向、闭环交付**。一步往往覆盖"triage→定位→修复→回归→提交→文档"整条链，单步信息密度高；能在长上下文里同时持有整条管线、并行跑 triage、对高风险改动先证伪再提交。
- **适配点**：更强的模型 + 更长上下文 + 更全的工具（并行 Bash、后台任务、回归 gate、记忆系统），使"一次推进一个完整根因类"成为可能。
- **优点**：长上下文可**持有整条管线**、根因导向、闭环交付（含回归 gate + 记忆 + 文档）、能批量 triage、能**自我纠错**（见第三部分 · 8 的 C2 翻案）。
- **缺点**：单步信息密度高、需要信任；成本高；**对提示词约束敏感**——缺"不要做什么"会改错地方（见第三部分 · 1 的 [#2→#3→#4 反例](Prompts/hlsl-interpreter-prompt-ClaudeCode.html#step-2)）；仍可能误判、需用户推动（C2 一度被判"不可解"）。

### 10.3 对比小结
| 维度 | 第二部分：opencode + minimax-m2.7 | 第三部分：claude code + opus |
|---|---|---|
| 目标 | 从无到有搭骨架 | 真机数据攻坚长尾 |
| 步幅 | 小而多（单功能/单算子） | 大而少（整条闭环/整类根因） |
| 上下文 | 短、聚焦单点 | 长、可持有整条管线 |
| 计划 | **人**切碎需求、找杠杆点 | **AI** 自己 triage→聚类→排序 |
| 验证 | 加打印、人工看 | verify-by-log + 自动回归 gate |
| 纠错 | 人手改兜底（[#37](Prompts/hlsl-interpreter-prompt-OpenCode.html#step-37)/[#42](Prompts/hlsl-interpreter-prompt-OpenCode.html#step-42)） | AI 自我纠错 + 回归当裁判 |
| 工具用法 | 基本编辑/运行 | 并行 triage、后台任务、记忆、git 闸门 |
| 典型产物 | [#11 修向量×矩阵](Prompts/hlsl-interpreter-prompt-OpenCode.html#step-11) | [#42 一个 bug 修 5 个 case + 扩回归](Prompts/hlsl-interpreter-prompt-ClaudeCode.html#step-42) |

> 两个周期是**互补**的：前期的小步与可观测性铺垫，是后期能够"沿数据流快速回溯根因"的前提。没有前期那些 `[STMT]`/`[BINARY OP]` 打印，后期的 C2/C3 根因不可能被一眼看穿。选型启示：**搭骨架/能力有限时，把需求切碎、靠人验证最稳；攻坚/上下文充足时，让 AI 持有全局、用回归+记忆闭环交付最高效。**

### 10.4 收尾期：可观测性回补（约 step 185–188）

攻坚长尾之后，最近几步把重心从"修正确性"转向"看得见执行过程"：在原有 tkinter 界面之外，新增了**静态 HTML 导出**（`html_mesh_view.py`）与**动态 Web 视图**（`web_mesh_view.py`）。动态视图起一个本地 HTTP 服务，浏览器轮询 `/state`、实时跟随 VS→光栅化→PS 的执行进度做逐顶点/逐图元/逐像素动画，并支持 normal 向量显示与每阶段动画延迟调节（step 186 加进度、187 修 normal 显示、188 让光栅化阶段也逐像素动画并可调延迟）。

这批工作与第三部分的纪律一脉相承：**都是可视化改动，不触碰解释器正确性**；因延迟默认 0、动画 hook 在 headless 回归路径上从不挂载，回归维持 118/123（4 个 witcher3_countryside 失败已在 step 179/182/184 判为结构性/数据问题，非解释器 bug）。可观测性从"事后读日志"进一步前移到"边跑边看"，正是第二部分埋下的"小步 + 可观测"理念在成熟阶段的自然延伸。

### 10.5 观测器深化 + 长尾回补（约 step 189–194）

step 186–188 立起动态 Web 视图后，step 189–193 继续把它做深：像素视图缩放与阶段回放、**逐指令 VS/PS 轨迹**（step 189）、Web 视图内存瘦身（惰性像素、引用而非拷贝、缓存 payload，step 190）、可拖拽自定义布局（step 191）、把顶点/像素信息拆成两个独立面板（step 192），以及直接浏览预览原始 draw zip 的「Draw Data」面板（step 193）。这批仍是纯可视化改动，不碰解释器正确性。

step 194 则回到正确性主线：一次**逐指令 VS/PS 轨迹 + verify-by-log** 的组合把 `manhattan_event1041` 从 0/228 修到 228/228——VS 只写 `o6.xy`，`TEX_COORD1` 打包进从未写的 `o6.zw`，而 D3D 语义要求未写的输出寄存器分量取每寄存器初值 `(0,0,0,1)`；`_resolve_slot_shared_params` 原来只在主参数溢出时回填次级参数，遗漏了「主参数恰好只写自己分量」的情形。修复让未写的次级输出继承寄存器默认（落在索引 3 的 `.w` 取 `1.0`）。同一步还把空的 `dump_case.csv` 补齐为 `Dump/` 现有 31 个 zip 并逐个 triage（11 PASS / 4 精度 FAIL / 1 无 golden / 15 因体量超时未评估）。

这条轨迹印证了第三部分的方法论在成熟阶段依旧成立：**先把执行过程"看得见"，再沿数据流回溯根因**——观测器的逐指令轨迹直接指向了未写寄存器的默认值这一根因，而回归/golden 比对继续充当裁判。

---

# 第四部分 · 周期任务的创建和执行（自动化期）

> 前三部分讲的是**人坐在旁边、一轮轮和 AI 对话**推进项目。到了成熟阶段，很多工作是**重复且可判定**的——"有没有新 case、跑不跑得过、状态有没有变化"。这类工作没必要每次都靠人发起，可以交给**周期任务（scheduled / cron task）**：由定时调度器在固定时刻拉起一个**无人值守**的 Claude Code 会话，按一份**固定的任务提示词**跑完整条闭环（扫描 → 执行 → 修复 → 验证 → 提交 → 留痕），跑完即退出，下次到点再来。
>
> 本部分用本项目真实运行的两个周期任务作为例子：
> - **`hlsl-interpreter-hourly-develop`**（每小时）——**开发型**：发现并攻克 `Dump/` 里的新 draw case，能修则修、修好进回归。
> - **`daily-hlsl-status-report`**（每天）——**巡检型**：产出一份项目状态日报，并顺手把 `ReadMe.md` / 手册 / 站点刷新。

## 1 · 什么是周期任务、它解决什么问题

一个周期任务 = **调度（何时触发）** + **任务提示词（触发后做什么）** + **一个无人值守会话（谁来做）**。它和前三部分的交互式开发的根本区别是：**没有人在环里逐步纠偏**。因此它只适合两类工作：

- **重复且高确定性**：每小时/每天都要做一遍，判据机器可判（有没有 `Error:`、`X==Y`、git 有没有新提交）。
- **可自我兜底**：即使某次跑偏，也不会破坏主线——改动要么被回归 gate 拦住，要么只落在自己的留痕文件里，等人事后审阅。

在本项目里，它正好接住第三部分留下的两类长尾负担：**① 源源不断新增的 draw dump**（人工一个个跑太累）；**② 每天的项目状态漂移**（提交了什么、哪块没提交、回归还绿不绿）。把它们交给周期任务后，人只需**事后读日报、审提交**，从"操作员"退到"验收者"。

## 2 · 如何创建一个周期任务（调度 + 任务提示词 + 留痕）

创建一个周期任务，实操上就是把三件事定下来：

1. **起个名、定个频率**：给任务一个稳定的名字（如 `hlsl-interpreter-hourly-develop`）和 cron 频率（每小时 / 每天）。名字要能一眼看出"多久一次 + 干什么"。
2. **写死一份任务提示词**：这是周期任务的灵魂。它和第三部分 §1 的交互式提示词写法一脉相承，但因为**无人在环**，对"提示词工程"的要求更苛刻：
   - **Steps 写成有序清单**：把整条闭环拆成编号步骤，AI 照着走，不用即兴发挥。
   - **判据必须机器可判**：什么叫"通过"要写死（本项目复用 `Error:` + `Total PASSED rows: X/Y` 的 X==Y 判据），不能留给 AI"自我感觉"。
   - **给足边界（Notice / 不要做什么）**：例如"修好的才进回归、没修好的只记名并删 zip"——这正是第三部分 §1 反复强调的"约束比目标更重要"。
   - **强制留痕**：要求把思考/执行/结果写进 `Sessions/hlsl-interpreter-stepN-*.md`，并把 summary 回填到 `Prompts/hlsl-interpreter-prompt-ClaudeCode.md`——让每次无人值守的运行都**可追溯、可核对**。
3. **约定产物与提交**：跑完要 `git commit && git push`，把留痕文件、状态报告一并入库。这样"人"第二天读 git log 和日报，就能验收昨晚发生的一切。

> **一句话**：交互式开发里，纠偏靠人；周期任务里，纠偏靠**提示词里写死的判据 + 回归 gate + 事后审阅**。所以周期任务的提示词，本质是把第三部分那套"CLAUDE.md 常驻规则 + 可机判成功标准 + 强制留痕"**固化成一份可反复触发的脚本**。

## 3 · 周期任务如何执行（无人值守闭环）

到点后，调度器拉起一个**全新的、headless 的**会话（没有前一次的上下文），它读到的只有：**这份任务提示词** + **常驻的 `CLAUDE.md`** + **仓库当前状态**。然后照 Steps 跑：

```
定时触发 → 扫描仓库/Dump 现状 → 按 Steps 执行（跑管线 / 生成报告）
         → 用机器判据验证 → 能修则修、否则记录 → commit & push
         → 写 Sessions 留痕 + 回填 Prompts → 退出
```

**两个真实的执行约束**（这也是无人值守区别于交互式的地方，必须写进提示词或事后靠人补）：

- **单次 shell 调用有 ~45s 预算**：`Dump/` 里的大 capture（19 MB–264 MB）在沙箱里跑不完。所以任务提示词允许"超预算的 case 不算失败、只记录不评估"——见例一的 15 个未评估 case。
- **上下文是干净的**：每次触发都从零开始，所以**自跟踪文件**（如 `dump_case.csv`）就是它的"记忆"——靠它区分"哪些 case 已处理过、哪些是新的"，保证任务**幂等**（重复触发不重复劳动）。

## 4 · 例一：`hlsl-interpreter-hourly-develop`（每小时开发）

**任务提示词**（原文，Steps + Notice 双段式）：

```
Run the new draw case on the HLSL interpreter project located at .../hlsl_interpreter/Dump.
Steps:
 1. Scan the folder .../Dump. If there is new case whose name is not in dump_case.csv.
 2. Add the new case's name to dump_case.csv.
 3. Run the new case.
 4. If the case cannot pass, find the root cause in hlsl_interpreter code and fix it.
 5. Commit the fix and push it to github.
 6. 修复后通过的 case 加入 regression test；没修复直接通过的把 case name 写入
    dump_case.csv，并删除 Dump 文件夹中的 draw zip 文件。
Notice:
 - 把思考、执行和结果写入 Sessions/hlsl-interpreter-stepN-***.md（stepnum 按当前 step 值填）。
 - 把 summary 填入 Prompts/hlsl-interpreter-prompt-ClaudeCode.md 对应的 Claude Code Session。
```

**它是一条"发现→攻克→固化"的开发闭环**，对应第三部分的方法论：`dump_case.csv` 是**自跟踪文件**（无人值守会话的"记忆"，实现幂等），回归套件是**固化闸门**，`Sessions/` + `Prompts/` 是**留痕**。

**真实一次运行**（见 [`Sessions/hlsl-interpreter-step194-*.md`](Sessions/hlsl-interpreter-step194-dump-new-cases-and-fix-slot-shared-w-default.html)，2026-07-07）：触发时 `dump_case.csv` 是空的（0 字节），于是 `Dump/` 里的 **31 个 zip 全部算新**，逐个 triage：

| 结果 | 数量 | 说明 |
|---|---|---|
| **PASS** | 11 | 直接通过（含本次新修好的 `manhattan_event1041`） |
| **FAIL — 逐顶点精度** | 4 | `manhattan_event87/124/198/124_indirect`，1–2 行孤立大 diff，另一类根因，本次未攻 |
| **FAIL — 无 golden** | 1 | `BlackMyth_event2063` 无 `MeshOut_*_mesh.csv`，数据缺失非 bug |
| **未评估 — 超 45s 预算** | 15 | 大 capture（最大 264 MB），沙箱跑不完，**不算失败** |

其中真正体现"能修则修"的是 `manhattan_event1041`：每行 `TEX_COORD1[1]` 都 output=`0.0` vs golden=`1.0`。逐指令轨迹显示 VS 只写了 `o6.xy`，而 `TEX_COORD1` 被打包进**从未写过的 `o6.zw`**——D3D 语义要求未写的输出寄存器分量取每寄存器初值 `(0,0,0,1)`。修 `_resolve_slot_shared_params` 让未写的次级输出继承寄存器默认后，`manhattan_event1041` 从 **0/228 → 228/228**，随即按 Step 6 **进回归**（`Cases/regression_test_zip_files.csv` 从 123 涨到 152）、commit & push。这一步的每个动作都可在 step194 session 与提交 `2cc8307` 里逐条核对。

## 5 · 例二：`daily-hlsl-status-report`（每日巡检）

**任务提示词**（原文节选）：

```
Produce a daily status report on the HLSL interpreter project located at .../hlsl_interpreter.
Steps:
 1. Scan the folder; read key source & project files (README, main sources, tests, TODO, config).
 2. If it's a git repo, check recent activity: commits since yesterday, current branch, dirty files.
 3. Summarize state & changes: new/modified/removed files, progress, TODOs, test status, anything broken.
 4. Save to daily-status/status-YYYY-MM-DD.md (create the folder; use today's date). Clean Markdown.
 5. Commit status-YYYY-MM-DD.md and push to github.
 6. Based on current status, update ReadMe.md, Docs/AI-Development-Handbook*, Docs/index.html.
    Commit & push.
Keep it concise and focused on status and changes. If the folder is empty/inaccessible, note that.
```

**它是一条"观察→记录→同步文档"的巡检闭环**，与例一互补：例一**改代码**，例二**只读状态 + 刷文档**，几乎不碰解释器正确性，风险极低，非常适合无人值守。

**真实产物**（见 [`daily-status/status-2026-07-07.md`](../daily-status/status-2026-07-07.md)）。报告结构稳定，每天同一套骨架，便于横向对比：

- **Summary**：一句话定性昨天到今天的重心（如"视图深化 steps 189–193 + 一处未提交的正确性修复 step 194"）。
- **Git activity**：列出昨日报告以来的新提交（如 `8f10895` step193 … `8dd37cf` step189），并核对 `origin/main == HEAD`。
- **Uncommitted / in-progress**：点名工作树里**尚未提交**的真实改动（step 194 的 `hlsl_interpreter.py` 修改），提醒人去审。
- **Dump triage / Test status**：复述回归口径（当前 152 case），并诚实标注"回归日志 stale、本次未全量重跑"——**不谎报绿**，这是巡检任务的底线。
- **Needs attention**：把需要人介入的事项单列。

跑完 Step 5/6 后，日报入库，`ReadMe.md`、`Docs/AI-Development-Handbook*`、`Docs/index.html` 一并刷新提交（见提交 `c32e83a`、`8236b88`）。第二天人只要读这份日报，就能不看代码地掌握"昨晚发生了什么、哪块要盯"。

## 6 · 两个周期任务的对比与设计要点

| 维度 | 例一 `hlsl-interpreter-hourly-develop` | 例二 `daily-hlsl-status-report` |
|---|---|---|
| 频率 | 每小时 | 每天 |
| 性质 | **开发型**（改代码） | **巡检型**（读状态 + 刷文档） |
| 触发对象 | `Dump/` 里的新 draw case | 整个仓库 + git 状态 |
| 机器判据 | `Error:` + `X==Y`（回归口径） | git log / 工作树 / 回归口径（只读） |
| 自跟踪"记忆" | `dump_case.csv` | 上一份 `status-*.md` |
| 主要产物 | 修复提交 + 回归条目 + step session | `daily-status/status-*.md` + 文档刷新 |
| 风险 | 中（动解释器，靠回归 gate 兜底） | 低（几乎只读） |
| 留痕 | `Sessions/` + `Prompts/` 回填 | `daily-status/` + `ReadMe`/手册/站点 |

**从这两个例子提炼的设计要点**（都是让无人值守可靠的关键）：

1. **判据写死、可机判**：复用第三部分那套 `Error:` + `X==Y`——绝不让 AI"自我感觉良好"就算过（回想第二部分 [OpenCode #37](Prompts/hlsl-interpreter-prompt-OpenCode.html#step-37) 的"假修复"教训，无人值守下这类假绿更危险）。
2. **幂等 + 自跟踪文件**：每次触发上下文归零，靠 `dump_case.csv` / 上一份日报区分"新旧"，避免重复劳动、避免遗漏。
3. **只固化"真绿"**：例一明确规定——**修复后通过的才进回归**；没修好、直接过的只记名并删 zip。回归套件的"金身"绝不被无人值守的乐观污染。
4. **诚实优先**：例二宁可写"回归日志 stale、未全量重跑"，也不谎报一个通过数——留痕的价值在于**真实可核对**。
5. **强制留痕 + 提交**：每次运行都落到 `Sessions/` / `daily-status/` 并 push，把"无人值守"变成"事后完全可审计"，人从操作员退为验收者。
6. **接受环境边界**：把"~45s/调用预算、超大 capture 跑不完"写进任务约定，让超预算 case **只记录不误判为失败**——诚实标注未知，胜过强行下结论。

> **与前三部分的关系**：周期任务不是新方法，而是把第三部分成熟的那套纪律（CLAUDE.md 常驻规则、可机判判据、回归 gate、记忆系统、强制留痕）**打包成一份可定时触发、无人值守的脚本**。前三部分回答"人和 AI 怎么协作把项目做出来"，第四部分回答"做出来之后，怎么让 AI 定时自己维护它、而人只需验收"。

---

*本手册随项目演进更新；HTML 版见 `AI-Development-Handbook.html`。*
