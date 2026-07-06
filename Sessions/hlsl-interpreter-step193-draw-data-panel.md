# Step 193 — "Draw Data" panel: browse & preview the raw zip

## Prompt

动态 web 视图增加窗口显示当前执行的原始 draw zip data：
1. 窗口左边是列表展示 zip data 内的文件；
2. 窗口右边是预览视图，可以预览 raw data / csv 文件 / image 文件；
3. 窗口默认摆放在最下面。

## Design

A new draggable panel (`panel-data`, "Draw Data") with a left file list and a
right preview, backed by two new server routes reading the extracted zip folder.
The temp folder lives for the whole interactive session (it is only
`shutil.rmtree`'d after `_execute_pipeline` returns, which blocks on the stdin
loop), so the server can read it while the user browses.

### Server (`web_mesh_view.py`)
- `set_data_folder(path)` — render.py points it at the extracted zip folder right
  after enabling the viewer (before VS), so the list is ready when the page opens.
- `GET /files` → JSON `[{name, size, kind}]` from an `os.walk`, where `kind` ∈
  `image | csv | text | binary` by extension.
- `GET /file?name=<rel>` → file bytes. Images are sent with the right image MIME
  (`image/bmp` etc.); text/csv decoded UTF-8 (errors replaced); binary as
  `application/octet-stream`. Non-image previews are capped at 512 KB.
- `_resolve_safe` normalizes the path and rejects anything escaping the data
  folder (path-traversal guard → 404).

### Client
- `panel-data` (width 1000) with `#filelist` (left) + `#filepreview` (right).
- `loadFileList()` fetches `/files` once and renders a clickable list (name, size,
  kind).
- `previewFile(f)` dispatches by kind: **image** → `<img src="/file?...">`;
  **csv** → parsed into an HTML `<table>` (first 300 rows); **text** → `<pre>`;
  **binary** → fetched as an ArrayBuffer and rendered as a **hex dump**.
- **Default at the bottom**: `tileLayout()` lays out the other panels with row
  wrapping, then docks `panel-data` on its own row below all of them. Layout key
  bumped `v2 → v3` so the new panel takes its default bottom slot.

## Files changed

- `web_mesh_view.py` — `set_data_folder` + `_list_files`/`_file_kind`/
  `_resolve_safe`/`_file_response`; `/files` and `/file` routes; `panel-data`
  markup + CSS; file-browser JS (`loadFileList`/`previewFile`/`hexdump`/
  `csvTable`); `tileLayout` bottom-docking; `LAYOUT_KEY` v3.
- `render.py` — `set_data_folder(data_folder)` at viewer-enable time.

## Verification

- Unit: fake data folder → `/files` returns correct kinds; hlsl/csv text and
  binary bytes served; **path traversal blocked (404)**; page carries
  `panel-data`/`#filelist`/`#filepreview` + the browser JS; `node --check` on the
  extracted script passes.
- End-to-end (`event104`): `/files` listed **56 real files** (18 csv, 26 binary,
  4 text, 8 image); CSV header previewed; `PS_shader.dxbc` served (900 B) for hex;
  `PS_slot_0_res_88_mip0_arr0.bmp` served (49206 B) for image preview.
- Regression: **118/123**, unchanged — the viewer is never instantiated on the
  headless regression path.
