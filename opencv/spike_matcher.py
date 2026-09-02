"""爆能器图标匹配：在计分板左侧竖条内定位爆能器图标。

竖条很窄（约 40px）、很高（约 400px），爆能器图标约 20~30px。
把 Spike.png（128×128）缩放到 ROI 高度的 4%~9% 后跑 matchTemplate（TM_CCOEFF_NORMED），
返回最佳匹配的置信度与匹配中心 y 的归一化坐标（0~1），由引擎映射到玩家行。

- 爆能器竖条是游戏画面背景（非计分板），不做亮度/纹理门控（背景多变不可预测），
  靠阈值过滤误配。阈值在 config.recognition.spike_threshold。
- 2x 放大后再匹配：小图标放大后区分度显著提高。
"""

from __future__ import annotations

import os

import cv2

from config import AppConfig


class SpikeMatcher:
    # 相对 ROI 高的小尺度：图标约为 ROI 高度的 4%~9%（2x 放大后按放大高计算）
    _SCALES = [0.04, 0.05, 0.06, 0.07, 0.08, 0.09]
    _UPSAMPLE = 2

    def __init__(self, cfg: AppConfig):
        rel = os.path.join(cfg.templates.other_dir, cfg.recognition.spike_template)
        path = cfg.resolve(rel)
        img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if img is None:
            raise RuntimeError(f"爆能器模板缺失: {path}")
        if img.ndim == 3 and img.shape[2] == 4:
            alpha = img[:, :, 3]
            gray = cv2.cvtColor(img[:, :, :3], cv2.COLOR_BGR2GRAY)
            gray[alpha < 40] = 128  # 透明角落填中灰，避免黑色主导匹配
        elif img.ndim == 3:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        else:
            gray = img
        self._tmpl = gray
        self._threshold = cfg.recognition.spike_threshold
        self._cache: dict[int, list] = {}

    def match(self, roi) -> tuple[bool, float, float]:
        """对竖条 ROI 匹配，返回 (是否找到, 置信度, 匹配中心 y 归一化 0~1)。

        roi 为 BGR 帧裁剪的竖条；同高度的缩放模板结果跨帧缓存。
        """
        if roi is None or roi.size == 0:
            return False, 0.0, 0.0
        if self._UPSAMPLE > 1:
            rh, rw = roi.shape[:2]
            roi = cv2.resize(roi, (rw * self._UPSAMPLE, rh * self._UPSAMPLE), interpolation=cv2.INTER_CUBIC)
        roi_gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        roi_h, roi_w = roi_gray.shape

        scaled = self._cache.get(roi_h)
        if scaled is None:
            scaled = []
            seen_wh = set()
            for scale in self._SCALES:
                h = int(roi_h * scale)
                if h < 8:
                    continue
                w = max(1, int(self._tmpl.shape[1] * h / max(1, self._tmpl.shape[0])))
                if w > roi_w or h > roi_h:
                    continue
                if (w, h) in seen_wh:
                    continue
                seen_wh.add((w, h))
                scaled.append(cv2.resize(self._tmpl, (w, h), interpolation=cv2.INTER_AREA))
            self._cache[roi_h] = scaled

        best_conf, best_y = 0.0, 0.0
        for resized in scaled:
            try:
                result = cv2.matchTemplate(roi_gray, resized, cv2.TM_CCOEFF_NORMED)
            except Exception:
                continue
            _, max_val, _, max_loc = cv2.minMaxLoc(result)
            if max_val > best_conf:
                best_conf = float(max_val)
                best_y = max_loc[1] + resized.shape[0] // 2  # 中心 y（放大后坐标）
        found = best_conf >= self._threshold
        cy_norm = best_y / roi_h if roi_h > 0 else 0.0
        return found, best_conf, cy_norm
