# Step 210 — Cases 全量过测：raw-bits 字面量/值运算 + cbuffer 尾数组补全，新增 9 例入回归

Date: 2026-07-20

## Prompts（本步任务）

1. 测试 `Cases/` 下所有 draw zip 是否都通过；未通过的**修复通过后**加入 regression test。
2. `Cases/` 下的 draw zip 是否都已加入 regression test；若未加入但**无需修复即通过**，则加入。

---

## 思考 · 盘点

`Cases/` 共 **164** 个 zip，回归清单原有 **155**，即 **9 个未入回归**。逐个跑（复用 `run_regression.run_case`
的确切判定：exit 0 且无 `Error:` 行且 `Total PASSED rows: X/X`）：

| zip | 结果 |
|---|---|
| Octopath-frame746_event2091 | ✅ PASS 6/6 |
| manhattan_frame_274_event1041 | ✅ PASS 228/228 |
| EndlessSpace2_event3061 | ❌ 1188/1536（NaN） |
| sekiro4_event2264 / 13742 / 13806 / 14228 / 16660 | ❌ 全 0/N |
| witcher3_countryside_event22229 | ❌ 10/12 |

外加**已在回归但失败**的 3 例：`OldWorld_event1034`、`OldWorld_event2767`（各 0/203328）、
`witcher3_countryside_event16834`（3/30）。

---

## 执行 · 三个独立通用根因（全部定位到 `<system-reminder>` 之外的真实 bug）

### Bug 1 · raw-bits 整数运算里的**源码数字字面量**被当成 float 位型（→ OldWorld ×2）

`OldWorld` VS：`cb5[r0.x+1]`，其中 `r0.x` 来自 `(uint)r0.x << 3`（位运算 → 标记为 `_RawBits`）。
索引里的 `+1`：

```
[BINARY OP] left=0, right=1065353216, op=+ (raw i32), result=1065353216
```

`1065353216 = 0x3F800000` = float `1.0` 的位型！源码字面量 `1`（解析为 float 1.0）在 raw-bits 伙伴规则
里被**位重解释**，于是 `cb5[1065353216]` 越界 → 返回 0 → 整条变换坍缩为 0，输出全 0。

**修复**（`evaluate_syntax_tree` 的 raw-i32 分支）：伙伴若是 `_is_numeric_literal_node`（源码数字字面量），
它是**真值**而非寄存器位型，保持整数（`int(1.0)=1`）。与 6604 行 `>>` 分支既有先例一致。
→ `cb5[r0.x+1]` 正确取 `cb5[1]`。**OldWorld ×2 → 203328/203328。**

### Bug 2 · 3Dmigoto **尾部 cbuffer 数组尺寸被低报**（→ sekiro4 ×5）

sekiro4 是蒙皮+实例网格：`float4x3 VC_aObjMatrix[2]`，但**实际绑定的 `constant_38312.bin` = 3072B =
64 个矩阵**，着色器用 `VC_aObjMatrix[r0.x/3]` 索引到 [8] 等。`override_cbuffers_from_binary` 按 HLSL
声明的 `[2]` 只加载 2 个元素，索引 [8] 越界 → 0 → 变换全 0：

```
VC_aObjMatrix (float4x3[2]):  [0] <none>  [1] <none>   （bin 里 matrix[8] 明明有真实数据）
```

根因：3Dmigoto 反编译常按“着色器静态可见的访问范围”写数组尺寸，而非实际绑定缓冲的大小。**绑定缓冲才是
真相。**

**修复**（`override_cbuffers_from_binary` 数组分支）：当数组是 cbuffer 的**最后一个字段**时，把加载元素数
扩展到缓冲实际能提供的数量（`avail = (len(decoded)-ri)//elem_regs`）。内部数组仍按声明尺寸（避免越入
后续字段）。→ 64 个矩阵全部加载，[8] 取到真实数据。**sekiro4 ×5 全部 0/N → 满过。**

### Bug 3 · raw-int **× 真实 float** 被当成整数位运算（→ witcher16834）

witcher 地形 VS：`r1.x = 2 << (int)r0.x`（=8，`_RawBits`），随后 `r1.x = cb5[2].z * r1.x`：

```
[BINARY OP] left=1098514432, right=8, op=* (raw i32), result=198180864
```

`1098514432 = 0x417C0000` = float `15.75`（即 cb5[2].z 的值）。应是 **float 乘**（DXBC 会先 `itof` 把
整数 8 转 8.0）：`15.75 * 8 = 126`。但 raw-bits 规则把 cb5[2].z（真实 cbuffer float）位重解释后做整数
乘 → 垃圾 → 高度采样坐标 `r2.xy` 偏（0.6567 vs 0.6646）→ 高程采样错 → SV_Position 偏 5~15。

**修复**：raw-int 操作数在 `*`/`/` 下遇到**真实 float 伙伴**（cbuffer 成员，非 `(int)` cast、非字面量）
时，是**值运算**——把 raw 操作数转成整数值走正常 float 算术，不位重解释。（`+`/`-` 保持原样以保护
witcher 噪声哈希的 iadd 位链。）→ **witcher16834 3/30 → 30/30。**

---

## 结果

**修复 8 例**（3 个通用根因，验证不破坏既有）：
- OldWorld_event1034 / event2767：203328/203328（Bug 1）
- sekiro4_event2264 / 13742 / 13806 / 14228 / 16660：满过（Bug 2）
- witcher3_countryside_event16834：30/30（Bug 3）

**新增入回归清单**（155 → 164 条）：
- 无需修复即通过：Octopath-frame746_event2091、manhattan_frame_274_event1041
- 修复后通过：sekiro4 ×5（OldWorld×2、witcher16834 本就在清单里）

**全回归**：`python run_regression.py` → **162/162 passed**（三个修复验证不破坏任何既有用例）。

**未修复 2 例（诊断清楚，非通用 bug，暂不入回归以保清单全绿）：**
- `witcher3_countryside_event22229`：10/12，仅 `TexCoord4[0]` 差 0.005075（容差 0.005），且落在
  trailing-float3 golden 列错位（o5 为尾部 float3）+ 浮点边界区——非解释器逻辑错，属精度/对齐边缘。
- `EndlessSpace2_event3061`：1188/1536。VS 内多次 `t1.SampleLevel` 解包粒子数据，某些顶点（row 157+）
  `r4.xy` 坍缩为 0 → `1/sqrt(0)=inf` → NaN；GPU 侧该处 `r4.xy` 非零（golden 有限）→ 上游 packed
  数据纹理解码在这些顶点发散。数据相关、深，留待后续。

---

## 关键结论

- `_RawBits` 传播过猛会污染两类场景：**(a) 索引/偏移里的字面量**（Bug 1）、**(b) raw-int × 真实 float 的
  值缩放**（Bug 3）。判据：字面量恒为值；`*`/`/` 配真实 float 恒为值（DXBC 必先 itof）；`+`/`-` 保守留位链。
- 3Dmigoto 的 **cbuffer 尾数组声明尺寸不可信**，绑定缓冲大小才是真相（Bug 2）。
- 精度边界与 packed-纹理解码发散是两类**非通用**难例，不应污染回归全绿。
