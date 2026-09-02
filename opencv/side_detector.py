"""攻守方识别：基于同队玩家头像侧色环的红/蓝像素比例（spec §7）。

对一队（1~5 或 6~10）5 名玩家的侧色环 ROI 分别统计红/蓝像素，累加后判攻守方。
单块 ROI 的色环可能被头像/背景遮挡导致比例失真（实测某帧 slot6 红蓝占比都 <15%），
聚合整队后信号更稳。换边后自动重新检测，因此必须在每轮实时循环里执行。
"""

from __future__ import annotations

import cv2
import numpy as np

from config import AppConfig


def _hue_mask(h, ranges):
    mask = np.zeros(h.shape, dtype=bool)
    for lo, hi in ranges:
        if lo <= hi:
            mask |= (h >= lo) & (h <= hi)
        else:  # 跨 0 度区间（如 165~180 与 0~15）
            mask |= (h >= lo) | (h <= hi)
    return mask


class SideDetector:
    def __init__(self, cfg: AppConfig):
        self._attack_ranges = [tuple(r) for r in cfg.side.attack_hue_ranges]
        self._defense_ranges = [tuple(r) for r in cfg.side.defense_hue_ranges]

    def detect(self, rois) -> tuple[str | None, float]:
        """聚合同队玩家侧色环 ROI 判攻守方。rois 可传入单块 ROI 或列表。

        返回 (side, confidence)。side ∈ {"attack","defense"}；不可靠时 value=None。
        """
        if isinstance(rois, np.ndarray):
            rois = [rois]
        red_n = blue_n = px_n = 0
        for roi in rois:
            if roi is None or roi.size == 0:
                continue
            hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
            h = hsv[:, :, 0]
            sat = (hsv[:, :, 1] > 60) & (hsv[:, :, 2] > 60)
            red_n += int((_hue_mask(h, self._attack_ranges) & sat).sum())
            blue_n += int((_hue_mask(h, self._defense_ranges) & sat).sum())
            px_n += roi.shape[0] * roi.shape[1]

        if px_n == 0:
            return None, 0.0
        red_frac = red_n / px_n
        blue_frac = blue_n / px_n
        total = red_frac + blue_frac
        if total <= 0:
            return None, 0.0
        side = "attack" if red_frac > blue_frac else "defense"
        return side, min(1.0, abs(red_frac - blue_frac) / total)
