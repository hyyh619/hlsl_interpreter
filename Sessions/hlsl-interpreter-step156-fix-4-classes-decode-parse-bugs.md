# Step 156 — 修可修的 4 类（D/E/F1/F2）：落地 2 个 decode/parse bug，触到结构性墙

## 任务

> 继续修复仍需写代码修的 4 类 (~31 cases): D(9), E(10), F1(2), F2(10)。

step 155 把 4 类标为「仍需写代码修」。本步动手，结论分两层：
**落地 2 个真实的 decode/parse 层 bug（零回归）**；但 4 类的 **PASS 阻塞点几乎都落在结构性问题上**
（矩阵主序反编译、golden CSV 列错位、R16/R32 格式歧义），与 B/C/E 同墙。

## 调查方法

并行起两个 subagent 深挖 D（Octopath 9 例）与 F2（混合 10 例）的根因；本人同时查 E（Texture2DArray，
step 154 已知根因）与 F2-Nobu586。F2 agent 中途 API 断线（ECONNRESET）未回结果，F2 改为本人手查；D agent
回了高质量报告。

## 落地的修复（均零回归，回归 125/125）

### 修复 1 — cbuffer 矩阵主序限定符解析 bug（`parse_cbuffer`, 约 990 行）

`row_major float4x4 mW2P : packoffset(c0)` 被 `line.split()` 切成 `type='row_major'` / `name='float4x4'`
（限定符没被剥掉，和 `_consume_struct` 在 951 行的处理不一致）。后果：**每个 `row_major`/`column_major`
声明的 cbuffer 矩阵字段都被错误命名 → CSV 与二进制都加载不到它**（Nobu586 的 `mW2P`/`mW2Pt`/`mW2S`
全 `<not loaded>`，VS 输出全 0）。

**修复**：split 前先 `re.sub(r'\b(row_major|column_major)\s+','',line)`。修后矩阵正确命名并由
`override_cbuffers_from_binary` 加载（Nobu586 输出从全 0 变为真实数值）。

### 修复 2 — R16G16B16A16_FLOAT typed buffer 解码（`_typed_buffer_load`, 约 3538 行）

D agent 定位：`Buffer<float4>` 且 `ElementByteSize=8`（`esize//comp==2`）时旧代码落到 float32 分支只读
2 个 float32，把 4 个 half 读成垃圾（Octopath 的逐骨骼矩阵行 / packed 法线）。

**关键陷阱（第一版修复回归了 4 个 PASS 用例）**：8 字节元素 **R16G16B16A16_FLOAT(4 half)** 与
**R32G32_FLOAT(2 float)** 从字节无法仅靠「half 解码全有限」区分——event1828 的 R32G32 值 `(0.4375,0.2031)`
恰好也能解成有限 half `(0,1.719,0,1.578)`，被误判 → 回归 121/125。
**双信号消歧**（`_infer_halfN_typed_buffer`）：仅当 (a) 全元素 half 解码有限且 `|v|<1024`
**且** (b) 同样字节按 float32 解出**denormal**（~1e-41，证明 float32 视图是垃圾）时才判 half；否则默认
float32（保持旧行为，绝不打扰合法 float32 buffer）。8 个样本（4 个要 float32 / 4 个要 half）完美分离：

| 用例 | half_bad | f32_denorm | 判定 |
|------|----------|-----------|------|
| 1320/1828/1031/283（要 float32）| 混合 | **False** | float32 ✓ |
| 3012/2912/664/2135（要 half）| False | **True** | half ✓ |

修后 event3012 由 0/51 → **30/51**；event2912/664/2135 的 buffer 值**已正确**（见下被 golden 错位挡住）。

## 触到的结构性墙（PASS 阻塞，非简单 bug）

### D（Octopath）— golden CSV 多 float3 列错位
half 修复后 event2135 的值已对：解码 element=`(-0.6611,-0.7188,-0.2131)`，|·| 正好等于 golden 基向量
`(0.66142,0.71906,0.21323)`。但比较仍错——golden CSV 本身整体错位：`TEXCOORD10` 列装着 SV_POSITION 数据
`[-1966,6752,10,7609]`，`SV_POSITION` 列是 `[1,0,0,0]`（明显损坏），基向量被挤到后面的列且**逐分量循环
移位**（gotcha #1 的多 float3 版）。loader 只补偿了单个 trailing float3。golden 尾部含「下一顶点垃圾」，
很可能**不可恢复** → 即使值对也无法 PASS。

### F2（Nobu586）— 矩阵主序反编译问题（非 cbuffer 加载）
修复 1 让矩阵加载后，发现真正阻塞是主序：golden 需 `clip=mul(mW2P,world)`（每行点乘 world，验证
`dot(row0,world)=-19.97 ✓`、`dot(row3,world)=161.44 ✓` 命中 golden），但反编译 HLSL 的
`world.x*_m00.. + world.y*_m10.. + ...` 实为 `mul(world,M)`=转置。这是 C/E 同款主序问题，**超出
「解释 HLSL 源」边界**。

### E（witcher）— Texture2DArray 采样缺口（step 154 已定位）
o2/o3 依赖 `t4.SampleLevel(s, r3.xyz, 0).xy`（`r3.z`=array slice）。两个 loader 都 `if arr!=0: continue`
只装 slice 0，`sample()` 也忽略 slice index → 采样返回 0。即使实现切片采样，命中 0.005 容差仍需精确
UV/filter/slice/格式，概率低；且 o2/o3 还叠加主序。未动（高成本低确定性）。

### F1（sekiro 3207/9493）— 精度容差
均 43329/45576，失败行 diff 略超 0.005。属容差/精度策略，未放宽全局容差（会掩盖真错）。

## 结果

- **2 个真实 decode/parse bug 已修，零回归（回归 125/125，exit 0）**：①cbuffer 矩阵主序限定符解析；
  ②R16G16B16A16_FLOAT typed buffer 双信号消歧解码。均为正确性改进（Nobu586 输出由全 0→真实值；
  Octopath event3012 0→30 行；event2912/664/2135 buffer 值由垃圾→正确）。
- **诚实结论**：step 155 的「可修」偏乐观——4 类的 **decode/parse 层 bug 确实可修且已修**，但 **PASS
  被结构性墙挡住**（golden CSV 多 float3 错位且尾部损坏、矩阵主序反编译、E 类纹理数组+主序、F1 精度），
  与 B/C/E 同源。本步无 Dump 用例转全 PASS，但有真实的正确性推进与一个完整的根因地图。
- 未改 golden loader / 未碰主序重建 / 未放宽容差——这些是高风险且很可能不可恢复（golden 尾部垃圾）的
  改动，不在「修可修 bug」范围内贸然动它会危及 125 基线。

## 后续（若要继续推 PASS）
1. D：实现 golden CSV「N 个连续 float3 输出」的列重映射（中等难度，且需先确认 golden 尾部是否可恢复——
   event2135 的 SV_POSITION 列已是 `[1,0,0,0]` 损坏，恐不可恢复）。
2. F2/C/E：矩阵主序——需要 DXBC 反汇编重建（step 154 的 `dxbc_diff` 寄存器级 golden 是正路），超出 HLSL 源。
3. E：Texture2DArray 切片采样（两 loader 去掉 `arr!=0` 跳过 + `sample()` 收 slice index）。
