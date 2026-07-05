# Step 181 — HS fork/join 相位 VM + sekiro4 双案转正

## 任务

类 C：sekiro4_19857/20244（SB stride + point-sprite 惯例）与
witcher 16803/16817/21346（HS fork/join 相位被反编译丢弃）。

## sekiro4_19857/20244：5/5、7/7 全 PASS

排查推翻了步 167 的两个旧判断：
1. **SB stride 已无问题**——步 169 的 `_captured_sb_strides`（ElementByteSize
   覆盖）已在 DS 路径生效（g_LightBuffer 正确装载 208B）。
2. **golden 不是 DS 域求值**：逐列比对发现每个变化列都等于**控制点属性**
   （COLOR.w=v1.w、TEXCOORD10.y=v11.w、SV_POSITION=(0,0,0,v0.w)），且流为
   **控制点扁平 float 按 DS 输出布局重切片**（golden TEXCOORD8 跨
   v10.y,v11.x,v11.y——v10 是 float2）。

**实现（render.py `_run_ds_stage`）**：`in_cp==out_cp==1` 的点精灵 patch 走
CP 直通比较——按 HS 签名 slot 分组拼接（多语义共享 slot：witcher 的
CORNER.xy+SIZE.z+CM_LEVEL.w），**宽度取自 HS disasm 的 dcl_input 掩码**
（反编译 HLSL 全部填成 float4：`v[1][9].xyz`→3、`v[1][10].xy`→2），
UInt golden 列按原始位比较。

## HS fork/join 相位执行基建（DXBC VM 扩展 + render 接线）

按步 175 路线："disasm 里指令还在，由 VM 生成 vpc* 喂给 DS"。

**dxbc_interp.py**：
- `dcl_immediateConstantBuffer` 解析（icb 表 + `icb[expr]` 操作数）；
- 动态输出目标 `o[r0.x + 0].x`（HS fork 的 indexRange 写法）；
- `ld/ld_indexable` 从占位 0 升级为真钩子（'ld'(slot, int_coords)，支持
  Texture2DArray 切片）；`resinfo` 新增（'resinfo'(slot, mip) 钩子）。

**render.py `_run_hs_patch_phases`**：切分 HS disasm 的 fork/join 相位
（实例数、`dcl_output_siv` 因子映射表 `_SIV_FACTOR_NAMES`）、装载 HS
cbuffer bin 行与 HS 纹理钩子、逐 patch × 逐实例运行 VM（vicp/vocp/
vForkInstanceID 输入）、收集 SV_TessFactor + vpc 常量行；
`executeDS_with_params(..., patch_constants=...)` 注入（该形参已在步 167
预留，此前恒 None → vpc*=0）。

**验证**：witcher 16803 的 fork 相位（464 条指令：dp4/div/ld_indexable/
resinfo/sqrt/movc/分支）完整执行成功——vpc 6 行、patch0 因子
edge=[2,2,2,2] inside=[2,2]（fractional_even）。DS 的 `vpc0.y` 首次拿到
真值。

## witcher 16803/16817/21346：golden 布局之谜（留档）

vpc 打通后输出仍 0/4——继续取证发现其 `_ds_mesh` golden 是**多域角点打包
的复合行**：row0 的 sv_position=(corner.x, corner.y, size, 0)（域(0,0)+
size），TexCoord=(corner.x+size, corner.y, size)（域(1,0)！），TexCoord2
是 UInt 位模式列——**单行打包了同一 patch 的多个角点求值**，与
"每行=一次 DS 求值"的既有模型不符。步 167 的"diff==size"观察即此。
需要下一步对该布局做专门取证（对照 RenderDoc 源码或逐列反推），
再决定 DS 求值序还是复合行重排。

## 全量验证

- **Cases 回归 148/148 全 PASS 零回归**（CP 直通模式、VM 扩展、vpc 接线均未
  破坏既有案例）。
- **Dump triage 25 案：sekiro4_19857（5/5）、20244（7/7）双双转 PASS**，
  其余逐案与基线持平（witcher 16803/16817 错误行 56→40 是 CP 模式尝试的
  显示变化，pass 数不变）。
- **2 案晋级**回归表（**150 案**）并从 Dump 移除（剩 **23 案**）。

## 类 C 收官状态

- sekiro4 两案消灭（点精灵 CP 流惯例破解 + disasm 宽度）。
- witcher 三案：fork/join 相位执行基建已就位（vpc 真值可供 DS），
  剩余阻塞在 `_ds_mesh` golden 的多角点复合行布局——单行打包同 patch
  多个域角点求值，需专门取证后重排比较。
