# Step 203 — Hourly dump scan: longshu particle-GS 新 case，修复 GS 输入寄存器打包 + Texture1D SampleLevel

## 背景 / 任务

定时任务（每小时）：扫描 `Dump/`，找出未登记进 `dump_case.csv` 的新 draw case 并运行；
不通过的定位 `hlsl_interpreter` 根因并修复、提交推送、纳入回归；直接通过的写入
`dump_case.csv` 并从 `Dump/` 删除。思考/执行/结果写入本 Session，摘要补入 Prompts 文档。

## 1. 扫描结果 —— 7 个新 case

`Dump/*.zip` 与 `./dump_case.csv` 的差集为 7 个：

- `heaven_frame2596_event11844.zip`、`_event9450`、`_event9682`、`_event9944`
- `longshu-case-35-1_event128.zip`、`longshu-case-35-1_event166.zip`、`longshu-case-37_event25.zip`

其中 4 个 heaven case 早已在 `Cases/`（且 `event11844` 已在回归清单里），只是从未登记进
`dump_case.csv`。3 个 `longshu-*` 是全新的游戏 capture（`Cases/` 下没有）。

## 2. 首轮运行 —— 5 通过 / 2 失败

用自建 driver（`--vs-only` 口径；因本 mount 禁止 `os.remove`，`triage_dump.py` 收尾清理会崩，
故绕开）跑 7 例：

| case | 结果 |
|------|------|
| heaven_frame2596_event11844 | PASS 54/54 |
| heaven_frame2596_event9450 | PASS 4680/4680 |
| heaven_frame2596_event9682 | PASS 9/9 |
| heaven_frame2596_event9944 | PASS 3/3 |
| longshu-case-37_event25 | PASS 10920/10920 |
| **longshu-case-35-1_event128** | **FAIL** 0/19，`GS emitted-vertex count 21 != golden 19` |
| **longshu-case-35-1_event166** | **FAIL** 150/161，`GS emitted-vertex count 186 != golden 161` |

两个失败 case 都是 **粒子系统几何着色器（Geometry Shader）+ Stream Output**（`dcl_inputprimitive
point` / `dcl_outputtopology pointlist`，`maxvertexcount(2)`/`(6)`），报"发射顶点数不符"。

## 3. 根因分析

### 3.1 主 bug：GS 输入寄存器打包错位（`v[i][r]` 按签名行号索引而非寄存器号）

GS 反编译体用 `v[0][2].z`（AGE）、`v[0][3].x`（TYPE）读输入。看 `GS_input_output_signature.csv`：

```
Input,2,0,SIZE
Input,2,0,AGE     <- 与 SIZE 同为 slot 2（SIZE 占 .xy，AGE 打包在 .z）
Input,3,0,TYPE
```

`executeGS_with_params` 原本把 `slot_meta` 建成**每签名行一个条目的扁平列表**，`v[i][j]` 按列表
**位置**索引 → `[POSITION, VELOCITY, SIZE, AGE, TYPE]`（下标 0..4）。于是：

- `v[0][2]` → 取到 SIZE（无 .z，读到 0/垃圾），而 shader 期望这里 `.z` 是 AGE；
- `v[0][3]` → 取到 AGE，而 shader 期望这里 `.x` 是 TYPE。

AGE、TYPE 双双读错 → `if (v[0][3].x==0)`（判断是否发射器粒子）与 `g_EmitInterval < age`
（是否到发射间隔）全乱 → 发射的顶点数/内容错位。

**修复**（`hlsl_interpreter.py` `executeGS_with_params`）：按**寄存器号（slot）分组**签名条目，
把打包进同一寄存器的多个语义按"连续分量偏移"合并成一个寄存器向量（SIZE.xy 后接 AGE.z），
并让 `v2d[i][r]` **按寄存器号 r 建索引**（0..max_slot）。分量宽度取自 GS 输入参数类型
（`float2 SIZE`→2、`float AGE`→1…）。单语义寄存器保持原样直传 —— 未打包的常见 GS（sekiro4 等）
行为完全不变，回归零风险。

修复后：`event128` 21→19 顶点数**对齐**（"emitted-vertex count"错误消失），18/19 通过；
`event166` 186→161 对齐，156/161 通过。仅剩"新发射粒子"那几行差异。

### 3.2 次 bug：Texture1D 的 SampleLevel 坐标是标量，被守卫挡掉返回 0

新发射粒子的速度/位置来自 `g_TextureRandom.SampleLevel(g_SamLinear_s, r0.y, r0.z)` —— 一张
**Texture1D**（1024×1，R32G32B32A32_FLOAT 的随机向量表，`.img` 原始数据，已支持解码）。

`SampleLevel` 分支的坐标守卫是 `if coords and isinstance(coords, list) and len(coords) >= 2:`。
Texture1D 的坐标是**标量**（只有 u），不是长度≥2 的列表 → 守卫为假 → 返回 `None` → 采样项被
静默清零 → 新粒子速度/位置为默认值（错）。

**修复**（`hlsl_interpreter.py` SampleLevel 分支）：坐标若是标量（或单元素列表），补成
`[u, 0.0]`（v=0 命中 1D 纹理的单行）再走既有采样。

修复后：`event128` **19/19 全过**。

## 4. 剩余差异：event166 的精度极限（非逻辑 bug）

`event166` 修复后 156/161，剩 5 行（随机纹理发射出的新粒子）POSITION 差 **0.007~0.028**，
略超 `float_tolerance=0.005`。原因：

- 该 shader 的新粒子 `POSITION = g_EmitPosW + float2(30,30)*random.xz` —— **不归一化、直接放大 30 倍**；
  采样误差 ~0.0003 被 ×30 放到 ~0.01，正好越过容差。
- 对照 `event128`（同一随机纹理、同坐标）却全过：因为它的速度是 `normalize(random)*4`，归一化
  把逐分量误差消掉了。

即差异纯粹来自 **wrap 大坐标（`g_GameTime+n/5`）下线性过滤的 float32/double 精度**，落在此前
Session（step 153/162/163）已记录的 GS/纹理精度极限区间（相对容差需 ~3e-4；且 `float32_emulation`
只在 VS 阶段启用）。线性过滤本身的纹素中心约定（`u*w-0.5`）与 wrap 取 frac 都已正确。深挖需对整个
GS+纹理路径做 float32 仿真，收益低、回归风险高，**判定为已知精度极限，不再追**。

## 5. 收尾动作

- **代码修复**：`hlsl_interpreter.py` 两处（GS 输入寄存器分组打包；SampleLevel 标量坐标兼容 Texture1D）。
- **`dump_case.csv`**：7 个新 case 名全部追加登记（146→153 行）。
- **回归清单**：`longshu-case-35-1_event128.zip`（经修复通过）已 `cp` 进 `Cases/` 并加入
  `Cases/regression_test_zip_files.csv`（按 step 6）。直接通过的 4 个 heaven + `longshu-case-37_event25`
  按 step 7 仅登记 `dump_case.csv`（heaven 本就在 `Cases/`；`longshu-37` 未加回归）。
- **`event166`**：未完全通过（剩精度差异），登记进 `dump_case.csv` 但**不加回归**，本文档记录其精度极限成因。
- **回归验证**：改动后跑 `python run_regression.py --keep-logs`（`--keep-logs` 规避本 mount 的
  `os.remove` 限制）。结果见下节。

## 6. 回归结果

本沙箱两个限制影响完整回归：(a) 后台进程不跨独立 bash 调用存活（`nohup` 起的 157 例全量跑到
第 10 例即被回收）；(b) 单次 bash 调用上限 45s，跑不完带 500MB 级 zip 的全量套件。故改为
**定向回归**——本次改动只可能影响两类 case：GS 路径（`executeGS_with_params`）与标量坐标的
`SampleLevel`。GS 改动对"单语义寄存器"完全直传、字节级不变，只有"打包寄存器"的 GS 才受影响；
`SampleLevel` 标量分支对 2D 纹理（坐标恒≥2 分量）是纯 no-op。

跑了回归清单里**全部 5 个含 GS 的 case** + 若干纹理/SampleLevel 重点 case（全流水线口径，
`float_tolerance=0.005`）：

| case | 结果 |
|------|------|
| manhattan_frame_274_event50 | PASS 1000/1000, 0 err |
| sekiro4_event20560 | PASS 1074/1074, 0 err |
| Octopath-frame746_event102 | PASS 6/6, 0 err |
| Octopath-frame746_event4014 | PASS 6/6, 0 err |
| **longshu-case-35-1_event128（新增）** | **PASS 19/19, 0 err** |
| witcher3_countryside_event895（VS SampleLevel） | PASS 4428/4428, 0 err |
| Octopath-frame746_event283 | PASS 48/48, 0 err |
| heaven_frame2596_event7611 | 0 err（大 case，未跑完汇总，无 Error） |
| valley_frame272_event608 | 0 err（大 case，未跑完汇总，无 Error） |

含 GS 的 case **零回归**，`event128` 全流水线通过。建议用户在本机跑一次完整
`python run_regression.py` 做最终确认。

## 7. 环境限制（非任务失败）

- 本 mount **禁止删除/重命名文件**（`rm` 报 `Operation not permitted`）：无法从 `Dump/` 删除已通过的
  zip（step 7 的删除动作）。文件保留在 `Dump/`，已登记进 `dump_case.csv`，下次扫描会跳过，不会重复运行。
- `cp`（新建文件）可用，故能把 `event128` 复制进 `Cases/` 供回归。
- **git 提交/推送被阻断**（step 5）：`.git/index.lock` 为残留空锁文件且本 mount 不可删除
  （`Operation not permitted`），任何 `git add/commit` 都 `fatal: Unable to create '.git/index.lock'`。
  所有改动已落盘在用户目录，**请用户在本机删除 `.git/index.lock` 后提交并 push**：
  `hlsl_interpreter.py`、`dump_case.csv`、`Cases/regression_test_zip_files.csv`、
  `Cases/longshu-case-35-1_event128.zip`、本 Session、`Prompts/hlsl-interpreter-prompt-ClaudeCode.md`。

## 8. 结论

发现 7 个新 case，5 个直接通过。2 个 longshu 粒子 GS case 定位并修复了**两个真实解释器 bug**：
(1) GS 输入寄存器打包错位（`v[i][r]` 应按寄存器号而非签名行号索引）、(2) Texture1D 的 SampleLevel
标量坐标被守卫误挡。`event128` 由此全过并入回归；`event166` 剩 5 行随机纹理放大误差，属已知
精度极限。
