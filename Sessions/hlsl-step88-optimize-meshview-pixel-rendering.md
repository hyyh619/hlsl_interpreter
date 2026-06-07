# Step 88 - 优化 MeshView 顶点/像素绘制性能

## 背景与问题

运行像 `Collision-fix-constant-buffer-and-RdotV-zero_event104.zip` 这种 case 时，
光栅化后需要在 MeshView 里显示约 **14 万个像素点**。此时 MeshView 界面极其卡顿：
窗口打开慢、旋转/缩放/平移几乎不可操作。

## 思考与根因分析

定位到 `mesh_view.py` 里三个像素绘制方法：

- `_draw_rasterizer_pixels`
- `_draw_pixel_shader_pixels`
- `_draw_output_merger_pixels`

它们都用 **每个像素一个 `canvas.create_rectangle(...)`** 的方式绘制。问题有两层：

1. **canvas item 数量爆炸。** 14 万像素 × 3 个画布 ≈ **42 万个持久化 canvas item**。
   tkinter 的 Canvas 会为每个 item 维护对象、参与命中测试、并在每次重绘时全部重新渲染。
   item 数量上万后就会出现严重卡顿——这是 tkinter Canvas 的固有特性，不是数据量本身的问题。

2. **重绘耦合。** `_draw_mesh_animated`（VS mesh 的核心重绘函数）里**无条件**调用了
   `_draw_rasterizer_pixels()` 和 `_draw_pixel_shader_pixels()`。也就是说，用户在
   "VS Result" 视图里每旋转/缩放/平移一次顶点 mesh，都会把另外两个像素画布的几十万个
   矩形**全部重建一遍**，即使那些画布当前根本不可见。

此外，像素画布自身的平移/缩放（`_on_mouse_drag_rasterizer` / `_on_mouse_wheel_rasterizer`）
调用的是全量的 `self._draw_mesh()`，把无关的 VS mesh 也一起重画了；
Pixel Shader 和 Output Merger 画布的交互回调甚至是空的 `pass`。

## 解决方案

核心思路：**用一张 `tk.PhotoImage` 取代成千上万个矩形 item**，并把"构建"与"显示"解耦、缓存复用。

### 1. 单张 PhotoImage 渲染（核心）

新增 `_build_pixel_base_image(pixels, color_fn)`：

- 计算像素包围盒，分配一张 `width×height`（原始分辨率，1 像素 = 1 图像点）的 PhotoImage。
- 背景填满画布底色，再逐像素覆盖颜色，拼成 Tk 的行块字符串 `"{c c c} {c c c} ..."`，
  用**一次** `img.put(data)` 整张写入。
- 结果（base image + 包围盒）返回并缓存。

新增 `_draw_pixels_image(canvas, pixels, color_fn, cache_key)`，统一三个画布的绘制：

- 用 `(id(pixels), len(pixels))` 作为指纹，像素数据没变就**复用缓存的 base image，不重建**。
- 缩放用 PhotoImage 的整数 `zoom()` / `subsample()`，并按缩放因子缓存放大/缩小后的图。
- 平移只改变 `create_image` 的位置，**几乎零成本**。

三个原方法改为薄封装，方法名与签名不变（`render.py` 的调用无需改动）：

- `_draw_rasterizer_pixels` → 按 `primitive_id` 着色（`_rasterizer_color_fn`）
- `_draw_pixel_shader_pixels` / `_draw_output_merger_pixels` → 有 `ps_output_color` 用之，
  否则回退 primitive 着色（`_shaded_color_fn`）

`primitive_id → hex 颜色` 用 `_prim_color_map` 做 memo 缓存，避免每像素重复算三角函数。

### 2. 解耦重绘

- 从 `_draw_mesh_animated` 中**移除**对像素画布的两处绘制调用。VS mesh 的旋转/缩放/平移
  不再触碰像素画布。
- 新增 `_redraw_active_pixel_tab()`：只重绘当前可见的 output 子标签对应的像素画布。
- 给 output `Notebook` 绑定 `<<NotebookTabChanged>>`，切标签时只刷新该标签。
- 像素画布的平移/缩放统一走 `_pan_pixel_view` / `_zoom_pixel_view`（三个像素画布共享
  scale/offset），并把原本为空的 Pixel Shader / Output Merger 交互回调接上。
- `_on_resize` 额外刷新一次活动像素标签，使图像随窗口重新居中。

## 验证

由于项目无单测，写了一个独立 smoke test：构造 **14 万**个稠密像素（400×350 区域），
用真实 Tk Canvas 走新的绘制路径，测量耗时与 canvas item 数：

```
pixel count: 140000
first draw (build+render): 0.231s
canvas items: 1            # 之前是 140000 个
pan redraw (cached):  0.000s
zoom redraw (rescale): 0.005s
```

`python -c "import ast; ast.parse(open('mesh_view.py').read())"` 通过。

## 结果总结

| 操作 | 优化前 | 优化后 |
|------|--------|--------|
| 画布 item 数（14 万像素） | ~140,000 个 | **1 个** |
| 首次绘制 | 数秒、卡顿 | ~0.23s |
| 平移像素视图 | 重建 14 万 item（卡死） | **0.000s** |
| 缩放像素视图 | 重建 14 万 item | ~0.005s |
| 旋转/缩放 VS mesh | 连带重建像素画布 | 不再触碰像素画布 |

关键收益来自把"每像素一个 canvas item"换成"一张 PhotoImage"，并让平移/缩放只移动/整数缩放
缓存好的图像，而不是重建几何。功能行为（着色规则、坐标映射、各阶段语义）保持不变，
`render.py` 调用接口未改动。

## 涉及文件

- `mesh_view.py`
  - `__init__`：新增 `_pixel_caches`、`_prim_color_map` 缓存。
  - 新增 `_prim_color` / `_rasterizer_color_fn` / `_shaded_color_fn` /
    `_build_pixel_base_image` / `_draw_pixels_image` / `_redraw_active_pixel_tab` /
    `_pan_pixel_view` / `_zoom_pixel_view`。
  - 重写 `_draw_rasterizer_pixels` / `_draw_pixel_shader_pixels` /
    `_draw_output_merger_pixels` 为薄封装。
  - `_draw_mesh_animated`：移除像素画布重绘调用。
  - `_setup_ui`：绑定 Notebook 标签切换。
  - 重写像素画布的鼠标拖动/滚轮回调；`_on_resize` 刷新活动像素标签。
