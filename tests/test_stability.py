"""稳定门: 变化像素占比 + 快/慢双重比对 —— 快挥的手和慢爬的机械臂都要拦住。"""

import numpy as np

from texas_agent.vision.stability import StabilityGate


def frame(noise_seed=0):
    rng = np.random.RandomState(noise_seed)
    base = np.full((600, 900, 3), 60, np.uint8)
    return base + rng.randint(0, 4, base.shape).astype(np.uint8)  # 传感器级噪声


def settle(g, t0, n=16, dt=0.15):
    """连续喂静止帧直到门放行, 返回最后时刻。"""
    t = t0
    for i in range(n):
        t = t0 + i * dt
        g.feed(frame(i), now=t)
    return t


def test_static_scene_becomes_still():
    g = StabilityGate(hold_s=0.8)
    t = settle(g, 100.0)
    assert g.feed(frame(99), now=t + 0.2) is True    # 静止场景最终放行


def test_fast_arm_blocks_judgement():
    g = StabilityGate(hold_s=0.8)
    t = settle(g, 100.0)
    arm = frame(50)
    arm[200:400, 300:600] = 180                      # 手臂扫入: 大块像素突变
    assert g.feed(arm, now=t + 0.1) is False         # 立即判"在动"
    assert g.feed(arm.copy(), now=t + 0.2) is False  # 停住: hold 重新计时
    # 手臂停稳 + 与 0.8s 前(也是手臂帧)一致后才重新放行
    assert g.feed(arm.copy(), now=t + 1.2) is False  # 0.8s 前还是无臂帧 → 慢比对拦住
    assert g.feed(arm.copy(), now=t + 2.2) is True   # 前后都是停稳的手臂 → 放行


def test_slow_creeping_arm_blocked_by_history():
    """机械臂慢速爬行: 逐帧位移小骗过帧差, 但对 0.8s 前的位移藏不住。"""
    g = StabilityGate(hold_s=0.8)
    t = settle(g, 100.0)
    for i in range(12):                              # 每帧只挪 5px, 帧差 <1%
        creep = frame(60 + i)
        x = 100 + i * 5
        creep[200:500, x:x + 200] = 180
        assert g.feed(creep, now=t + 0.1 + i * 0.1) is False


def test_single_card_change_counts_as_motion():
    """一张牌(约占桌垫 1%)落下的那一瞬也算"在动", 落定后才判。"""
    g = StabilityGate(hold_s=0.8)
    t = settle(g, 100.0)
    card = frame(70)
    card[250:345, 400:470] = 240                     # 63×88mm 牌 @1px/mm
    assert g.feed(card, now=t + 0.1) is False
