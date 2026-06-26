# Step 90 — Fix MeshView macOS Threading (Continued)

## Problem

After step 89's fix (running tkinter on the main thread via `set_main_thread_root()`), MeshView still crashed on macOS when `mesh_view_enabled = true`.

## Root Cause Analysis

Step 89 correctly moved `tk.Tk()` and `mainloop()` to the main thread via the macOS-specific path in `render.py`. However, three categories of tkinter calls were **still being made from the background pipeline thread**:

### Crash 1 — `_start_gui_thread` (immediate crash)

```python
# mesh_view.py:168-170  (old code)
self._root = _main_thread_root
self._root.title(self.title)       # ← background thread! Cocoa crash
self._root.geometry("1700x700")    # ← background thread! Cocoa crash
self._root.after(0, self._setup_ui_macos)
```

`title()` and `geometry()` are direct Cocoa window operations. On macOS, any direct Tk/Cocoa call from a non-main thread triggers an NSException or Python runtime crash.

### Crash 2 — `show()` (deiconify from background thread)

```python
# mesh_view.py:1843  (old code)
self._root.deiconify()   # ← background thread!
```

`deiconify()` is called from `hlsl_interpreter.display_input_mesh()`, `display_output_mesh()`, and from `_execute_pipeline` in render.py — all running in the background pipeline thread on macOS.

### Crash 3 — `_draw_*_pixels()` (PhotoImage + canvas ops from background thread)

```python
vs_interp._mesh_view._draw_rasterizer_pixels()    # ← background thread
vs_interp._mesh_view._draw_pixel_shader_pixels()  # ← background thread
vs_interp._mesh_view._draw_output_merger_pixels() # ← background thread
```

These call `_draw_pixels_image()` which does:
- `canvas.delete("all")`
- `canvas.winfo_width()`
- `tk.PhotoImage(width=w, height=h)`
- `canvas.create_image(...)`

All of these are unsafe from a non-main thread on macOS Cocoa.

## Fix

### `mesh_view.py`

**1. `_start_gui_thread`** — remove `title()` / `geometry()` calls; only do the safe pointer assignment and `after()` queue:

```python
def _start_gui_thread(self):
    global _main_thread_root
    if _main_thread_root is not None:
        self._root = _main_thread_root   # pointer assignment — safe from any thread
        self._root.after(0, self._setup_ui_macos)  # queue work for main thread
    else:
        self._gui_thread = threading.Thread(target=self._gui_thread_run, daemon=True)
        self._gui_thread.start()
```

**2. `_setup_ui_macos`** — now owns the title/geometry too:

```python
def _setup_ui_macos(self):
    """Runs on the main thread via after()."""
    self._root.title(self.title)
    self._root.geometry("1700x700")
    self._setup_ui()
    self._gui_ready_event.set()
```

**3. `_schedule_on_main` (new helper)**:

```python
def _schedule_on_main(self, func):
    global _main_thread_root
    if self._root is not None and self._root is _main_thread_root:
        self._root.after(0, func)
    else:
        func()
```

**4. `show()`** — replace direct `deiconify()` with `_schedule_on_main`:

```python
self._schedule_on_main(self._root.deiconify)
self._schedule_draw()
```

**5. `_draw_*_pixels()`** — wrap with `_schedule_on_main`:

```python
def _draw_rasterizer_pixels(self):
    self._schedule_on_main(lambda: self._draw_pixels_image(
        self._rasterizer_canvas, self._rasterizer_pixels,
        self._rasterizer_color_fn, 'rasterizer'))

def _draw_pixel_shader_pixels(self):
    self._schedule_on_main(lambda: self._draw_pixels_image(
        self._pixel_shader_canvas, self._rasterizer_pixels,
        self._shaded_color_fn, 'pixel_shader'))

def _draw_output_merger_pixels(self):
    def _do():
        pixels = self._output_merger_pixels if self._output_merger_pixels else self._rasterizer_pixels
        self._draw_pixels_image(self._output_merger_canvas, pixels,
                                self._shaded_color_fn, 'output_merger')
    self._schedule_on_main(_do)
```

## Why `after()` is safe from a background thread

`tk.after()` posts a timer event to Tcl's event queue via `Tcl_CreateTimerHandler`, which is protected by Tcl's internal mutex. It does **not** make direct Cocoa/AppKit calls; it simply queues a callback for the event loop on the main thread to process. This is the standard pattern for thread-safe tkinter communication.

## Non-macOS path unchanged

On Windows/Linux, `_main_thread_root` is None, so `_schedule_on_main` calls the function directly — identical to previous behavior. No regression risk on those platforms.

## Result

- All three crash sites fixed.
- Regression suite: 46/46 PASS.
