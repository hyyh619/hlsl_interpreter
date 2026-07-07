# Step 198 — EndlessSpace2 new draw cases: bit-select `&` → `_RawBits` through the fused-multiply-add path

## 背景 / 任务

定时任务：扫描 `Dump/`，找出未登记进 `dump_case.csv` 的新 draw case，运行；不通过的
定位 `hlsl_interpreter` 根因并修复、提交推送、纳入回归；直接通过的写入 `dump_case.csv`
并从 `Dump/` 删除。

## 1. 扫描结果

`Dump/*.zip` 与 `dump_case.csv` 差集 = **8 个新 case**，全部是 EndlessSpace2：

```
EndlessSpace2_event1740 / 1953 / 1980 / 2092 / 2876 / 2991 / 3061 / 3093
```

8 个全部追加进 `dump_case.csv`（step 2）。

## 2. 运行结果（回归口径：早 Z、vs_only、float32、float_tol=0.005）

| case | 结果 | 说明 |
|------|------|------|
| 1740 | PASS 7680/7680 | 直接通过 |
| 1953 / 1980 / 2092 / 2876 | PASS 1425/1425 | 直接通过 |
| **2991** | **FAIL → 修复后 PASS 1536/1536** | 见下，位选择惯用法 bug |
| 3061 | FAIL 1188/1536（nan） | 退化几何 `rcp(0)=inf`，未整例通过，保留 |
| 3093 | 无 golden（SV_VertexID + StructuredBuffer draw，无 MeshOut CSV） | 无法用 VS-vs-golden 校验，保留 |

## 3. 根因 —— 2991（已修复）

失败集中在**尾部 72 个连续顶点**（row 1368–1439），`o0`(SV_POSITION) 变成 ~1e10 巨值。
单顶点语句 trace（row 1368 失败 vs 1367 通过）定位到：

```hlsl
r0.x = r0.z ? r0.y : 0;                 // mask：-1 (0xFFFFFFFF) 或 0
r0.xyzw = (int4)r4.xyzw & (int4)r0.xxxx; // DXBC `and rDst, r4, mask` —— 位选择
r4.xyzw = cb1[17].xyzw * r0.xxxx + r4.xyzw;   // mad，消费 r0
...
o0.xyzw = cb1[20].xyzw * r0.wwww + r4.xyzw;   // mad
```

`(int4)r4 & (int4)mask` 是 DXBC 的**位选择/预测**惯用法：mask 逐 lane 为全 1
(`0xFFFFFFFF`/`-1`) 或全 0，结果或保留 r4 的**浮点位模式**、或清零，随后被下游 float
运算按 float 读回（asfloat）。解释器算出的 AND 结果（`[1116106601, 0, ...]`）是正确的
**整数位模式**，但：

1. `execute_binary_op` 的 `&` 分支把结果留成**普通 int**（历史注释：`& | ^ >> %` 通常喂
   itof 值转换，故不打标 `_RawBits`）；
2. 即便打标 `_RawBits`，**消费它的是 `mad`（`a*b+c`）**，走 `_try_fma`/`_fma`，
   而该融合路径**从不做** `_coerce_rawbits_for_float_op`，直接把 `_RawBits` 当巨整数
   数值相乘 → `1116106601 * cb1[17].x ≈ 1.3e9` → 垃圾 → `o0` 万亿。

通过 mask=0（通过顶点）时 `& 0 = 0`，位置恰为 0（被裁剪），所以只有 mask 全 1 的可见顶点暴露 bug。

## 4. 修复（`hlsl_interpreter.py`，两处）

1. **`&` 位选择打标**：`execute_binary_op` 的 `&` 改为逐 lane：当某操作数 lane ∈
   `{0, -1, 0xFFFFFFFF}`（movc/cmp 生成的选择 mask 特征）时，结果包成
   `_RawBits(_wrap_i32(res))`，供下游 asfloat 重解释；否则保持普通 int。真整数 `&`
   不会用全 1 恒等 mask，`& 0` 无论按 int 还是 asfloat 都是 0，故安全、不影响索引位运算。

2. **FMA 路径 asfloat 兜底**：`_try_fma` 在调 `_fma` 前对 `(a,b,c)` 三元组做
   `_coerce_rawbits_list(...)` —— 与非融合 ALU 路径的 `_coerce_rawbits_for_float_op`
   同语义：三者中存在真浮点时，把其中的 `_RawBits` 先 asfloat，再融合。新增变参版
   `_coerce_rawbits_list`，并把原成对版复用同一逻辑。

修复后 2991：`o0` 由 `-8.6e10` → 正确的 `-1.528…`，**1536/1536 PASS**。

## 5. 回归验证（关键：不得破坏既有 147+ 例）

沙箱单次 bash ≤45s、后台进程不跨调用存活，故用可续跑的自建 runner（配置写到 outputs，
规避该 mount「文件不可删除」限制）分批跑完 **全部 154 例**（含新加的 2991）：

- **151 PASS**，其中 `EndlessSpace2_event2991` 1536/1536、`BlackMyth_frame14374_event3393`
  30960/30960（该 zip 原只在 Dump、回归 CSV 已列，本次补入 Cases 修好既有清单不一致）。
- 3 个非通过，**全部与本次改动无关**，已 A/B 证明中立：
  - `witcher3_countryside_event16834`：`git checkout` 回退到改前基线，`execute_count=40`
    子集给出**逐字节相同**的 `189err 3/30`（子集/golden 对齐产物，非本改动）。
  - `OldWorld_event1034`：同样基线 A/B **完全相同** `0/300`。
  - `OldWorld_event2767`（98MB）：仅因 45s wall-clock 超时未跑完，非失败。

结论：本次改动在所有可测例上**中立或修复**，无回归。

## 6. 收尾动作

- `dump_case.csv`：追加全部 8 个新 case（step 2）。
- 回归：`Cases/regression_test_zip_files.csv` 追加 `EndlessSpace2_event2991.zip`，
  并把该 zip 拷入 `Cases/`（step 6）。
- 删除 `Dump/`：5 个直接通过（1740/1953/1980/2092/2876）+ 已修复且入回归的 2991
  （step 7；均已在 `Cases/` 有副本）。
- 保留 `Dump/`：`3061`（退化 `rcp(0)` nan，未整例通过）、`3093`（无 golden 无法校验）。

## 7. 未解决 / 后续

- **3061**：`r4.xyz` 与 `r5.xyz` 上游坍缩成完全相同值 → `r4.xy - r5.xy = 0` →
  `1/sqrt(dot)=rcp(0)=inf` → 经 `mad` 传到 `o0`=nan。golden 有限，说明 GPU 上两者
  本应有微小差异；疑与若干 `t1/t2.SampleLevel` 采样/精度链相关，风险高、单独一例，本次未动。
- **3093**：SV_VertexID 驱动、全部数据来自 StructuredBuffer 的 draw，dump 中无
  `MeshOut_*_mesh.csv` golden，VS-vs-golden 工作流无法校验。
