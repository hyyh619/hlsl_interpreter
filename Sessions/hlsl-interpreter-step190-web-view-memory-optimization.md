# Step 190 — Cut the dynamic web viewer's memory footprint

## Prompt

动态 web 视图显示太消耗内存，请优化内存开销。

## Diagnosis

Two hotspots in `WebMeshView`, both scaling with pixel count (46k+ for a normal
draw):

1. **Persistent packed duplicates.** `set_rasterizer_pixels(pixels)` and
   `set_output_merger_pixels(pixels)` each built a full packed list
   (`[x,y,prim,r,g,b]` per pixel) and kept it — ~10 MB each for 46k pixels, on
   top of the interpreter's own Pixel objects. So ~20 MB of pure duplication.

2. **Per-poll re-serialization of everything, forever.** `_snapshot()` embedded
   `input` + `output` + `rasterizer` + `ps_pixels` + `output_merger` and the
   `/state` handler `json.dumps`'d the whole thing on **every poll (4×/sec)** —
   even when nothing changed. With 3× 46k-pixel arrays that's a ~3.2 MB string
   allocated 4 times a second for as long as the browser stays open, and the
   browser re-parses ~3.2 MB of JSON each time. Measured: a finished pipeline
   burned ~3.2 MB × 4/sec of transient garbage doing nothing.

## Fix

Keep pixels as **references**, serve them **lazily**, and **cache** both payloads
so unchanged data is never rebuilt.

- **Refs, not packed copies.** `set_rasterizer_pixels` / `set_output_merger_pixels`
  now just store the Pixel-list reference (`_rast_final` / `_om_final`). Packing
  to the compact JSON form happens on demand. Removes the ~20 MB duplication.
- **Split heavy pixels out of `/state`.** `/state` is now lightweight — progress,
  stats, vertex arrays, pixel **counts**, and a `pixels_ver` — but no pixel
  arrays. A new `GET /pixels?which=rast|ps|om` returns the packed array for one
  view.
- **Two-level cache.** `/state` bytes are cached by `_seq`; each `/pixels` view's
  bytes are cached by `_pixels_ver` (bumped only when pixel data actually
  changes, via `_bump_pixels`). So a browser polling a finished pipeline reuses
  the same cached `bytes` object — zero packing, zero `json.dumps`.
- **Client fetches pixels lazily.** `renderOutput` draws pixel tabs from a
  client-side cache (`pixelCache` / `pixelCacheVer`); `ensurePixels(which)` fetches
  `/pixels?which=` only when `pixels_ver` changed, so zoom/pan/idle redraws are
  free and only the *active* tab ever transfers pixels.

## Files changed

- `web_mesh_view.py` — `_rast_final`/`_om_final` refs; `_pixels_ver` +
  `_bump_pixels`; `_state_cache` / `_pixels_cache`; `_view_pixels` + `_pixels_body`
  + lightweight `_state_body` (replaces `_snapshot`); `/pixels` route; client
  `pixelCache`/`ensurePixels` + `renderOutput` rewrite. (No changes to the
  pipeline, interpreter, or other viewers.)

## Verification (real `event104`, 42726 pixels)

- `/state`: **350 KB** (vertices + progress only), `has pixel arrays: False` —
  down from ~3.2 MB when it carried 3× the pixel arrays.
- `/pixels?which=rast|ps|om`: 42726 each, fetched on demand.
- **Idle churn: ~19 KB net allocation over 30 `/state` polls** (~640 B/poll,
  just request overhead) vs. re-serializing ~3.2 MB per poll before — a ~99.98%
  reduction in steady-state allocation.
- `/state` and `/pixels` return byte-identical cached responses when unchanged.
- Replay + vertex/pixel trace still work; live VS/rasterizer/PS animation and the
  normals toggle unaffected (verified endpoints).
- Regression: **118/123**, unchanged — the viewer is never instantiated on the
  headless regression path.
