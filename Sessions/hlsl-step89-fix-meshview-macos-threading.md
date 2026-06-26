# Step 89: Fix MeshView Crash on macOS (tkinter Thread Safety)

## Problem

启动 MeshView 后程序崩溃，错误信息：

```
MeshView enabledmacOS 26 (2603) or later required, have instead 16 (1603) !
```

## Root Cause Analysis

### 根本原因：tkinter 在 macOS 上必须运行在主线程

`MeshView._start_gui_thread()` 通过 `threading.Thread` 启动一个后台线程，在该线程中调用 `tk.Tk()`：

```python
def _gui_thread_run(self):
    self._root = tk.Tk()  # ← 在非主线程调用，macOS 不允许
    ...
    self._root.mainloop()
```

macOS 的 Cocoa/AppKit 框架（tkinter 的 Aqua 后端底层依赖）要求 `NSApplication`（即 Tk 的事件循环）必须运行在主线程上。在非主线程调用 `tk.Tk()` 会触发底层 Cocoa 版本检查失败，输出类似 `macOS 26 required, have 16` 的内部错误。

错误信息中 "macOS 26 (2603)" 和 "16 (1603)" 是 Apple Tk 框架内部的 SDK 版本号，并非 macOS 系统版本号。

### 为何 Windows/Linux 没有问题

Windows（Win32 GDI）和 Linux（X11）的 Tk 后端不对线程有此强制要求。因此该问题是 macOS 特有的。

## Solution

**架构思路：主线程跑 GUI，后台线程跑 pipeline**

1. **`mesh_view.py`**：新增模块级 `_main_thread_root` 变量和 `set_main_thread_root(root)` 函数。`MeshView._start_gui_thread()` 检查该变量：若已设置（macOS 模式），直接复用主线程的 root，通过 `root.after(0, self._setup_ui_macos)` 将 UI 初始化调度到主线程执行；否则沿用原有 threading 方式（Windows/Linux 不变）。
2. **`render.py`**：`main()` 中检测 `mesh_view_enabled and sys.platform == 'darwin'`，若满足：
   - 在主线程创建 `tk.Tk()` 并隐藏（`withdraw()`）
   - 调用 `mv_module.set_main_thread_root(root)` 注册
   - 将 pipeline 放入 daemon 线程运行
   - 主线程调用 `root.mainloop()` 阻塞，直到用户关闭窗口或 pipeline 结束
3. **`MeshView.close()`**：在 macOS 模式（`self._root is _main_thread_root`）下只调用 `root.quit()` 退出 mainloop，不调用 `root.destroy()`（root 由 render.py 的 main() 拥有）。
4. **`MeshView.show()`**：将 `if self._root is None: wait()` 改为 `self._gui_ready_event.wait(timeout=10)`，确保在 macOS 模式下也等待 UI setup 通过 after() 完成后再 deiconify。

## Code Changes

### `mesh_view.py`

1. 新增 `import sys`
2. 新增模块级变量 `_main_thread_root = None` 和函数 `set_main_thread_root(root)`
3. `_start_gui_thread()` 改为：若 `_main_thread_root is not None` → 设 `self._root`，via `after(0)` 调度 `_setup_ui_macos()`；否则走原线程路径
4. 新增 `_setup_ui_macos()` 方法：调用 `_setup_ui()` 后设置 `_gui_ready_event`
5. `show()` 改为 `self._gui_ready_event.wait(timeout=10)` 代替原来的 `if self._root is None: wait()`
6. `close()` 中新增 macOS 分支：`if self._root is _main_thread_root: root.after(0, root.quit)`，不再 destroy

### `render.py`

1. 新增 `import threading`
2. `main()` 新增 macOS 分支：创建 root → 注册 → 起 daemon 线程运行 pipeline → `root.mainloop()`

## Data Flow (macOS Mode)

```
main thread                     pipeline thread
-----------                     ---------------
tk.Tk() → withdraw()
set_main_thread_root(root)
Thread(target=_run_pipeline).start()
                                _run_zip_workflow(...)
                                  MeshView.__init__()
                                    _start_gui_thread()
                                      self._root = _main_thread_root
                                      root.after(0, _setup_ui_macos)
                                  ...shader execution...
                                  show() → _gui_ready_event.wait()
root.mainloop()
  ← processes after() queue
  ← _setup_ui_macos() runs
  ← _gui_ready_event.set()
                                  show() unblocks → deiconify()
  ← user interacts with GUI
                                  user types 'x'
                                  close() → root.after(0, root.quit)
root.quit() → mainloop() exits
main() returns → program exits
```

## Regression

运行 `python run_regression.py` 回归测试，所有 case 均 PASS（mesh_view_enabled=False，走非 macOS 分支，代码路径不变）。
