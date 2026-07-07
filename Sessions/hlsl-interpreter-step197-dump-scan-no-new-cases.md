# 197 扫描 Dump 新 draw case —— 本次无新增

## 任务

定时任务：扫描 `Dump/`，把不在 `dump_case.csv` 里的新 zip 追加进 csv 并运行；
不通过的定位并修复 `hlsl_interpreter.py`、提交推送；修好通过的加入回归、直接通过的写入
`dump_case.csv` 并删除 Dump 里的 zip。

## 思考与执行

1. **列出 Dump 里的 zip 与 csv 记录做集合对比**：
   - `Dump/*.zip`（basename 排序）= **123 个**
   - `dump_case.csv`（去表头/回车/空行后排序）= **123 条**
   - `comm -23`（Dump 有、csv 无）→ **空**
   - `comm -13`（csv 有、Dump 无）→ **空**
   - 两个集合完全一致，**没有任何未登记的新 case**。

2. **对照上一次运行**：上一步 step196（commit `368e870`）已经把 BlackMyth 四个 frame
   （12199 / 14374 / 15293 / 19470）总计 84 个新 case 一次性追加进 `dump_case.csv`。
   这批是顶点压缩解码 shader，`asfloat(exp<<23)` 位模式修复后 `sv_position` 已从
   `-2.7e13` 收敛到量级正确（`-0.078` vs golden `-0.118`），但四元数/切线帧重构支仍未整例通过，
   所以按规则**保留在 Dump、未入回归**，留作后续。它们已在 csv 中，故本次不再视为"新 case"。

3. **git 状态**：工作区干净，位于 `c940533`（最新）。本次无代码改动。

## 结果

- **无新 case**：Dump 内 123 个 zip 全部已登记在 `dump_case.csv`，无需追加、无需运行、无需修复。
- 未修改 `hlsl_interpreter.py`，未改动回归集，未删除任何 zip。
- 仅新增本会话记录文档。

## 备注（给后续运行）

- Dump 里现存的 BlackMyth 批次虽已在 csv，但**尚未整例通过**（step196 遗留：四元数/切线帧
  重构分支导致 `o1.xyz(TEXCOORD11)=0`）。若后续要清理，应先修复该分支使其整例通过，
  再删 zip / 入回归——目前不满足"直接通过即删"的条件。
- 判定"新 case"的口径：`Dump/*.zip` 的 basename 与 `dump_case.csv`（第二行起）的差集。
