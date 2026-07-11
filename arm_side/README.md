# 机械臂三机联调手册 (Panthera 协调服务器 ↔ 牌桌智脑)

> 机械臂组的协调服务器包在 `panthera_package/`(部署说明见其中 HANDOFF 文档)。
> 本文件补充**牌桌智脑侧的对接约定**与双方要各自完成的事。

## 架构 (定稿)

```
牌桌智脑(Spark, PantheraArm 适配器) --POST /api/run--> 机械臂电脑(:5100, 唯一控臂者)
                 |                                        |--SSE--> 灵巧手电脑(夹/放)
                 └----认牌完成后 POST /api/hand/status done (放行 camera 窗口)----┘
```

**阶段级宏观原语** —— 每个发牌阶段一条完整 run, 臂+手自主执行, 每阶段只回零一次:

| 原语 | run 序列 | Agent 介入点 |
|---|---|---|
| 发手牌 | `[hover,pick,camera,P区]×N人×2` | 每个 camera 窗口认牌后回 done |
| 翻牌 | `[hover,pick,MUCK]` + `[hover,pick,camera,C1..C3]` | 3 个 camera 窗口 |
| 转牌/河牌 | `[hover,pick,MUCK]` + `[hover,pick,camera,C4/C5]` | 1 个 camera 窗口 |

公共牌**直接以正面姿态放下**(C 区点位手腕姿态决定), 无翻牌动作。

## 机械臂组要做的

1. 按 HANDOFF 部署服务器、录制轨迹(建议两个文件: A=流程点, B=14 个牌区点)
2. **点位姿态要求**: camera 点=牌面正对读牌相机 20~30cm; C1~C5=放下后牌面朝上;
   P 区/MUCK=面朝下; home/零位不遮顶视相机
3. 把 `GET /api/library` 的引用表(A1/B3...)发给牌桌智脑组填 `config/arm_points.yaml`
4. 告知 Windows 的 LAN IP + api_token

## 灵巧手电脑要做的 (⚠ 关键契约)

在 `hand_event_client.py` 的 `run_linker_hand_action(event)` 里按**引用名**分发
(监听 segment_start 事件取 reference):

| 引用(点位) | 动作 |
|---|---|
| deck_pick 对应引用 | pinch 夹牌, 完成回 done |
| 各牌区/MUCK 对应引用 | release 松牌(+open), 完成回 done |
| **camera 对应引用** | **什么都不做、也不回 done** —— 这一步的 done 由 Spark 认牌后发 |
| 其余所有步 | 立即回 done |

> wait_for_hand_done 模式下服务器对**每一步**都等 done, 漏回会卡到超时。

## 牌桌智脑侧命令 (Spark)

```bash
cp config/arm_points.example.yaml config/arm_points.yaml   # 填好引用映射
bash tools/run_live.sh --robot \
  --arm-url http://<Windows_LAN_IP>:5100 --arm-token <TEAM_SECRET>
```

失败语义: 任何 run error/stopped/超时 → 该动作 False → 智脑 alert + 人肉提词接管,
连续 2 败标记臂离线整手跳过 —— **臂罢工演示不死**。软停止: 服务器 `/api/stop`。

## 联调顺序

1. 服务器起好 → Spark: `curl -H 'X-Panthera-Token: ..' http://<IP>:5100/api/health`
2. 灵巧手电脑跑 `hand_event_client.py --auto-done`(先不接真手)
3. Spark 跑 `--robot --arm-url ...` 空跑一手(桌上无牌), 看 29 条指令流全过
4. 灵巧手接真动作(pinch/release 映射) → 上牌整手
