# Step 171 — Index buffer raw data：ib_res_*.bin 解析验证 + 交叉校验 + 全量重跑

## 任务

1. 修 dx_dump.py（抽出 `DumpIndexBufferBin`，两条 early-return 路径都补）→ 导出侧已完成（外部工具，不在本仓库；本步消费其产物）。
2. 根据 `ib_res_*.bin` 和 `ia_vertex_data.csv` 解析 index buffer。
3. 重新运行 Dump 目录和 Cases 目录的所有 case，保证零回归。

## 背景与现状勘察

dx_dump.py 修复前，**两条 early-return 路径漏掉了 index buffer 的 raw dump**，导致大量 zip
没有 `ib_res_{resid}.bin`——解释器只能回退到 `ia_vertex_data.csv` 的 `IDX` 列
（`load_index_column`）。修复并重新导出后：

- **Dump 40 zip：26 个带 `ib_res_*.bin`**；其余 14 个均为 `Indexed=False` 的非索引 draw
  （manhattan×4、sekiro2 GS 案×5、sekiro4×2、witcher 16803/16817、EndlessSpace 3093 等），
  本来就不需要 IB。
- Cases 的 zip 亦全部覆盖（indexed 案例全有 bin）。

解释器侧的二进制 IB 解析（`load_index_list_from_binary`，步 118/121 引入）早已存在，
路径：`ia_input_layouts.csv` 的 `IndexBuffer` 段给出 ResourceId / 视图 ByteOffset / stride，
`draw_call_info.csv` 给出 `NumIndicesOrVertices` / `IndexOffset`（索引单位）/ `BaseVertex` /
`Indexed`；实际读取位置 = `视图 ByteOffset + IndexOffset × stride`，按 stride 2/4 解码
uint16/uint32，加 `BaseVertex`。非索引 draw 返回 `range(VertexOffset, VertexOffset+N)`。

**关键疑问**：新导出的 bin 是「整个资源」还是「仅 draw 用到的区间」？若是后者，现有
逻辑会重复加偏移。

## 系统性验证（scan_ib.py，全部 173 个 zip）

写了一个扫描脚本对 Dump+Cases 每个 zip 做三方对账：
bin 按「现有逻辑（加偏移）」解码 vs 按「偏移=0」解码 vs `ia_vertex_data.csv` 的 IDX 列。

结论：

- **所有 indexed 且带 bin 的案例（130+），「加偏移」解码与 CSV IDX 列 100% 逐项一致；
  零例只有「不加偏移」才匹配**。bin 确认是**完整资源**的 dump（例如 TombRaider 的 IB bin
  正好 10485760 = 10MB 上限截断，sekiro4 达 8.26MB），解析必须加偏移——现有实现正确。
- 非零偏移的强证据案（均只在加偏移时匹配）：
  - `IndexOffset` 大偏移：TombRaider 2848/7308（ioff=364746）、sekiro2 3207/9493
    （ioff=1476352）、sekiro4_14228（ioff=3484336）等 40+ 案；
  - IB 绑定 `ByteOffset`：witcher3 21979/22049（boff=13440）、event3191（boff=130272）、
    event8573（boff=211536）、event895（boff=8856，恰为半个资源）等；
  - 二者叠加与 `BaseVertex` 均验证通过。
- stride=4（`R32_UINT`）案例（witcher 2703/2732/21895/6977、Octopath 1031 等）同样全对。
- `Nobu15_event2894` 虽 `Indexed=False` 也 dump 了 IB bin（绑定了但未用）——无害。

即：**dx_dump 修复后的产物与解释器既有解析约定完全兼容，无需改动解码逻辑。**
新 dump 的实际增益是 26 个 Dump 案从「CSV IDX 回退路径」切换到「二进制权威路径」
（两者数值一致，但 bin 不受 CSV 截断/舍入风险影响，且是 GPU 真正读的字节）。

## 代码改动（render.py）

在 `_execute_pipeline` 的 IB 装载处加**交叉校验护栏**：当二进制 IB 与
`ia_vertex_data.csv` 的 IDX 列同时存在时逐项比对，不一致则输出
`Warning: binary IB disagrees with ia_vertex_data.csv IDX on N/M indices (binary wins)`
（以 bin 为准）。防止未来 dump 工具的偏移/stride 回归悄悄溜进来——本次全量重跑
中该警告零触发，即两源全量一致。

## 全量重跑结果

### Dump（`triage_dump.py --vs-only --workers 6 --timeout 300`）：40 案，1 新 PASS，零退步

**新 PASS：`OldWorld_event3338` 23814/23814**。该案历史轨迹：步 151 时 23352/23814
（diff 0.2~0.76 精度边界）→ 步 155/159/164 时 23550/23814 → 步 168 归入超时类①。
本轮全过 = 新 dump 补齐的二进制数据（该 zip 此前无 `ib_res_*.bin`）+ 步 170 的
GPU dpN 逐步舍入叠加生效。已晋级 Cases + 回归表（134 案）。

其余 39 案与既有基线**逐案完全一致**（无一行退步）：

| 案例 | 本轮 | 基线（步） | 类 |
|---|---|---|---|
| EndlessSpace2_2991 | 1464/1536 | 1464/1536（168） | ⑧ |
| Octopath 576/2651/2682 | 8/23064 | 8/23064（168） | ⑨ |
| TombRaider 2848/7308 | 827/1548 | 827/1548（170） | ⑥ |
| manhattan 87/124/161/198 | 998~999/1000 | 998~999/1000（169） | ⑥ |
| sekiro2_14998 | 28974/29148 | 28974/29148（169） | ⑦ |
| sekiro2_3207/9493 | 43337/45576 | 43337/45576（170） | ⑦ |
| sekiro2_4833 | 12/24 | 12/24（168） | ⑦ |
| sekiro4_7844 | 162/324 | 162/324（168） | ⑦ |
| sekiro4_19857/20244 | 0/5、0/7 | 0（167） | ⑤ |
| witcher 16215 | 0/30（150 Error 行） | 150 Error 行（170） | ⑧ |
| witcher 16803/16817/21346 | 0/4、0/4、0/1024 | 0（167） | ⑤ |
| witcher 21719/21895/21979/22049/22092/22201/22229/22260 | 0~132/N | 同前 | ⑧ |
| OldWorld 1034/2767 | timeout >300s | timeout（203k 顶点） | ① |
| ES2_3093 / Nobu2894 / sekiro2 13554/13931/14130/14388 | 无 golden 可比 | 同前 | ② |

### Cases（`run_regression.py`，vs_only，容差 0.005）

**主套件 133/133 全 PASS（exit 0）零回归；新晋级的 OldWorld_event3338 以完全相同的
回归配置单独复核通过（23814/23814，0 Error，VS 29.6s）——回归表现为 134 案全过。**
全部 case 日志确认走「binary index buffer」路径（`Loaded N indices from binary index buffer`），
交叉校验警告（`binary IB disagrees`）在 Dump+Cases 全部日志中**零触发**。

## 结论

1. dx_dump.py 的 `DumpIndexBufferBin` 修复产物（完整资源二进制 + `ia_input_layouts.csv`
   IndexBuffer 段）与解释器既有 `load_index_list_from_binary` 约定完全兼容，
   173 个 zip 三方对账（bin加偏移 / bin零偏移 / CSV IDX）证明「视图 ByteOffset +
   IndexOffset×stride」的读取方式对所有非零偏移案例均正确。
2. render.py 新增 bin↔CSV IDX 交叉校验护栏（不一致打 `Warning:`，bin 为准）。
3. 全量重跑：Cases 134/134，Dump 零退步 + OldWorld_event3338 转 PASS 晋级。

