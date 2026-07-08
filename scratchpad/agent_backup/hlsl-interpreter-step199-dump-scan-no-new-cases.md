# Step 199 — Hourly dump scan: no new cases; deferred 3061/3093 unchanged

## 背景 / 任务

定时任务（每小时）：扫描 `Dump/`，找出未登记进 `dump_case.csv` 的新 draw case，运行；
不通过的定位 `hlsl_interpreter` 根因并修复、提交推送、纳入回归；直接通过的写入
`dump_case.csv` 并从 `Dump/` 删除。

## 1. 扫描结果 —— 无新 case

`Dump/*.zip`（当前 137 个 zip）与**规范 `dump_case.csv`（仓库根 `./dump_case.csv`）**的差集为空。

- `Dump/` 里仅剩两个未被删除的 zip：`EndlessSpace2_event3061.zip`、`EndlessSpace2_event3093.zip`。
- 两者**均已在** step 198 登记进根 `dump_case.csv`（该文件含全部 8 个 EndlessSpace2 条目），
  因此**不是新 case**，本次无需运行。

### 关于两份 dump_case.csv 的说明

仓库里存在两份同名文件：

| 路径 | 状态 | 说明 |
|------|------|------|
| `./dump_case.csv`（根） | **规范** | step 198 写入，含全部 EndlessSpace2 条目（8 个），最近修改 |
| `./Dump/dump_case.csv` | 陈旧副本 | 无 EndlessSpace2 条目，早于根副本 |

历史各 step 均以**根 `./dump_case.csv`** 为准，本次沿用。（若后续想统一，可删除 `Dump/`
下的陈旧副本，但本次未改动，避免在无人值守时引入歧义。）

## 2. 两个保留 case 的状态（沿用 step 198 的结论，未变）

- **`EndlessSpace2_event3061`**：退化几何 —— 上游 `r4.xyz`/`r5.xyz` 坍缩成相同值 →
  `r4.xy - r5.xy = 0` → `1/sqrt(dot) = rcp(0) = inf` → 经 `mad` 传到 `o0` = nan。
  golden 有限，说明 GPU 上两者本应有微小差异；疑与 `t1/t2.SampleLevel` 采样/精度链相关，
  修复风险高、涉及采样精度链，**仍保留、未动**。
- **`EndlessSpace2_event3093`**：SV_VertexID 驱动、全部数据来自 StructuredBuffer 的 draw，
  dump 中无 `MeshOut_*_mesh.csv` golden，VS-vs-golden 工作流**无法校验**，**仍保留**。

本次未修改任何 `hlsl_interpreter` 代码，因此这两例的行为与 step 198 结论一致。

## 3. 仓库 git 状态（遗留问题，本次未触碰）

`git status` 显示 `Docs/` 下若干生成文件处于 **unmerged（UU/AA）** 冲突状态，且
`.git/index.lock` 残留、**无法删除**（该 mount 禁止删除文件，与 step 195/198 记录的
「文件不可删除」限制一致）。要点：

- 冲突**仅限 `Docs/` 生成的文档站文件**，**核心解释器代码（`hlsl_interpreter.py`
  等）完好、无冲突**。
- HEAD 仍为 `3bb9e72`（step 198 的修复），领先 `origin/main` 1 个提交，**尚未推送**
  （与既往「沙箱无 git 凭据 / 无法推送」记录一致）。

由于 `index.lock` 在本沙箱内不可删除，沙箱侧的 `git add/commit/push` 会失败。本次
**刻意不做 git 手术**（避免在无人值守时恶化已冲突的仓库状态）。**建议由用户在本机**：
解决 `Docs/` 冲突、清理 `index.lock`、并 `git push` 现有领先提交（含 step 198 的解释器修复）。

## 4. 收尾动作

- 未新增/修改解释器代码；未改回归清单；未改 `dump_case.csv`（已是最新）。
- 本会话记录写入 `Sessions/hlsl-interpreter-step199-dump-scan-no-new-cases.md`。
- 摘要补入 `Prompts/hlsl-interpreter-prompt-ClaudeCode.md` 对应 session。
