# Step 185 — HTML 网格视图 + 三态显示选择机制

## 任务

1. 模仿 MeshView 创建一个 HTML 页面展示；
2. HTML 的数据窗口与 MeshView 一致；
3. 提供选择机制：不显示 / MeshView（tk）/ HTML 页面。

## 设计

**同 API 双实现**：`HtmlMeshView`（新 `html_mesh_view.py`）暴露与
`mesh_view.MeshView` **完全相同的公开方法**（管线实际调用的那些：
`set_input_data`/`set_output_data`/`set_rasterizer_pixels`/
`set_pixel_shader_output`/`set_output_merger_pixels`/`set_pipeline_stats`/
`set_primitive_topology`/`clear`/`show`/`close`，加 `_draw_*` 空钩子与
re-execute 空钩子）。管线无需区分二者——`self._mesh_view` 是谁都照常调用。

**不画 tk canvas，改累积数据 + 输出自包含 HTML**：`show()` 时把所有数据窗口
序列化为 JSON，注入到一份内联了 canvas 渲染 JS 的 HTML 模板
（`_HTML_TEMPLATE` 的 `/*__PAYLOAD__*/{}` 标记处），写文件并
`webbrowser.open('file://…')`。零外部依赖、可直接 file:// 打开。

**数据窗口与 MeshView 一致**（HTML 内 JS 复刻其绘制）：
- 顶部**统计栏**：输入/输出顶点数、拓扑名、管线统计（verts/prims/clipped/
  culled/rast px/depth fail/PS px）——与 `_update_info` 完全一致的字段与措辞；
- **Input Vertices** canvas：3D 投影拓扑线框（复刻 `_transform_vertex` 的
  旋转 + `_project` 的缩放/居中；三角形列表/带、线列表/带、点列表全支持）；
- **Output** 选项卡：VS Result 线框 + Rasterizer / Pixel Shader / Output Merger
  像素图（复刻 `_prim_color`（primitive_id→稳定色）与 `_shaded_color_fn`
  （ps_output_color 优先））；
- **Selected Vertex Info** 面板：右键选顶点，显示输入/输出顶点的
  Position/Normal/Color/TexCoord（复刻 `_update_vertex_info_panel`）；
- **交互**：拖拽旋转、滚轮缩放、右键选点——与 tk 视图一致。

**像素数据压缩**：每个 Pixel 压成 `[x, y, primitive_id, psR, psG, psB]`
（无 ps 输出时 RGB=-1），避免大像素集撑爆 payload。

## 选择机制（三态）

- config 新增 `mesh_view_mode`：`"none"` | `"tk"` | `"html"`；
- **向后兼容**：无 `mesh_view_mode` 时回退旧布尔 `mesh_view_enabled`
  （true→tk、false→none）——所有既有 Cases 配置零改动仍按原样工作；
- `render._resolve_mesh_view_mode(config)` 统一解析；两个工作流
  （zip / legacy）都据此调 `enable_mesh_view(mode=..., html_path=...)`；
- `HLSLInterpreter.enable_mesh_view` 改为按 mode 分派：none 不建视图、
  tk 建 `MeshView`、html 建 `HtmlMeshView`（各自可用性缺失时安全降级并告警）；
  旧签名 `enable_mesh_view(True/False)` 保留兼容。
- HTML 写到日志同目录的 `mesh_view.html`。

## 验证

- **HTML 生成**：合成数据（1 三角形 + 24 像素 + 完整 stats）驱动
  `HtmlMeshView`，产物 11.8KB、`<!doctype>` 开头、`__TITLE__` 占位已替换、
  payload 注入成功、JSON 反解含全部 9 键、input/output/rast/om/stats 计数正确。
- **接线单测**：`_resolve_mesh_view_mode` 对 html/tk/none/legacy-true/
  legacy-false/缺省/非法值 7 种输入全部正确；`enable_mesh_view` 对
  html/none/legacy 分派出正确的视图类型与启用标志。
- **回归**：（见下）默认配置 `mesh_view_enabled:false` → none，应零影响。

## 结果

- **Cases 回归 152/152 全 PASS 零回归**：所有既有配置 `mesh_view_enabled:false`
  经兼容回退解析为 `none`，行为与改动前完全一致。
- 新增 `html_mesh_view.py`（`HtmlMeshView`）；`hlsl_interpreter.enable_mesh_view`
  三态分派；`render._resolve_mesh_view_mode` + `_mesh_view_html_path` 接线两工作流。
- 用法：在 Cases JSON 里设 `"mesh_view_mode": "html"`（或 `"tk"` / `"none"`）；
  HTML 写到日志同目录的 `mesh_view.html` 并自动在浏览器打开。

## 三种显示方式对照

| mesh_view_mode | 行为 | 依赖 |
|---|---|---|
| none（默认） | 不显示 | 无（回归/批量用） |
| tk | tkinter MeshView 窗口（可 re-execute） | tkinter |
| html | 写自包含 HTML 并开浏览器 | 无（跨平台、可分享） |
