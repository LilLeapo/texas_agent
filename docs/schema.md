# 消息 Schema v0.3

> D1-1 交付物。所有消息经唯一 ws_hub (/pub :8765, /sub :8766) 广播并逐行落 `sessions/*.jsonl`。
> 变更需三方(Agent组/前端组/面部组)确认后升版本号。
> v0.2 (纯新增, 向后兼容): deal 事件 detail 新增可选 `source` 字段; 新增 `audit_report` 消息。
> v0.3 (纯新增, 向后兼容): 新增 `deal_preview` 消息(识别即推送, 先画牌不等记账)。

## deal_preview (v0.3 新增, 识别即推送)

机械臂模式下, 牌飞过读牌位被识别的**瞬间**发出 —— 不等引擎按序入账
(底牌臂序重映射会把前几张攒到位后才涌出 game_event)。前端收到即可先把牌画到
对应牌位; 随后的 `game_event(deal)` 才是权威记账, 两者不一致时(人工纠错等)
**以 game_event 为准覆盖**。胶着待仲裁的窗不发预览。

```json
{"type":"deal_preview","zone":"P3b","card":"9c","via":"yolo","votes":"9c:31 9s:6"}
{"type":"deal_preview","zone":"C4","card":"Jd","via":"yolo","votes":"Jd:55"}
```

zone: 底牌为**物理牌位**(如 P3b; 同座位 a/b 与权威记账可能互换, 按座位归并即可);
公共牌为 C1..C5。

## 信封 (所有消息)

| 字段 | 类型 | 说明 |
|---|---|---|
| `seq` | int | hub 盖的全局自增序号 (进程内 LocalBus 亦盖) |
| `ts` | float | epoch 秒, 3 位小数 |
| `type` | str | 消息类型, 见下 |
| `replayed` | bool? | 回放器重播时追加 |

**座位号约定: 全系统消息一律 0 基** (`seat: 0` = P1)。前端展示时 +1。
PokerKit 座位语义: 0=小盲, 1=大盲, …, n-1=庄位; **单挑例外: 0=大盲, 1=庄位兼小盲(翻前先行动)**。
牌面格式: 点数大写+花色小写 (`As` `Td` `7h`), 未知 `??`。

## game_event

每次引擎状态变化发一条, `state` 含全量快照(前端无需自行维护状态机)。

```json
{"type":"game_event","event":"deal","detail":{...},"state":{...}}
```

`event` ∈ `hand_start | deal | action | showdown | settlement | correction`

| event | detail 字段 |
|---|---|
| hand_start | `hand_no, blinds, positions` |
| deal | `deal_kind(burn/hole/board), card, zone, board, source?` |
| action | `seat, action(fold/check/call/raise), amount` |
| showdown | `seat, hole`(亮牌数组, muck 为 null) |
| settlement | `payoffs`(每座位盈亏), `stacks` |
| correction | `author, op_index, old, new, zone` |

`state` 快照: `hand_no, street, board, pot, actor, complete, seats[]`;
seat 项: `seat, position, stack, bet, in_hand, hole`(公开模式为 null)。

`source`(可选): 牌面认定来源, `yolo`(YOLO 高置信) | `yolo+vlm`(YOLO+VLM 双确认) |
`vlm`(VLM 认定) | `ops`(操作员补录); 缺省 = 预排/NCC 主路。前端可据此挂识别徽章。

## dealer_prompt / input_request / agent_trace / alert

```json
{"type":"dealer_prompt","text":"发转牌 → C4","level":"normal|again|alert","tts":true}
{"type":"input_request","seat":2,"legal":["fold","call 100","raise 金额, 200~20000"],"timeout_s":30}
{"type":"agent_trace","text":"提示荷官: 发转牌 → C4; 等待 C4"}
{"type":"alert","text":"发牌超时: 发转牌 → C4"}
```

## correction (独立事件, 区域纠错)

```json
{"type":"correction","author":"ops","field":"zone","old":"C4","new":"C5"}
```

(牌面纠错走 game_event/correction, 由引擎重放产生, 含重放后全量快照。)

## analysis_update (M6, 每次状态变化整包重发)

```json
{"type":"analysis_update","street":"flop","mode":"god|public",
 "board":["Jh","8h","3c"],
 "board_next":{"flush":0.2,"straight":0.3556,"pair_board":0.1556,"overcard":0.2222},
 "deck_remaining":{"s":12,"h":9,"d":13,"c":11},
 "players":[{"seat":0,
   "equity":{"win":0.2556,"tie":0.0},
   "final_dist":{"high":0.2455,"pair":0.3152,"flush":0.3626,"...":0},
   "now":{"cat":"high","cat_zh":"高牌","hs_pct":0.592,"nut_rank":47,"ahead":false},
   "outs":{"clean":8,"tainted":1},
   "comeback":0.2556}],
 "actor":{"seat":0,"to_call":300,"pot_odds":0.25,"required_eq":0.25,
          "spr":21.11,"alpha":0.3333,"mdf":0.6667},
 "nuts":{"desc":"JsJd 三条","holder":1},
 "n_runouts":990,"elapsed_ms":3.2}
```

语义定义(计算纪律见手册 M6):

- 条件集 = 各家已知底牌 + 已发公共牌。**烧牌视为未知**, 预排牌序的未来信息绝不进入计算。
- `equity`: flop 起精确枚举全部剩余走向; preflop 蒙特卡洛(3000 样本)。
- `outs`: **仅落后者有**。out = 拿到后能击败对手当前牌力的下一张牌; 其中对手同时改善反压的记 `tainted`。领先者与 preflop 为 null。
- `board_next`: 下张危险牌条, 按真实剩余牌堆精确计数。`flush`=使公共牌达到三张同花; `straight`=新增可被两张手牌补全的顺子窗口; `pair_board`=公共牌配对; `overcard`=高于当前最大公共牌。
- `now.hs_pct`: 当前成牌击败多少比例的可能对手两张组合; `nut_rank`: 第 N 坚果(1=坚果)。
- `ahead`: 上帝模式下的确定事实; `comeback`: 落后者的总权益(win + tie 折算)。
- `actor.alpha` = 跟注额/(跟注额+下注前底池) = 诈唬盈亏平衡点; `mdf` = 1−α。
- **公开模式置空** (前端显示 "—"): `deck_remaining, nuts, outs, now.ahead`。
- 翻牌发到一半(板面 1~2 张)不发本消息。

## gto_hint / gto_deviation (M7)

```json
{"type":"gto_hint","seat":2,"node":"9max_100bb/UTG/RFI","hero":"99",
 "summary":{"raise_2.5":0.121,"fold":0.879},
 "matrix":{"AA":{"raise_2.5":1.0},"A5s":{"raise_2.5":0.5,"fold":0.5},"...":{}},
 "actions":["raise_2.5","fold"],"source":"chart"}
{"type":"gto_deviation","seat":2,"hand":"99","node":"9max_100bb/UTG/RFI",
 "gto":{"raise_2.5":1.0},"action":"call"}
```

- `matrix` 为整张 169 手牌表, 前端 13×13 渲染(对角=对子, 右上=同花s, 左下=非同花o), hero 格金色描边。
- `source` ∈ `chart`(转录图表) | `placeholder`(占位假数据) | `solver_offline`(剧情牌离线求解, 界面须标注)。
- 查不到的节点(罕见 limp 链等)**静默不发**, 绝不编造。
- 措辞纪律: 界面与解说一律称 **"GTO 基线 / 参考策略"**, 不宣称现场实时求解。

## audit_report (发牌审计员, 慢循环 VLM)

每次 deal(烧牌除外)后异步把顶视静止帧发 VLM 对答案。**只有建议权, 不阻塞主循环**;
VLM 失联/看不清则本消息静默缺席(拔掉 Spark 整场无感)。`mismatch` 时另发一条 `alert`。

```json
{"type":"audit_report","street":"flop","board":["Jh","8h","3c"],
 "verdict":"match","note":""}
{"type":"audit_report","street":"flop","board":["Jh","8h","3c"],
 "verdict":"mismatch","note":"公共牌区只有 2 张明牌"}
```

verdict ∈ `match | mismatch`; `note` 仅 mismatch 时非空。积压时只审计最新一次发牌。

## arm_command / arm_ack (机械臂, v0.2 新增)

Agent 指挥机械臂经总线走; 机械臂组实现订阅者: 收 command → 执行 → 回 ack(同 id)。
超时(默认 15s)或 ok:false 时 Agent 自动降级人肉提词, 牌局不卡死。详见
`docs/机械臂-Agent集成设计.md`; 联调可用内置 SimArm(`live_cli --robot --sim-arm`)。

```json
{"type":"arm_command","id":19,"action":"deal_to","zone":"P1a","face":"down"}
{"type":"arm_ack","id":19,"ok":true}
{"type":"arm_ack","id":20,"ok":false,"reason":"gripper miss"}
```

`action` ∈ `home | pick_from_deck | present_to_camera | deal_to | sweep`

## commentary (M6 解说)

```json
{"type":"commentary","text":"剧情反转! P1 反超, 胜率来到 76%。","trigger":"reversal"}
```

trigger ∈ `flop_leader | comeback | reversal | danger | gto_dev | settlement`。
同源 5s 去抖(reversal 豁免); LLM 3.5s 超时/异常自动降级模板句库(LLM 只润色模板句, 事实由模板锁定)。
