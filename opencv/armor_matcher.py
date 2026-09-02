"""护甲模板匹配。每轮实时识别（spec §10）。

策略（实测确定）：
- 纹理门控（唯一判空）：空槽是均匀区域，灰度 std ≤10；真实护甲图标有纹理，std ≥47。
  门控 20 干净排除空槽。亮度门控不可靠：
  - 空槽可能是高亮均匀区（std=4.2 但均值 114 的 slot2 被误配成 Heavy Armor）；
  - 真护甲可能偏暗（slot4 均值 61.3、std 70.9 被 min_brightness=65 误杀）。
  std 同时避开这两类，是唯一判空依据。
- 类型判别用 CCOEFF + 128 灰填充：Heavy/Regen 0.85+、Light ~0.56，判别最稳。
- upscale=2：护甲图标 ~30px 太小，放大 ROI 提升小图标区分度，低分辨率窗口采集也不掉置信度。
"""

from __future__ import annotations

from config import AppConfig
from .template_matcher import TemplateMatcher


class ArmorMatcher(TemplateMatcher):
    _SCALES = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2]

    def __init__(self, cfg: AppConfig):
        super().__init__(
            cfg.resolve(cfg.templates.armor_dir),
            suffix=cfg.templates.armor_suffix,
            scale_factors=self._SCALES,
            match_threshold=cfg.templates.match_threshold,
            upscale=2,
            min_std=20,
        )
