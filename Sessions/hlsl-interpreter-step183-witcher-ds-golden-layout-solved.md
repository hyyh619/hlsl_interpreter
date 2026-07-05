# Step 183 — witcher HS/DS golden 复合布局破案：16803/16817 全过、21346 至 855/1024

## 任务

调查修复 witcher 16803/16817/21346 的 `_ds_mesh` golden"多角点复合行"布局。

## 布局取证（决定性）

直接读 `_ds_mesh.bin` 原始浮点：

1. **16803/16817（stride 60B，4 行）**：数据是 **4-float 记录流**
   （corner.x, corner.y, size, cm_level），且**行间滑动窗口重叠**——
   row i = 底层流字节 [i·16, i·16+60)（`rows[i][4:] == rows[i+1][:11]`
   逐位成立）。即写入器用 DS 输出布局的 60B stride 拷贝 16B 记录，
   每行**越读**了后续 2.75 条记录。"多角点复合行"实为越读伪影。
2. **21346（stride 76B，1024 行）**：同型缺陷，记录 13 floats
   （TEXCOORD0 4 + TEXCOORD1 **1** + TEXCOORD2 4 + TEXCOORD3 4），
   行读 19 floats 越读 6。
3. **golden 的本体 = VS 输出流（HS 输入控制点）**，各属性宽度 =
   **VS disasm 的 `dcl_output` 掩码**（o1 在 HLSL 声明 int4 但
   dcl_output o1.x → 宽 1；其 int −1 的位模式即 golden 中的 NaN）。
   21346 的 HS CP 相位有槽位重映射（o1←v[cpid][2]），但对 golden 无影响
   ——dump 存的就是 VS 输出。

## 修复（render.py `_run_ds_stage` CP-stream 分支 + hlsl_interpreter.py）

1. **比较截断**：每行只比较前 cp_width 浮点（cp_width = VS dcl_output
   掩码宽度之和），忽略越读尾巴；sekiro4（无越读）行为不变。
2. **门槛泛化**：`in_cp == out_cp`（不再限 1）且 golden 行数 =
   patches × out_cp——21346（4×4，256 patch）纳入。
3. **键与宽度来源**：HS 输入签名语义（=VS 输出语义，与 CP 字典键一致）；
   宽度从 VS disasm 的 dcl_output 掩码**并集**（o0.xy+o0.z+o0.w → 4），
   共享 slot 的多语义按**各自掩码 lane 放置**（CORNER@.xy、SIZE@.z、
   CM_LEVEL@.w）。
4. **`_dedupe_duplicate_out_params`（重要通用修复）**：3Dmigoto 把共享
   slot 的额外输出全部命名 `pN`——16803 的 SIZE 与 CM_LEVEL **同名 p0**，
   第二次 `p0.x=` 赋值覆盖第一次，SIZE 值静默丢失（DS 正常路径同样受害）。
   preprocess 阶段按程序序把重复声明改名 `p0__dupK` 并重定向对应赋值
   （赋值数与声明数一致才动，防误伤）。
5. dxbc_interp.py：动态二维输入 `v[r0.x + 0][2]`（HS CP 相位形态，本步
   实验用，保留为 VM 能力）。

## 结果

| 案例 | 修复前 | 修复后 |
|---|---|---|
| 16803 | 0/4 | **PASS 4/4**（完整 4 浮点比较） |
| 16817 | 0/4 | **PASS 4/4** |
| 21346 | 0/1024 | **855/1024** |
| sekiro4 19857/20244 | PASS | PASS（回归保持） |

**注**：步 181 时 16803 曾短暂显示 4/4，实为旧宽度取 max=2 只比较了
corner.xy 的弱通过；本步修复后是完整记录比较。

## 21346 余 169 行的定性：又是帧级动态资源

全部残差集中在 cp[4]（TEXCOORD1.x = clipmap 页索引）：我们 1 vs golden 0。
trace 证实顶点 (131.25, −318.75) 确实在 dump 的 t17 页表 page0 矩形
(257.8, −179.7)–(757.8, 320.3) **之外**——我们的包含判定正确；golden 选
page0 意味着 **GPU 当时的 t17 页表内容不同**（clipmap 页表随相机逐帧更新）。
残差归入"帧级动态资源捕获时机"家族（t22/cb12/t1/t17 同族第四证据）。

## 全量验证

- **Cases 回归 150/150 全 PASS 零回归**（p0 去重是解释器级改动，未伤任何既有案例）。
- **Dump triage 23 案：16803（4/4）、16817（4/4）双双转 PASS**，21346 至 855/1024，
  其余逐案与基线持平。
- **2 案晋级**回归表（**152 案**）并从 Dump 移除（剩 **21 案**）。

## HS/DS 类收官

- witcher HS/DS golden 布局之谜**彻底破案**（写入器滑窗越读 + golden=VS 输出流）。
- 16803/16817 消灭；21346 的余 169 行随 t17 页表归入帧级动态资源家族
  （该家族第四个证据：t22 探针图集、cb12 帧常量、t1 拖尾 SB、t17 clipmap 页表）。
- 类 C（HS/DS）作为解释器缺陷类**清空**。
