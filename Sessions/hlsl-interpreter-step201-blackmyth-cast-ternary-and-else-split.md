# Step 201 — BlackMyth 顶点压缩解码：两处解析器/控制流根因（cast-吞噬-三元、`;`前的`else`被拆散）

Date: 2026-07-07

## 任务

修复 BlackMyth 顶点压缩解码 shader 的 `0/N` 全失败。这批 case（frame12199/14374/15293/19470
四帧、150–500MB 不等）在 step196（`asfloat(exp<<23)` 位模式）、step198（`&` 位选择走 FMA）
之后仍整例不过，被标记为"四元数/切线帧重构支未解决"。本轮定位并修复其中的主根因。

## 调试方法

`Dump/` 里最小的失败 case 是 `BlackMyth_frame19470_event7117`（**只有 6 个顶点**，7MB），
是理想的调试标本。用 `execute_count=1, print_sequence=1` 拿到顶点 0 的**逐语句 [STMT] 轨迹**，
逐行对照寄存器值定位分歧点。

初始症状（step196 修完巨值后）：
- `sv_position` 六个顶点**完全相同**（golden 逐顶点不同），而法线 `TEXCOORD10` 逐顶点在变；
- `TEXCOORD11.w`（切线符号）恒为 `-1`，golden 恒为 `+1`。

"位置对所有顶点相同、但法线在变"这一反差指向：**切线基向量 `r4/r5/r6` 解码坍缩成常量/零**，
于是 `r2_local = v0·基 + 中心` 丢失了 `v0`（逐顶点位置）的贡献，只剩每-instance 常量中心。

## 根因一：cast 贪婪吞掉三元表达式（`hlsl_syntax_tree.py`）

轨迹里这条**基向量交换惯用法**（DXBC `movc` 对）算错：

```hlsl
r0.x = (int)r5.z ? r0.x : r5.w;   // 轨迹: => r0.x = 0   （r5.z=32768, r0.x=0.7616, r5.w=-0.648）
r2.x = (int)r5.z ? r5.w : r0.x;   // 轨迹: => r2.x = 0
```

两个结果都是 `0`——既不是真分支也不是假分支值，但恰好 `(int)0.7616 = 0`、`(int)(-0.648) = 0`。
即 `(int)r5.z ? A : B` 被解析成 `(int)(r5.z ? A : B)`：cast 把整个三元表达式当成了操作数，
再把选中的分支**值截断**为 0，导致 `r5.xyz`（切线基）整支归零。

`_parse_expression` 的顺序是：一元 not → **位运算 top-level 守卫** → **cast** → 括号 → 三元。
位运算已有守卫（`(int2)a | (int2)b` 不被 cast 吞），但**三元没有**。而三元条件运算符在 HLSL 里
**优先级最低**（低于 cast 和所有二元/位运算），必须最先切分。

**修复**：把 top-level `?` 的检测**移到 cast/位运算守卫之前**。存在 top-level `?` 时先按三元切分，
条件子式（如 `(int)r5.z`）递归解析时 cast 才正确地只作用于 `r5.z`。
（顺带修正 `a & b ? c : d` 这类会误切在 `&` 的情形。）

## 根因二：`;` 后的 `else` 被拆成孤儿语句（`hlsl_interpreter.py`）

修完根因一后，只剩 `TEXCOORD11.w` 符号错。轨迹显示 `ubfe`（位域提取）惯用法**整条没执行**：

```hlsl
if (1 == 0) r0.x = 0; else if (1+20 < 32) { r0.x = (uint)r0.y << 11; r0.x = (uint)r0.x >> 31; } else r0.x = (uint)r0.y >> 20;
r0.x = r0.x ? -1 : 1;      // r0.x 停在上游残值(973078528, 真) → -1；应提取 bit20=0 → +1
```

轨迹里这条的 "Executing:" 竟以 `else if (...)` 开头 —— 说明**语句分割器在 `r0.x = 0` 后的 `;`
处切断了**，把 `else if ... {...} else ...` 拆成一条以 `else` 开头的孤儿语句（无法识别→静默丢弃），
于是符号提取从未运行，`r0.x` 保留上游残值。

`GenerateStmts` 对 `}` 后紧跟 `else` 已有"向后窥探、暂不切分"的处理（step 早期为 `if(){}else{}`
加的），但对 **`;` 后紧跟 `else`** 没有同样的窥探。

**修复**：在 top-level `;` 切分处加同样的 `else` 前瞻——下一个非空白记号是 `else` 时不切分、
把 `;` 并入当前语句，保持 `if (c) s; else ...` 完整。

## 验证

- `event7117`：`0/6 → 6/6`（两处修复缺一不可：只修根因一仍差切线符号）。
- 批量复跑此前 `0/N` 的小 BlackMyth：**新增通过** `event8441 900/900`、`event8484 705/705`、
  `event2939 1065/1065`、`event5690 528/528`。
- 解析器单元抽查：`(int)r5.z ? a : b`→ternary、`(int)r0.y`→cast、`(uint)r0.x<<1`→binary(<<)、
  `cb1[0].xyzw*r1.xxxx`→binary(*) 均正确，无回归。

### 仍未通过（**不同的根因**，非本轮范围）
- `event8040`（0/6）：`COLOR1.x` 与 `COLOR1.z` **互换**（output[0]↔[2]）——某处 swizzle/分量顺序问题。
- `event9319 / 3256 / 9526 / 9829 / 15293_8040`：`TEXCOORD11`/`TEXCOORD7` 仍偏大——更深的解码变体。

这些是**独立的后续问题**，与本轮修复的 cast/三元、`else` 拆分无关。

## 回归

`python run_regression.py` 全量跑完 **154 例：151 PASS**，与 step198 基线**逐一致**——3 个未过
全部是 step198 已 A/B 证明**与本改动无关的既有失败**：`witcher3_countryside_event16834`
（189err 3/30，子集/golden 对齐产物）、`OldWorld_event1034` / `OldWorld_event2767`
（0/203328，改前即 0/N）。对位运算最敏感的 `BlackMyth_frame14374_event3393` 仍 **30960/30960**，
Octopath/witcher/sekiro/EndlessSpace 全绿。结果数与失败集与改前**完全相同 → 本次改动零回归**。

新增回归：把 `BlackMyth_frame19470_event7117`（6 顶点、两处修复的规范标本）加入
`Cases/regression_test_zip_files.csv`（并拷入 `Cases/`），锁死本次修复。

## 结论
两处修复都是**解析器/控制流层面的通用正确性修复**（不是给 BlackMyth 打补丁）：cast 不再吞三元、
`if…; else…` 不再被 `;` 拆散。修好一批 BlackMyth 顶点压缩 case，其余失败为不同根因，留作后续。
