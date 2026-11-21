# 多采样率剪辑时间轴、真实混音档案与有声导览（audiomix）

纯 Python 3.11 实现（NumPy/SciPy/标准 `wave`），无视频、无音频设备、无网页。
三个服务：

1. **可独立编辑的音频时间轴服务**（`audiomix/timeline.py`）：插入/移动/修改/删除/
   区间查询/发布，clip 身份独立于素材，有理数时间，不渲染即可点查询。
2. **实际混音数据仓**（`audiomix/archive.py`）：只消费不可变发布产品与实际素材字节，
   固定 16kHz 输出，按固定样本块存档，内容寻址复用，支持范围读取与逐块追溯；
   不读取编辑器私有操作日志，也接受外部合法时间轴产品。
3. **有声导览编排**（`audiomix/packets.py` + `audiomix/guide.py`）：把"某产品版 /
   某 clip 实例 / 某段源样本"编译成设备整包，在展厅路线图上给出能保障所有路线的
   非预知装载方案，或给出冲突证据；可逐路回放真实字节并出收听记录。

## 目录

```
audiomix/
  rationals.py   精确有理数（Fraction）表示与边界整数运算
  assets.py      PCM WAV 生成/读取 + 素材登记仓（asset_id=正文 SHA-256，绑定摘要/样本数）
  timeline.py    时间轴编辑器（整笔校验、点查询、不可变发布产品）
  engine.py      混音引擎：逐样本参考解释器 + 等价的 NumPy 分块/任意区间渲染
  archive.py     混音数据仓：内容寻址块存储 + SQLite 依赖索引 + 增量渲染 + 读取/追溯
  packets.py     导览段编译：段引用解析、任意区间真实混音、固定包长切整包、内容寻址包仓
  guide.py       展厅路线图 + 设备缓存 + 非预知装载规划 + 冲突证据 + 逐路回放/符号验证
  cli.py         命令行入口
tests/           手算案例、数据仓案例、边界语义、导览编排案例
demo/            30 素材/100 clip/120s 规模案例；45 展厅/768 路线的导览规模案例
run_all_checks.sh 一键全量验证
```

## 核心语义（均为精确有理数，杜绝浮点边界误差）

- 素材：单声道有符号 16bit PCM，采样率仅 8000/16000/48000；
  `asset_id = SHA-256(PCM 正文)`，同字节内容永远同一身份，绑定摘要与样本数。
- clip：`clip_id` 与素材独立；同一素材可被任意多 clip 反复引用；多 clip 允许重叠。
  时长 `(b-a)/(源采样率×速度)`，参数全部为正/非负有理数。
- 编辑整笔拒绝：未知素材、源区间 `[a,b)` 越界、淡化之和超过 clip 时长等，
  单次/批量操作要么全部生效要么状态不变。
- 输出样本：固定 16kHz，总时长 T 显式给定，输出恰为所有满足
  `0 ≤ n/16000 < T` 的样本（数量 `ceil(16000T)`），区间一律左闭右开，
  不因浮点四舍五入多一个样本。
- 源映射：实数样本位置 `p = a + (t-start)×源采样率×速度`；相邻样本线性插值；
  `p` 到达最后一个样本之后（仍在 clip 时长内）保持末样本值，绝不读区间外。
- 淡化：增益乘 0→1 线性淡入、1→0 线性淡出；淡化时长为 0 时因子恒 1；
  边界用严格小于判定（`d < fade_in`、`remain < fade_out`）。
- **舍入只做一次**：所有有效 clip 的浮点贡献全部相加后，才做“恰半取偶”
  （banker's rounding）一次，再饱和到 `[-32768, 32767]`。单片段先裁剪/先舍入
  再相加是不同语义，本工程不采用。
- 块：每块 1600 样本（0.1s）。块存档记录峰值、实际削顶样本数、所用 clip/asset。

## 复用依据（为什么更正后不会误用旧块）

块键 `block_key = SHA-256(规范JSON{输出采样率, 块序号, 该块重叠到的全部 clip
完整身份与全部转换参数, 这些 clip 引用素材的摘要})`。

- 刻意**不含 product_id、不含 T**：时间轴更正产生新产品时，依赖逐项相同的
  未受影响块仍可复用；块内容与总时长无关。
- 任何重叠 clip 的参数变化或素材正文（摘要）变化都会令键改变 → 新产品不可能
  命中旧块。规模案例中移动一个 clip，1200 块里仅 2 块重渲染、1198 块复用。

## 命令速览（有理数接受 `1/3`、`0.25`、`7` 等写法）

```bash
# 素材登记（序列/斜坡/正弦/静音，自动生成 PCM WAV）
python3 -m audiomix.cli asset-seq --root DIR/registry --rate 16000 --values 0,100,200,300
python3 -m audiomix.cli asset-list --root DIR/registry

# 编辑
python3 -m audiomix.cli tl-insert --root DIR/registry --clip-id c1 \
    --asset-id AID --a 0 --b 4 --start 1/4000 --speed 2 --gain 3/2 \
    --fade-in 1/16000 --fade-out 1/8000
python3 -m audiomix.cli tl-move   --root DIR/registry --clip-id c1 --new-start 1/8000
python3 -m audiomix.cli tl-modify --root DIR/registry --clip-id c1 --set gain=1 --set speed=1
python3 -m audiomix.cli tl-delete --root DIR/registry --clip-id c1
python3 -m audiomix.cli tl-query  --root DIR/registry --lo 0 --hi 1/8000
python3 -m audiomix.cli tl-at     --root DIR/registry --time 1/16000   # 不渲染点查询
python3 -m audiomix.cli tl-publish --root DIR/registry --out product.json

# 校验/渲染/读取/追溯
python3 -m audiomix.cli verify-product --product product.json --registry-root DIR/registry
python3 -m audiomix.cli render-compare --product product.json \
    --registry-root DIR/registry --T 120          # 分块渲染 vs 独立逐样本解释器
python3 -m audiomix.cli render --archive-root DIR/archive --product product.json \
    --registry-root DIR/registry --T 120 --render-id r1
python3 -m audiomix.cli read  --archive-root DIR/archive --render-id r1 --start 0 --end 1600
python3 -m audiomix.cli trace --archive-root DIR/archive --render-id r1 --start 0 --end 3200
python3 -m audiomix.cli export-wav --archive-root DIR/archive --render-id r1 --out out.wav
python3 -m audiomix.cli verify-integrity --archive-root DIR/archive
```

完整端到端示例：`bash demo/cli_walkthrough.sh`；全量验证：`bash run_all_checks.sh`。

## 有声导览：段编译、整包与"边走边选路"的装载

路线图 JSON 为一个有限无环图（`route_map.json`）。每厅合同是一列窗口：
`play`（所列段整包必须已就绪）、`transmit`（本窗口最多传 budget 个整包）。
第一个 `play` 是入厅首包；全部窗口处理完后才揭晓后继分支。

```jsonc
{
  "entrance": "A", "preload_budget": 2,
  "halls": [
    {"hall_id": "A", "windows": [{"type": "transmit", "budget": 2}],
     "successors": ["B", "C"]},
    {"hall_id": "B", "windows": [{"type": "play", "segments": [
      {"product_id": "…", "clip_id": "c1", "src_a": 0, "src_b": 4}]}],
     "successors": []},
    {"hall_id": "C", "windows": [{"type": "play", "segments": [
      {"product_id": "…", "clip_id": "c2", "src_a": 0, "src_b": 4}]}],
     "successors": []}
  ]
}
```

- **段**明确引用 `(product_id 译制版本, clip_id 实例, 源区间 [src_a,src_b))`；
  源区间必须是该 clip 自身 `[a,b)` 的子集，否则拒绝（绝不拿同素材的别的 clip 顶替）。
  播放范围按原项目换算：`t = start + (p-a)/(源采样率×速度)`，输出样本
  `n ∈ [ceil(16000·t0), ceil(16000·t1))`。段 PCM 是该区间上的**真实混音**
  （重叠 clip 一并混入，与 `engine` 同语义）。
- **整包**按设备固定包长切分，末包可短；包身份 `SHA-256(版本 ‖ 长度 ‖ 字节)`，
  仅当译制版本/实际 PCM 字节/长度全一致才复用（内容寻址，跨厅共享单份存储）。
- **规划**给出 `(展厅, 窗口, 当前缓存) -> 动作` 的非预知策略，保障路线图里
  **每一条**完整路线都能无停顿播放，并遵守传输额度与缓存容量；不可行时返回
  冲突证据（容量或传输额度不足，附各分支必须驻留的整包集合）。
  大会图用 `(展厅,窗口,缓存)` 状态空间符号验证，**不逐条展开或复制整条路线的声音**。

```bash
# 校验路线图（入口/可达/路线条数，DP 计数不展开）
python3 -m audiomix.cli route-map-validate --route-map route_map.json

# 编译段 -> 整包：查询每段播放范围、每包字节；--cache 为设备缓存容量（整包数）
python3 -m audiomix.cli guide-compile --route-map route_map.json \
    --product p1.json --product p2.json --registry-root DIR/registry \
    --packet-samples 1600 --cache 2

# 规划：可行则给策略/预装；不可行则给冲突证据
python3 -m audiomix.cli guide-plan --route-map route_map.json \
    --product p1.json --product p2.json --registry-root DIR/registry --cache 2
# 符号验证全部路线（状态空间，非逐条）
python3 -m audiomix.cli guide-verify --route-map route_map.json \
    --product p1.json --product p2.json --registry-root DIR/registry --cache 2
# 按某条具体路线回放真实字节，导出 WAV
python3 -m audiomix.cli guide-replay --route-map route_map.json \
    --product p1.json --product p2.json --registry-root DIR/registry --cache 2 \
    --route A B --wav route_ab.wav
# 比较两种硬件租用条件（不同缓存容量）是否都能承诺无停顿路线
python3 -m audiomix.cli guide-compare --route-map route_map.json \
    --product p1.json --product p2.json --registry-root DIR/registry --cache 1 2
```

规模复演：`python3 -m demo.guide_scale_demo OUT`（45 展厅、768 条入口路线，
真实短 WAV 编译成整包，抽样导出 WAV，并比较 7/8 包容量两种租用条件）。

## 验证策略（不使用任何外部混音结果）

- `tests/test_hand_calc.py`：短整数序列素材，10 个案例人工推导每个输出样本
  （跨采样率插值、末样本保持、2 倍/半倍速、淡化边界、重叠相消、削顶、
  恰半取偶、跨块边界、整笔拒绝、48k 抽点），并要求**手算值 = 逐样本参考
  解释器 = 分块向量化渲染 = 逐块拼接**四者一致。
- `tests/test_archive.py`：内容寻址复用、移动 clip 的增量渲染、素材摘要变化
  零误用、外部产品篡改拒绝、编辑器日志移走后旧 render 仍可读。
- `tests/test_edge.py`：非整数 T、左闭右开边界、非法修改回滚、持久化重开、
  “先求和后单次舍入”与“逐片段先舍入”的语义区分。
- `demo/scale_demo.py`：30 素材（每采样率 10 个）、100 clip、120 秒/192 万样本，
  含全部要求情形；窗口化逐样本交叉核对，输出 WAV、块峰值、削顶数与来源索引。
- `tests/test_guide.py`：复刻题面手算场景——B/C 入口分支（容量 1 冲突、容量 2 可行、
  同译制版同首包时容量 1 也复用可行）、D 汇合不同余包（入口统一预装 vs 知情后分路
  装载）、非预知性（入口动作唯一不依赖后揭晓分支）、源区间越界拒绝、任意区间渲染
  与参考解释器一致、包复用仅按版本+字节+长度、全路线逐路回放与符号验证一致。
- `demo/guide_scale_demo.py`：45 展厅/768 路线的交叉汇合路线图，3 个译制版本、
  真实短 WAV 编译整包；符号验证约 700 个状态（不逐条存路线声音），比较 7/8 包容量
  两种租用条件，输出抽样路线 WAV。

参考解释器（`engine.render_reference`）是独立的纯 Python 逐样本 Fraction 实现，
与向量化渲染路径代码完全分开，仅共享产品装载/校验，作为语义基准。
