# Step 158 — golden CSV「N 个连续 float3 输出」列重映射：核实已实现 + 加固 + 锁回归

## 任务

> D：实现 golden CSV「N 个连续 float3 输出」的列重映射。

（来自 step 156 的判断：「Octopath golden CSV 多 float3 列错位，loader 只补偿了单个 trailing float3」。）

## 核心结论：该重映射**已正确实现**，step 156 的前提不准确

深入读 `load_vs_golden_from_mesh_csv` 后发现：它**不是**「只补偿单个 trailing float3」，而是用
**「SV_Position 在前 + 每输出按 dumped 宽度（header 列组计数 `_dumped_count`）定位」**的方案，这本身就能正确
重映射**任意条数的 reduced-width（含 float3）连续输出**。step 156 写「loader 只补偿一个 float3」是没细读
loader 的误判（CLAUDE.md 里 `[None,x,y]` 那段描述的是早已被重写掉的旧实现，现 loader 与比较函数里都没有该逻辑）。

**实证 1 — event3502（典型 N 连续 float3）**：输出含 `TEXCOORD12`(float4 只写 .xyz→dump 3 列) +
`TEXCOORD13`(float3→3 列) **两个连续 3 列组**。step 156 的 half4 解码修复后，本步实测 **PASS 384/384**。
loader 把物理列正确切给各输出（`TEXCOORD12/13` 取到 dump 中的尾部 0，且我方解释器同样输出 ~0，匹配）。

**实证 2 — Collision 套件（trailing float3 WORLDPOS，回归用例）**：`out float2 TEXCOORD0/1` +
`out float3 NORMAL/WORLDPOS`，header 组宽度 2/2/3/3，长期 PASS。即「中间 float2 + 尾部连续 float3」也早被
正确处理。

**实证 3 — 全 86 个 Dump 用例扫描**：新增「列重映射对账」诊断（见下）后跑全部 Dump，**0 个触发错位告警** ——
即 loader 的 SV-first + dumped-width 切分在每个用例都精确消费完所有物理分量列，无一错位。

## 本步落地（加固 + 锁定，零行为风险）

既然重映射已正确，贸然改 loader 的切分逻辑只会有回归风险且无收益。故落地两件安全且有价值的事：

1. **对账诊断 + 显式文档**（`hlsl_interpreter.py::load_vs_golden_from_mesh_csv`）：
   - 给列分配循环补上明确注释，点明它就是「N 连续 reduced-width/float3 输出」的重映射处（举 event3502 /
     Collision 为例）。
   - 加 **reconciliation guard**：SV-first、按 dumped 宽度的分配必须**恰好**消费完物理分量列；若
     `cursor != len(comp_col_indices)` 则 `log_output` 告警 —— 这正是未处理的 mesh-export 错位的信号，
     变「静默错切后续输出」为「响亮报警」。跑全 86 Dump + 126 回归均无告警。

2. **把典型用例锁进回归**：将 `Octopath-frame746_event3502.zip`（2 连续 float3 + half4 解码）加入
   `Cases/regression_test_zip_files.csv`（拷贝 zip 到 `Cases/`）。回归 **126/126 PASS**。此用例同时守护
   step 156 的 half4 解码与本步的 N-float3 列重映射，防回归。
   （注：`Cases/` 整目录 .gitignore，回归套件本就是本地数据，此锁定与既有 125 条一样为本地生效。）

## 其余 D 类失败 ≠ 列重映射问题（澄清）

复triage 9 个 Octopath D 用例（step 156/157 后现状）：
- **event3502**：PASS 384/384（半精度修复之功）。
- **event2135 / event2912**：`TEXCOORD10/11` 失败，但**不是**列错位 —— 是**四元数/骨骼基重建的分量置换**
  （o0 实测 `[0.719,0.661,0.213]` vs golden `[0.661,0.719,0.213]` 为 x↔y 交换；o1 为 3-cycle；每输出置换
  各异，非统一移位）。属解释器解码 bug，非 golden 列问题。
- **event664 / event3012**：`sv_position` 失败（蒙皮/精度；event3012 30/51）。
- **event576 / event2651 / event2682**：地形 heightmap 退化（cb 全 0 → LOD/索引塌缩），sv_position 8/23064。

均与「golden 列重映射」无关。

## 结果

- **核实**：N 连续 float3 列重映射在 loader 中**已正确实现**（SV-first + dumped-width 定位），event3502
  PASS 384/384、Collision 长期 PASS、全 86 Dump 对账零错位。
- **加固**：新增列重映射对账诊断（misalignment 即告警）+ 显式文档。
- **锁定**：event3502 入回归，**126/126 PASS（零回归）**。
- **诚实结论**：本步无新 Dump 用例因「列重映射」翻盘 —— 因为该功能本就正确；step 156 的「待修」前提系误读
  loader。剩余 D 失败是四元数解码 / sv_position 精度 / 地形退化，属不同问题。

## 后续
- D 类真正可推进的是 **event2135/2912 的四元数/骨骼基分量置换**（解码顺序 bug，非列重映射）——下一个该查的点。
- event664/3012 sv_position 蒙皮精度；event576/2651/2682 地形退化（cb 全 0，可能需要缺失的 cbuffer 数据）。
