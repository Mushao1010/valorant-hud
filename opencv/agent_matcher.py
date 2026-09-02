"""干员模板匹配。只在初始化阶段识别一次（spec §6.4）。"""

from __future__ import annotations

from config import AppConfig
from .template_matcher import TemplateMatcher


class AgentMatcher(TemplateMatcher):
    # 干员头像 ROI 只有 ~46×46：2x 放大 + 更宽的尺度金字塔区分度最好
    _SCALES = [0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3]

    def __init__(self, cfg: AppConfig):
        super().__init__(
            cfg.resolve(cfg.templates.agents_dir),
            suffix=cfg.templates.agents_suffix,
            scale_factors=self._SCALES,
            match_threshold=cfg.templates.match_threshold,
            upscale=2,
        )
        # 匹配专用覆写模板：同名覆盖 agents_dir 的模板（仅匹配用，不碰展示头像）。
        # 展示头像读 agents_dir 美术图，匹配模板可另存实机紧框提取的版本。
        override = cfg.templates.agents_override_dir
        if override:
            self.add_templates(cfg.resolve(override), cfg.templates.agents_suffix)
