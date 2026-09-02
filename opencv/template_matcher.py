"""通用多尺度模板匹配器。

模板都是大图（agent 图标 256×256、武器 ~420×128），而 ROI 很小，
所以把模板缩放到 ROI 高度后，再按尺度金字塔跑 matchTemplate（TM_CCOEFF_NORMED）。
带 alpha 通道的透明模板：透明角落读进来是黑色，会主导匹配结果，
加载时把 alpha<40 的像素填 128 中灰。

- upscale>1 时先把 ROI 放大再匹配，小 ROI（如 46×46 干员头像）放大后区分度显著提高。
- min_brightness 设置后先做亮度门控：ROI 灰度均值低于阈值直接判空，
  用于空槽（死亡玩家行）显著偏暗、模板会把暗背景误配成装备的场景。
- min_std 设置后先做纹理门控：ROI 灰度标准差低于阈值直接判空。
  空槽是均匀暗区（std 很低），武器/护甲图标必有纹理（std 明显更高），
  且 std 不受场景整体光照影响（亮度会随光照漂移，纹理不会）——比亮度门控更稳。
"""

from __future__ import annotations

import glob
import os

import cv2


class TemplateMatcher:
    def __init__(
        self,
        template_dir: str,
        suffix: str = "",
        scale_factors=None,
        match_threshold: float = 0.8,
        upscale: int = 1,
        min_brightness: float | None = None,
        min_std: float | None = None,
    ):
        self._templates: dict[str, list[object]] = {}
        self.scale_factors = scale_factors or [0.8, 0.9, 1.0, 1.1, 1.2]
        self.match_threshold = match_threshold
        self.upscale = max(1, int(upscale))
        self.min_brightness = min_brightness
        self.min_std = min_std
        self._scaled_cache: dict[tuple[int, int], list[tuple[str, object]]] = {}
        self._restricted: set[str] | None = None
        self._load(template_dir, suffix)

    def _load(self, template_dir: str, suffix: str) -> None:
        self._templates = self._load_agg(template_dir, suffix)

    def _load_agg(self, template_dir: str, suffix: str) -> dict[str, list[object]]:
        """扫描模板目录，返回 {agent名: [灰度图, ...]}。

        同一 agent 允许多张变体模板共存：与模板名同名的子目录里的所有 png
        都归属该 agent（如 Brimstone/ 子目录与顶层 Brimstone_icon.png 同为
        "Brimstone"）。用于同一图标存在不同渲染形态（进攻/防守行背景不同等），
        匹配时对该 agent 的多张模板取最高分（best-of），不互相覆盖。
        """
        agg: dict[str, list[object]] = {}
        if not os.path.isdir(template_dir):
            return agg

        def add(path: str, name: str) -> None:
            if not name:
                return
            img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
            if img is None:
                return
            if img.ndim == 3 and img.shape[2] == 4:
                alpha = img[:, :, 3]
                gray = cv2.cvtColor(img[:, :, :3], cv2.COLOR_BGR2GRAY)
                gray[alpha < 40] = 128  # 透明角落填中灰，避免黑色主导
            elif img.ndim == 3:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            else:
                gray = img
            agg.setdefault(name, []).append(gray)

        # 顶层 png：文件名 → agent 名
        for path in sorted(glob.glob(os.path.join(template_dir, "*.png"))):
            base = os.path.basename(path)
            if suffix and base.endswith(suffix + ".png"):
                name = base[: -len(suffix + ".png")]
            else:
                name = base[: -len(".png")]
            add(path, name.replace("_", " ").strip())

        # 子目录（目录名 = agent 名）：目录内所有 png 是该 agent 的变体模板
        for sub in sorted(os.listdir(template_dir)):
            sub_path = os.path.join(template_dir, sub)
            if not os.path.isdir(sub_path):
                continue
            sub_name = sub.replace("_", " ").strip()
            if not sub_name:
                continue
            for path in sorted(glob.glob(os.path.join(sub_path, "*.png"))):
                add(path, sub_name)
        return agg

    def add_templates(self, template_dir: str, suffix: str = "") -> None:
        """追加加载模板目录；同名 agent 用新目录的模板整体替换（含其全部变体）。

        用于把「匹配专用模板」（如实机紧框提取的干员模板）与「展示用美术图」
        分开：展示头像读 agents_dir 的美术，匹配用这里覆写的紧框模板。
        """
        new = self._load_agg(template_dir, suffix)
        if not new:
            return
        self._scaled_cache.clear()
        for name, grays in new.items():
            self._templates[name] = grays  # 同名 agent 整体替换（保留其它 agent）

    def __len__(self) -> int:
        return sum(len(v) for v in self._templates.values())

    def set_restriction(self, names: set[str] | None) -> None:
        """限制后续 match 只匹配名单内的模板名（None 取消限制）。"""
        self._restricted = names

    def match(self, roi) -> tuple[str | None, float]:
        """对 ROI 匹配，返回 (名称, 置信度)。ROI 为 BGR 帧。

        同一 (ROI 高, ROI 宽) 的模板缩放结果跨帧缓存：
        计分板每行高度固定，10 个 slot 只有 10 种尺寸，避免每帧重复 resize 大模板。
        """
        if roi is None or roi.size == 0 or not self._templates:
            return None, 0.0
        if self.upscale > 1:
            rh, rw = roi.shape[:2]
            roi = cv2.resize(roi, (rw * self.upscale, rh * self.upscale), interpolation=cv2.INTER_CUBIC)
        roi_gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        # 亮度门控：空槽 ROI 显著偏暗，先排除，避免模板把暗背景误配成装备
        if self.min_brightness is not None and float(roi_gray.mean()) < self.min_brightness:
            return None, 0.0
        # 纹理门控：空槽是均匀暗区（std 很低），先排除。std 比亮度更抗光照漂移
        if self.min_std is not None and float(roi_gray.std()) < self.min_std:
            return None, 0.0
        roi_h, roi_w = roi_gray.shape

        scaled = self._scaled_cache.get((roi_h, roi_w))
        if scaled is None:
            scaled = []
            for name, grays in self._templates.items():
                for tmpl in grays:
                    seen_wh = set()
                    for scale in self.scale_factors:
                        h = int(roi_h * scale)
                        if h < 8:
                            continue
                        w = max(1, int(tmpl.shape[1] * h / max(1, tmpl.shape[0])))
                        # 宽模板（如狙击枪）按高度缩放会超出 ROI 宽度，改为按宽度钳制、保持比例
                        if w > roi_w:
                            w = roi_w
                            h = max(1, int(tmpl.shape[0] * w / max(1, tmpl.shape[1])))
                        if h > roi_h or h < 8:
                            continue
                        # 宽模板被钳制后多个尺度落到同一尺寸，去重避免重复 matchTemplate
                        if (w, h) in seen_wh:
                            continue
                        seen_wh.add((w, h))
                        scaled.append((name, cv2.resize(tmpl, (w, h), interpolation=cv2.INTER_AREA)))
            self._scaled_cache[(roi_h, roi_w)] = scaled

        candidates = scaled if self._restricted is None else [(n, r) for n, r in scaled if n in self._restricted]
        if not candidates:
            return None, 0.0

        best_name, best_conf = None, 0.0
        for name, resized in candidates:
            try:
                result = cv2.matchTemplate(roi_gray, resized, cv2.TM_CCOEFF_NORMED)
            except Exception:
                continue
            _, max_val, _, _ = cv2.minMaxLoc(result)
            if max_val > best_conf:
                best_conf = float(max_val)
                best_name = name
        return best_name, best_conf
