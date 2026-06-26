# Step 91 — Fix MeshView macOS Crash: input() Triggers Tcl EventHook on Background Thread

## Problem

After step 90's fix, MeshView still crashes on macOS when `mesh_view_enabled = true`. The crash report shows Thread 11 (the background pipeline thread) aborting with:

```
Thread 11 Crashed:
0   __pthread_kill
1   pthread_kill
2   abort
3   Tcl_PanicVA
4   Tcl_Panic
5   Tcl_WaitForEvent      ← called from wrong thread
6   Tcl_DoOneEvent
7   EventHook             ← tkinter's PyOS_InputHook
8   my_fgets
9   PyOS_StdioReadline
10  PyOS_Readline
11  builtin_input         ← Python's input() called from background thread
...
19  thread_run            ← confirms it's the background pipeline thread
```

## Root Cause

When `mesh_view_enabled = true` on macOS, `main()` runs the pipeline in a daemon background thread (step 89's fix). The pipeline thread eventually reaches an interactive `input()` loop in `render.py`.

When tkinter's `Tk()` is created and `mainloop()` is called, `_tkinter` installs `PyOS_InputHook = EventHook` **globally** in CPython. This hook is called from `my_fgets` (the C-level readline implementation) whenever `input()` is waiting for user input.

`EventHook` calls `Tcl_DoOneEvent` to keep the Tk GUI responsive during stdin blocking. But `Tcl_DoOneEvent` → `Tcl_WaitForEvent` is not thread-safe; on macOS Cocoa it can only run from the main thread. Calling it from Thread 11 triggers `Tcl_Panic` → `abort()`.

### The two crash sites in render.py

**Site 1** — `_execute_pipeline` (zip workflow), line 1357:
```python
while True:
    user_input = input("\nEnter 'x' to exit, 'o' to open MeshView: ").strip().lower()
```

**Site 2** — `_run_legacy_workflow`, line 1484:
```python
user_input = input("\nEnter 'x' to exit, 'o' to open MeshView, 'r' to rerun executeVS: ")
```

Both run in the background pipeline thread on macOS.

## Why `input()` triggers `PyOS_InputHook` but `sys.stdin.readline()` does not

`input()` in CPython calls `PyOS_Readline` → `PyOS_StdioReadline` → `my_fgets`. Inside `my_fgets`, CPython checks `PyOS_InputHook` and calls it while waiting for stdin data.

`sys.stdin.readline()` goes through Python's `io` stack: `TextIOWrapper.readline()` → `BufferedReader.readline()` → `FileIO.read()` → `read(fd=0)` syscall. This path **never calls `PyOS_InputHook`**, making it safe to call from background threads.

## Fix

Replace `input(prompt)` with `sys.stdout.write(prompt) + sys.stdout.flush() + sys.stdin.readline()` at both sites. Semantically identical, but bypasses the `PyOS_InputHook` mechanism.

### render.py — _execute_pipeline (line 1357)

```python
# Before:
user_input = input("\nEnter 'x' to exit, 'o' to open MeshView: ").strip().lower()

# After:
sys.stdout.write("\nEnter 'x' to exit, 'o' to open MeshView: ")
sys.stdout.flush()
user_input = sys.stdin.readline().strip().lower()
```

### render.py — _run_legacy_workflow (line 1484)

```python
# Before:
user_input = input("\nEnter 'x' to exit, 'o' to open MeshView, 'r' to rerun executeVS: ")
user_input = user_input.strip().lower()

# After:
sys.stdout.write("\nEnter 'x' to exit, 'o' to open MeshView, 'r' to rerun executeVS: ")
sys.stdout.flush()
user_input = sys.stdin.readline().strip().lower()
```

## Why the non-macOS path is unaffected

On non-macOS, `_main_thread_root` is None, the pipeline runs on the main thread (no background thread), and `mainloop()` is never called. `PyOS_InputHook` is never set by tkinter, so `input()` works normally. The change to `sys.stdin.readline()` is functionally identical in that context.

## Result

- Crash eliminated: `input()` no longer triggers tkinter's `EventHook` from the background thread.
- Regression suite: 46/46 PASS.
