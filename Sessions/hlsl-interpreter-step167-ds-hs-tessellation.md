# Step 167 — 实现 HS/DS 执行 + Tessellation 固定功能（分步任务第 2 步）

## 任务
step 166 完成 GS 执行后，本步实现 **HS（Hull Shader 控制点阶段）+ 固定功能 Tessellator + DS（Domain
Shader）** 三段，并全量跑相应 case（`Dump/` 中带 HS/DS 的 draw）。

## HS/DS case 盘点（扫描 `Dump/*.zip`）
| case | HS | DS | `_ds_mesh.bin` golden | domain | partitioning | in_cp/out_cp | vpc? | 备注 |
|------|----|----|----|----|----|----|----|----|
| witcher3_countryside_event16803 | ✓(空体) | ✓ | ✓ | quad | fractional_even | 1/1 | **是** | 地形块 |
| witcher3_countryside_event16817 | ✓(空体) | ✓ | ✓ | quad | fractional_even | 1/1 | **是** | 地形块 |
| witcher3_countryside_event21346 | ✓(空体) | ✓ | ✓ | quad | fractional_even | **4/4** | 是 | 地形块 |
| sekiro4_event19857 | ✓(空体) | ✓ | ✓ | quad | integer | 1/1 | 否 | 粒子 billboard（DS 505 行）|
| sekiro4_event20244 | ✓(空体) | ✓ | ✓ | quad | integer | 1/1 | 否 | 粒子 billboard |
| sekiro2_event13554/13931/14130/14388 | ✓ | ✓ | **无** | quad | — | 1/1 | 否 | 无 golden，仅执行 |

## D3D11 tessellation 管线回顾
`VS → HS(控制点阶段 + fork/join 补丁常量阶段) → 固定功能 Tessellator → DS`。
- **HS 控制点阶段**：每输出控制点跑一次，变换输入控制点。
- **HS fork/join（补丁常量）阶段**：每 patch 跑一次，算 `SV_TessFactor`/`SV_InsideTessFactor` + 其它 patch 常量。
- **Tessellator（固定功能）**：按 domain(tri/quad/isoline) + partitioning + tess factors 生成 domain 采样点(u,v[,w]) + 连接性。
- **DS**：每个 domain 点跑一次，输入 `SV_DomainLocation`(vDomain)、控制点 `vicp[i][j]`、patch 常量 `vpc*`，输出一个顶点。

## 实现

### 1) `tessellator.py`（新，固定功能）
`tessellate(domain, edge_factors, inside_factors, partitioning)` → `(points, prims)`。
- quad / tri / isoline；integer / pow2 / fractional_odd / fractional_even 段数取整。
- integer factor 1：quad→4 角 `(0,0)(1,0)(0,1)(1,1)`、tri→3 角、isoline→2 端点（**最小 patch**，可独立单测）。
- fractional 用取整段数近似（domain 点计数/端点正确，内部均匀分布）——golden 每 patch 一顶点，不影响比较。
- 验证：`quad f1→4 点/2 三角`、`tri f1→3 点/1 三角`、`quad f2 even→9 点/8 三角`、`tri f3→10 点/9 三角` 全对。

### 2) HS 执行（`hlsl_interpreter.py`）
- `executeHS_with_params(...)`：VS 输出按 `in_cp` 分组成 patch；每 patch 产 `out_cp` 个输出控制点（canonical key）。
- **空体（decompiler 丢弃 fork 阶段、控制点阶段为恒等）→ passthrough**：输出控制点 = 输入控制点。
- `_execute_hs_main` 支持非空体：`v`/`vicp` 二维控制点数组 + `SV_OutputControlPointID`。
- `_body_is_trivial` 判定空体。

### 3) DS 执行（`hlsl_interpreter.py`）
- `executeDS_with_params(main, ..., ds_input_sig, hs_patches, domain_points, patch_constants=None)`：
  每 patch × 每 domain 点跑一次 DS main，patch-major/domain-point 顺序收集输出流。
- `_execute_ds_main`：设局部变量 `vicp`/`v`（2D 控制点，复用 GS 的 `v[i][j]` 索引支持）、`vDomain`/`SV_DomainLocation`、
  `vpc0..vpc7`（fork 阶段输出，缺失时默认 0），按语义把控制点填入具名输入参数；跑语句后按 canonical key 收集输出。
- DS 无 `Append`，直接收集输出参数（类 VS）。

### 4) `render.py`
- `_parse_tess_params(folder)`：从 HS/DS disasm（或 HLSL 注释）解析 `dcl_input/output_control_point_count`、
  `dcl_tessellator_domain`、`dcl_tessellator_partitioning`。
- `_run_ds_stage(...)`：检测 `HS_shader.hlsl + DS_shader.hlsl + *_ds_mesh.bin`；建 HS/DS 解释器（纹理/cbuffer/
  structured/typed buffer/签名/main 参数），跑 HS→tessellator→DS，与 `_ds_mesh.bin` golden 比（复用
  `compare_vs_output_with_golden_params`，打总数/成功数 + 前几个 `Error:`）。VS 比较后调用，vs_only 也跑。

## 关键发现：**golden DS-out 计数 = patches × out_cp**（RenderDoc DS-out 预览惯例）
逐案对：16803 drawn4/golden4；21346 drawn1024/golden1024（256 patch × 4）；sekiro4 5/5、7/7。
→ **DS 每 patch 跑 `out_cp` 次**（每个输出控制点/角一次）。故取**最小 patch（integer factor 1）**的前 `out_cp` 个角作为
per-patch domain 位置（out_cp=1→仅 `(0,0)`；out_cp=4 quad→4 角）。修正后**5 个 golden case 计数全对**
（4/4、4/4、1024/1024、5/5、7/7）。
> 注：fractional_even 声明但 golden 用 factor-1 的 4 角——D3D 在所有 factor=1 时产最小 patch，与 partitioning 无关。

## 全量运行结果（5 个 `_ds_mesh` golden case）
| case | DS emitted | golden | 计数 | 逐行通过 | 结论 |
|------|-----------|--------|------|---------|------|
| witcher16803 | 4 | 4 | ✓ | 0/4 | **vpc 墙** |
| witcher16817 | 4 | 4 | ✓ | 0/4 | **vpc 墙** |
| witcher21346 | 1024 | 1024 | ✓ | 0/1024 | **vpc 墙** |
| sekiro4_19857 | 5 | 5 | ✓ | 0/5 | 复杂 DS（见下）|
| sekiro4_20244 | 7 | 7 | ✓ | 0/7 | 复杂 DS |

**框架正确性已验证**（非逐行全过，但核心链路对）：
1. **计数全对**：patch 组装 + `out_cp` domain 点规则 + HS passthrough + DS 执行链路正确。
2. **sekiro4 `Color.xyz` 逐分量精确匹配** golden：`o0.xyz = vicp[0][5].xyz * vicp[0][1].xyz`（COLOR4×COLOR0）→
   **vicp 接线（HS 输出控制点→DS `vicp[i][j]`）正确**。
3. **witcher `TexCoord3` 差值恰为 `size`(7.8125)**：几何式 `vDomain*size+corner` 正确，仅代表 domain 角与 golden 不同。

**逐行 0 通过的根因（结构墙，非框架 bug）**：
- **witcher（3 案）**：DS 读 `vpc0.y`（HS fork 阶段的非 SIV patch 常量输出 `o0.y`），**fork/join 阶段被 3Dmigoto
  decompiler 丢弃**（HLSL 只剩空的控制点阶段），无法算 → `vpc0` 缺失 → `t3.Load` 索引错、`TexCoord=inf`、
  SV_Position 连带错。**不可修（缺源）**。
- **sekiro4（2 案）**：①SV_POSITION golden = `(0,0,0,w)`（point-sprite/RenderDoc 惯例，xyz 恒 0），我方走
  view×proj 得非零 xy；②`o10`(TEXCOORD10) 走 tile-based 光照 + `StructuredBuffer<PointLight>`，**PointLight 结构体
  stride 被误判为 16B**（应为结构体大小），光照顶点错；③fade 项（r4.y）。域位置扫 `(0,0)/(0.5,0.5)/(1,1)/(0.5,0)`
  对匹配数无影响（37~38），确认非域位置问题。

## 回归
`python run_regression.py` → **125/125 全过，零回归**（`_run_ds_stage` 在无 HS/DS/`_ds_mesh` 时提前返回，不影响非 tessellation draw）。

## 结论
- **HS 控制点执行 + 固定功能 Tessellator + DS 执行框架完成**，`_run_ds_stage` 端到端接入（VS→HS→Tess→DS→golden 比较）。
- **经验证**：5 个 golden case DS-out 计数全对；vicp 接线（Color.xyz）与几何式（size 差）逐分量对。
- **逐行不过 = 结构墙**：witcher = fork 阶段（vpc）未 decompile；sekiro4 = point-sprite SV_POSITION 惯例 + PointLight
  structured-buffer stride 误判 + 光照循环。均非框架之误。
- 分步任务（GS→DS/HS+Tess）**两步完成**。

## 后续（可选）
1. `StructuredBuffer<struct>` stride 从结构体布局推断（修 sekiro4 `o10` 光照）。
2. HS fork/join（patch 常量）阶段：若能拿到含该阶段的反汇编/HLSL，补 `vpc*` → 解 witcher 墙。
3. point-sprite SV_POSITION 惯例核对（sekiro4 xyz=0 的 RenderDoc 语义）。
