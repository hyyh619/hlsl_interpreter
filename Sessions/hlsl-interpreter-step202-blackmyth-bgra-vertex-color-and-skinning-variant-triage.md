# Step 202 — BlackMyth 收尾：B8G8R8A8 顶点色 R/B 通道交换（修复），骨骼/morph 解码变体（定位未修）

Date: 2026-07-07

## 任务

继续修 step201 遗留的、**不同根因**的 BlackMyth 失败：
- `event8040`：`COLOR1` 的 `.x`/`.z` 互换；
- `event9319 / 3256 / 9526 / 9829 / 15293_8040`：更深的解码变体，`TEXCOORD11`/`TEXCOORD7` 仍错。

## 根因（已修复）：B8G8R8A8_UNORM 顶点属性未做 R/B 通道交换

`event8040`（`frame19470`，6 顶点）是一个简单的 sRGB 顶点色解码 shader，不是压缩解码那支。
错误特征极干净：`COLOR1.x` 与 `COLOR1.z` **对调**（output[0]=sRGB(0.0)、golden[0]=sRGB(0.694)；
[2] 反之）。

输入布局：`ATTRIBUTE4 : B8G8R8A8_UNORM`。`0.694` 是 `ia_vertex_data.csv` 里 `ATTRIBUTE4.z`，
而 golden 要求 shader 的 `v4.x = 0.694`。即 **B8G8R8A8 在内存里按 B,G,R,A 排布，但输入装配阶段
交给 shader 时是 `.x=R, .y=G, .z=B, .w=A`**——需要把 R 和 B（分量 0 与 2）对调。
`_decode_vertex_element` 对 `UNORM/comp_byte_width==1` 直接 `unpack '<4B'` 得到内存序 `[B,G,R,A]`，
**漏了这次交换**。（同一 shader 的 `ATTRIBUTE3` 也是 B8G8R8A8，但数据恰好全 `1.0`，交换不可见，
所以 `COLOR0` 一直"碰巧对"，只有非对称的 `ATTRIBUTE4` 暴露 bug。）

**修复**（`hlsl_interpreter.py` `_decode_vertex_element`）：解码后，若格式以 `B8G8R8` 开头且
≥3 分量，交换 `vals[0]` 与 `vals[2]`。

**验证**：`event8040` `0/6 → 6/6`（走 binary VB 路径，命中此修复）。修复通用（任何 B8G8R8A8/
B8G8R8X8 顶点属性），非 BlackMyth 专用。

## 定位但未修：骨骼/morph 解码变体（`event9319` 家族）

`event9319/3256/9526/9829/15293_8040` 是与 step201 修好的 `event7117` **不同的、更复杂的**
顶点解码 shader：多了骨骼/morph 混合（`mad((int3)r0.zzz, int3(40,40,40), int3(1,3,7))` 逐 lane
算索引、从 `t0` 读多行矩阵），输出 `TEXCOORD10/11/7`。

逐语句轨迹（row 0）结论：
- **`TEXCOORD10`（法线，o0）正确**；`TEXCOORD11`（切线，o1）与 `TEXCOORD7`（世界坐标，p6，
  偏 ~6.5%）错。
- 关键观察：`o0 = r15.zzz*r18 + r15.xyw`，而本例 `r15.x=r15.y≈0` → `o0` 实际只等于
  `r15.z * r18`（基向量**第 2 列**）。也就是说 **o0 只校验了基第 2 列 `r6`/`r18` 与 `r15.z`，
  并不约束基第 0/1 列 `r4/r5`（`r17`/`r2.xyw`）**。
- `o1 = r16.x*r17 + r16.y*r2.xyw + r16.z*r18`，重度依赖 `r17`/`r2.xyw`（基第 0/1 列）。
  确认 `r2.xyz` 在 tangent 算出后到 `o1.xyz=r2.xyz` 之间**没有被覆盖**（只 `r2.w` 被改），
  即解释器数据流无误——**切线之所以错，是基第 0/1 列与真机不符**。

矛盾点：**同一段基解码代码在 step201 的 `event7117` 上整例通过**（含切线），说明解码逻辑本身对；
本例第 0/1 列却错、第 2 列对。差异只可能来自**输入数据装载**（本例基数据来自 `t1`，`event7117`
来自 `t2`）或某条**数据相关的罕见分支**。在没有逐语句 golden 的情况下无法进一步确证，且贸然改动
风险高（易砸 `event7117` 等已通过例），故本轮**不改**，如实记录待后续。

## 回归

`python run_regression.py` 全量 **152/155 PASS**（155 = step198 的 154 + step201 新增的
`event7117`）。3 个未过均为与本改动无关的既有失败：`witcher3_countryside_event16834`（3/30）、
`OldWorld_event1034` / `event2767`（0/203328，改前即 0/N）。其余全绿（含所有用到 B8G8R8A8
顶点属性的例）→ **R/B 交换零回归**。已把 `event8040` 也加入 `Cases/regression_test_zip_files.csv`。

## 结论
- 修好 `event8040`（B8G8R8A8 顶点色 R/B 交换）——通用正确性修复，已加入回归。
- `event9319` 骨骼/morph 变体定位到"基第 0/1 列与真机不符"，但根因未确证、未修，留作后续。
诚实记录：本轮修 1 个、留 5 个（不同根因）。
