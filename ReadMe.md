# HLSL Interpreter

一个用**纯 Python** 实现的 Direct3D 11 图形管线软件模拟器。它**直接解释执行 HLSL 着色器源码**（不编译、不依赖 `eval`），跑完整条管线 —— 顶点着色器 → 光栅化 → 深度/模板测试 → 像素着色器 → 输出合并 —— 并把顶点着色器的输出与抓帧得到的「golden」参考数据逐分量比对，用来验证解释执行的正确性。

输入数据来自 **RenderDoc / 3Dmigoto** 抓帧导出、打包成 zip 的一帧绘制数据。

> 主要用途：在没有 GPU / 调试器的情况下，逐语句复现并排查某次 Draw Call 的 HLSL 执行逻辑，定位渲染结果不对的根因（颜色错误、坐标错位、矩阵乘法、swizzle、反射向量等）。

## 特性

- **HLSL 解释执行**：手写递归下降表达式解析器，构建语法树后求值；支持 swizzle（`r0.xyz`）、类型转换（`(int2)`）、按位运算（`| &`）、一元/二元 `+/-` 区分、三目运算、矩阵/向量内建函数（`mul` / `dot` / `normalize` / `reflect` / `length` 等）。
- **完整管线**：VS → Rasterizer → Depth/Stencil → PS → Output Merger，各阶段独立模块。
- **golden 数据比对**：VS 输出按语义逐分量与抓帧参考数据比对，支持浮点容差。
- **零第三方依赖**：仅用 Python 标准库；可选的 Mesh 可视化界面用内置 `tkinter`。
- **可视化（可选）**：`tkinter` 界面查看输入/输出网格与光栅化像素。

## 环境要求

- Python 3.8+（仅标准库 + `tkinter`，无需 `pip install`）
- 无构建步骤、无 lint 配置

## 快速开始

```bash
python render.py ./Cases/Default.json
```

唯一的命令行参数是一个 JSON 配置文件。`Cases/Default.json` 是标准入口，它通过 `data_path` 指向 `Cases/` 下的一个抓帧 zip。运行后会在 `Cases/output.log` 写出完整日志。

VS Code 用户可直接用 `.vscode/launch.json` 里的 **"Python hlsl_interpreter"** 配置启动调试。

> 注意：`.vscode/tasks.json` 里的 build task 是无关项目（Irrlicht 示例）的遗留配置，与本项目无关，忽略即可。

## 配置文件说明

配置文件是一个 JSON，常用字段：

| 字段 | 说明 |
|---|---|
| `data_path` | 要运行的抓帧 zip 路径（相对配置文件）。**设置它即走 zip 工作流（当前主路径）** |
| `log_file_path` | 日志输出路径（相对配置文件）。Default 写到 `Cases/output.log` |
| `log_file_mode` | `"w"` 覆盖 / `"a"` 追加 |
| `execute_count` | 执行多少个顶点；`-1` = 全部 |
| `float_tolerance` | golden 比对的浮点容差（Default 为 `0.005`） |
| `early_z` | `true` 在 PS 前做深度测试，`false` 为后置（late-Z） |
| `primitive_topology` | 图元拓扑；不设置时由 `pipeline_state.csv` 决定 |
| `mesh_view_enabled` | 是否打开 tkinter 网格可视化界面 |
| `printSyntaxTree` | 是否在日志中打印每条语句的语法树 |

`Default.json` 示例：

```json
{
    "data_path": "./Cases/Collision-fix-constant-buffer-and-RdotV-zero_event399.zip",
    "log_file_path": "output.log",
    "early_z": true,
    "float_tolerance": 0.005,
    "execute_count": -1,
    "primitive_topology": 4,
    "mesh_view_enabled": true,
    "log_file_mode": "w"
}
```

## 「跑日志验证」工作流

本项目**没有单元测试**，正确性靠运行管线后检查日志来验证：

1. 运行：`python render.py ./Cases/Default.json`
2. 打开 `Cases/output.log`，搜索以 `Error:` 开头的行 —— 每行是 VS 输出与 golden 的逐分量差异：
   ```
   Error: Row 0 Color[0]: output=1.640544 golden=0.431490 diff=1.209054
   ```
3. 反复修正直到没有 `Error:` 行，并出现：
   ```
   Comparison PASSED: All output data matches golden data within tolerance
   ```

`output.log` 同时是主要调试面：开启 `printSyntaxTree` 后会输出每条语句的 `[STMT]` 执行结果和 `[SYNTAX TREE]` 语法树，方便把 bug 定位到具体语句和解析结果。

## 管线架构

`render.py` 是调度器，根据配置分两条路径：

- **zip 工作流（当前主路径）**：解压抓帧 zip 到临时目录，从一堆 CSV/HLSL/BMP 散文件驱动管线（VS/PS 的输入输出由 `main()` 函数参数 + 语义定义，而非 struct）。
- **legacy 工作流**：旧的基于 struct 的路径，配置里没有 `data_path` 时触发，兼容旧格式。

数据流：

```
JSON 配置 → 解压 zip → 加载 cbuffer/顶点/签名
          → VS 解释执行 → 与 golden 比对
          → 光栅化（按拓扑）→ 深度/模板测试
          → PS 解释执行 → 输出合并
```

模块职责：

| 文件 | 职责 |
|---|---|
| `render.py` | 管线调度器（zip / legacy 两条工作流） |
| `hlsl_interpreter.py` | 核心：HLSL 解析（cbuffer/struct/函数/纹理采样器绑定）+ 语句执行 + 表达式求值（~3300 行） |
| `hlsl_syntax_tree.py` | 手写递归下降表达式解析器，产出 `SyntaxTreeNode` 树。**运算符优先级表 `_OPERATORS` 在这里** |
| `rasterizer.py` | 光栅化：按图元拓扑分发，三角形用边函数 + 透视校正重心插值，含视口变换与面剔除 |
| `output_merger.py` | 深度/模板测试，支持 early-Z 与 late-Z |
| `texture.py` | 纹理：BMP 解析、mipmap 生成、最近/线性采样、寻址模式 |
| `pixel.py` | `Pixel` 数据类：光栅化 → 深度 → PS 之间的中间记录 |
| `d3d.py` | D3D 枚举常量（`SHADER_STAGE_*`、图元拓扑） |
| `mesh_view.py` | 可选的 tkinter 可视化界面 |

## 目录结构

```
hlsl_interpreter/
├── render.py                 # 入口 / 管线调度
├── hlsl_interpreter.py       # 核心解释器
├── hlsl_syntax_tree.py       # 表达式语法树解析器
├── rasterizer.py             # 光栅化
├── output_merger.py          # 深度/模板
├── texture.py                # 纹理采样
├── pixel.py / d3d.py         # 数据结构 / 常量
├── mesh_view.py              # 可视化界面
├── Cases/                    # 输入抓帧 zip + 配置 JSON + output.log
├── Prompts/                  # 开发需求/规格记录（驱动开发的提示词历史）
└── Sessions/                 # 88+ 份逐步开发会话日志
```

## 抓帧 zip 内的数据约定

每个 `Cases/*.zip` 解压后是一个顶层文件夹，包含：

- `VS_shader.hlsl` / `PS_shader.hlsl` —— 着色器源码（`main()` 用**带语义的参数**声明输入输出，如 `out float4 o0 : SV_POSITION0`）
- `VS_input_output_signature.csv` / `PS_input_output_signature.csv` —— 签名，用于参数 ↔ 寄存器槽位映射
- `ia_vertex_data.csv` + `ia_input_layouts.csv` —— 输入装配阶段的顶点数据与布局
- `VS_constant_buffers.csv` / `PS_constant_buffers.csv` —— 常量缓冲区
- `pipeline_state.csv` —— 光栅化/混合/深度模板状态 + 图元拓扑
- `MeshOut_*_mesh.csv` —— VS 输出的 golden 参考数据
- 纹理为 BMP；`.dxbc` / `_disasm.txt` / `.bin` 为参考产物，解释器不消费

## ⚠️ 两个重要的坑

1. **3Dmigoto 末尾 `float3` 错位**：`MeshOut_*_mesh.csv` 的 golden 数据中，末尾的 `float3` 输出（如 WORLDPOS）会整体错位一个 float —— `WORLDPOS.x` 实际是 `o.y`，`WORLDPOS.y` 是 `o.z`，`WORLDPOS.z` 是**下一个顶点**的数据（垃圾值）。所以**正确的解释器输出可能反而和 golden 对不上**。改解释器之前先验证数学是否正确：如果 `SV_POSITION`(float4) 和 `NORMAL`(float3) 这些对齐可靠的输出都匹配，那么出错的末尾 float3 几乎一定是 golden 错位，而非解释器 bug。详见 `Prompts/hlsl-interpreter-prompt-ClaudeCode.md` 第 2~4 节。

2. **一元 vs 二元 `+/-`**：表达式 `r0.xyz * -r2.xxx + r1.xyz` 中 `*` 后面的 `-` 是一元负号而非减法。解析器在 `_find_top_level_operator_cached` 中回看前一个非空字符来区分。这类优先级问题通常表现为颜色/向量结果错误，而不是崩溃。

## 开发历史文档

- `Prompts/` —— 驱动开发的需求与规格记录，`hlsl-interpreter-prompt-ClaudeCode.md` 是近期 bug 修复与设计决策最详细的记录。
- `Sessions/` —— 88+ 份逐步开发会话日志（`hlsl-stepN-*.md`），适合追溯「为什么这样实现」。
