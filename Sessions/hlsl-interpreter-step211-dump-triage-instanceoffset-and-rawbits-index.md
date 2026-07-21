# Step 211 — Dump 全量分诊：InstanceOffset + 语义映射 + 结构化缓冲原始位索引，修好 20 例

Date: 2026-07-20

## Prompts（本步任务）

1. 测试 `Dump/` 下所有 draw zip 是否通过；未通过的**修复通过后**加入 regression test。
2. `Dump/` 下不需修复即通过的 zip：**删除该 zip 文件**，并把通过的测试加入
   `Cases/pass_but_not_in_regression.csv` 列表。

---

## 思考 · 盘点与批测

`Dump/` 共 **489** 个 zip、6.4GB。写并行批测脚本（复用 `run_regression.BASE_CONFIG` + `analyze_log`
的确切判定），首轮结果：**406 PASS / 83 FAIL**。

按游戏前缀分组失败：**57 PlanetCoaster** 独占鳌头，其余 witcher3(6)、manhattan(5)、EndlessSpace2(2)、
TombRaider(2) 等零散。→ 主攻 PlanetCoaster（一修多得）。

---

## 执行 · 三个通用根因（PlanetCoaster GPU-driven vertex-pulling）

PlanetCoaster VS 是极复杂的 GPU 驱动顶点拉取：`SV_VertexID` + 6 个 StructuredBuffer（`t0..t5`，
`struct{float val[N]}`）+ 位运算 + 整数除 + `t3.Load`，逐级从缓冲拉取并解压网格。

### Bug 1 · 实例属性拉取忽略 `InstanceOffset`（StartInstanceLocation）

`draw_call_info.csv`：`DrawInstanced, NumInstances=1, InstanceOffset=201`。POSITION7 是
`R32_SINT, PerInstance=True`，D3D 按 `InstanceOffset + instanceID` 取实例属性 → 应取 VB[201]=18836，
但 `load_per_instance_data(instance_index=0)` 取 VB[0]=0 → 整条顶点拉取从错误基址开始。

**修复**：新增 `render.py::_draw_instance_offset()` 读 `InstanceOffset`；`load_per_instance_data` 加
`instance_offset` 参数，属性取址用 `(instance_offset+instance_index)*stride`（SV_InstanceID 仍取
`instance_index`，D3D 语义如此）。

### Bug 2 · RenderDoc 布局丢失语义索引，实例属性映射不到 VS 输入

VS 输入 `int4 v0 : POSITION7`，但 `ia_input_layouts.csv` 的元素名只有 `POSITION`（丢了索引 7）。
`load_per_instance_data` 只按 `{base}{idx}` 或 `idx==0 时的 base` 匹配 → `POSITION7` 匹配不到
`POSITION` → v0 默认 0。

**修复**：当没有精确 `{base}{idx}` 元素时，回退到 **base 名匹配**（`POSITION` → `POSITION7`）。

### Bug 3 · StructuredBuffer `float` 成员存整数索引，被当浮点截断

`t1[i].val[3]` 存的是 t0 的整数索引（如 25603），但 struct 声明 `float val[4]`，解释器解码成
float32 **非规格化数**（25603→3.6e-41）；随后 `t0[r1.w]` 用它做下标，`int(3.6e-41)=0` → 恒取 t0[0]。
而 DXBC 的 `ld_structured` 下标用寄存器**原始 uint 位**（=25603）。

**修复**（`_eval_subscript`）：缓冲下标求值时，若结果是极小非零浮点（`0<|v|<1e-30`，只有整数位型
才会落此区间；真实浮点下标 ≥1），按 `asuint` 位重解释而非截断。

---

## 结果

三修复令 **20 个 PlanetCoaster** 从 FAIL→PASS，且**零回归**（全 489 重测：406→426 PASS，无一
PASS→FAIL；162 回归仍全绿）。

**任务动作（分诊 489）：**
- **20 修复例**（均 PlanetCoaster）→ 拷入 `Cases/` + 加入 regression（**155→182 条**）+ 删除 Dump 副本。
- **406 免修通过**：149 个本就在回归清单（删除 Dump 冗余副本，不重复入表）；257 个不在回归 → 写入
  `Cases/pass_but_not_in_regression.csv`（257 条）+ 删除 Dump 文件。
- **63 仍失败** → 保留在 Dump，待后续。共删除 Dump 中 **426** 个 zip，Dump 余 63。

**全回归**：`python run_regression.py`（182 例）→ **182/182 passed**（20 新增 PlanetCoaster 全过，零回归）。

**63 仍失败分类（诊断留档，非通用 bug）：**
- **37 PlanetCoaster**：同族但更深的变体——(a) 输出寄存器存整数、golden 按 int 比较而解释器按 float
  （如 event12358 `TEXCOORD4=46198815` vs 输出 2.8e-37=同位型）；(b) 少数顶点发散（event10717
  18931/18944）。需输出类型感知 + 逐顶点边界排查。
- **8 no-comparison**：无 VS-golden 网格可比（compute/UAV/GS-only 等不同 draw 类型），无法按 VS 判定。
- **manhattan(5)/witcher3(7)/TombRaider(2)/sekiro2(1)** 多为近过（998~999/1000、827/1548 等）精度/边界。
- **EndlessSpace2_event3061** NaN（packed 数据纹理解码发散，step210 已诊断）；three.js exit=1 崩溃。

---

## 关键结论

- 实例化 draw 必须尊重 **InstanceOffset**（StartInstanceLocation）取实例属性；SV_InstanceID 不含该偏移。
- RenderDoc 布局可能**丢语义索引**，实例属性映射需 base 名回退。
- DXBC 缓冲下标用**寄存器原始 uint 位**；存于 `float` 成员的整数索引解码为非规格化浮点，下标求值须
  `asuint` 而非截断。
- PlanetCoaster 余下失败是**输出整型比较**与**逐顶点边界**两类更深问题；其余多为精度近过或无 golden
  可比，属非通用长尾，留待后续。
