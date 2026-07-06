# Step 192 — Split "Selected Info" into two independent panels

## Prompt

动态 web 视图 selected Vertex/Pixel info 窗口分成两个独立的。

## Context

Step 191 made the viewer panels draggable, but "Selected Vertex Info" and
"Selected Pixel Info" still lived inside a single `panel-info` window (two `<h3>`
sections stacked in one draggable panel). The user wants them as two separate,
independently draggable windows.

## Change (front-end only, `web_mesh_view.py` page template)

- **Split the panel.** `panel-info` (which held both `#info` and `#pxinfo`) is
  replaced by two panels:
  - `panel-vertex` — titlebar "Selected Vertex Info", body `#info`.
  - `panel-pixel` — titlebar "Selected Pixel Info", body `#pxinfo`.
  Each has its own `.titlebar`, so the existing drag system (`initPanels` /
  `makeDraggable`, which iterate every `.panel`) makes both independently
  draggable and persisted with zero extra wiring.
- **Bumped layout key** `hlsl_web_layout_v1` → `v2`: the panel set changed
  (`panel-info` is gone, two new ids exist), so a fresh key avoids restoring a
  stale layout that positioned the old combined panel.
- **Row-wrapping default tile.** With 4 panels now, a single left-to-right row
  would push the last panel off-screen, so `tileLayout()` wraps to a new row when
  the next panel would exceed the viewport width. `initPanels()` uses
  `tileLayout()` for a fresh layout and, when a saved layout exists, applies it
  and appends any panel lacking a saved position after the rightmost.

## Files changed

- `web_mesh_view.py` — panel markup (one panel → two), `LAYOUT_KEY` v2,
  `tileLayout()` row-wrapping, `initPanels()` fresh-vs-saved handling.

## Verification

- Page now serves **4 panels / 4 titlebars**; `panel-vertex`, `panel-pixel`,
  `#info`, `#pxinfo` all present; old `panel-info` gone; layout key is
  `hlsl_web_layout_v2`.
- Extracted the page `<script>` and ran `node --check` → **JS syntax OK**.
- Server endpoints unchanged (front-end only); vertex trace still targets `#info`
  and pixel trace `#pxinfo` (unchanged element ids).
- Regression: **118/123**, unchanged — the viewer is never instantiated on the
  headless regression path.
