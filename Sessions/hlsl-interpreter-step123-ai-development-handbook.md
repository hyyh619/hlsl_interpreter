# Step 123 — 撰写《AI 辅助开发手册》（HTML + Markdown 两版）

## 任务

用户要求把"如何与 AI 协作开发"沉淀成一份开发手册，回答 11 个问题（提示词、需求分析、
执行、数据流、调试、回归、TDD、纠错、记忆系统、两个 agent 周期对比），并产出两个版本：
HTML（带图）与 Markdown。所有论述要用**本项目这次开发里的真实例子**佐证。

## 思考

把 11 个问题映射到本仓库可核对的真实材料，避免泛泛而谈。选定贯穿全篇的三个案例
（都来自最近 step119–120，证据链完整、可复述）：

- **C1 多数组 cbuffer**（step119，提交 `1716ee5`）：`override_cbuffers_from_binary`
  只填首个数组字段就 `break`，`texgen[2]@c2` 读成 0。一个根因修好 5 个 case。
- **C2 反规格化数 FTZ**（step120，提交 `c398d86`）：`cb12[271].z` 字节 `0x00000001`
  作 float 是反规格化数 `1.4e-45`，GPU flush-to-zero 取 0；解释器保留非零→分支选错。
- **C3 (int) 强转语义**（step120，提交 `122e4f4`）：`(int2)v1.zw` 被位重解释成
  `1073741824` 而非 `2`，越界读纹理→`0/0=NaN`；改为仅在位运算下位重解释。

各问题的取材：
1. 提示词 → CLAUDE.md 四层上下文 + 本轮编号任务清单 + 约束清单 + 留痕要求。
2. 需求分析/计划 → triage 129 个 zip、聚类成根因、按 ROI 用 TodoWrite 排序。
3. 执行 → 七步闭环，以 C1 沿数据流回溯到那行 `break` 为例。
4. 数据流 → AI 累积上下文输入 / 工具调用输出 / 项目自身 zip→管线→golden 数据流。
5. 调试输出 → `[STMT]`/`[BINARY OP]`/`[METHOD]` 轨迹、分量级 `Error:`、`Total PASSED`，
   以 C3 的 `[1073741824]` smoking gun 和 C2 的分支回溯为例。
6. 回归 → 数据驱动 CSV + run_regression.py + 三条判据 + 每类加代表 case（23341/20899）。
7. TDD → "golden 即测试"变体，红→绿→防回退，以 C2 的 0/2160→2160/2160 为例。
8. 纠错 → 自我纠错(C2 误判翻案)、回归当裁判(C3 先证伪再改)、CLAUDE.md 护栏、用户预标约束。
9. 记忆系统 → memory/ 一事一文件 + MEMORY.md 索引，列出现有记忆及其避免的重复劳动。
10. 两周期 → 前期 opencode+minimax-m2.7（奠基/小步），后期 claude code+opus（攻坚/闭环）。

## 执行

- `Docs/AI-Development-Handbook.md` — 302 行 Markdown，含目录、表格、ASCII 数据流图。
- `Docs/AI-Development-Handbook.html` — 自包含 HTML，深色技术文档风，内嵌 5 个 SVG 图
  （提示词四层、开发闭环、输入/输出数据流、TDD 红绿循环、两周期时间线）、表格与
  callout。无外部资源依赖，本地浏览器直接打开即可看图。
- 校验：HTML 标签配平（svg 5/5、div 32/32、table 4/4、10 个 h2）。

## 结果

两版手册落在 `Docs/`；本 step 文档与提示词历史更新。手册内容全部可回溯到本仓库的
提交（`1716ee5`/`c398d86`/`122e4f4`）、`Sessions/` 日志与 `memory/` 记忆文件。

第 10 题（两个 agent 周期对比）基于 Sessions/ 步骤日志与 git 时间线的可见证据归纳：
step1–~110 小步搭骨架的节奏对应前期 opencode+minimax；step110+ 真机 capture +
回归纪律 + 根因攻坚对应后期 claude code+opus。
