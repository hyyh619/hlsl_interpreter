# AI 辅助开发手册 — 以 HLSL 解释器项目为例

> 本手册用 **本项目（纯 Python 的 D3D11 管线 / HLSL 解释器）** 的真实开发记录，
> 系统说明"如何与 AI 协作完成一个真实工程"。所有例子都取自本仓库的提交、
> `Sessions/` 步骤日志与 `Prompts/` 提示词历史，可逐条核对。
>
> 贯穿全篇的三个核心案例（均来自最近一轮开发，step119–120）：
> - **C1 多数组 cbuffer**：`texgen[2]` 读成 0 → 修复二进制重载只填首个数组字段的 bug。
> - **C2 反规格化数 FTZ**：大气散射输出 o0/o1 错 → GPU flush-denormals-to-zero 语义。
> - **C3 (int) 强转语义**：顶点属性 `(int2)v1.zw` 被位重解释成天文数字 → NaN 位置。

---

## 目录
0. [项目概述](#0-项目概述)
1. [如何给 AI 提供提示词](#1-如何给-ai-提供提示词)
2. [AI 如何分析需求、生成开发计划](#2-ai-如何分析需求生成开发计划)
3. [AI 如何按计划执行](#3-ai-如何按计划执行)
4. [AI 的输入 / 输出数据流](#4-ai-的输入--输出数据流)
5. [什么样的数据流输出能帮助调试](#5-什么样的数据流输出能帮助调试)
6. [回归测试如何建立与应用](#6-回归测试如何建立与应用)
7. [TDD 在本项目的应用](#7-tdd-在本项目的应用)
8. [如何纠正 AI 的错误](#8-如何纠正-ai-的错误)
9. [记忆系统如何避免重复开发](#9-记忆系统如何避免重复开发)
10. [两个 Agent 开发周期的对比](#10-两个-agent-开发周期的对比)

---

## 0. 项目概述

### 0.1 这是什么
本项目是一个**纯 Python 实现的 Direct3D 11 图形管线软件仿真器**，核心特点是**直接解释执行 HLSL 着色器源码**——不编译、不调用 `eval`，而是自己做词法/语法分析并逐语句解释。它把一帧真机捕获的 GPU 数据喂进这条仿真管线，跑完整的固定功能 + 可编程阶段：

```
Vertex Shader → Rasterizer → Depth/Stencil → Pixel Shader → Output Merger
```

然后把顶点着色器（VS）的输出与真机捕获的 **golden 参考数据**逐分量对比，以此验证解释器的正确性。

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

## 1. 如何给 AI 提供提示词

提示词不是"一句话指令"，而是一个**分层的上下文系统**。本项目里有效的提示词由四层组成：

### 1.1 常驻项目说明（`CLAUDE.md`）
最重要的一层。它在每次会话开始时自动注入，相当于"团队 onboarding 文档 + 不可逾越的规则"。本项目的 `CLAUDE.md` 写清了：
- **这是什么**：纯 Python 解释 HLSL 源码、模拟 D3D11 管线、对比 golden 数据。
- **怎么运行**：`python render.py ./Cases/Default.json`。
- **核心工作流**：verify-by-log —— 跑管线、grep 日志里的 `Error:` 行、迭代到无错。
- **两个"反直觉陷阱"**：3Dmigoto 尾部 `float3` 错位、表达式里一元 vs 二元 `+/-`。
- **硬性规则**：每次改动后必须跑回归测试 `python run_regression.py`。

> **要点**：把"踩过的坑"和"必须遵守的纪律"写进常驻说明，AI 才不会反复犯同样的错。
> 例如 C1 修复时，AI 直接知道要去看 golden 对比日志、改完要跑回归——这都来自 `CLAUDE.md`。

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

### 1.3 现状/约束清单
用户把"已知未修复的长尾"列在前面（八面体法线、raw buffer、SNORM/UNORM、超时），并标注哪些是"capture 限制、无解"。这让 AI **不浪费时间去碰已判定不可解的问题**，把精力放在可解项（C1/C2/C3）。〔提示词原文同上：[ClaudeCode #42 的"尚未修复（剩余长尾）"清单](Prompts/hlsl-interpreter-prompt-ClaudeCode.html#step-42)〕

### 1.4 给提示词的实操建议
- **给可验证的成功标准**，而不是"修好它"。本项目的标准是机器可判的：日志无 `Error:` 且 `Total PASSED rows: X/X`。
- **指明数据/代码在哪**（zip 目录、配置文件），省去 AI 猜测。
- **把"不要做什么"写出来**（已知无解的类别），避免无效探索。
- **要求留痕**（写 session 文档），既是交付物，也是下一轮的记忆。

### 1.5 好提示词 vs 差提示词：本项目的真实对照

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

## 2. AI 如何分析需求、生成开发计划

面对"挨个修复 issue"这种开放任务，AI 不会盲目动手，而是先**侦察（triage）→ 聚类 → 排序 → 立计划**。

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

## 3. AI 如何按计划执行

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

## 4. AI 的输入 / 输出数据流

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

## 5. 什么样的数据流输出能帮助调试

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

## 6. 回归测试如何建立与应用

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

## 7. TDD 在本项目的应用

经典 TDD 是"先写失败测试，再写实现使其通过"。本项目是 **TDD 的一种变体——"golden 即测试"**：

- **测试先于实现存在**：每个 capture zip 自带 GPU 真机跑出的 `*_vs_mesh.csv`（golden）。它就是"期望输出"，在 AI 动手前就已存在且必然正确。
- **红 → 绿循环**：
  - **红**：跑 event20899 → `Total PASSED rows: 0/2160` + 一堆 `Error:`。
  - **绿**：实现 FTZ 修复 → `2160/2160`，无 `Error:`。
  - **重构/防回归**：把该 case 加进回归集，确保未来任何改动都得让它保持绿。
- **失败先驱动定位**：和 TDD 一样，**先让失败可复现、可量化**（哪一行哪一分量差多少），再写最小修复。C1 就是先确认 `texgen` 读成 0（红），再精确改二进制重载循环（绿）。

> 与单元 TDD 的差别：这里的"测试用例"不是手写断言，而是**真机 golden 数据**；"测试框架"是 verify-by-log + 回归脚本。但红绿节奏、"测试驱动最小实现"、"测试防回归"三大要义完全一致。

---

## 8. 如何纠正 AI 的错误

AI 会犯错，纠错来自**四个机制**，本轮都出现了：

### 8.1 让 AI 自我纠错（更深的证据）
step119 时 AI 把 event20899 判为"大气数学的精度长尾、暂缓"。这其实是**误判**。step120 用户要求继续后，AI 重新用逐语句轨迹深挖，发现真因是反规格化数分支选错（C2），把它从"不可解"变成"一行修复"。**纠错的关键是回到数据流、要更细的证据，而不是停在第一个看似合理的结论。**

### 8.2 用回归/验证当"客观裁判"
C3 改动会改变 `(int)floatattr` 的语义，AI 担心破坏既有的打包-uint case。于是**先查证**：把 event1031、event1399 的 `v1` 声明翻出来，确认它们是 `uint` 声明（不在 float-only 顶点集，根本不受影响），再动手；改完再用回归 46/46 复核。**"先证伪自己的担忧再提交"**。

### 8.3 把陷阱写进常驻规则（防止重犯）
`CLAUDE.md` 的"两个非显然陷阱"（golden 尾部 float3 错位、一元/二元 `+/-`）就是历史错误固化成的护栏。AI 读到后不会再在这些点上犯错。

### 8.4 用户直接纠偏
用户在提示词里预先标注"哪些是 capture 限制、无解"，等于提前纠正 AI"什么都想修"的倾向，避免在不可解问题上空转。

---

## 9. 记忆系统如何避免重复开发

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

## 10. 两个 Agent 开发周期的对比

本项目历经两个 agent 周期（约从 step1 到 step123、2026-06-05 起）。以下特征基于 `Sessions/` 日志与提交历史的可见证据归纳。

### 10.1 前期：opencode + minimax-m2.7（奠基期）
- **做的事**：从 0 搭起解释器骨架——HLSL 词法/递归下降表达式解析（`hlsl_syntax_tree.py`）、语句解释、cbuffer/struct/函数解析、定点管线（rasterizer/depth/OM/texture）、legacy struct 工作流、golden 对比的雏形。
- **节奏特征**：**小步快跑**。step1 init、step2 打印 cbuffer、step10 给语法树加 eval 打印、step11 修向量×矩阵、step12 格式化矩阵打印……每步一个很小的功能或修复。
- **适配点**：在能力相对有限的模型上，把任务切得很碎、每步可见即可验证，是稳妥策略；大量"加打印/加注释/修一个算子"的步骤为后期可观测性打了底（第 5 节的轨迹输出就源于此期）。

### 10.2 后期：claude code + opus（攻坚期）
- **做的事**：引入 zip/RunDrawFromDump 真机 capture 工作流、建立**数据驱动的回归套件**、并系统化啃长尾——批量 triage 280→355 个 capture、按根因聚类、逐类深挖修复（C1/C2/C3、structured buffer skinning、raw .img 纹理、R10A2 解码……）。
- **节奏特征**：**根因导向、闭环交付**。一步往往覆盖"triage→定位→修复→回归→提交→文档"整条链，单步信息密度高；能在长上下文里同时持有整条管线、并行跑 triage、对高风险改动先证伪再提交。
- **适配点**：更强的模型 + 更长上下文 + 更全的工具（并行 Bash、后台任务、回归 gate、记忆系统），使"一次推进一个完整根因类"成为可能。

### 10.3 对比小结
| 维度 | opencode + minimax-m2.7（前期） | claude code + opus（后期） |
|---|---|---|
| 目标 | 从无到有搭骨架 | 真机数据攻坚长尾 |
| 步幅 | 小而多（单功能/单算子） | 大而少（整条闭环/整类根因） |
| 上下文 | 短、聚焦单点 | 长、可持有整条管线 |
| 验证 | 加打印、人工看 | verify-by-log + 自动回归 gate |
| 工具用法 | 基本编辑/运行 | 并行 triage、后台任务、记忆、git 闸门 |
| 典型产物 | "step11 修向量×矩阵" | "step119 一个 bug 修 5 个 case + 扩回归" |

> 两个周期是**互补**的：前期的小步与可观测性铺垫，是后期能够"沿数据流快速回溯根因"的前提。没有前期那些 `[STMT]`/`[BINARY OP]` 打印，后期的 C2/C3 根因不可能被一眼看穿。

---

*本手册随项目演进更新；HTML 版见 `AI-Development-Handbook.html`。*
