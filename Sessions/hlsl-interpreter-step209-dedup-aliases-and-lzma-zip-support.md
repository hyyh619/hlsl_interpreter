# Step 209 — 支持新压缩方式的 draw zip：LZMA per-member codec + dedup 别名重建

Date: 2026-07-20

## Prompts（本步任务）

> draw zip 文件更改了压缩方式，请更新代码来支持读取。`Dump/` 下面的 draw zip 文件已经更新成新的压缩方式的文件。
>
> **Lever C — per-member LZMA codec**：dedup 之后，剩余成员仍各自独立 DEFLATE 压缩。把 per-member
> codec 换成 LZMA（zip method 14），raw texel/.bin/.csv 负载再缩 ~30%（实测 bin −22%、raw −19%、
> csv −18%；全语料 13.0 GB → 8.8 GB，−32%）。这只是 per-member 编解码变更，不捕获跨成员冗余（那需要
> solid archive，会破坏随机访问）；精确的跨成员重复已经由 **Lever B** 去掉了。

---

## 思考 · 诊断

新格式其实是**两个杠杆叠加**，只有第二个会真正影响读取代码：

### Lever C — LZMA（method 14）：stdlib 原生支持，无需改代码

用 `zipfile.ZipFile(...).infolist()` 检查新 zip，成员的 `compress_type` 混合了 `8`(DEFLATE) 与
`14`(LZMA)。Python 标准库 `zipfile` 在解压时会调用 `lzma` 模块，对 method 14 **原生可读**——
直接 `extractall` 与 `read()` 全部成功：

```
methods: {'DEFLATE': 26, 'LZMA': 5}
READ OK method 14 .../GS_shader.hlsl 1029
```

所以 LZMA 部分不需要任何代码改动。

### Lever B — dedup 别名：这才是会 break 的部分

新（以及部分旧）zip 在**根目录**多了一个 `dedup_aliases.csv`（与 case 文件夹平级），内容是
`alias,canonical` 两列，两侧路径都相对 case 文件夹：

```
alias,canonical
PS_constant_buffer_info.csv,GS_constant_buffer_info.csv
PS_constant_buffers.csv,GS_constant_buffers.csv
VS_constant_buffer_info.csv,GS_constant_buffer_info.csv
VS_constant_buffers.csv,GS_constant_buffers.csv
buffer_1545.bin,PS_slot_20_res_1545_buffer.bin
```

被去重的成员**物理上不在 zip 里**（`extractall` 后磁盘上没有 `PS_constant_buffers.csv` 等）。
这带来两处 break：

1. **顶层文件夹探测失效**：原逻辑
   ```python
   items = os.listdir(temp_dir)
   if len(items) == 1 and os.path.isdir(...): data_folder = 子文件夹
   else: data_folder = temp_dir
   ```
   dedup zip 根目录现在有 2 项（`dedup_aliases.csv` + case 文件夹），`len==2` 走 else →
   `data_folder = temp_dir`（**错**，文件其实在子文件夹里）。实测直接报
   `Error: VS shader not found: .../hlsl_interp_xxx/VS_shader.hlsl`。

2. **别名文件缺失**：即便定位对了文件夹，pipeline 需要的 `PS_constant_buffers.csv` /
   `VS_constant_buffers.csv` / `buffer_1545.bin` 等物理不存在，会继续失败。

（注：旧 `Cases/` zip 也早有 `dedup_aliases.csv`，但只去重了 MSAA 多 sample 的 `.raw`、
`vb_slot2` 等 pipeline 用不到的文件，所以旧回归“碰巧”没暴露 bug。新格式把 VS/PS/GS **常量缓冲**
也跨阶段去重了，才踩中。）

---

## 执行 · 修改（都在 `render.py` 的 zip workflow）

### 1) 顶层文件夹探测忽略松散文件

只按“唯一目录”定位 case 文件夹，忽略根目录的松散文件（manifest）：

```python
items = os.listdir(temp_dir)
dir_items = [it for it in items if os.path.isdir(os.path.join(temp_dir, it))]
if len(dir_items) == 1:
    data_folder = os.path.join(temp_dir, dir_items[0])
else:
    data_folder = temp_dir
```

### 2) 新增 `_materialize_dedup_aliases(temp_dir, data_folder)`

`extractall` 之后、进入 pipeline 之前调用。读 `dedup_aliases.csv`（先找根目录、再找 case 文件夹），
把每个 `canonical` 复制回它的 `alias` 位置：

- `alias` 已存在 → skip（没真正被去重）；
- `canonical` 缺失 → 打印 Warning 并跳过（不硬崩）；
- 用 `os.makedirs(..., exist_ok=True)` + `shutil.copyfile` 支持带子目录的别名；
- 无 manifest（旧单文件夹 zip）→ no-op，向后兼容。

统计并打印 `Rehydrated N deduplicated file(s)`。

---

## 结果 · 验证

**之前失败的 dedup 案例（`Cases/witcher3_countryside_event994.zip`，2 项根目录）现在通过：**

```
Rehydrated 2 deduplicated file(s) from dedup_aliases.csv
Total PASSED rows: 2490/2490      （Error: 0 行）
```

**新格式 Dump zip（`Dump/Assassins-frame9018_event969.zip`，含 LZMA 成员 + 5 个别名）端到端跑通：**

```
Rehydrated 5 deduplicated file(s) from dedup_aliases.csv
exit=0，无 Error/Traceback
```

**全回归套件**：`python run_regression.py` → **152/161 passed**（155 个 zip 实际在库；6 个大文件本地缺失
计入 not-passed）。剩下 3 个 FAIL 全是 golden 数值不匹配、与本改动无关：

- `OldWorld_event1034` / `OldWorld_event2767`：**非** dedup zip（无 manifest，本改动对其为 no-op），
  纯插值器/数据问题。
- `witcher3_countryside_event16834`：是 dedup zip，文件已正确 rehydrate；其 `dedup_aliases.csv`
  只去重了 PS 的 mip9 `.img` 纹理（不影响 VS），失败在 `sv_position` 数值 diff——独立的 math 问题。

关键：**修改前这些 2 项根目录的 dedup zip 根本读不出**（探测到 `temp_dir` → `Error: VS shader not
found`），本改动是严格改进（从“完全打不开”变为“正确读入并运行”）。

---

## 关键结论

- LZMA(method 14) 由 stdlib `zipfile`/`lzma` 原生解压，**不需改代码**。
- 真正要处理的是 dedup（Lever B）：`dedup_aliases.csv` 让根目录多出松散文件、并让被去重成员物理缺席。
  修法 = **顶层文件夹探测按“唯一目录”走 + 解压后按 manifest 把 canonical 复制回 alias**。
- 向后兼容：无 manifest 的旧单文件夹 zip 完全不受影响（no-op）。
