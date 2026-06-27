# hlsl-step94：修复 C 类（sekiro 实例索引/struct-in-cbuffer）与 F 类（派生 quad 崩溃）

## 目标

修复 step93 分类文档里的两类失败：
- **C 类（53 个 sekiro）**：ByteAddressBuffer 实例索引未解析 + struct-in-cbuffer 命名成员访问缺失 + float4x3 矩阵布局错误，导致 sv_position 全错。
- **F 类（2 个崩溃）**：派生（ddx/ddy）邻居 lane 重执行时 `list * float` TypeError 崩溃。

## 诊断（关键证据）

以 `sekiro2_event2282`（C 类）与 `sekiro2_event13516`（F 类）为样本，结合 `VS_shader_disasm.txt`
反汇编逐条核对：

1. **F 类崩溃根因 = C 类同源**：崩溃语句是
   `r3.xyz = VC_InstanceData[r0.w].ShLightMask.xxx * FC_DirLightCol_0.xyz`。
   `VC_InstanceData` 是 struct-in-cbuffer 数组，但 step92 的 `__struct__` 只支持 `_mRC` 寄存器式
   访问，**不支持命名成员**（`.ShLightMask`）。于是 `VC_InstanceData[i].ShLightMask.xxx` 返回了整个
   元素（一个矩阵/嵌套 list），`矩阵 * float3` 抛 `can't multiply sequence by non-int`。

2. **ByteAddressBuffer 实例索引**：反汇编
   `ld_raw(rawbuffer) r0.x, r0.x, g_InstanceIndexBuffer` 在 HLSL 里被 3Dmigoto 标为
   "No code for instruction" 注释掉。t20 缓冲 `buffer_38386.bin` 内容是恒等表 `[0,1,2,3,…]`，
   `r0.y = r0.x*17`（struct 元素步长 17 寄存器），`cb4[r0.y+4]` = `matricesData` 成员。

3. **float4x3 行/列主序**：反汇编 `dp4 r2.x, v0, cb5[r0.x+0]` 直接用寄存器 0 与位置点乘。
   HLSL 写作 `VC_aObjMatrix[i]._m00_m10_m20_m30`。`VC_aObjMatrix` 是默认 **列主序** `float4x3`，
   3 个寄存器 = 3 列。而旧的二进制加载对 float4x3 **存原始寄存器不转置**，`_mRC` 访问器（`obj[r][c]`，
   假定寄存器=行）取到的是错误转置。float4x4 早已转置成行主序逻辑存储，float4x3 被遗漏。

## 修复内容

### 修复 1 — struct-in-cbuffer 命名成员访问（解决 F 类 + C 类前置）

- `parse_cbuffer._consume_struct` 现在按 HLSL cbuffer packing 规则计算每个成员的布局
  `(name, type, reg_off, comp_off, arr_size)`，存入 `FieldDefinition.struct_members`。
  （sekiro `cbInstanceData`：matricesData@reg4、ShLightMask@reg10、元素步长 17 寄存器，均与反汇编吻合。）
- 二进制加载额外保存每个元素的整数寄存器行 `struct_int_data`，供 `uint4 matricesData` 等整数成员
  取精确位（用于索引）。
- 新增 `_struct_member_access(field, elem_idx, rest)`：解析 `NAME[i].member[.swizzle]` /
  `NAME[i].member[k]`，按成员布局从浮点或整数寄存器行切片；遇 `_mRC`（非命名成员）则回退到原有
  矩阵访问器。
- 索引兼容两种约定：3Dmigoto 有时把下标预乘元素步长（`cb4[idx*17]`），故越界索引按
  `idx // elem_regs` 回退到元素号（element 0 仍正确）。

### 修复 2 — ByteAddressBuffer 原始加载（C 类）

- 解析 `ByteAddressBuffer NAME : register(tN);` → `self.byte_address_buffers`。
- `load_typed_buffer_data` 复用 `buffer_params.csv` 一并加载其原始字节。
- `_byte_address_load(bab, byte_addr, ncomp)` 按字节地址读 uint32。
- `execute_statement` 识别被注释的裸指令
  `ld_raw[_indexable](...) DST, ADDR, tN.xxxx`，执行 `DST = bufferN.Load(ADDR)`；该行无 `;`，
  语句切分会把下一条粘上来，故执行完原始加载后再执行粘连的尾部语句。

### 修复 3 — float4x3 列主序转置（C 类）

二进制加载（数组分支与非数组分支）对 `float4x3` 把 3 个列寄存器转置成行主序逻辑 `4 行 × 3 列`，
与 float4x4 的处理一致。访问器 `_mRC` 与 `mul()` 因此对所有矩阵统一按行主序逻辑读取，TombRaider
（行主序 float4x4，struct 内存原始寄存器）与 Frame-frame9222（float4x4 数组）不受影响。

### 修复 4 — 二元运算防御性护栏（F 类兜底）

`execute_binary_op` 对 `+ - * /`：当某操作数是嵌套 list（矩阵出现在期望向量处）时，塌缩到首行，
退化为有限的错误值而非抛异常中断整条 draw。

## 结果

- **F 类**：`sekiro2_event13516`、`sekiro4_event20560` 不再崩溃（命名成员访问返回正确类型）。
- **C 类**：`sekiro2_event2282` 的 sv_position 等输出 **3/3 通过**（ByteAddressBuffer + struct
  命名成员 + float4x3 转置三者合力）。
- 回归：117/117 保持通过（float4x3 转置只改 float4x3，未触及 float4x4/访问器，TombRaider/
  Frame/witcher 不变）。

（更详细的逐 case sekiro 通过数见提交说明与回归日志。）
