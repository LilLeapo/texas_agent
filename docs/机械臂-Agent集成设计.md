# Agent × 机械臂集成设计

> 2026-07-11。核心论点: **机械臂不是给系统加难度, 而是把识别从"最难场景"变成"最简单场景"**
> —— 固定位姿、固定距离、固定光照的亮牌位, YOLO 置信将稳定 >0.9, VLM 退居复核与审计。
> 架构无需重构: 编排器天生就是状态机, 今天驱动"人肉提词", 明天同一循环驱动机械臂。

## 1. 不变量 (已建成, 机械臂直接受益)

```
engine.next_required() → Need(发哪张/发到哪) → [执行器] → 顶视 watch 核验落位 → record_deal
                                                              ↑ 这一整条不动
```

- **执行器可插拔**是手册 day-0 设计(`executor: human | robot` 热切换): 人肉版执行器=提词器+荷官,
  机械臂版=arm 指令序列。编排器、引擎、分析、GTO、解说、前端对接**一行不改**。
- 认牌链(YOLO→VLM复核→VLM→ops)、发牌审计、`source` 溯源标记, 全部照用。
- ShoeGod(人肉亮牌) 与 RobotShoeGod(机械臂亮牌) 同接口 `next_card(kind)`, 一换即用。

## 2. 取牌阶段: Agent 控制相机 + VLM/YOLO 认牌 (核心流程)

```
Need(DEAL hole P1a)
  → Agent: arm.pick_from_deck()          机械臂从牌堆取顶张(面朝下)
  → Agent: arm.present_to_camera()       移到固定亮牌位(读牌相机正对, ~25cm, 遮光罩)
  → Agent: 认牌 [YOLO 130ms; conf<0.85 → VLM 复核; 分歧 → arm 微调姿态重亮(最多2次) → ops]
  → Agent: arm.deal_to("P1a")            面朝下放到目标区
  → 顶视 watch("P1a") 核验落位            (现有逻辑)
  → engine.record_deal(card, "P1a", source="yolo")
```

要点:
- **烧牌不亮**: `Need(burn)` → `arm.pick_from_deck()` + `arm.deal_to("MUCK")`, 跳过亮牌(信息纪律)。
- **重复牌防线**照旧: 认出已发过的牌 = 误读 → 重亮; 两次失败 → 网页补录, 牌局永不卡死。
- **亮牌位物理设计**(给机械臂组的需求): 相机朝下拍桌面上的固定小垫(非朝上拍天花板——逆光),
  臂把牌送到垫上方 20~30cm 静止 ≥0.5s; 位置重复精度 ±2cm 内即可(YOLO 姿态鲁棒)。

## 3. 全流程各阶段的 Agent 控制点

| 阶段 | Agent 指令 | 核验 | 失败分支 |
|---|---|---|---|
| 开局 | `arm.home()` + 顶视标定+基线 | 8 码可见 | 网页"重新标定" |
| 发底牌×8 | pick → present(认牌) → deal_to(P1a..P4b) | watch 落位 | 重亮→补录; 落错区→confirm |
| 翻前~河下注 | 无臂动作(玩家键盘/语音申报) | — | — |
| 烧牌×3 | pick → deal_to(MUCK), 不亮 | watch(MUCK 首张) | force_pass |
| 翻/转/河 | pick → present(认牌) → deal_to(C1..C5) **面朝上** | watch + 发牌审计(VLM 对答案) | 同上 |
| 结算后 | `arm.sweep()` 收牌(可选) → 基线重置 | 全区 none | 网页"重设基线" |
| 任意时刻 | — | — | 网页 🔁 重开本手 → arm.home() |

## 4. 与机械臂组的接口契约 (HTTP, 2026-07-11 定稿)

**臂控电脑(局域网)开 HTTP server**, 包一层壳暴露既有点位控制; Spark 侧 `HttpArm` 客户端调用。
契约与骨架见 `arm_side/README.md` + `arm_side/arm_server_skeleton.py`:

```
GET  /health → {"ok": true}
POST /arm {"action":"deal_to","zone":"P1a","face":"down"} → 阻塞执行 → {"ok":true|false,"reason":...}
```

- `action` ∈ `home | pick_from_deck | present_to_camera | deal_to | sweep`
- Spark 侧单条超时 25s; 超时/失败两次 → 臂标记离线, 整手切人肉提词, 发 alert
  (**机械臂现场罢工, 荷官顶上, 演示不死**, 与慢循环同一哲学)。
- 总线 `arm_command/arm_ack` 协议保留用于 SimArm 联调(`--robot --sim-arm`)。

## 5. VLM 在闭环里的完整分工 (机械臂时代)

1. **低置信复核**(同步, 仅 YOLO conf∈[0.60,0.85) 时): 一致才采信——错判=0 的双保险;
2. **兜底认牌**(同步, YOLO 弃权时): 黑色人头牌等已知弱项正好是 VLM 强项(实测 5/5);
3. **发牌审计**(异步, 每次发牌后): 顶视整帧"对答案", 独立于认牌链的第三只眼, 抓
   "认对了牌但臂放错了区/漏放"这类**执行层错误**——机械臂时代它的价值更大;
4. **解说**(异步): 已建成;
5. (可选加分) **异常场景描述**: watch 连续超时/审计连续 mismatch 时, Agent 把顶视帧发 VLM
   开放式提问一次("桌面发生了什么异常?"), 结果只进 alert 供操作员参考——
   这是唯一允许开放式提问的场景, 因为它只有建议权。

## 6. 过渡计划 (机械臂调好前)

- 人肉 ShoeGod 已实现同样的流程(系统等亮牌), **今天就能跑通全数据链**;
- 机械臂组给出上表 5 个动作的可调用形态(HTTP/ROS/串口皆可, 我们包一层订阅者)即接入;
- 联调顺序: 空跑指令序列(无牌) → 单张 pick+present+认牌 → 全手; 每步都有人肉降级兜底。
