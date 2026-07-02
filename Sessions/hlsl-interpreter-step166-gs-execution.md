# Step 166 — 实现 GS（几何着色器）执行（分步任务第 1 步）

## 任务
实现 GS/DS/HS 执行 + Tessellation 固定功能，分步：**先 GS**，再 DS/HS/Tessellation；每步全量跑对应 case。
本步 = **GS 执行**。

## GS case（共 9，均有 gs_mesh.bin golden）
Octopath event102/event4014（passthrough triangle strip）；manhattan event50/87/124/161/198（粒子发射器 GS，
point→展开）；sekiro2_event14998；sekiro4_event20560。

## GS 执行模型（实现）
1. 跑 VS → 每顶点输出（canonical key）。
2. **primitive 组装** `_assemble_primitives(idx_list, topology)`：point(1)/line(2)/linestrip(3)/trianglelist(4)/
   **trianglestrip(5)**。strip 绕序：偶 tri `(i,i+1,i+2)`、奇 tri `(i,i+2,i+1)`（末两个交换，与 gs_mesh golden 核对确定）。
3. 每 primitive 建 `v[i][j]`（i=primitive 顶点、j=GS 输入 slot，按 semantic 映射到 VS 输出 key）；系统值
   SV_VertexID→该顶点 draw 索引、SV_InstanceID→实例号。
4. 执行 GS main：`stream.Append(...)` 快照当前输出寄存器为一个输出顶点、`stream.RestartStrip()` 分隔。
5. 收集 emit 顶点 → 与 gs_mesh golden 比较（复用 `compare_vs_output_with_golden_params`，打印总数/成功数 + 前几个
   `Error:` 例子）。

## 实现细节
- `hlsl_interpreter.py`：
  - `_assemble_primitives`（拓扑→primitive 顶点组）。
  - `executeGS_with_params`（slot→canonical 映射 + SV 系统值 + 遍历 primitive×instance 调 `_execute_gs_main`，收集 emit）。
  - `_execute_gs_main`（设 `v` 2D + 输出初始化为 (0,0,0,1)/默认，跑语句，emit 经 Append 回调）。
  - **2D 索引** `v[i][j].swz`：`get_value` 数组分支支持嵌套 `[j]`。
  - **Append/RestartStrip 拦截**：`execute_statement` 顶部在 GS 模式（`self._gs_emit` 非 None）识别
    `<stream>.Append(...)`/`.RestartStrip()`；`__init__` 加 `_gs_emit`/`_gs_restart=None`。
- `render.py`：`_run_gs_stage`（建 gs_interp、解析 GS shader/cbuffer/signature、跑 GS、加载 gs_mesh golden、比较），
  VS 比较后调用（vs_only 也跑）；`_draw_input_topology`（从 draw_call_info 的 PrimitiveTopology 取输入拓扑）。
  GS 日志经 `vs_interp.log_output` 输出（gs_interp 自身缓存不 flush，否则丢失）。

## 全量运行 9 个 GS case 结果
- **Octopath event102 / event4014：GS 6/6 PASS**（passthrough triangle strip，端到端正确）。修复过程：strip 绕序
  先错（3/6→4/6→6/6，最终确定偶 `(i,i+1,i+2)`/奇 `(i,i+2,i+1)`）。
- **manhattan ×5（粒子发射器）**：VS 999/1000（另一 VS 问题）；GS **emit 0 vs golden 3000**。GS 条件 spawn
  依赖 `emitter_startBirthIdx/endBirthIdx`（我方解出 411/412 → 仅 1 顶点应 spawn，但 golden=3000=全 1000×3），
  **疑 GS cbuffer（3 个 float4x4 + uint）偏移解码有误**，需逐案追。
- **sekiro2_event14998 / sekiro4_event20560**：类似 emit-count 不符。
- 新增 emit-vs-golden 计数不符的显式 `Error:` 行（避免 0/0 假通过）。

## 结果
- **GS 执行核心已实现并验证**（passthrough 2 case 6/6）；primitive 组装/`v[i][j]`/Append/RestartStrip/SV 系统值齐备。
- **回归 125/125 零回归**（event102 在回归内，同时守护 VS+GS）。
- 复杂 GS（粒子 emit-count、GS cbuffer 偏移；sekiro）未全通过——记为待查，非 passthrough 执行框架之误。

## 后续（下一步 = DS/HS + Tessellation）
1. DS/HS 执行 + Tessellation 固定功能（task 第 3 步，step 167）。
2. 粒子 GS：GS cbuffer（emitter_*）偏移解码修正 → manhattan/sekiro emit-count 对齐。
