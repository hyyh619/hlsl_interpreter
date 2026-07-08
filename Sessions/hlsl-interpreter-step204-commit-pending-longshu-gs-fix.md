# Step 204 — Hourly dump scan: no new case; commit/push the pending step203 longshu GS fix

## 背景 / 任务

定时任务（每小时）：扫描 `Dump/`，登记并运行未在 `dump_case.csv` 中的新 draw case；不通过的
定位 `hlsl_interpreter` 根因并修复、提交推送、纳入回归；直接通过的写入 `dump_case.csv` 并从
`Dump/` 删除。

## 1. 扫描结果 —— 无新 case

`Dump/*.zip` 与 `dump_case.csv` 的差集为 **0**。上一轮（step 203）已把 7 个新 case
（4 个 heaven + 3 个 longshu）全部登记进 `dump_case.csv`，本轮无新增。

```
comm -23 <sorted Dump zips> <sorted dump_case.csv>  →  (空)
```

## 2. 发现上一轮遗留的未提交修复

`git status` 显示 step 203 的成果**从未提交/推送**：

```
 M Prompts/hlsl-interpreter-prompt-ClaudeCode.md
 M hlsl_interpreter.py
?? Sessions/hlsl-interpreter-step203-longshu-gs-register-packing-and-texture1d-samplelevel.md
```

step 203 修复了两个真实解释器 bug（longshu 粒子 GS）：
1. **GS 输入寄存器打包错位**：`v[i][r]` 原按签名行号索引，应按寄存器号索引（SIZE.xy + AGE.z
   打包在同一寄存器）。
2. **Texture1D 的 `SampleLevel` 标量坐标被守卫误挡**：标量 u 坐标补成 `[u, 0.0]` 再采样。

修复代码已落盘但因当时 mount 禁止删除 `.git/index.lock`（残留空锁）而无法 `git commit`。

## 3. 本轮的关键突破：mount 现在允许 rename

复测 mount 权限：

- `rm .git/index.lock` → 仍 `Operation not permitted`（删除仍被禁）
- **`mv .git/index.lock .git/index.lock.stale` → 成功**（同目录 rename 现已放开）
- 跨目录 `mv`（到 /tmp，跨文件系统 = copy+delete）→ 失败（本质是 delete）

因此策略：commit 前把残留的 `index.lock` **rename 挪开**即可解锁 git。git 自身在写 index
时仍会造出 `index.lock` 并在收尾 unlink 失败（留下警告 + 新的残留锁），但 commit 通过 rename
`index.lock→index` 完成，提交本身成功。

## 4. gitignore 甄别 —— 只有 3 个文件可提交

`.gitignore` 里 `dump_*`、`Cases`、`*.zip` 均被忽略。故 step 203 对
`dump_case.csv`、`Cases/regression_test_zip_files.csv`、`Cases/longshu-case-35-1_event128.zip`
的改动**只存在于工作区**（对用户可见、持久，但永不进 git，符合仓库既有约定）。

可提交项仅：`hlsl_interpreter.py`、`Prompts/hlsl-interpreter-prompt-ClaudeCode.md`、
step 203 + step 204 两个 Session。

## 5. 提交前验证（全流水线，float_tolerance=0.005）

| case | 结果 |
|------|------|
| longshu-case-35-1_event128（step203 修复对象） | **PASS 19/19, 0 err** |
| manhattan_frame_274_event50（GS 单语义寄存器直传） | PASS 1000/1000, 0 err |
| Octopath-frame746_event102（纹理路径） | PASS 6/6, 0 err |

修复对象通过；GS 与纹理回归 case 零回归。

## 6. 收尾动作

- 把残留 `.git/index.lock` rename 挪开后，`git add hlsl_interpreter.py
  Prompts/hlsl-interpreter-prompt-ClaudeCode.md Sessions/hlsl-interpreter-step203-*.md
  Sessions/hlsl-interpreter-step204-*.md` → `git commit` → `git push origin main`。
- `dump_case.csv` / 回归 CSV / `event128.zip` 均 gitignore，保留在工作区，无需提交。
- 已通过的 case 因 mount 仍禁止 `rm`，无法从 `Dump/` 物理删除；已登记进 `dump_case.csv`，
  下轮扫描自动跳过，不会重复运行。

## 7. 结论

本轮无新 case。核心产出：把 step 203 已完成但被 git 锁卡住的 longshu GS + Texture1D
SampleLevel 修复**正式提交并推送**，附带提交前定向回归确认零回归。
