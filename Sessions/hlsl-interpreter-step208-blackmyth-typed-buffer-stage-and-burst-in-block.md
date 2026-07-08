# Step 208 — BlackMyth 类 1 收尾：typed-buffer 跨阶段视图 + if/while 块内 SB 索引 burst 未复位

Date: 2026-07-08

## Prompts（本步任务）

step207 把类 1（14 例）修到 9/14，遗留两支：

1. `event8750`(1293/30960)、`event9055`(4158/58212)：A 支着色器（含淡入块），仅少数行通过，
   AND-zero 修复前后不变 → 另一条数据相关路径的错。
2. `event9999 / 10575 / 11070`（各 12162 顶点，0/N）：另一支变体（输出仅 `o0..o7`；`t4/t5` 是
   typed `Buffer<float4>` 而非 StructuredBuffer；无淡入块）。`SV_POSITION`(o7) 正确，但
   `TEXCOORD10/11`(o0/o1，法线/切线) 坍缩为 0，定位到 `r4.xyz = t5.Load(i).xyz` 每次读回单位
   四元数 `[0,0,0,1]`——`t5` 被按 `R16_UINT/2B/elem/131072` 装载，而真实 SRV 应为逐顶点切线帧。

本步把这两支都修掉——**是两个各自独立、通用的根因**。

---

## Bug 1 · typed-buffer 视图格式跨阶段串号（9999/10575/11070）

### 根因

`buffer_params.csv` 对**同一 t-slot 在不同阶段**各有一行：

```
VS,SRV,5,176857,texture5,TypedBuffer,18344,4,0,18344,R8G8B8A8_SNORM,buffer_176857.bin   ← VS 真正的切线帧
PS,SRV,5,4927, texture5,TypedBuffer,262144,2,0,262144,R16_UINT,     buffer_4927.bin      ← PS 的另一资源
```

`load_typed_buffer_data` 建 `by_reg` 时**只按 slot 建键、不看 Stage**，且逻辑
`if slot not in by_reg or dtype == 'TypedBuffer'` 让后出现的行覆盖——两行都是 `TypedBuffer`，
于是 **PS 行（R16_UINT/buffer_4927）盖掉了 VS 行**。VS 解释器遂用错资源装载 `t5`：
`t5.Load(i)` 每次读回 `[0,0,0,1]`（单位四元数）→ 法线/切线全 0。

（step207 的 A 支 event4812 不受影响：其 PS slot 5 是 `ReadWriteBuffer`(UAV) 而非 `TypedBuffer`，
新旧逻辑都保留 VS 的 `TypedBuffer` 行。故此 bug 只在"VS/PS 两行都是 TypedBuffer 同 slot"时触发。）

### 修复

- `HLSLInterpreter` 增 `self.shader_stage`，由 `render._make_interpreter` 设置（VS/PS/GS/… 各阶段
  创建时即知道自己是谁）。
- `load_typed_buffer_data` 读 `Stage` 列，`by_reg[slot]` **优先选阶段匹配的行**：填充优先级
  (1) 尚无 → (2) 本行匹配本阶段且已存的不匹配 → (3) 都不匹配但本行是 TypedBuffer。
  **绝不让不匹配的行覆盖已匹配的行**。`shader_stage` 未知时退化为旧行为（零风险）。

### 验证

`t5` 改为正确装载 `R8G8B8A8_SNORM / 4586 elem / buffer_176857.bin`，
`t5.Load(0)=[1.0,0.0,0.0079,1.0]`（真实切线帧），`o0=[1.0001,0,0.0079]` 对齐 golden
`[1.000124,_,0.007875]`。三例全过：`event9999/10575/11070 各 12162/12162`。

---

## Bug 2 · if/while 块内 StructuredBuffer 索引 burst 未按语句复位（8750/9055）

### 现象

`event8750` row 0–269 过、row≥270 起 `TEXCOORD10/11`(法线/切线) 坍缩为 0，`TEXCOORD7`(worldpos)
仅差 ~0.04。

### 根因

这是与前两支**都不同**的第三支着色器（`t0..t12` 多 buffer 蒙皮 + 逐实例裁剪 + 样条切线）。切线由
`t8` 相邻元素的**有限差分**求得：

```
r1.xyz = t8[r4.z]...;          // r4.z=48，建立 burst(idx=48)
r1.w = (uint)r4.z % ...;       // 算"是否块末元素"
...
r4.z = (int)r1.w + 1;          // r4.z 变成 49
r6.yzw = t8[r4.z]...;          // 应读 t8[49]
r7.xyz = t8[r1.w]...;          // r1.w=48，读 t8[48]
r6.yzw = -r7.xyz + r6.yzw;     // = t8[49]-t8[48]（切线方向）
```

`t8[48]=[-40084.9,8743.0,-2062.8]`、`t8[49]=[-40083.5,8742.7,-2059.1]`，**确为两个不同点**，
差分非零。但解释器把 `r6.yzw` 读成了 `t8[48]`（`-40084.9`…），与 `r7` 相同 → 差分 0 → 法线/切线归零。

原因：DXBC split-vector-load 的 `_sb_index_burst` 缓存（把一条 `ld_structured` 拆成的多行同索引读
合并，索引只解析一次）**只在顶层语句循环 `_execute_void_main` 里按语句复位**（token 不在当前语句就清）。
而这段解码在 `if (r1.w == 0) { ... }` **块体内**——块体经 `execute_block` 顺序执行，**从不做这个按语句
复位**。于是 `t8[r4.z]` 的 burst(idx=48) 跨过 `r4.z=49` 的赋值一直存活，第二次 `t8[r4.z]` 读**复用了
陈旧的 48**。row<270 恰好走的分支里索引没在两次同 token 读之间被改，故未暴露；row≥270 走到样条差分分支
才触发——典型数据相关。

### 修复

在 `execute_block` 的语句循环里加上与顶层循环**同款**的 burst 复位：

```python
for stmt in statements:
    if self._sb_index_burst is not None and self._sb_index_burst['token'] not in stmt:
        self._sb_index_burst = None
    self.execute_statement(stmt, local_vars)
```

严格更保守：连续同 token 的 split-load 仍合并（token 在语句里→不复位），一旦出现不含该 token 的语句
（如改索引的算术）即复位。与顶层循环行为一致，覆盖 if/else/while 块体（含嵌套）。

### 验证

`event8750 30960/30960`、`event9055 58212/58212` 全过。

---

## 全量回归

`python run_regression.py`（161 例）：**158/161 PASS**。3 个未过均为**与本改动无关的既有失败**
（step205/207 同款基线）：`witcher3_countryside_event16834`(3/30)、`OldWorld_event1034`(0/203328)、
`OldWorld_event2767`(0/203328)。既有 BlackMyth 全绿（`event3393 30960/30960`、`event9319`、
`event7117`、`event8040`、step207 加的 `event4812 144/144`），新加的
`event9999 12162/12162`、`event8750 30960/30960` 也过 → **两处修复零回归**。

新加入本地回归：`event9999`（typed-buffer 跨阶段修复）、`event8750`（块内 burst 复位修复）。
（`Cases/*.zip` 与回归 CSV 均 gitignore，为本地回归网。）

## 结论

step206 triage 的"类 1"（14 例）**至此全部清零**：
- step207：AND-zero 误标 `_RawBits`（9 例，event4812 支）。
- step208 Bug1：typed-buffer 视图跨 VS/PS 串号（3 例，event9999 支）。
- step208 Bug2：if/while 块体内 SB 索引 burst 未复位（2 例，event8750 支）。

三个根因**各自独立、均为通用正确性修复**（非 BlackMyth 专用补丁），共同点只是同一批资产的不同着色器
恰好各踩一个。类 1 净收益 **0/14 → 14/14**。
