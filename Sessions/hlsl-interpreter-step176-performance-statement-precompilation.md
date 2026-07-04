# Step 176 — 执行性能优化：语句级预编译与缓存体系

## 任务

1. 优化 OldWorld_event1034/2767（203,328 顶点，>300s）与 sekiro2_event3207/9493
   （45,576 顶点 × 风力循环）的执行性能；
2. 语句级预编译（赋值目标解析、swizzle 处理预解析为闭包）；
3. 确认 `max_workers` 顶点级并行在 vs_only 路径是否吃满。

## 画像（cProfile，OldWorld 3000 顶点切片，基线 21.2s）

| 热点 | 成本 | 根因 |
|---|---|---|
| `SyntaxTreeParser.parse` | 16.2 万次调用 / ~3s | **完全无缓存**——每条语句每顶点重新解析（"语法树已缓存"的旧认知有误） |
| `get_value` | 36 万次 / 5.7s | 每次做字面量 float()、正则、字符串拆解 |
| debug f-string | 分散 ~2s | `debug_print(f"...")` 即使不打印也构建字符串 + `_format_float` |
| `execute_statement` 级联 | — | 每语句每顶点重跑 GetDimensions/ld_raw/sincos 等特例正则 |
| `execute_block`/`if`/`while` | 循环案显著 | 每次调用（循环体每迭代！）重做 GenerateStmts/括号配对 |

## 六项优化（语义零改变，回归门禁）

1. **parse 记忆化**（hlsl_syntax_tree.py）：`_parse_cache[expr] → tree`。
   语法树在求值期只读，按解析器实例（=按解释器）缓存。
2. **value 节点访问器缓存**（`_build_value_accessor`，缓存在节点 `_ev` 上）：
   - 数字字面量 → 常量闭包（保持 `_f32` 舍入语义）；
   - 裸名 `r0` → `lv.get` 闭包；`r0.xyz` → 预解析分量下标元组闭包；
   - 字面下标 cbuffer 引用（`cb0[24].xyzw`/具名字段）→ 每 draw 静态值缓存，
     **列表按副本返回**（swizzle 赋值是原地写，共享引用会污染 cbuffer）；
   - 其余（动态下标、struct 成员、buffer）保持完整 get_value 路径；
   - 访问器 miss（变量缺失/形状不符）时回退 get_value，语义不变。
3. **语句级快派发**（`_stmt_fast`）：swizzle 赋值/简单赋值/带初值声明三类在首个
   顶点分类一次（存目标名、swizzle、RHS 串），后续顶点直接命中，跳过整个特例
   正则级联。仅缓存纯赋值——GS Append/if/while 等永不进缓存，不会遮蔽。
4. **块/if/while 结构缓存**：`execute_block` 的 GenerateStmts、`execute_if_statement`
   的条件+分支切分、`execute_while_statement` 的条件+体切分各缓存一次
   （风力循环每迭代都在重做这些）。
5. **debug 快标志**：`self._dbg` 随 `_should_print` 维护；31 处单行
   `debug_print(f"[FUNC/BINARY OP/METHOD/...]")` 加 `if self._dbg:` 守卫，
   不打印时零字符串构建。debug_print 内部完整条件仍在（语义兜底）。
6. `execute_statement` 首行 `[STMT] Executing` 打印同样守卫。

## 结果（3000 顶点切片）

| 案例 | 基线 | 优化后 | 加速 |
|---|---|---|---|
| OldWorld_1034（无循环，40 语句/顶点） | 21.2s | **5.43s** | **3.9×** |
| sekiro2_3207（含风力 while 循环） | ~21s* | **3.28s** | **~6.5×** |

（*3207 切片基线未单测，按同规模推算；循环案受益于块缓存+debug 守卫更多。）

全案预估/实测：sekiro2_3207/9493（45,576 顶点）≈ 1 分钟级；
OldWorld（203,328 顶点）≈ 6–7 分钟（含 ~20s 一次性装载）。

## max_workers 核查结论（任务 3）

- `max_workers` 的 ThreadPoolExecutor **只存在于 legacy struct 路径**
  （`interpret`/`execute_main_function`），当前 `executeVS_with_params` 主路径是
  **单线程顺序循环**。
- 即使接上线程池也无收益：解释器是纯 Python CPU 密集，**GIL 下线程并行无效**。
- 真并行需进程池分块，但解释器状态（文件句柄、闭包缓存、纹理执行器）不可
  pickle，需要按块重建全套装载——工程量大，收益与 6 项单线程优化（已 4–6.5×）
  相比不划算，**暂不实施**（如需 203k 案入回归，建议 triage 对大案用
  `--timeout 900` 单独跑，或后续做 render.py 级子进程分块）。

## 优化引出的一个隐蔽回归（已修复）：引用循环吞掉日志最终 flush

首轮回归 143/144：witcher3_event22420 日志 0 字节 → "no VS-vs-golden comparison"。
根因：`_cb` 访问器闭包捕获了 `self`，闭包存在解析器缓存的节点上 →
**interp → parser → 节点 → 闭包 → interp 引用循环**。解释器析构从"引用计数归零"
（文件仍开）延迟到进程关停期 GC——此时运行时已回收文件对象，`__del__` 里的最终
flush 抛 "I/O operation on closed file"，小案（日志 < 缓存阈值、从未中途 flush）
的日志整体丢失。大案因周期性 flush 只丢尾部，正确性比较仍在 stdout。

修复：① `_cb` 闭包只捕获缓存字典（首次解析由求值侧经 get_value 回填，命中返回
副本）；② render.py `_make_interpreter` 登记所有解释器，`_run_zip_workflow` 的
finally 里**确定性 flush**（不再依赖 `__del__`）；③ `__del__` 兜底 try/except。

**教训**：把捕获 `self` 的闭包挂到长生命周期缓存（解析树）上，会改变对象析构时机
——依赖 `__del__` 做 I/O 收尾的代码随之炸裂。确定性资源收尾必须显式。

## 全量验证

- **OldWorld_event1034：PASS 203328/203328**、**OldWorld_event2767：PASS
  203328/203328**（timeout 900 独跑，各约 6–7 分钟）——两个 203k 超时案不仅能跑完，
  而且**全过**（它们从来不是正确性问题）。**双双晋级** Cases + 回归表。
- sekiro2_event3207/9493：按时完成（约 1 分钟级），44341/45576 维持
  （残余为步 174 已定性的 sin 大相位精度墙）。
- **Cases 回归（含日志修复后重跑）：144/144 全 PASS 零回归**；输出数值与优化前
  逐位一致（所有优化均为纯执行加速，无语义改动）。
- **OldWorld 两案晋级**回归表（**146 案**，注意二者各需 ~7 分钟 vs-only，
  套件总时长相应增加）；Dump 剩 **27 案**，超时类清零。
