# AI 辅助开发手册 — 以 HLSL 解释器项目为例

> 本手册用 **本项目（纯 Python 的 D3D11 管线 / HLSL 解释器）** 的真实开发记录，
> 系统说明"如何与 AI 协作完成一个真实工程"。所有例子都取自本仓库的提交、
> `Sessions/` 步骤日志与 `Prompts/` 提示词历史，可逐条核对。
>
> 本手册按**项目的两个开发周期**组织成三大部分：
> - **第一部分 · 项目总体介绍** —— 这是什么、怎么跑、怎么验证。
> - **第二部分 · 基于 OpenCode + minimax-m2.7 的开发（奠基期）** —— 从零搭骨架的协作方式。
> - **第三部分 · 基于 ClaudeCode 的开发（攻坚期）** —— 真机数据攻坚长尾的协作方式。

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

> 每节都附本仓库 `Prompts/` 提示词历史与 `Sessions/` 步骤日志的超链接，可逐条核对。

---

# 第一部分 · 项目总体介绍

## 项目概述

### 0.0 项目的目的

本项目的初衷并不是"做一个 D3D11 仿真器"，而是**一次关于 AI coding 的研究**——它的目标随开发推进逐步演化：

1. **最初为研究 AI coding 而创建**：项目最早是为了探索"用 AI 写代码"这件事本身能走多远而立项的。
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

这段约束把"完成"变成了**机器可判、不可赖账**的三条硬指标——AI 不能自称"修好了"，必须让 `run_regression.py` 亮绿。它还顺带堵死了两条捷径：**不许改 runner 硬编码绕过**、**失败必须回到解释器侧修**（而不是去动 golden 或放宽判据）。

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
- `Cases/regression_test_zip_files.csv` 列出必须保持通过的 capture zip（一行一个）。
- `run_regression.py` 读这个 CSV，逐个 headless 跑 `render.py`，每个写一份 `Cases/regression_logs/<name>.log`，最后打印 PASS/FAIL 汇总，任一失败则退出码非零（可 gate CI）。
- **通过判据**（三条全满足）：① `render.py` 干净退出；② 日志无 `Error:` 行；③ `Total PASSED rows: X/X`（X==Y）。

### 6.2 在开发流程中的应用
- **每次改动后必跑**（`CLAUDE.md` 硬性规则）。C1 改完跑出 44/44、C2 跑出 45/45（含新加的 20899）、C3 跑出 46/46。
- **每修好一类，就把代表 case 加进回归**。本轮新增 `event23341`（多数组代表）和 `event20899`（FTZ 代表），让这两类**永不回退**。
- **回归是"提交闸门"**：只有回归绿了才提交。C3 是个"改变既有强转语义"的高风险改动——正是回归 46/46 才让它敢提交。

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

本项目已有的记忆（节选）：
| 记忆文件 | 内容 | 避免的重复劳动 |
|---|---|---|
| `golden-vs-mesh-sv-position-first` | golden CSV 把 SV_Position 列排在最前，要按位置而非表头映射 | C2 分析时无需重新推导列序，直接确认 o6 对得上 |
| `per-vertex-binary-vb-decode-r10a2` | NORMAL/TANGENT 在二进制 VB 里是 R10G10B10A2 | 不必重新逆向顶点格式 |
| `raw-img-texture-loading` | 纹理按 DXGI 格式从 .img 读、BC7 回退 BMP | 直接复用，不重造 |
| `witcher3-array-cbuffers-instanced-inputs` | 数组 cbuffer 与实例化输入的加载与坑 | C1 的背景知识 |

> **如何避免重复开发**：当一个"非显而易见、且未来还会用到"的事实被发现，就写成一条记忆（带"为什么"和"怎么用"），并在 `MEMORY.md` 留一行指针。下次会话开始即载入索引，相关记忆在需要时被召回。这样**同一个坑只踩一次**——例如 golden 列序错位这种反直觉点，靠记忆一次性记住，后续所有 witcher case 都不再重新困惑。
>
> 纪律：① 不记录代码/git 已经记录的东西；② 先查重再写，宁可更新已有文件；③ 召回的记忆是"当时为真"的背景，引用到具体文件/函数时要先核实其仍存在。

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

---

*本手册随项目演进更新；HTML 版见 `AI-Development-Handbook.html`。*
