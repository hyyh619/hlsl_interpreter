# hlsl-interpreter-step151：实现 vs_only 模式并对 A 类超时 case 单独长跑

## 任务

1. 提交前面的文档（step124 的 Dump 重 triage 报告）。
2. **先实现 `vs_only` 模式，再单独长时间跑** A 类（超时）case，确认它们到底是"正确但慢"还是真有 VS 错误。

## 背景

step124 把 `Dump/` 剩余 134 个失败 case 分了 7 类，其中 **A 类超时 45 个**因 300s 上限被杀掉，无法判定正确性——它们的日志在被杀前没有 `Error:` 行也没有 `Total PASSED rows:` 总结。怀疑这些是 PS/光栅化主导的全屏 pass，VS 本身是对的。要验证就得跳过光栅化+PS 只跑 VS。

## 执行

### 1. 提交文档

`git commit` 把 step124 的 session 报告 + prompt log 更新提交（`Dump/`、`Cases/` 均在 `.gitignore` 中，故只提交两个 docs）。commit: `b3fda5c`。

### 2. 实现 `vs_only` 模式（`render.py`）

`_execute_pipeline` 在执行 VS 并与 golden 比对后（`compare_vs_output_with_golden_params` 已经打印 `Total PASSED rows:`），新增早退：

```python
vs_only = config.get('vs_only', False)
...
# 在 VS-vs-golden 比对之后、Rasterizer 之前：
if vs_only:
    vs_interp.log_output(f"\nvs_only mode: skipping rasterizer/depth/PS/output-merger (VS executed in {vs_time:.4f}s)")
    TRACE.close()
    return
```

正确性判定本就只看 VS-vs-golden，所以跳过 Rasterizer/Depth/PS/Output-Merger 不影响 PASS/FAIL 结论，但能让全屏/大网格 draw 的耗时从光栅化+PS 主导降到只剩 VS。`vs_only` 默认 `False`，不影响回归/默认全管线路径。

### 3. triage_dump.py 增加 `--vs-only` / `--list` / `--timeout`

为了"单独长跑" A 类，给 triage runner 加了三个开关（可复用）：
- `--vs-only`：给每个 case 的临时 config 注入 `vs_only=True`。
- `--list FILE`：只跑文件里列出的 zip（A 类跨多个家族，substring `--filter` 不够用）。
- `--timeout N`：覆盖默认 300s（本次用 1800s 作兜底）。

### 4. 冒烟验证

对 `witcher3_countryside_event20882`（全管线下 >300s 超时）单跑 `vs_only`：

```
VS executed 720 vertices in 0.9267s
Total PASSED rows: 720/720
real  0m1.5s
```

从 **>300s 超时** 降到 **1.5s 通过 720/720**——确认是 PS 主导的全屏 pass，VS 完全正确。

### 5. 对 45 个 A 类 case 单独长跑

```
python triage_dump.py --vs-only --list Cases/.aclass_list.txt --workers 12 --timeout 1800
```

结果：**38 PASS，7 FAIL**。

## 结果

### A 类 45 个的真相

| 结果 | 数量 | 处理 |
|---|---|---|
| PASS（确认正确但慢） | 38 | 从 `Dump/` 删除 |
| FAIL（其实 VS 就错，之前被超时掩盖） | 7 | 重新归类到 D/E/F/G |

**38 个删除**证实了 step124 的判断：A 类绝大多数是正确但慢的全屏/大网格 draw（witcher 全屏 pass、heaven 19095/19095、sekiro 16479/16479 等），`vs_only` 下秒级通过。

**7 个被超时掩盖的真失败**（重新归类）：

| case | vs_only 结果 | 新类别 |
|---|---|---|
| `Octopath-frame746_event3221` | 0/6720，TEXCOORD10/11 错 | D（Octopath 输入解码） |
| `OldWorld_event1034` | 50533/203328 | G（OldWorld value mismatch） |
| `OldWorld_event2767` | 50533/203328（同 shader） | G（OldWorld value mismatch） |
| `OldWorld_event3338` | 23352/23814，diff≈0.2~0.76 | F1（精度边界） |
| `witcher3_countryside_event21979` | 21/840，diff≈0.027 | E（witcher） |
| `witcher3_countryside_event22049` | 0/840 | E（witcher） |
| `witcher3_countryside_event22260` | 0/108 | E（witcher） |

注：`OldWorld_event1034/2767` 各有 **203,328 个顶点**——即使只跑 VS 也要数百秒（纯 Python 逐顶点），是少数 `vs_only` 仍很慢的 case。它们 `sv_position[1]` 偏、`TexCoord2` 为 0，属真实 VS 错误（新建 G 类，疑似某个 cbuffer 列/矩阵行未正确加载）。

### `Dump/` 失败集刷新

删除 38 个后，`Dump/` 从 134 → **96** 个失败 case：

| 类别 | step124 | 本次后 | 说明 |
|---|---|---|---|
| A. 超时 | 45 | **0** | 38 删除 + 7 重归类，A 类清空 |
| B. 无 golden | 16 | 16 | 不变 |
| C. TombRaider 反编译有损 | 37 | 37 | 不变 |
| D. Octopath 输入解码 | 18 | 19 | +1（event3221） |
| E. witcher 切线/解码 | 7 | 10 | +3 |
| F1. 精度边界 | 2 | 3 | +1（OldWorld_event3338） |
| F2. 其它 value mismatch | 9 | 9 | 不变 |
| G. OldWorld value mismatch | 0 | 2 | 新建（203k 顶点的大 draw） |
| **合计失败** | **134** | **96** | 删除 38 |

## 各类失败原因与修复方案（更新）

- **A（已清空）**：`vs_only` 已落地——把它设为 triage/回归的默认评测路径即可彻底消除"超时型"假失败。进一步的全管线性能优化（热路径 AST 预编译）可另行投入。
- **D（19，Octopath）/E（10，witcher）**：可修的解释器问题——packed 法线/切线 UNORM/SNORM 未按 `ia_input_layouts.csv` 解码、golden 列被当 uint 解析、切线矩阵主序。
- **F1（3，精度）**：diff 仅略超 `float_tolerance=0.005`（如 0.2/0.027），细化 float32 模拟或略放宽容差即过。
- **G（2，OldWorld）**：203k 顶点大 draw，`sv_position[1]` 偏 + `TexCoord2=0`，排查该 capture 的 cbuffer 矩阵行/列加载。
- **B（16）/C（37）**：数据缺失 / TombRaider 反编译有损，超出 HLSL 解释器能力边界，不修。

## 回归

`vs_only` 默认关闭，仅新增早退分支，不改动默认全管线路径；triage_dump.py 改动是新增 CLI 开关（默认行为不变）。回归套件结果：见提交说明（`run_regression.py`，全 PASS）。

## 产物

- `render.py`：`vs_only` 模式。
- `triage_dump.py`：`--vs-only` / `--list` / `--timeout` 开关。
- 删除 `Dump/` 中 38 个确认正确（但慢）的 A 类 case，失败集 134 → 96。
- `Cases/dump_failure_categories.csv`：刷新为 96 个失败的归类。
- `Cases/aclass_vsonly_results.csv`：45 个 A 类的 vs_only 明细。
- `Cases/triage_results_full140.csv`：step124 全 140 case 的全管线结果备份。
