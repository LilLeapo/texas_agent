"""靴口读牌 · 上帝模式 god_source: shoe 的无机械臂过渡形态。

机械臂就位前由人代劳: 每张牌发出前, 系统提示"亮牌"→ 荷官把牌面对准
读牌相机(机械臂位下方) → VLM 高清认牌(真牌已验 0 错判) → 荷官再按提词发牌。
引擎因此拿到**真实牌面**, 胜率全部按真数据计算, 牌堆无需预排。

纪律:
- 烧牌不亮(分析纪律: 烧牌对信息集永远未知);
- 重复认出已发过的牌 = 误读或重复亮牌 → 要求重亮;
- 不确定/超时 → 网页补录兜底, 永不卡死;
- 机械臂就位后, 本类换成"机械臂过牌位自动亮牌", 接口不变。
"""

from __future__ import annotations

import threading
import time

from .. import cards as C
from ..vlm import UNCERTAIN, VlmCardReader
from .matcher import CardMatcher


class ShoeStream(threading.Thread):
    """后台常驻读牌线程 —— 批次程序臂的旁路识别。

    臂自由跑整段程序(OPENING/C1..C3), 牌逐张飞过 Link2C, 不等任何人;
    主流程若阻塞在网页补录/引擎推进上, 同步读法会漏掉后续过牌。
    本线程独占读牌相机, 按"飞行窗"分段投票, 结果按到达顺序追加进 arrivals,
    消费侧(ShoeGod)凭序号取 —— 到达顺序即物理发牌顺序, 底牌臂序重映射靠它。

    胶着窗(如 Kc:20 vs Ks:17)不在线程内做 VLM 仲裁 —— VLM 秒级会漏下一张,
    存最高置信帧, card=None 交消费侧定夺。
    每窗收尾后排空(等 gap_s 无命中)再开新窗, 同一张牌绝不产生两条记录。
    gap_s=3.0: 弱检出牌(黑花色)中途断帧 ~2s 会把一张牌拆成两窗(2026-07-11
    #6/#7 实测), 臂发牌节奏 ~28s/张, 3s 内绝无第二张 —— 宁慢勿拆。"""

    def __init__(self, cam, yolo, bus=None, gap_s: float = 3.0,
                 max_votes: int = 60, on_push=None):
        super().__init__(daemon=True)
        self.cam, self.yolo, self.bus = cam, yolo, bus
        self.gap_s, self.max_votes = gap_s, max_votes
        self.on_push = on_push           # (arr, n) → 捕获即回调(实时预览等)
        self.arrivals: list[dict] = []   # {card|None, via, votes, conf, frame}
        self.cond = threading.Condition()
        self._halt = False   # 勿命名 _stop: 会遮蔽 Thread._stop, join 时炸
        self.last_frame = None           # 最近一帧, 供 /shoe 网页快照(只读引用)
        self.window_note = "等待过牌"    # 当前窗口状态文本(叠加在快照上)

    def stop(self) -> None:
        self._halt = True

    def run(self) -> None:
        votes: dict[str, int] = {}
        best: dict[str, tuple[float, object]] = {}
        first_hit = last_hit = 0.0
        draining = False                  # 窗已裁决, 等牌离场
        while not self._halt:
            ok, frame = self.cam.read()
            now = time.time()
            if not ok:
                time.sleep(0.05)
                continue
            self.last_frame = frame
            card = self.yolo.read_image(frame)
            hit = card not in (None, UNCERTAIN)
            if draining:
                if hit:
                    last_hit = now
                elif now - last_hit > self.gap_s:
                    draining = False
                    self.window_note = "等待过牌"
                continue
            if hit:
                votes[card] = votes.get(card, 0) + 1
                if self.yolo.last_conf > best.get(card, (0.0, None))[0]:
                    best[card] = (self.yolo.last_conf, frame.copy())
                first_hit = first_hit or now
                last_hit = now
                ranked = sorted(votes.items(), key=lambda kv: -kv[1])[:3]
                self.window_note = "收票: " + " ".join(f"{c}:{n}" for c, n in ranked)
            if first_hit and (now - last_hit > self.gap_s
                              or sum(votes.values()) >= self.max_votes):
                self._push(self.decide_window(votes, best))
                votes, best, first_hit = {}, {}, 0.0
                draining = True
                self.window_note = f"已发布#{len(self.arrivals)}, 排空中"

    @staticmethod
    def decide_window(votes: dict, best: dict) -> dict:
        """窗口裁决(纯函数): 压倒性多数采信 YOLO; 否则 card=None 留帧待仲裁。"""
        ranked = sorted(votes.items(), key=lambda kv: -kv[1])
        votes_s = " ".join(f"{c}:{n}" for c, n in ranked[:3])
        c1, n1 = ranked[0]
        n2 = ranked[1][1] if len(ranked) > 1 else 0
        if n1 >= 6 and n1 >= 0.7 * (n1 + n2):
            conf, frame = best.get(c1, (0.0, None))
            return {"card": c1, "via": "yolo", "votes": votes_s,
                    "conf": conf, "frame": frame}
        top = ranked[:2]
        conf, frame = max((best[c] for c, _ in top if c in best),
                          key=lambda cf: cf[0], default=(0.0, None))
        return {"card": None, "via": None, "votes": votes_s,
                "conf": conf, "frame": frame}

    def _push(self, arr: dict) -> None:
        with self.cond:
            self.arrivals.append(arr)
            n = len(self.arrivals)
            self.cond.notify_all()
        if self.bus:
            what = arr["card"] or "胶着待仲裁"
            self.bus.emit({"type": "agent_trace",
                           "text": f"靴口捕获#{n}: {what} (票 {arr['votes']})"})
        if self.on_push is not None:
            try:
                self.on_push(arr, n)
            except Exception:
                pass    # 预览是锦上添花, 出错不碰识别主链


class ShoeGod:
    def __init__(self, bus, prompter, ops, client, card_cam=None,
                 show_timeout_s: float = 45.0, grab=None, fast_reader=None,
                 arm=None, stream: bool = False, hole_map: list[int] | None = None,
                 board_scan=None, phys_order: list[str] | None = None):
        self.arm = arm                    # ArmClient: 有臂则取牌/亮牌由臂执行
        self.bus = bus
        self.prompter = prompter
        self.ops = ops
        self.card_cam = card_cam          # cv2.VideoCapture (LiveVision.card_cam)
        self.show_timeout_s = show_timeout_s
        self._grab_override = grab        # 测试注入
        self._matcher = CardMatcher()     # 仅用 _align 做"有牌出现"检测
        self.reader = VlmCardReader(client, image_source=lambda z: None)
        self.fast = fast_reader           # YoloCardReader, 毫秒级第一级, 可缺席
        self.verify_below = 0.85          # YOLO 置信低于此值时要求 VLM 复核一致
        self.dealt: set[str] = set()
        self.last_via: str | None = None  # 本张牌的认定来源, 编排器写进 deal.source
        # 流式旁路(批次程序臂): 后台线程独占相机, 本对象只按序消费 arrivals
        self.board_scan = board_scan      # () → {牌面:置信}: 顶视公共牌带全帧多检
        self.hole_map = hole_map          # 引擎第 k 张底牌 → 本阶段第 hole_map[k] 个到达
        self.phys_order = phys_order      # 臂物理发牌顺序(到达序号→牌位, 预览用)
        self._street: str | None = None
        self._phase_base = 0              # 当前街的到达起点(new_phase 时快照)
        self._hole_i = 0
        self._board_i = 0
        self.stream: ShoeStream | None = None
        if stream and card_cam is not None and fast_reader is not None:
            self.stream = ShoeStream(card_cam, fast_reader, bus=bus,
                                     on_push=self._preview)
            self.stream.start()

    def _preview(self, arr: dict, n: int) -> None:
        """捕获即预览: 到达序号→物理牌位已知, 不等引擎按序入账, 立刻推给前端。
        引擎稍后的 game_event(deal) 才是权威记账(人工纠错以它为准)。"""
        if arr.get("card") is None:
            return                        # 胶着窗等仲裁, 不预览半成品
        idx = n - 1 - self._phase_base
        if idx < 0:
            return
        if self._street in (None, "preflop"):
            if not self.phys_order or idx >= len(self.phys_order):
                return
            zone = self.phys_order[idx]
        else:
            base, count = {"flop": (0, 3), "turn": (3, 1),
                           "river": (4, 1)}.get(self._street, (None, 0))
            if base is None or idx >= count:
                return                    # 本街该来的都来了, 多余窗是杂散
            zone = f"C{base + idx + 1}"
        self.bus.emit({"type": "deal_preview", "zone": zone,
                       "card": arr["card"], "via": arr["via"],
                       "votes": arr["votes"]})

    # ---- 相机侧 ----

    def _try_read(self):
        """在超时窗内持续识别一张飞行/亮出的牌, 返回 (card, via)。

        实测(2026-07-11 真臂发牌): 每张牌在途可见 50+ 帧, 但单帧有噪声
        (黑花色 K 混淆 Kc:20 vs Ks:17、偶发点数误读) —— 取首个自信读数会错判。
        因此**多帧投票**: 压倒性多数直接采信; 票数胶着 → 最高置信帧交 VLM 仲裁。"""
        if self._grab_override is not None:               # 测试注入: 单发旧语义
            g = self._grab_override()
            frame, aligned = g if isinstance(g, tuple) else (g, g)
            return self._read_pair(frame, aligned)
        if self.card_cam is None:
            return UNCERTAIN, None
        deadline = time.time() + self.show_timeout_s
        votes: dict[str, int] = {}
        best: dict[str, tuple[float, object]] = {}   # card → (conf, frame)
        first_hit = last_hit = 0.0
        next_vlm = 0.0
        while time.time() < deadline:
            ok, frame = self.card_cam.read()
            if not ok:
                time.sleep(0.05)
                continue
            now = time.time()
            if self.fast is not None:
                card = self.fast.read_image(frame)
                if card not in (None, UNCERTAIN):
                    votes[card] = votes.get(card, 0) + 1
                    if self.fast.last_conf > best.get(card, (0.0,))[0]:
                        best[card] = (self.fast.last_conf, frame.copy())
                    first_hit = first_hit or now
                    last_hit = now
                # 收票结束: 有过命中且 1.2s 无新命中(牌已飞离) 或票够多
                if first_hit and (now - last_hit > 1.2
                                  or sum(votes.values()) >= 60):
                    return self._decide(votes, best)
                continue
            if now >= next_vlm:                       # 无 YOLO: 老路径 VLM 轮询
                aligned = self._matcher._align(frame)
                v = self.reader.read_image(aligned if aligned is not None else frame)
                if v not in (None, UNCERTAIN):
                    return v, "vlm"
                next_vlm = now + 4
        if votes:
            return self._decide(votes, best)
        return UNCERTAIN, None

    def _decide(self, votes: dict, best: dict):
        """投票裁决: 压倒性多数采信 YOLO; 胶着交 VLM 仲裁最高置信帧。"""
        ranked = sorted(votes.items(), key=lambda kv: -kv[1])
        self.last_votes = " ".join(f"{c}:{n}" for c, n in ranked[:3])   # 准确率观测用
        c1, n1 = ranked[0]
        n2 = ranked[1][1] if len(ranked) > 1 else 0
        if n1 >= 6 and n1 >= 0.7 * (n1 + n2):
            return c1, "yolo"
        # 票数胶着(如 Kc:20 vs Ks:17): 取争议双方中置信最高的帧, VLM 定夺
        top = ranked[:2]
        _, frame = max((best[c] for c, _ in top if c in best),
                       key=lambda cf: cf[0], default=(0.0, None))
        v = self.reader.read_image(frame)
        if v not in (None, UNCERTAIN):
            return v, "yolo+vlm"
        return UNCERTAIN, None

    def _read_pair(self, frame, aligned):
        """单发识别(测试路径): YOLO → 低置信复核 → VLM。"""
        card, via = UNCERTAIN, None
        if frame is not None:
            if self.fast is not None:
                card, via = self.fast.read_image(frame), "yolo"
                if card not in (None, UNCERTAIN) and \
                        self.fast.last_conf < self.verify_below:
                    v = self.reader.read_image(aligned)
                    if v == card:
                        via = "yolo+vlm"
                    else:
                        card = UNCERTAIN
            if card in (None, UNCERTAIN):
                card, via = self.reader.read_image(aligned), "vlm"
        return card, via

    def _wait_clear(self, timeout: float = 10.0) -> None:
        """等牌离开读牌相机视野, 防止下一张把同一画面读两遍。
        有 YOLO 用它判"画面里还有没有牌"(切边/手持也认得), 否则退回牌形对齐。"""
        if self.card_cam is None:
            return
        deadline = time.time() + timeout
        misses = 0
        while time.time() < deadline and misses < 3:
            ok, frame = self.card_cam.read()
            if not ok:
                time.sleep(0.1)
                continue
            if self.fast is not None:
                present = self.fast.read_image(frame) != UNCERTAIN
            else:
                present = self._matcher._align(frame) is not None
            misses = misses + 1 if not present else 0
            time.sleep(0.1)

    # ---- 流式消费 (批次程序臂) ----

    def new_phase(self, street: str | None = None) -> None:
        """街转换(编排器在提交臂程序**前**调用): 快照到达起点, 丢弃上一街的
        多余/杂散窗口, 序号从本街第一张重新算 —— 漏识别不会跨街错位。"""
        self._street = street
        if self.stream is None:
            return
        with self.stream.cond:
            self._phase_base = len(self.stream.arrivals)
        self._board_i = 0

    def _stream_take(self, idx: int) -> dict | None:
        """等本阶段第 idx 个到达并返回。

        超时判定看臂: 程序没跑完 → 牌还在路上, 展期继续等(OPENING 全程数分钟,
        重映射后第一张底牌要等臂序第 6 张); 臂回 idle 后仍没等到 → 真没了。"""
        st = self.stream
        deadline = time.time() + self.show_timeout_s
        while True:
            with st.cond:
                if len(st.arrivals) > self._phase_base + idx:
                    return st.arrivals[self._phase_base + idx]
                st.cond.wait(min(2.0, max(0.1, deadline - time.time())))
                if len(st.arrivals) > self._phase_base + idx:
                    return st.arrivals[self._phase_base + idx]
            if time.time() >= deadline:
                if self._arm_busy():
                    deadline = time.time() + self.show_timeout_s
                    continue
                return None

    def _arm_busy(self) -> bool:
        try:
            idle = getattr(self.arm, "_idle", None)
            return bool(idle and not idle())
        except Exception:
            return False    # 臂状态查不到时不无限等

    def _board_scan_new(self, timeout: float = 30.0) -> str | None:
        """顶视公共牌带全帧多检, 集合差找出"新出现的那张明牌"。

        不按横坐标对应 C 序号(落点漂移会张冠李戴): 检出牌 − 已发牌 = 新牌。
        牌从过牌位飞到落位还要几秒, 轮询等它; 恰好一张新牌才采信。
        放弃时把看到的集合写日志: "没检出"和"集合对不上"(此前某张仲裁判错花色
        会多出一张'新牌')是两种完全不同的病, 一眼可判。"""
        deadline = time.time() + timeout
        new: list[str] = []
        found: dict = {}
        while time.time() < deadline:
            try:
                found = self.board_scan() or {}
            except Exception:
                found = {}
            new = [c for c in (C.normalize(k) for k in found)
                   if c not in self.dealt]
            if len(new) == 1:
                return new[0]
            time.sleep(2)
        self.bus.emit({"type": "agent_trace",
                       "text": f"顶视扫描放弃: 检出 {sorted(found)} "
                               f"其中新牌 {sorted(new)} (需恰好1张)"})
        return None

    def _next_from_stream(self, kind: str | None) -> str | None:
        if kind == "burn":                # 批次流程无实体烧牌, 保险分支
            self.last_via = None
            return None
        if kind == "hole":
            k = self._hole_i
            self._hole_i += 1
            idx = self.hole_map[k] if self.hole_map else k
        else:
            idx = self._board_i
            self._board_i += 1
        arr = self._stream_take(idx)
        card, via, note = None, None, ""
        if arr is None:
            self.bus.emit({"type": "alert",
                           "text": f"过牌位未见本街第{idx + 1}张牌: "
                                   f"疑似空靴/空取或漏识别"})
            note = "过牌位超时未见牌"
        else:
            card, via = arr["card"], arr["via"]
            if card is None:              # 胶着窗 → 消费侧 VLM 仲裁最高置信帧
                v = self.reader.read_image(arr["frame"])
                if v not in (None, UNCERTAIN):
                    card, via = v, "yolo+vlm"
                else:
                    note = f"胶着且 VLM 未定 (票 {arr['votes']})"
            if card is not None and card in self.dealt:
                note = f"识别为 {card} 但已发过, 疑似误读 (票 {arr['votes']})"
                card = None
        if card is None and kind != "hole" and self.board_scan is not None:
            # 公共牌明置于顶视之下: 静置扫描远比飞行帧可靠, 人工前先试它
            self.bus.emit({"type": "agent_trace",
                           "text": f"飞行识别未定({note}), 顶视静置扫描公共牌带…"})
            card = self._board_scan_new()
            if card is not None:
                via = "top-yolo"
        if card is None:
            card, via = self.ops.ask_card(f"靴口认牌失败({note}), 人工输入这张牌"), "ops"
        card = C.normalize(card)
        self.dealt.add(card)
        self.last_via = via
        self.bus.emit({"type": "agent_trace",
                       "text": f"靴口识别: {card} ({via}"
                               f"{', 票 ' + arr['votes'] if arr else ''}"
                               f"{', 臂序第' + str(idx + 1) + '张' if kind == 'hole' and self.hole_map else ''})"})
        return card

    # ---- god 协议 ----

    def next_card(self, kind: str | None = None) -> str | None:
        if self.stream is not None:
            return self._next_from_stream(kind)
        if kind == "burn":
            self.last_via = None
            if self.arm is not None and self.arm.alive:
                self.arm.pick_from_deck(present=False)   # 烧牌: 取不亮, 放 MUCK 由编排器指挥
            return None                   # 烧牌不亮不读
        armed = False
        if self.arm is not None and self.arm.alive:
            armed = self.arm.pick_from_deck() and self.arm.present_to_camera()
        if not armed:                     # 无臂/臂失败: 人肉亮牌, 演示不死
            self.prompter.prompt("亮牌: 将下一张牌面对准读牌相机")
        fails = 0
        while True:
            card, via = self._try_read()
            if card in (None, UNCERTAIN):
                fails += 1
                if fails >= 2:                            # 三级: 网页补录
                    card, via = self.ops.ask_card("靴口认牌失败, 人工输入这张牌"), "ops"
                else:
                    self.prompter.prompt("没认出来, 请把牌拿稳、正对相机再亮一次",
                                         level="again")
                    continue
            card = C.normalize(card)
            if card in self.dealt:
                self.prompter.prompt(f"识别到 {card} 但这张已发过(疑似重读), 请重新亮牌",
                                     level="again")
                self._wait_clear()
                continue
            self.dealt.add(card)
            self.last_via = via
            votes = getattr(self, "last_votes", "")
            self.bus.emit({"type": "agent_trace",
                           "text": f"靴口识别: {card} ({via}"
                                   f"{', 票 ' + votes if votes else ''})"})
            self.last_votes = ""
            self._wait_clear()
            return card
