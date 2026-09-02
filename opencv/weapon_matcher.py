"""武器模板匹配。每轮实时识别（spec §9）。"""

from __future__ import annotations

from config import AppConfig
from .template_matcher import TemplateMatcher


class WeaponMatcher(TemplateMatcher):
    # 武器图标实际比 ROI 矮（约 0.5~0.7 倍），且宽模板多，需更宽尺度 + 宽度钳制。
    # 0.05 步长：粗尺度（0.1 步）在非标定分辨率下模板高度 int 取整量化偏差会翻转匹配赢家
    # （实测 1366x768/1440x810 slot3 Bulldog 被误配 Ghost、1536x864/1600x900 slot8 Bandit
    #  被误配 Classic），细尺度让正确模板总能贴合图标（0.05 全分辨率实测全对）。
    _SCALES = [0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.15, 1.2]
    # 纹理门控（min_std）：死亡玩家空武器槽是均匀暗区（std ≤11），真实武器必有纹理
    # （std ≥29.4，跨帧实测）。亮度门控不可靠：真实武器亮度随场景光照漂移
    # （89.6~133 与 75.7~113.5 两帧不同），78 的门控会误杀变暗帧的真实武器，
    # 而 std 只反映「有没有内容」，不受整体光照影响。空槽均匀区上 CCOEFF_NORMED
    # 会出虚假高分（实测空槽最高 0.703 压过阈值 0.7），门控 std<20 直接判空。
    _MIN_STD = 20

    def __init__(self, cfg: AppConfig):
        super().__init__(
            cfg.resolve(cfg.templates.weapons_dir),
            suffix=cfg.templates.weapons_suffix,
            scale_factors=self._SCALES,
            match_threshold=cfg.templates.match_threshold,
            min_std=self._MIN_STD,
        )
