# Step 122 — Load pre-draw depth/stencil buffer from raw DSV resource dump

## 任务 (Prompts)

1. 执行 draw 前，需要加载 pre-draw 的 depth/stencil buffer 的内容，以前是使用 `pre_draw_depth_stencil.csv`
2. 现在替换成加载原始的 depth/stencil buffer 的数据文件 `pre_draw_ds_res_*.raw`
3. 加载 `pre_draw_ds_res_*.raw` 的具体数据格式解析，请根据 `output_merger.csv` 来获取
4. 改动完成后执行 regression test

## 思考与分析

### 现状
- `render.py` 在 depth/stencil 阶段调用 `depth.load_pre_draw_depth_stencil(pre_draw_ds_csv)` 读取已解码的
  `pre_draw_depth_stencil.csv`（列为 `X,Y,Depth,Stencil`，逐像素一行）来初始化 depth/stencil buffer。
- CSV 是 RenderDoc 解码后的文本，depth 只有 6 位小数精度。原始 `.raw` 是 GPU 资源的字节级 dump，精度完整。

### `output_merger.csv` 描述资源布局
每个 zip 内的 `output_merger.csv` 的 `DSV` 行给出了 depth/stencil 资源的全部布局信息：

```
ViewType,Slot,ResourceId,ResourceType,ViewFormat,Width,Height,...,SampleCount
DSV,,50,Texture2D,D24_UNORM_S8_UINT,640,480,...,1
```

- `ResourceId` → raw 文件名中的 id（`pre_draw_ds_res_50_640x480_D24_UNORM_S8_UINT.raw`）
- `ViewFormat` → 决定字节解码方式
- `Width`/`Height` → 行主序紧密排布的尺寸
- `SampleCount` → >1 时为 MSAA，dump 被拆成 `..._sample0.raw` ... `..._sample7.raw`

### 格式解码（已用现有 CSV 作为 ground truth 验证）

| 格式 | stride | depth 解码 | stencil 解码 |
|------|--------|-----------|-------------|
| `D24_UNORM_S8_UINT`   | 4 字节 | `(word & 0xFFFFFF) / 0xFFFFFF`（低 24 位） | `(word >> 24) & 0xFF`（高 8 位） |
| `D32_FLOAT_S8X24_UINT`| 8 字节 | offset+0 处 float32 | offset+4 处 uint32 & 0xFF |
| `D32_FLOAT`           | 4 字节 | float32 | 0 |
| `D16_UNORM`           | 2 字节 | `u16 / 0xFFFF` | 0 |

**验证结果（对照 `pre_draw_depth_stencil.csv`）:**
- `Collision-event104`（D24_UNORM_S8_UINT，640×480）：低 24 位 depth + 高 8 位 stencil 完全匹配 CSV（例如像素 (20,0) raw word=16755103 → depth=0.998682 == CSV）。
- `4k1w_event1124`（D32_FLOAT_S8X24_UINT，1920×1200，SampleCount=8）：读取 `sample0`，全部 2,304,000 像素与 CSV 0 mismatch。

MSAA 取 sample0，与单采样 depth test 比较一致。

## 实现

### `output_merger.py`
新增 `Depth.load_pre_draw_depth_stencil_raw(self, data_folder)`：
1. 读 `output_merger.csv`，找 `ViewType==DSV` 行 → 取 `ResourceId`/`ViewFormat`/`Width`/`Height`。
2. `glob` 匹配 `pre_draw_ds_res_<id>_*.raw`，优先无 `sample` 的文件，否则 `sample0`。
3. 按格式选择 stride 与 decode 闭包，逐像素写入 `_depth_buffer`/`_stencil_buffer`。
4. 缺文件 / 不支持格式 / 字节数不足 → 返回 0（安全降级）。

旧的 `load_pre_draw_depth_stencil`（CSV 版）保留，作为没有 raw dump 的旧 capture 的回退。

### `render.py`
depth/stencil 阶段改为：
```python
depth = Depth()
loaded = depth.load_pre_draw_depth_stencil_raw(data_folder)
pre_draw_ds_csv = os.path.join(data_folder, 'pre_draw_depth_stencil.csv')
if loaded == 0 and os.path.exists(pre_draw_ds_csv):
    loaded = depth.load_pre_draw_depth_stencil(pre_draw_ds_csv)  # 回退
if loaded > 0:
    depth.config.depth_enable = True
    depth.config.depth_write_mask = True
    depth.config.depth_func = ComparisonFunc.LESS
    ...
```
优先 raw，找不到再回退 CSV；深度测试启用逻辑改为以 `loaded > 0` 为条件。

## Regression 结果

`python run_regression.py` → **123/123 passed，0 失败**，退出码 0。

关键验证点：
- `4k1w_event1124.zip`（D32_FLOAT_S8X24_UINT，MSAA SampleCount=8，1920×1200）：passed 444/444，
  raw 多采样路径取 sample0 正常。
- 全部 `Collision-*`（D24_UNORM_S8_UINT，640×480）及 witcher/sekiro/octopath/valley/EndlessSpace 等
  各场景 depth test 行为与替换前一致，无回归。

替换为 raw 加载后所有 case 保持通过，且 depth 数据获得完整 float32 精度（不再受 CSV 6 位小数限制）。
