# Step 191 — Draggable panels for custom web-viewer layouts

## Prompt

动态 web 视图支持动态拖动网页上的小窗口进行不同的布局。

## Goal

Let the user rearrange the three viewer panels (Input Vertices / Output /
Selected Info) by dragging them, instead of the fixed flex row.

## Approach

Pure front-end change in `web_mesh_view.py`'s page template — no server/pipeline
changes.

- **Free positioning.** `.wrap` becomes the positioning context
  (`position: relative`, JS-managed `min-height/min-width`); each `.panel` is
  `position: absolute`. Each panel got a stable `id` (`panel-input`,
  `panel-output`, `panel-info`).
- **Drag handle.** Each panel gets a `.titlebar` (cursor: move, `user-select:
  none`) with the panel name + a grip glyph. Dragging is bound to the titlebar
  only, so the canvases keep their own rotate/pan/click interactions and the info
  panel's inner section headers (`<h3>`) don't trigger a move.
- **Drag mechanics.** `makeDraggable(panel)` listens for a left-button mousedown
  on the titlebar, then tracks `mousemove`/`mouseup` on `document` (so a fast
  drag that leaves the bar keeps working), updating `left/top` and clamping to
  ≥ 0. A `.dragging` class lifts the panel (`z-index`, shadow) while moving.
- **Persistence.** Positions are saved to `localStorage` (`hlsl_web_layout_v1`)
  on drop and restored on load, so the arrangement survives reloads/reruns.
  `initPanels()` applies the saved layout or, on first run, tiles the panels
  left-to-right (matching the old look). `fitWrap()` grows the container to
  contain the panels (and re-runs on window resize).
- **Reset.** A "Reset Layout" button in the header clears the saved layout and
  re-tiles.

## Files changed

- `web_mesh_view.py` — CSS (`.wrap`/`.panel`/`.titlebar`/`.dragging`), panel
  markup (ids + titlebars), a "Reset Layout" header button, and the drag JS
  (`loadLayout`/`saveLayout`/`fitWrap`/`tileLayout`/`resetLayout`/`makeDraggable`
  /`initPanels`), wired in the init IIFE.

## Verification

- Page serves with all three panels carrying a `.titlebar`, absolute
  positioning, `cursor: move`, and the drag/persist JS + Reset Layout button
  (checked markers present; 3 panels ⇔ 3 titlebars).
- Extracted the page `<script>` and ran `node --check` → **JS syntax OK**.
- Server endpoints unchanged (step-190 `/state` `/pixels` `/replay` `/trace_*`
  all still valid — this step touches only HTML/CSS/JS).
- Regression: **118/123**, unchanged — the viewer is never instantiated on the
  headless regression path.

## Notes

- Dragging is by the title bar; canvas drag still rotates the mesh (VS tab) or
  pans the pixel image (pixel tabs), because those handlers are on the canvas,
  not the titlebar.
- Layout persists per browser via `localStorage`; "Reset Layout" restores the
  default tiling.
