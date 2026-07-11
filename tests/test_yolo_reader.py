"""YOLO 读牌解码: 高置信取牌、双牌同框不猜、无检测=不确定。"""

import numpy as np
import pytest

pytest.importorskip("cv2")

from texas_agent.vision.yolo_reader import CLASSES, YoloCardReader


class FakeSession:
    def __init__(self, pred):
        self.pred = pred

    def get_inputs(self):
        class I:
            name = "images"
        return [I()]

    def run(self, _, feeds):
        return [self.pred]


def make_pred(*hits):
    """hits: (class_name, conf) → (1, 56, 8400) 输出张量。"""
    pred = np.zeros((1, 56, 8400), np.float32)
    for i, (name, conf) in enumerate(hits):
        col = 100 + i * 50
        pred[0, :4, col] = [320, 320, 40, 60]
        pred[0, 4 + CLASSES.index(name), col] = conf
    return pred


def read(pred):
    r = YoloCardReader(session=FakeSession(pred))
    return r.read_image(np.zeros((480, 640, 3), np.uint8)), r.last_conf


def test_confident_single_card():
    card, conf = read(make_pred(("AS", 0.91), ("AS", 0.88)))  # 一张牌两个角标
    assert card == "As" and conf == pytest.approx(0.91, abs=1e-3)


def test_ten_class_maps_to_T():
    card, _ = read(make_pred(("10H", 0.85)))
    assert card == "Th"


def test_two_different_cards_uncertain():
    card, _ = read(make_pred(("AS", 0.9), ("KD", 0.8)))
    assert card == "uncertain"      # 双牌同框, 不猜


def test_low_confidence_uncertain():
    card, _ = read(make_pred(("QC", 0.4)))
    assert card == "uncertain"


def test_no_detection_uncertain():
    card, _ = read(make_pred())
    assert card == "uncertain"
