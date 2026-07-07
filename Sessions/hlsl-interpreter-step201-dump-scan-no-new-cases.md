# Step 201 — Hourly dump scan: no new cases; git blockers cleared

## 背景 / 任务

定时任务（每小时）：扫描 `Dump/`，找出未登记进 `dump_case.csv` 的新 draw case 并运行；
不通过的定位 `hlsl_interpreter` 根因并修复、提交推送、纳入回归；直接通过的写入
`dump_case.csv` 并从 `Dump/` 删除。思考/执行/结果写入本 Session，摘要补入 Prompts 文档。

## 1. 扫描结果 —— 无新 case

当前 `Dump/` 有 **139 个 zip**，规范 `./dump_case.csv`（仓库根）有 **145 条**登记（不含表头）。
两者差集 `comm -23`（Dump 独有 − csv）为**空** → **没有任何未登记的新 case**，本次无需运行、
无需修复。

### 关于两份 dump_case.csv 的说明（沿用 step 199）

| 路径 | 状态 | 说明 |
|------|------|------|
| `./dump_case.csv`（根） | **规范** | 最新，含全部当前 Dump case（145 条） |
| `./Dump/dump_case.csv` | 陈旧副本 | 缺 EndlessSpace2 / Frame / heaven 等条目，早于根副本 |

历史各 step（197/199/200）均以**根 `./dump_case.csv`** 为准，本次沿用。若拿 `Dump/dump_case.csv`
陈旧副本比对会误报 16 个「新」case（实际都已在根 csv 登记），已核实排除。

## 2. 保留 case 状态（未变）

`Dump/` 内仍保留此前直接通过但因 mount 限制未能删除的 zip，以及两个刻意保留的 EndlessSpace2 case：

- **`EndlessSpace2_event3061`**：退化几何，上游 `r4/r5.xyz` 坍缩 → `rcp(0)=inf` → `o0`=nan；
  修复风险高（涉采样精度链），保留。
- **`EndlessSpace2_event3093`**：SV_VertexID + StructuredBuffer draw，dump 无 `MeshOut_*_mesh.csv`
  golden，VS-vs-golden 工作流无法校验，保留。

两者均已在根 `dump_case.csv` 登记，故本次扫描按「已登记」跳过，行为与 step 198/199 一致。

## 3. 仓库 git 状态 —— 此前遗留阻塞已清除 ✅

与 step 198/199/200 记录的阻塞不同，本次 `git status` 干净：

- **`.git/index.lock` 已不存在**（此前残留且不可删除的空锁文件已被清理）。
- **无 `Docs/` unmerged 冲突**（此前 UU/AA 冲突已解决）。
- `git status -sb`：`## main...origin/main`，仅一个未跟踪目录 `scratchpad/`（临时物，未纳入）。
- `git rev-list --left-right --count origin/main...HEAD` = `0  0` → **本地 main 与 origin/main 完全同步**，
  step 198 的解释器修复（`3bb9e72`）及后续 docs 提交（`b941107`）均已在 origin 上。

即：先前各 step 反复提示「需用户在本机清 index.lock / 解冲突 / push」的历史遗留问题，
**现已解决，无待推送的领先提交**。

## 4. 收尾动作

- **未发现新 case** → 未运行、未修复、未改动任何 `hlsl_interpreter` 代码。
- **未改回归清单**（`Cases/regression_test_zip_files.csv` 保持不变）。
- **未改 `dump_case.csv`**（已是最新，含全部当前 Dump case）。
- **无 Dump 删除动作**：无新通过 case；历史保留 zip 仍受 mount「禁止删除文件」限制（`rm` 报
  `Operation not permitted`），但已登记，下次扫描仍会跳过。
- 本会话记录写入本文件；摘要补入 `Prompts/hlsl-interpreter-prompt-ClaudeCode.md`。

## 5. 结论

本小时扫描：`Dump/` 139 个 zip 全部已登记在规范 `dump_case.csv`，**无新增 draw case**，
无需运行 / 修复 / 提交。相较上一次运行的唯一变化是**仓库 git 阻塞（index.lock 残留、Docs 冲突、
领先未推送）已清除，main 与 origin 同步**。
