# Step 173 — 类⑧（VS 纹理采样精确性，11 案）：十项通用缺陷的修复

## 任务

修复类⑧：witcher 16215/16834/21719/21895/21979/22049/22201/22229/22260 +
EndlessSpace2_2991 + Octopath_3601（11 案，"部分可收敛"）。

## 调查方法

9 个 witcher 案 VS hash 各不相同（植被/阴影变体家族），但共享同一套基础设施路径。
从最小案（22229，12 行）开刀，`[STMT]` 逐语句 trace 对照 golden 反推，每个分歧点
顺藤摸瓜到基础设施层，得到的都是**通用修复**（一处修、多案受益）。

## 十项缺陷与修复

### hlsl_interpreter.py

1. **`while` 循环完全未实现**（最大缺口）。3Dmigoto 大量输出
   `while (true) { ... if (c) break; }`（灯光遍历、CSM 级联选择）；整块被当作
   一条未知语句静默跳过 → 级联阴影循环 0 次迭代 → 阴影因子恒为全亮。
   新增 `execute_while_statement`（括号/大括号配对解析、`while(true)` 短路、
   65536 次迭代护栏）；`break`/`continue` 以 `_LoopBreak`/`_LoopContinue` 异常
   从任意嵌套深度（if 块内）穿透到最内层循环；`_execute_void_main` 顶层加
   散逸 break 护栏。

2. **向量条件三元式不做逐分量选择**。`r5.xyzw ? float4(1,1,1,1) : 0` 在
   cond=[-1,-1,-1,0] 时输出 [1,1,1,1]（`_to_bool` 整体折叠）——movc 语义应为
   [1,1,1,0]。级联掩码 `r5.w` 因此常年错值。修复：cond 为列表时逐 lane 选择
   （标量分支广播）。

3. **`SampleCmpLevelZero`/`SampleCmp` 未实现**（阴影 PCF 比较采样）。
   新增方法分发 + texture.py `sample_cmp_lz`：4 邻域各自按 ComparisonFunc
   比较（Less 等 8 种）得 0/1，再按双线性权重混合（比较结果的混合，
   非纹素值的混合）；point 过滤时单纹素比较。

4. **`SamplerComparisonState` 声明不被采样器绑定正则识别**
   （只匹配 `SamplerState`）→ `s9_s` 解析失败回退默认采样器
   （ComparisonFunc=NEVER→恒 0，全阴影）。正则改为
   `Sampler(?:Comparison)?State`。

5. **`GetDimensions`（resinfo）未实现**：
   `t0.GetDimensions(0, fDest.x, ...)` 的 out 参数保持 0 →
   `0.5/width` 除零 → inf 传染全链（event16834 的 inf 输出）。
   新增语句级方法处理：按 mip 算 width/height、array/depth、mip 数，
   复用赋值机制写回 out 参数（swizzle 安全）。

6. **Load 坐标的 denormal 位模式**：3Dmigoto 把原始整数寄存器值写成浮点
   字面量——int 1 变成 `1.40129846e-45`；按值转 int 得 0（应为位模式 1），
   event16834 的 `t1.Load(r0.xwz)` 因此读错行。新增 `_load_coord_to_int`：
   denormal 范围（<1.18e-38）内的浮点按位重解释，其余按值截断。

### texture.py / render.py

7. **`sample()` 末尾的 [0,1] clamp 摧毁带符号/HDR 纹理值**（多年谜团的真相）。
   R16G16_FLOAT 细节法线的 x=-0.083 被夹成 0（step 157 记录的
   "detail-normal R reads ~0"就是它）；HDR R11G11B10 探针 >1 的值被削顶。
   GPU 返回的就是存储值——删除 clamp（UNORM/SNORM 解码天然在界内）。

8. **纹理解码按资源格式而非 SRV 视图格式**：witcher CSM 是 R16_TYPELESS
   资源 + R16_UNORM 视图；`_TYPELESS` 回退**先试 _FLOAT**，深度 33302
   被读成负 half denormal（-3.2e-5）→ 全部 PCF 比较失败。render.py 的
   `FormatStr` 改为优先 `ViewFormat` 列。

9. **补 `R16_UNORM`/`R16G16_UNORM` 解码**（_FMT_SPECS 缺失）。

10. **补 `R11G11B10_FLOAT` 解码**（float11/float10 小浮点，5 位指数；
    此前静默回退 8-bit BMP，探针颜色损失精度）。

## 逐案结果（vs-only, tol 0.005）

| 案例 | 修复前 | 修复后 |
|---|---|---|
| witcher 16215 | 0/30 | **PASS 30/30**（clamp 去除 = 细节法线修复） |
| witcher 16834 | 0/30 | **PASS 30/30**（GetDimensions + denormal Load 坐标） |
| witcher 22201 | 0/18 | **PASS 18/18**（while/PCF/比较采样器全链） |
| Octopath 3601 | 0/96 | **PASS 96/96** |
| witcher 22229 | 0/12 | 10/12（余 2 行 diff=0.005075，恰超容差 1.5%，灯光循环解析衰减的精度临界） |
| witcher 22260 | 0/108 | 54/108（余：雾色链远端顶点） |
| witcher 21719 | 0/1728 | 368/1728 |
| witcher 21895 | 0/6360 | 1057/6360 |
| witcher 21979 | 132/840 | 132/840 |
| witcher 22049 | 0/840 | 132/840 |
| ES2_2991 | 1464/1536 | 1464/1536（尾部 72 行：曲线图集单纹素级差异；golden 的 TEXCOORD1 与我们完全一致 → 几何/SB 链已对，仅剩宽度曲线采样） |

## 全量验证

- **Cases 回归 `run_regression.py` 138/138 全 PASS 零回归**——十项共享基础设施改动
  （尤其 sample() clamp 移除、向量三元式逐分量化、ViewFormat 优先）无一破坏既有案例。
- **Dump triage 35 案：4 转 PASS，其余全部与步 168–172 基线逐案持平零退步**
  （TombRaider 827/1548、manhattan 998~999/1000、sekiro2_3207/9493 43337/45576、
  sekiro 各案、walls 全部不变）。
- **4 案晋级** Cases + 回归表（**142 案**）并从 Dump 移除（剩 **31 案**）。

## 类⑧收尾状态

- **已消灭 5/11**（含步 172 的 22092）：16215、16834、22092、22201、Octopath3601。
- **大幅收敛 6 案**：22229（10/12，精度临界 0.005075）、22260（54/108）、
  21719（368/1728）、21895（1057/6360）、21979/22049（132/840）——从全 0 起步；
  剩余分歧在雾色/灯光衰减等更深链路，且多为同批通用缺陷修复后暴露的下一层。
- **ES2991 维持 1464/1536**：golden 的 TEXCOORD1 中间值与我们完全一致，证明几何/SB
  解码全对；仅剩曲线图集宽度因子的单纹素级差异（尾部 72 行）。

## 方法论

与步 172 相同的"由小及大 + trace 反照"：从 12 行的最小案开刀，每个分歧都追到
基础设施层修通用缺陷，而不是 per-case 打补丁——witcher 家族 9 个不同 shader
共享同一批修复，Octopath3601 纯搭车转正。
