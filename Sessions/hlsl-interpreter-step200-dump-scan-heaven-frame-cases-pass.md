# Step 200 — Hourly dump scan: 14 new cases (heaven_frame2596 + Frame-frame9222) all pass directly

## 背景 / 任务

定时任务（每小时）：扫描 `Dump/`，找出未登记进 `dump_case.csv` 的新 draw case 并运行；
不通过的定位 `hlsl_interpreter` 根因并修复、提交推送、纳入回归；直接通过的写入
`dump_case.csv` 并从 `Dump/` 删除。思考/执行/结果写入本 Session，摘要补入 Prompts 文档。

## 1. 扫描结果 —— 14 个新 case

`Dump/*.zip` 与规范 `./dump_case.csv` 的差集为 14 个：

- `Frame-frame9222_event1734.zip`、`Frame-frame9222_event1971.zip`
- `heaven_frame2596_event65.zip`、`_event801`、`_event1944`、`_event2134`、`_event7347`、
  `_event7416`、`_event7448`、`_event7582`、`_event7611`、`_event8284`、`_event8537`、`_event8928`

其中 **13 个（除 `heaven_frame2596_event65` 外）本就已在 `Cases/regression_test_zip_files.csv`
里**（此前的 run 已把它们纳入回归、且 `Cases/` 下已有对应 zip），只是从未登记进 `dump_case.csv`。
`heaven_frame2596_event65` 是唯一真正全新的 case（既不在回归、`Cases/` 下也没有）。

## 2. 运行结果 —— 全部直接通过（0 fix）

用 `triage_dump.py --list <14 个> --vs-only --workers 4`（VS-vs-golden 口径）运行，逐例
读取 `Cases/triage_logs/<stem>.log` 的 `Error:` 行数与 `Total PASSED rows`：

| case | Error: | PASSED rows |
|------|--------|-------------|
| Frame-frame9222_event1734 | 0 | 8475/8475 |
| Frame-frame9222_event1971 | 0 | 1404/1404 |
| heaven_frame2596_event65 | 0 | 19095/19095 |
| heaven_frame2596_event801 | 0 | 99/99 |
| heaven_frame2596_event1944 | 0 | 4680/4680 |
| heaven_frame2596_event2134 | 0 | 2688/2688 |
| heaven_frame2596_event7347 | 0 | 780/780 |
| heaven_frame2596_event7416 | 0 | 1848/1848 |
| heaven_frame2596_event7448 | 0 | 2688/2688 |
| heaven_frame2596_event7582 | 0 | 108/108 |
| heaven_frame2596_event7611 | 0 | 19095/19095 |
| heaven_frame2596_event8284 | 0 | 642/642 |
| heaven_frame2596_event8537 | 0 | 732/732 |
| heaven_frame2596_event8928 | 0 | 7248/7248 |

**全部 14 例 0 error、golden 全行匹配 → 直接通过，无需修改任何解释器代码。**

（注：`triage_dump.py` 在跑完后 `os.remove(临时配置)` 会因本 mount 禁止删除文件而抛
`PermissionError`；但这是收尾清理，各例的 render 与日志已正常产出，不影响判定。）

## 3. 收尾动作

- **`dump_case.csv`**：14 个新 case 名全部追加登记（现 146 行）。✅
- **回归清单**：本次**无需修复**，按任务约定「不需修复直接通过的 case 只写入 `dump_case.csv`、
  不入回归」，故未改 `regression_test_zip_files.csv`。其中 13 个本就已在回归中；
  `heaven_frame2596_event65` 为直接通过的新 case，按约定仅登记进 `dump_case.csv`。
- **未修改任何解释器代码**（`hlsl_interpreter.py` 等无改动）。

## 4. 无法完成的步骤（环境限制，非任务失败）

本沙箱 mount **禁止删除 / 重命名文件**（`rm`/`mv` 均报 `Operation not permitted`，与
step 195/198/199 记录一致），由此：

1. **无法从 `Dump/` 删除已通过的 14 个 zip**（任务 step 7 的删除动作）。文件保留在 `Dump/`，
   已登记进 `dump_case.csv`，下次扫描会因已登记而跳过，不会重复运行。
2. **无法 `git add/commit/push`**：`.git/index.lock` 为残留空锁文件且不可删除，任何改动
   索引的 git 操作都会 `fatal: Unable to create '.git/index.lock': File exists`。且历史记录
   本沙箱无 git 凭据、HTTPS 无法推送。本次**无解释器修复需提交**（全部直接通过），
   仅有 `dump_case.csv`/文档类改动。

**建议用户在本机**：清理 `.git/index.lock`，解决遗留的 `Docs/` unmerged 冲突，并
`git push` 现有领先提交（含既往 step 198 的解释器修复），随后如需可提交本次
`dump_case.csv` 登记与本 Session 文档。

## 5. 结论

本次扫描发现 14 个未登记 case，全部直接通过（0 error、golden 全匹配），**未发现需要修复的
解释器缺陷**。仅登记进 `dump_case.csv`；受 mount 限制未能删除 Dump 内 zip、未能 git 提交。
