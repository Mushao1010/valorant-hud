"""玩家存活识别：头像 ROI 亮度（HSV V 通道均值）。

实测两批图（origional.png / text1.png，共 18 个样本）：
  死亡头像 V ≤ 82，存活头像 V ≥ 94，中间 12 个单位为歧义区。
落在歧义区返回 None（置信 0），由引擎的 alive_threshold 门控。
置信度 = 0.5 基准 + 偏离边界距离的一半，越靠近歧义区越不确定。
"""

from __future__ import annotations

import cv2

from config import AppConfig


class AliveDetector:
    def __init__(self, cfg: AppConfig):
        self._dead_max = cfg.alive.dead_max_v
        self._alive_min = cfg.alive.alive_min_v

    def detect(self, roi) -> tuple[bool | None, float]:
        """返回 (alive, confidence)。落在歧义区或无法判断时 alive=None。"""
        if roi is None or roi.size == 0:
            return None, 0.0
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        mean_v = float(cv2.mean(hsv)[2])
        gap = self._alive_min - self._dead_max
        if mean_v <= self._dead_max:
            conf = min(1.0, 0.5 + (self._dead_max - mean_v) / (2 * gap))
            return False, conf
        if mean_v >= self._alive_min:
            conf = min(1.0, 0.5 + (mean_v - self._alive_min) / (2 * gap))
            return True, conf
        return None, 0.0
