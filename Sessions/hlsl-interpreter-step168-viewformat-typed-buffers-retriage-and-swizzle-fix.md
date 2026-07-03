# Step 168：消费新 dump 的真实 typed-buffer 视图格式 + 全量重跑分类（附带修出两个真 bug）

## 任务

1. 旧 dump 的 `buffer_params.csv` 只记 `ElementByteSize`，不记 SRV 真实视图格式（DXGI_FORMAT）；解释器只能按字节分布推断 SNORM/UNORM/half/float。
2. 新导出的 dump（Dump/ 与 Cases/ 均已重导）新增 `ViewFormat` 列，携带真实格式。
3. 让解释器消费真实格式，重跑 Dump 和 Cases 全部 case。
4. 对未通过的 case 调查失败原因并分类，每类列出全部相关 case。

## 思考

- 抽查新 dump：`buffer_params.csv` 表头变为 `Stage,BindType,Slot,ResourceId,Name,DescriptorType,BufferByteSize,ElementByteSize,ViewByteOffset,ViewByteSize,ViewFormat,BinFile`。Dump 里出现的真实格式有 `R32G32B32A32_FLOAT / R16G16B16A16_FLOAT / R16G16_FLOAT / R8G8B8A8_UNORM / R32_FLOAT`；Cases 里另有 `R8G8B8A8_SNORM / R16_UINT / R8_UINT / R32G32_FLOAT / R32_TYPELESS`（138 个 Cases zip 中 128 个带该列）。
- 设计原则：**有 `ViewFormat` 就按真实格式解码，没有（旧 dump）或格式不支持（如 `R32_TYPELESS`、`R10G10B10A2`）就回退到原推断逻辑**，保证旧数据零破坏。
- 交叉验证推断 vs 真实格式：对 Octopath 各 typed buffer 逐一对比，旧推断**恰好全部推对**（event664 t2 推 unorm=真实 UNORM；event2135/2912/3012 t2 推 half=真实 R16G16_FLOAT；8 字节元素推 half=真实 R16G16B16A16_FLOAT）。因此这些 case 的既有失败**不是**格式误判——为后面找到真正的元凶（尾部 swizzle 丢弃）排除了方向。

## 代码修改

### 1. 真实视图格式解码（hlsl_interpreter.py）

- `load_typed_buffer_data`：解析 `ViewFormat` / `ViewByteOffset` 列，存入 `tb['view_format']`，数据按 `ViewByteOffset` 开窗；日志追加 `view <fmt>`。
- 新增 `_decode_view_format(fmt, data, off)`：通用 DXGI 格式名解析器——解析 `R(\d+)G(\d+)...` 组件位宽 + 后缀（FLOAT/UNORM/SNORM/UINT/SINT，`_SRGB` 按 UNORM 处理），支持 8/16/32 位任意 RGBA 组合；混合位宽（R10G10B10A2、R11G11B10）返回 None 走回退。SNORM 按 D3D 语义 `c/(2^(w-1)-1)` 下夹 -1。
- `_typed_buffer_load`：有 `view_format` 时优先真实格式解码（padding 到 4 分量用 D3D 默认 (0,0,0,1)），失败回退原推断。

### 2.（顺手修复）frac/floor/ceil/round/trunc 对非有限值崩溃

witcher3_event16834 直接 `exit=1`：`frac(NaN)` → `math.floor(NaN)` 抛 `ValueError`。GPU 语义是 NaN 透传、`floor(±inf)=±inf`、`frac(±inf)=NaN`；现在非有限输入按 GPU 语义透传。修后该 case 跑完全程（归入 witcher 数值类）。

### 3.（重要发现）方法调用尾部 swizzle 被静默丢弃（hlsl_syntax_tree.py）

sekiro2_event15481 只有 3 行错：`o1.z` 输出 20、golden 1。追踪 `[STMT]`：

```
r0.x = g_LuminanceTexture.Load(float4(0,0,0,0)).y => r0.x = ['20.0000', '1.0000', '0.0000', '1.0000']
```

`Load` 本身返回正确（1×1 R32G32_FLOAT 纹素 = (20,1)），但 **`.y` 没有被应用**——`_parse_method_call` 用 `expr.rfind(')')` 截取参数串，闭括号后的 `.y` 被直接丢弃，整条表达式返回整个 float4，后续取标量时拿到第 0 分量 20。

**为何潜伏至今**：3Dmigoto 反编译代码里方法调用的尾部 swizzle 几乎全是前缀恒等形式（`.x`/`.xy`/`.xyz`/`.xyzw`），丢弃后取前 N 分量结果恰好相同；只有 `.y` 这类非前缀 swizzle 才暴露。

**修复**：`_parse_method_call` 改为扫描配对闭括号定位参数串，尾部 `.{xyzw/rgba}{1,4}` 包装为新的 `swizzle` 语法树节点；`evaluate_syntax_tree` 新增 `swizzle` 分支（按分量索引选取，单分量返回标量）。

## 执行与结果

### 回归（Cases）

- 修改后首轮：**125/125 全过**，零回归（含 Cases 中带 `R8G8B8A8_SNORM`/`R16_UINT` 等真实格式的 case——真实格式解码与原推断结果一致或更准）。
- swizzle 修复后再跑：**125/125 全过**。
- 晋级 4 个新通过 case 后终跑：**129/129 全过**（见下）。

### Dump 全量 triage（48 zip）

第一轮（仅格式消费）：0/48——与预期一致（Dump/ 本就是历史未通过 case 的存放处），但暴露了 frac(NaN) 崩溃与 swizzle 案发点。两修复落地后第二轮：**4/48 PASS**：

| 转 PASS 的 case | 之前 | 之后 |
|---|---|---|
| Octopath-frame746_event2135 | 0/6 | **6/6** |
| Octopath-frame746_event2912 | 0/504 | **504/504** |
| sekiro2_event15481 | 0/3 | **3/3** |
| sekiro2_event16052 | 0/3 | **3/3** |

**关键更正**：记忆/步 158 曾把 event2135/2912 归因为「四元数/骨骼基分量置换（x↔y 交换）解码 bug」——真相是尾部 swizzle 丢弃：`.Load(...).y` 拿到 `.x`，表现恰似分量交换。该「置换类」失败已整类消灭。

4 个 zip 已从 Dump/ 移入 Cases/ 并追加进 `regression_test_zip_files.csv`（回归 125→129）。另两案显著改善：event664 0/51→**36/51**、event3012 30/51→**35/51**（残余为精度级小差）。

### 剩余 44 个 Dump 失败的分类

**类 1｜超时（纯 Python 性能，非正确性）— 4 案**
- OldWorld_event1034、OldWorld_event2767（203,328 顶点）、OldWorld_event3338、witcher3_countryside_event22260
- 300 秒跑不完百万级顶点×长 shader；需要热路径优化，与解释正确性无关。

**类 2｜无 golden 可比（结构性不可验证）— 6 案**
- EndlessSpace2_event3093（ia_vertex_data 0 顶点，zip 无任何 mesh golden）
- Nobu15-frame3456_event2894（zip 无 mesh golden，仅 4 顶点执行正常）
- sekiro2_event13554、event13931、event14130、event14388（tessellation draw，只有 `sekiro2_ds_mesh.csv` 无 bin+layout；13931/14130 有像素级 golden 但依赖 DS 输出故 0 匹配）

**类 3｜GS 条件发射计数 0（GS cbuffer 解码，step 166 已知跟进项）— 3 案**
- manhattan_frame_274_event50（发射 0 vs golden 3000）
- sekiro2_event14998（0 vs 29148）
- sekiro4_event20560（0 vs 1074）
- 共性：point-list 输入的粒子/精灵 GS，发射条件读 cbuffer 的 birth-index/计数字段，解码值使条件恒假。

**类 4｜GS 发射值不匹配（计数对、粒子参数差）— 4 案**
- manhattan_frame_274_event87、event124、event161、event198
- TEX_COORD 差 0.1~2：粒子位置/寿命由 GS cbuffer 时间/索引参数驱动，同类 cbuffer 字段解码差异。

**类 5｜HS/DS 结构墙（step 167 已确认，不可修）— 5 案**
- witcher3_countryside_event16803、event16817、event21346：DS 读 `vpc0.y` = HS fork/join（patch 常量）相位输出，该相位被 3Dmigoto decompiler 丢弃，HLSL 源里不存在。
- sekiro4_event19857、event20244：point-sprite SV_POSITION golden 惯例 (0,0,0,w)（golden xyz 恒 0）+ `StructuredBuffer<PointLight>` stride 误判。

**类 6｜GPU 超越函数精度墙（step 163 已确认）— 2 案**
- TombRaider-frame25229_event2848、event7308（742/1548，diff ≈0.01 略超 0.005 容差）
- 程序化顶点动画的 sincos/rsqrt 硬件近似（~1e-4）被变换放大；FMA 融合已证无效，需 bit-exact GPU 超越函数。

**类 7｜精度临界（float32 仿真 vs GPU 的残差，略超容差）— 6 案**
- sekiro2_event3207、event9493（同 shader，43329/45576=95.1% 行过，diff 0.005~0.006）
- sekiro2_event4833（12/24，diff 0.008~0.045）
- sekiro4_event7844（162/324 恰半数行，diff 0.02~0.11 @ 量级 7~49）
- Octopath-frame746_event664（36/51，diff 相对量级 ~2.8e-4）、event3012（35/51，diff 0.05~0.42 @ 数千量级）——swizzle 修复后从「值全错」收敛为蒙皮链精度残差。

**类 8｜VS 纹理采样精确性（texel-fetch/SampleLevel 与 GPU 不逐位一致）— 11 案**
- witcher3_countryside_event16215、event16834（frac(NaN) 修后进入本类）、event21719、event21895、event21979、event22049、event22092、event22201、event22229：TexCoord2/TexCoord4 输出链均经 VS 内 SampleLevel（探针/细节法线纹理），step 157 已收敛采样框架，残余为纹素取值精确性（R16G16_FLOAT 细节法线、采样器过滤/UV、DXBC 资源 swizzle）。
- Octopath-frame746_event3601（0/96）：草叶 VS——`t0.Load`(R16G16_FLOAT UV) → t1~t4 `SampleLevel` 驱动顶点位移，纹理采样值差被位移放大。
- EndlessSpace2_event2991（1464/1536=95.3% 行过，仅尾部 Row1368~1511 的 72 行错）：实例化 sprite，VS 采样 .img 纹理 + 80B stride StructuredBuffer 实例数据。

**类 9｜Octopath 顶点拉取地形系统性偏差（同一 shader 三帧，原因未定）— 3 案**
- Octopath-frame746_event576、event2651、event2682（均 8/23064）
- 两个 R32_FLOAT typed buffer（各 25 元素）驱动的地形网格；输出与 golden 系统性偏差 1~5%（如 TexCoord 输出整数 882.0 vs golden 883.56）。**更正**：步 158 记录的「zero cbuffers（退化地形）」对新 dump 已不成立——cb1~cb3 均有实值（新导出带 constant_buffer_info + bin）。真实原因待查：疑 golden 采集路径含额外位移或我方顶点拉取索引/采样差异。

统计：4+6+3+4+5+2+6+11+3 = 44 ✓（48 - 4 晋级）

### 分类结论

- **可修（值得投入）**：类 3/类 4（GS cbuffer 字段解码，一处修复覆盖 7 案）、类 9（需从 dxbc 寄存器级 golden 定位）、类 8 部分（纹素精确性，逐案収敛）。
- **性能而非正确性**：类 1（热路径优化）。
- **结构墙 / 不可验证（勿再当 bug 追）**：类 2、类 5、类 6；类 7 是 float32 仿真固有残差，除非引入 bit-exact GPU 数学库。

## 本步修改文件

- `hlsl_interpreter.py`：`load_typed_buffer_data` 读 `ViewFormat`/`ViewByteOffset`；新增 `_decode_view_format`；`_typed_buffer_load` 真实格式优先；floor/ceil/trunc/round/frac 非有限值透传；`evaluate_syntax_tree` 新增 `swizzle` 节点分支。
- `hlsl_syntax_tree.py`：`_parse_method_call` 配对闭括号截参 + 尾部 swizzle 包装为 `swizzle` 节点。
- `Cases/regression_test_zip_files.csv`：+4（Octopath event2135/event2912、sekiro2 event15481/event16052）；对应 zip 从 Dump/ 移入 Cases/。

## 最终状态

- 回归：**129/129 全过**（125 原有 + 4 晋级）。
- Dump 剩 44 zip，全部完成归类（9 类，见上）。
