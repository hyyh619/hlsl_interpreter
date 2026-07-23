# HLSL Interpreter

一个用**纯 Python** 实现的 Direct3D 11 图形管线软件模拟器。它**直接解释执行 HLSL 着色器源码**（不编译、不依赖 `eval`），跑完整条管线 —— 顶点着色器 → 光栅化 → 深度/模板测试 → 像素着色器 → 输出合并 —— 并把顶点着色器的输出与抓帧得到的「golden」参考数据逐分量比对，用来验证解释执行的正确性。

输入数据来自 **RenderDoc / 3Dmigoto** 抓帧导出、打包成 zip 的一帧绘制数据。

> **主要用途**：在没有 GPU / 调试器的情况下，逐语句复现并排查某次 Draw Call 的 HLSL 执行逻辑，定位渲染结果不对的根因（颜色错误、坐标错位、矩阵乘法、swizzle、反射向量等）。

---

## 特性

- **HLSL 解释执行**：手写递归下降表达式解析器，构建语法树后求值；支持 swizzle（`r0.xyz`）、类型转换（`(int2)`）、按位运算（`| & << >> ^ ~ %`）、十六进制字面量、一元/二元 `+/-` 区分、三目运算符、矩阵/向量内建函数（`mul` / `dot` / `normalize` / `reflect` / `length` / `sincos` / `exp2` / `f16tof32` / `f32tof16` 等）。
- **完整管线**：VS → Rasterizer → Depth/Stencil → PS → Output Merger，各阶段独立模块，支持 early-Z 与 late-Z。
- **golden 数据比对**：VS 输出按语义逐分量与抓帧参考数据比对，支持浮点容差。
- **顶点格式解码**：按输入布局解码各类顶点属性；`B8G8R8A8` / `B8G8R8X8` 等 BGRA 格式自动做 R/B 通道交换，交叉复用输出寄存器按 D3D 语义补默认值（step 194–202）。
- **纹理采样**：BMP 纹理、mipmap 生成、最近/线性采样、wrap/mirror/clamp/border 寻址模式。
- **GPU 精度模拟**：VS 阶段模拟 GPU float32 算术（非规格化数 FTZ，与 GPU 行为一致）。
- **零第三方依赖**：仅用 Python 标准库；可选的 Mesh 可视化界面用内置 `tkinter`。
- **回归测试套件**：`run_regression.py` 批量验证所有已知正确的抓帧，修改后一键检查有无回退（当前 **191 个用例，191/191 全绿**）。
- **新版 draw-zip 格式**：支持 LZMA 压缩成员 + 去重别名重建的抓帧包（step 209）；raw-bits 字面量/缓冲索引、`InstanceOffset`、整型 VS 输出按位重解释比较等（step 210–217）。
- **Debug Trace**：可配置的逐像素管线数据导出，用于定位 PS / 纹理采样的精度问题。
- **可视化（可选）**：三选一显示后端 —— `tkinter` 桌面界面、静态 HTML 导出、或**动态 Web 视图**（浏览器实时跟随 VS/光栅化/PS 执行进度逐顶点/逐图元/逐像素动画，支持 normal 向量显示与每阶段动画延迟调节）。Web 视图还支持像素视图缩放、阶段回放、逐指令 VS/PS 轨迹、可拖拽自定义布局、独立的顶点/像素信息面板，以及「Draw Data」面板直接浏览预览原始 draw zip（step 189–193）。

---

## 环境要求

- Python 3.8+（仅标准库 + `tkinter`，无需 `pip install`）
- 无构建步骤、无 lint 配置、无 `requirements.txt`
- macOS / Windows / Linux 均支持

---

## 快速开始

```bash
python render.py ./Cases/Default.json
```

唯一的命令行参数是一个 JSON 配置文件。`Cases/Default.json` 是标准入口，它通过 `data_path` 指向 `Cases/` 下的一个抓帧 zip。运行后会在 `Cases/output.log` 写出完整日志。

**VS Code 用户**：直接用 `.vscode/launch.json` 里的 **"Python hlsl_interpreter"** 配置启动调试。

> `.vscode/tasks.json` 里的 build task 是无关项目（Irrlicht 示例）的遗留配置，忽略即可。

---

## 配置文件说明

配置文件是一个 JSON，所有路径均相对于配置文件所在目录。

### 完整字段参考

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `data_path` | string | — | 抓帧 zip 路径。**设置此字段即走 zip 工作流（主路径）** |
| `log_file_path` | string | `"hlsl_interpreter.log"` | 日志输出路径 |
| `log_file_mode` | string | `"a"` | `"w"` 覆盖 / `"a"` 追加 |
| `log_to_file` | bool | `true` | 是否写日志文件（false 时只打印到 stdout） |
| `execute_count` | int | `-1` | 执行多少个顶点；`-1` = 全部 |
| `float_tolerance` | float | `0.0001` | VS 输出与 golden 比对的浮点容差 |
| `pixel_tolerance` | float | `0.01` | PS 像素颜色比对的容差 |
| `depth_tolerance` | float | `0.01` | 深度值比对的容差 |
| `early_z` | bool | `true` | `true` = PS 前做深度测试；`false` = late-Z（PS 后） |
| `primitive_topology` | int | — | 图元拓扑枚举值；不设置时由 `pipeline_state.csv` 决定 |
| `max_workers` | int | `1` | VS 并行线程数（目前建议保持 1） |
| `print_sequence` | int | `1` | 每 N 个顶点打印一次进度；大数据集设大值可减少日志量 |
| `printSyntaxTree` | bool | `true` | 是否在日志中打印每条语句的语法树（调试用，大数据集建议 `false`） |
| `print_interpreter_result` | bool | `true` | 是否打印每条语句的求值结果 |
| `mesh_view_enabled` | bool | `false` | 是否打开 tkinter 网格可视化界面 |
| `debug_trace` | object | — | 逐像素管线数据导出（见下节） |

### Default.json 示例

```json
{
    "data_path": "./Cases/witcher3_countryside_event7301.zip",
    "log_file_path": "output.log",
    "log_file_mode": "w",
    "log_to_file": true,
    "printSyntaxTree": false,
    "print_interpreter_result": false,
    "print_sequence": 10000,
    "float_tolerance": 0.005,
    "pixel_tolerance": 0.1,
    "depth_tolerance": 0.01,
    "execute_count": -1,
    "max_workers": 1,
    "primitive_topology": 4,
    "mesh_view_enabled": true,
    "early_z": true,
    "debug_trace": {
        "enabled": false,
        "file": "pipeline_debug.log",
        "ps_pixels": true,
        "texture_lod": true,
        "derivatives": false,
        "target_pixels": ["366,354", "408,367"]
    }
}
```

### Debug Trace 字段说明

`debug_trace` 块启用时，会把详细的管线中间数据写入一个单独的日志文件，用于排查 PS / 纹理采样的精度问题。

| 子字段 | 类型 | 说明 |
|---|---|---|
| `enabled` | bool | 主开关；`false` 时整个块不生效 |
| `file` | string | 输出文件路径；默认为 `<zip 名>_debug.log` |
| `ps_pixels` | bool | 记录 PS 每个像素的输入 + 输出颜色 |
| `texture_lod` | bool | 记录每次 `Texture.Sample` 的 u/v/LOD/导数/采样结果 |
| `derivatives` | bool | 记录屏幕空间 UV 梯度（quad 差分） |
| `target_pixels` | array | 只追踪指定像素 `["x,y", ...]`；不填或空 = 追踪所有像素（数据量大慎用） |

---

## 「跑日志验证」工作流

本项目**没有单元测试**，正确性靠运行管线后检查日志来验证：

1. 运行：`python render.py ./Cases/Default.json`
2. 打开 `Cases/output.log`，搜索以 `Error:` 开头的行 —— 每行是 VS 输出与 golden 的逐分量差异：
   ```
   Error: Row 0 Color[0]: output=1.640544 golden=0.431490 diff=1.209054
   ```
3. 反复修正，直到没有 `Error:` 行，并出现：
   ```
   Comparison PASSED: All output data matches golden data within tolerance
   ```

`output.log` 中的 `[STMT]` 和 `[SYNTAX TREE]` 行是主要调试面：开启 `printSyntaxTree` 后，每条语句都会打印解析结果和求值过程，可快速定位 bug 到具体语句。

---

## 回归测试

每次修改代码后，用回归套件验证所有已知用例没有回退：

```bash
python run_regression.py
```

`run_regression.py` 读取 `Cases/regression_test_zip_files.csv`（每行一个 zip 文件名），对每个 zip 无界面地跑完整管线，把日志写到 `Cases/regression_logs/<zip名>.log`，最后打印 PASS/FAIL 汇总。

**通过条件**（三项全满足）：
1. `render.py` 正常退出（无异常）
2. 日志中无 `Error:` 行
3. `Total PASSED rows: X/Y` 中 `X == Y`

添加或删除测试用例：直接编辑 `Cases/regression_test_zip_files.csv`。

---

## MeshView 可视化界面

设置 `"mesh_view_enabled": true` 后，运行完管线会自动弹出 tkinter 窗口。

### 窗口布局

窗口分两大区域：

- **左侧**：输入网格（VS 前）和输出网格（VS 后），显示线框 + 可选法线
- **右侧**（Notebook 标签页）：
  - **Rasterizer**：光栅化后的像素（原始插值颜色）
  - **Pixel Shader**：PS 着色后的像素颜色
  - **Output Merger**：经过深度/模板测试后的最终像素

### 交互操作

| 操作 | 说明 |
|---|---|
| 拖拽（左键）| 旋转网格视角 |
| 右键单击顶点 | 显示该顶点的属性信息 |
| 滚轮 | 缩放 |
| 工具栏按钮 | 缩放 +/-、旋转（↺↻↑↓）、平移（◀▶▲▼）、Reset |
| Show Normals | 显示/隐藏法线方向箭头 |
| Animation Play/Pause/Prev/Next | 动画帧步进（如有动画配置） |
| Active: Input / Output | 切换左键拖拽作用在输入还是输出网格 |

像素面板支持鼠标拖拽和滚轮缩放，点击像素可显示详细颜色值。

### 控制台命令（运行期间）

管线跑完后，控制台会等待输入：

```
Enter 'x' to exit, 'o' to open MeshView:
```

| 命令 | 说明 |
|---|---|
| `x` | 关闭窗口并退出 |
| `o` | 重新打开/显示 MeshView 窗口 |

### macOS 注意事项

macOS 的 Cocoa 框架要求所有 GUI 操作在主线程执行。本工具已针对 macOS 做了适配：启用 MeshView 时，主线程运行 `tkinter` 事件循环，管线在后台线程执行，GUI 更新通过 `root.after()` 安全地调度到主线程。用户无需做任何额外设置。

---

## 管线架构

`render.py` 是调度器，根据配置分两条路径：

- **zip 工作流（当前主路径）**：解压抓帧 zip 到临时目录，从 CSV/HLSL/BMP 散文件驱动管线。VS/PS 的 `main()` 函数用**带语义的参数**声明输入输出（`out float4 o0 : SV_POSITION0`），而非 struct。
- **legacy 工作流**：旧的基于 struct 的路径，配置里没有 `data_path` 时触发，兼容旧格式 JSON。

数据流：

```
JSON 配置
  └─→ 解压 zip → 加载 cbuffer / 顶点数据 / 签名
        └─→ VS 解释执行 → 与 golden 比对
              └─→ 光栅化（按图元拓扑）
                    └─→ 深度/模板测试（early-Z）
                          └─→ PS 解释执行 → 输出合并
                                └─→ 深度/模板测试（late-Z，若配置）
```

模块职责：

| 文件 | 职责 |
|---|---|
| `render.py` | 管线调度器（zip / legacy 两条工作流） |
| `hlsl_interpreter.py` | 核心：HLSL 解析（cbuffer/struct/函数/纹理绑定）+ 语句执行 + 表达式求值（~3 300 行） |
| `hlsl_syntax_tree.py` | 手写递归下降表达式解析器，产出 `SyntaxTreeNode` 树。**运算符优先级表 `_OPERATORS` 在这里** |
| `rasterizer.py` | 光栅化：按图元拓扑分发，三角形用边函数 + 透视校正重心插值，含视口变换与面剔除 |
| `output_merger.py` | 深度/模板测试，支持 early-Z 与 late-Z |
| `texture.py` | 纹理：BMP 解析、mipmap 生成、最近/线性采样、寻址模式（wrap/mirror/clamp/border） |
| `pixel.py` | `Pixel` 数据类：光栅化 → 深度 → PS 之间的中间记录 |
| `d3d.py` | D3D 枚举常量（`SHADER_STAGE_*`、图元拓扑） |
| `debug_trace.py` | 逐像素管线数据导出（Debug Trace 功能） |
| `mesh_view.py` | 可选的 tkinter 可视化界面（网格 + 像素 + 动画） |
| `html_mesh_view.py` | 静态 HTML 网格导出（离线查看，无需 tkinter） |
| `web_mesh_view.py` | 动态 Web 视图：本地 HTTP 服务，浏览器实时跟随 VS/光栅化/PS 进度动画，含 normal 显示与每阶段动画延迟 |
| `run_regression.py` | 回归测试套件驱动脚本 |

---

## 目录结构

```
hlsl_interpreter/
├── render.py                  # 入口 / 管线调度
├── hlsl_interpreter.py        # 核心解释器（~3 300 行）
├── hlsl_syntax_tree.py        # 表达式语法树解析器
├── rasterizer.py              # 光栅化
├── output_merger.py           # 深度/模板测试
├── texture.py                 # 纹理采样
├── debug_trace.py             # 逐像素调试数据导出
├── pixel.py                   # Pixel 数据类
├── d3d.py                     # D3D 枚举常量
├── mesh_view.py               # tkinter 可视化界面
├── html_mesh_view.py          # 静态 HTML 网格导出
├── web_mesh_view.py           # 动态 Web 视图（实时进度动画 + 延迟调节）
├── run_regression.py          # 回归测试套件
├── Cases/
│   ├── Default.json           # 标准运行配置（入口）
│   ├── *.zip                  # 抓帧数据包
│   ├── output.log             # 运行日志（Default 配置输出到此）
│   ├── regression_test_zip_files.csv   # 回归测试用例列表
│   └── regression_logs/       # 每个测试用例的独立日志
├── Prompts/                   # 开发需求 / 规格记录（驱动开发的提示词历史）
└── Sessions/                  # 217+ 份逐步开发会话日志（hlsl-stepN-*.md）
```

---

## 抓帧 zip 内的数据约定

每个 `Cases/*.zip` 解压后是一个顶层文件夹，包含：

| 文件 | 说明 |
|---|---|
| `VS_shader.hlsl` / `PS_shader.hlsl` | 着色器源码（`main()` 用带语义的参数声明输入输出） |
| `VS_input_output_signature.csv` / `PS_input_output_signature.csv` | 签名，用于参数 ↔ 寄存器槽位映射 |
| `ia_vertex_data.csv` + `ia_input_layouts.csv` | 输入装配阶段的顶点数据与布局 |
| `VS_constant_buffers.csv` / `PS_constant_buffers.csv` | 常量缓冲区（支持 float / uint / binary 混合格式） |
| `pipeline_state.csv` | 光栅化/混合/深度模板状态 + 图元拓扑 |
| `MeshOut_*_mesh.csv` | VS 输出的 golden 参考数据 |
| `*.bmp` | 纹理文件（BMP 格式） |
| `*.dxbc` / `*_disasm.txt` / `*.bin` | 参考产物，解释器不消费 |

---

## ⚠️ 两个重要的坑

### 1. 3Dmigoto 末尾 `float3` 错位

`MeshOut_*_mesh.csv` 中，末尾的 `float3` VS 输出（如 WORLDPOS）整体错位一个 float：
- `WORLDPOS.x` 实际是 `output.y`
- `WORLDPOS.y` 实际是 `output.z`
- `WORLDPOS.z` 是**下一个顶点**的数据（垃圾值，比对时跳过）

因此，**正确的解释器输出可能反而和 golden 对不上**。改解释器之前先验证数学：如果 `SV_POSITION`（float4）和 `NORMAL`（float3 但非末尾）等对齐可靠的输出都匹配，则出错的末尾 float3 几乎一定是 golden 错位，而非解释器 bug。

详见 `Prompts/hlsl-interpreter-prompt-ClaudeCode.md` 第 2~4 节。

### 2. 一元 vs. 二元 `+/-`

表达式 `r0.xyz * -r2.xxx + r1.xyz` 中，`*` 后的 `-` 是一元负号而非减法。解析器在 `_find_top_level_operator_cached` 中回看前一个非空字符来区分：若前一个字符是运算符/分隔符（`+-*/%(,[|&!<>=`），则视为一元，跳过作为二元分裂候选。

这类优先级 bug 的表现通常是颜色/向量结果错误，而非崩溃。

---

## 常见问题

**Q: 运行后日志里全是 `Error:`，但我没改任何代码？**

先检查 `float_tolerance` 是否太小（Default 用 `0.005`），以及 `execute_count` 是否 `-1`（全部）。如果换了抓帧 zip，确认 golden 数据格式是否有 float3 错位问题（见上节）。

**Q: 启用 MeshView 后窗口没出现？**

确认 Python 安装包含 `tkinter`（`python -c "import tkinter"` 无报错）。macOS 上系统自带 Python 可能缺少 tkinter，建议用 Homebrew 安装的 Python。

**Q: 回归测试某个用例 FAIL，但手动运行 OK？**

回归测试跑的是 headless 模式（禁用 MeshView），用例日志在 `Cases/regression_logs/<zip名>.log`，grep `Error:` 确认具体失败分量。

**Q: `printSyntaxTree: true` 日志太大跑得很慢？**

大数据集（几千顶点）建议设 `"printSyntaxTree": false, "print_interpreter_result": false, "print_sequence": 10000`，只保留必要输出。

---

## 开发历史文档

- `Prompts/hlsl-interpreter-prompt-ClaudeCode.md` —— 近期 bug 修复与设计决策最详细的记录（按步骤编号索引）
- `Sessions/hlsl-stepN-*.md` —— 217+ 份逐步开发会话日志，适合追溯「为什么这样实现」
