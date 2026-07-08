# Step 195 — 运行 Dump 新 draw case，修复交叉复用输出寄存器"真零写入被误判为未写"

## 任务
扫描 `/Dump`，把 `dump_case.csv` 未记录的新 case 逐个运行；不过的定位 `hlsl_interpreter` 根因并修复，
通过的加入回归 / 记录，最后提交推送。（scheduled hourly，无人值守。）

## 环境限制（本轮踩到的坑）
- 每次 shell 调用上限 45s；后台进程**跨调用不保活**（VM 只持久化文件系统，不保活进程）——故回归无法用
  `nohup` 一次跑完，改为**前台分片**（每片 <44s，结果追加到持久文件）。
- 挂载目录（Dump / Cases / outputs）**禁止删除文件**（`os.remove`/`rm` → "Operation not permitted"），
  但可写/可追加。因此 step 6 "删除 Dump 中已通过的 zip" 本环境无法执行，改为仅把已解决的 case 写入
  `dump_case.csv`（防止后续 run 重复处理）。
- zip 解压临时目录默认落在 `/tmp`（根盘仅 ~4GB），大 case 会撑爆——把 `TMPDIR` 指到挂载盘
  `Dump/.scratch_tmp`（193GB）后大 case 也能解压，仅剩时间是瓶颈。

## 新 case 识别
`Dump/` 共 123 个 zip，`dump_case.csv` 已记录 31 个 → **92 个新 case**，很多极大（多个 200–517MB）。
本轮在时间预算内处理了最小的一批（<35MB 优先）。

## 运行结果（本轮已评估的新 case）
- **PASS（直接通过，7）**：`BlackMyth_frame14374_event1343 / event1320 / event10199`、
  `BlackMyth_frame19470_event1293 / event6294`、`BlackMyth_frame12199_event9702`、
  `BlackMyth_frame15293_event9127`。
- **FIXED（修复后通过，1）**：`BlackMyth_frame14374_event3393`（0/30960 err → 30960/30960）。
- **FAIL（复杂 shader 类，未修，需后续专项）**：`BlackMyth_frame19470_event5690 / event2939 / event6173 /
  event7117`、`BlackMyth_frame12199_event8441 / event9319`、`BlackMyth_frame15293_event8040 / event8484`、
  `BlackMyth_frame14374_event3256 / event9526 / event8750 / event9829` 等——全部 `passed 0/N`，
  `sv_position` 直接是 ~1e12 级垃圾。
- 其余 ~74 个大 case（多为上述失败类，或因 45s 预算超时）本轮未评估，留待后续 run。

## 修复的根因（event3393）
失败只在 `TEXCOORD1.y`（键 `TexCoord2[1]`），周期性行 `output=1.0 golden=0.0`。

VS 把 `TEXCOORD0`(o1) 与 `TEXCOORD1`(p1) **打包进同一输出槽 slot 1**：`o1.xy`=TEXCOORD0，
`o1.zw`(=`p1.xy`)=TEXCOORD1，即 `o1.w` 与 `p1.y` 是**同一物理分量**。
- 退化分支写 `o1.xyw=0`（其中 `.w` 即 TEXCOORD1.y），从不写 `p1.y`；
- 实几何分支写 `p1.y=(r0.x%16)/16`、`p1.x`，从不写 `o1.w`。

`_resolve_slot_shared_params` 用"次级参数是否全零"判断它有没有被 shader 写：在 `r0.x%16==0` 的行上
shader **合法地写了 `p1=(0,0)`**，却被 `is_default`（全零）误判为"未写"，于是回退去取主参数 `o1` 溢出分量——
而 `o1.w` 是 D3D 寄存器初值 `(0,0,0,1)` 的 `.w=1.0`，导致 TEXCOORD1.y 被写成 1.0。

（注：step194 刚修过本函数"次级*真未写*时补 `.w=1.0` 默认"的分支；本轮是同一函数里"次级*被写成零*被
误判为未写"的相反 bug。）

## 修复
把"全零猜测"换成**精确的写入追踪**：
- `_execute_void_main` 每次调用前建 `self._out_written={}`、`self._output_param_names`；
- `_apply_swizzle_assign`（及整量赋值 `_record_full_out_write`，覆盖 fast-path kind2 与 `_assign_lvalue`）
  记录每个输出参数被 shader 实际写入的**分量下标**；
- `_resolve_slot_shared_params` 逐寄存器分量按优先级重建：①次级语义被直接写→用它；
  ②否则主参数对应 lane 被写（如 `o1.w` 溢入 TEXCOORD1.y）→用主参数；③都没写→D3D 寄存器初值
  （寄存器索引 3=`.w` 取 1.0，其余 0.0）。写追踪不可用时保留旧全零启发式作 fallback。

## 验证
- `event3393`：30960/30960（原 0/30960）。
- **全量回归 152 case 全过、0 FAIL**（其中 4 个超大 case 因 45s 预算只做了前 1500 顶点/整段的
  0-error 有界验证）。改动只影响"交叉复用次级输出"的重建，与 D3D 语义对齐，只会改善匹配。
- 特别确认既有交叉复用类（Octopath `event3502` 的 TEXCOORD12/13、witcher 的重复 `p0`）仍通过。

## 改动文件
- `hlsl_interpreter.py`：`_apply_swizzle_assign`、新增 `_record_full_out_write`、fast-path/`_assign_lvalue`
  写记录、`_execute_void_main` 初始化写追踪、`_resolve_slot_shared_params` 重写。
- `Dump/dump_case.csv`：追加 7 直接通过 + `event3393`。
- `Cases/regression_test_zip_files.csv`：追加 `BlackMyth_frame14374_event3393.zip`。

## 后续 TODO
- 处理剩余 ~74 个新 BlackMyth 大 case。
- 专项攻关失败的"复杂顶点解压/蒙皮"shader 类（typed `Buffer<float4>.Load`、位域抽取、half-float 解包、
  四元数蒙皮）——目前 `sv_position` 全是垃圾，属多特性缺口，非单点 bug。
