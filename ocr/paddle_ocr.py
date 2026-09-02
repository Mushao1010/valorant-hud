"""PaddleOCR 封装。

- 单例实例，只在识别线程创建/调用（PaddleOCR 不支持并发 predict）。
- 启动时预热，避免首帧卡顿。
- 数字区域 OCR 后做正则过滤，只保留数字（spec §11）。
- batch_ocr_digits 把多个数字 ROI 拼成合成图一次 predict，映射回各 ROI。
"""

from __future__ import annotations

import math
import re

import cv2
import numpy as np

from config import OCRConfig


class PaddleOcrEngine:
    # 合成图放大倍数：2x 是耗时/恢复率平衡点
    _UPSCALE = 2
    # 单格回退放大倍数：多数小字 2x 稳定读出（如 slot1 deaths 的 9），个别字形只在 5x 读出
    # （如 slot7 deaths 的 12 在 2x 全空、5x 读 0.52）。回退先 2x 后 5x。
    _FALLBACK_UPSCALE = 5
    _RETRY_CONF = 0.6  # 低于此置信度的合成图结果触发单格重试（略高于 digit_threshold 0.5）
    _MAX_SINGLE_FALLBACK = 12

    def __init__(
        self,
        cfg: OCRConfig,
        digit_regex: str = r"^\d{1,3}$",
        credits_regex: str = r"^\d{1,5}$",
    ):
        self._cfg = cfg
        self._digit_re = re.compile(digit_regex)
        self._credits_re = re.compile(credits_regex)
        # 大招点数恒为单数字（0~9，max 来自干员文件）。720p 下 11px 小格会被 rec 读成
        # "186"/"456" 之类的多位噪声，单数字正则直接拒绝，杜绝不可能值。
        self._ult_re = re.compile(r"^\d$")
        self._batch = cfg.batch_rois
        from paddleocr import PaddleOCR

        kw = {}
        if getattr(cfg, "det_box_thresh", None):
            kw["text_det_box_thresh"] = cfg.det_box_thresh
        self._ocr = PaddleOCR(
            device=cfg.device,
            text_detection_model_name=cfg.detection_model,
            text_recognition_model_name=cfg.recognition_model,
            use_doc_orientation_classify=cfg.use_doc_orientation_classify,
            use_doc_unwarping=cfg.use_doc_unwarping,
            use_textline_orientation=cfg.use_textline_orientation,
            **kw,
        )

    def warmup(self) -> None:
        dummy = np.zeros((64, 256, 3), dtype=np.uint8)
        for _ in range(self._cfg.warmup_calls):
            self._ocr.predict(dummy)

    def predict_texts(self, frame) -> list[tuple[str, float]]:
        """返回 [(text, confidence), ...]（按检测框顺序）。"""
        res = self._ocr.predict(frame)[0]
        texts = res["rec_texts"]
        scores = res["rec_scores"]
        return [(str(t), float(s)) for t, s in zip(texts, scores)]

    def _normalize_digits(self, text: str) -> str:
        # 数字单元格常被 OCR 出 `=1600`、`1.050`（前导 `=`/`.` 噪点），
        # 直接剥离所有非数字字符，避免严格正则全匹配失败
        return re.sub(r"[^\d]", "", text)

    def ocr_digits(self, frame, credits: bool = False) -> tuple[int | None, float]:
        """数字区域 OCR：剥离逗号/空格后只保留纯数字，返回 (int|None, confidence)。

        credits=True 用经济值正则（允许 4~5 位），否则用 K/D/A 正则（≤3 位）。
        """
        if frame is None:
            return None, 0.0
        pattern = self._credits_re if credits else self._digit_re
        best, best_conf = None, 0.0
        for text, score in self.predict_texts(frame):
            normalized = self._normalize_digits(text)
            if not re.fullmatch(pattern, normalized):
                continue
            if score > best_conf:
                best, best_conf = int(normalized), score
        return best, best_conf

    def _upscale(self, roi, scale: int | None = None):
        """放大数字 ROI（INTER_CUBIC），小号数字识别率显著提升。"""
        scale = scale or self._UPSCALE
        h, w = roi.shape[:2]
        return cv2.resize(roi, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)

    def ocr_name(self, frame) -> tuple[str | None, float]:
        """名称区域 OCR：取最高置信度文本。"""
        if frame is None:
            return None, 0.0
        best, best_conf = None, 0.0
        for text, score in self.predict_texts(frame):
            text = text.strip()
            if not text:
                continue
            if score > best_conf:
                best, best_conf = text, score
        return best, best_conf

    def batch_ocr_digits(
        self, rois: list[np.ndarray], credits: bool = False, upscale: int | None = None
    ) -> list[tuple[int | None, float]]:
        """批量识别数字 ROI，返回与 rois 等长的 [(value, confidence)]。

        拼接成带留白的合成图一次 predict，再按检测框中心点映射回各 ROI。
        upscale 可单独指定（KDA 用 2x，大号数字的 credits 用 1x 就够，更快）。

        注意：不要为了"提高检出率"而把格子拆成小批合成图——实测拆成 6 格一批后，小合成图
        里相邻数字的检测框会合并/串扰，把值读错（0→10、1→14、9→19、12→2）。30 格一批
        虽然检出率只有 ~70%，但读出的值都是对的，漏检交给下面的单格回退补齐。
        """
        upscale = upscale or self._UPSCALE
        if not self._batch or len(rois) <= 1:
            return [self.ocr_digits(self._upscale(r, upscale), credits=credits) for r in rois]

        upscaled = [self._upscale(r, upscale) for r in rois]
        # 大留白：pad 太小会导致相邻单元格数字被检测框合并/跨格串扰
        pad = 16 * upscale
        cell_h = max(r.shape[0] for r in upscaled) + 2 * pad
        cell_w = max(r.shape[1] for r in upscaled) + 2 * pad
        cols = int(math.ceil(math.sqrt(len(upscaled))))
        rows = int(math.ceil(len(upscaled) / cols))
        comp = np.full((rows * cell_h, cols * cell_w, 3), 255, dtype=np.uint8)
        cells = []  # (idx, center_x, center_y)
        for idx, roi in enumerate(upscaled):
            r, c = divmod(idx, cols)
            y = r * cell_h + pad
            x = c * cell_w + pad
            h, w = roi.shape[:2]
            comp[y:y + h, x:x + w] = roi
            cells.append((idx, x + w // 2, y + h // 2))

        res = self._ocr.predict(comp)[0]
        texts = res["rec_texts"]
        scores = res["rec_scores"]
        boxes = res["rec_boxes"]
        results: list[tuple[int | None, float]] = [(None, 0.0)] * len(rois)
        pattern = self._credits_re if credits else self._digit_re
        for text, score, box in zip(texts, scores, boxes):
            normalized = self._normalize_digits(str(text))
            if not re.fullmatch(pattern, normalized):
                continue
            pts = np.asarray(box, dtype=np.float32).reshape(-1, 2)
            cx, cy = float(pts[:, 0].mean()), float(pts[:, 1].mean())
            idx = min(range(len(cells)), key=lambda i: (cells[i][1] - cx) ** 2 + (cells[i][2] - cy) ** 2)
            if score > results[idx][1]:
                results[idx] = (int(normalized), float(score))

        # 合成图偶发漏检/弱读：对「没读到」或「置信度偏低」的格子单格补测。
        # 回退放大倍数有个坑：同一字形不同倍数读出的结果不同——slot1 deaths 的 9 是
        # 1x/2x 读 0.96/0.9999、5x 反而全空；slot7 deaths 的 12 是 2x 全空、5x 读 0.52。
        # 所以先 2x，空/低置信再试 5x，取置信度更高者。低置信 5x 不能省：实测有 2x 读
        # 0@0.44（低于显示阈值会被滤成空）、5x 读到 0@0.68 的硬格，省了会丢数值。
        # 设上限避免大量空格拖慢实时循环。
        fallback = 0
        for i, (v, c) in enumerate(results):
            if (v is not None and c >= self._RETRY_CONF) or fallback >= self._MAX_SINGLE_FALLBACK:
                continue
            v2, c2 = self.ocr_digits(self._upscale(rois[i], self._UPSCALE), credits=credits)
            fallback += 1
            if (v2 is None or c2 < self._RETRY_CONF) and fallback < self._MAX_SINGLE_FALLBACK:
                v5, c5 = self.ocr_digits(self._upscale(rois[i], self._FALLBACK_UPSCALE), credits=credits)
                fallback += 1
                if c5 > c2:
                    v2, c2 = v5, c5
            if c2 > c:
                results[i] = (v2, c2)
        return results

    def batch_ocr_ult(
        self, rois: list[np.ndarray], upscale: int = 3
    ) -> list[tuple[int | None, float]]:
        """批量识别大招当前点数：只识别数字，返回与 rois 等长的 [(当前点数|None, 置信度)]。

        大招单元格未就绪显示当前点数（单个数字），就绪时不显示数字。识别模式与
        KDA 数字一致：normalize 后必须纯数字，取最佳置信度。无数字 → None（就绪）。
        上限不 OCR，由干员名查 images/ult 文件名得到。大招格极小，用 3x 放大提高恢复率。
        """
        upscale = upscale or self._UPSCALE
        if not self._batch or len(rois) <= 1:
            return [self._ocr_ult(self._upscale(r, upscale)) for r in rois]

        upscaled = [self._upscale(r, upscale) for r in rois]
        pad = 16 * upscale
        cell_h = max(r.shape[0] for r in upscaled) + 2 * pad
        cell_w = max(r.shape[1] for r in upscaled) + 2 * pad
        cols = int(math.ceil(math.sqrt(len(upscaled))))
        rows = int(math.ceil(len(upscaled) / cols))

        comp = np.full((rows * cell_h, cols * cell_w, 3), 255, dtype=np.uint8)
        cells = []  # (idx, center_x, center_y)
        for idx, roi in enumerate(upscaled):
            r, c = divmod(idx, cols)
            y = r * cell_h + pad
            x = c * cell_w + pad
            h, w = roi.shape[:2]
            comp[y:y + h, x:x + w] = roi
            cells.append((idx, x + w // 2, y + h // 2))

        res = self._ocr.predict(comp)[0]
        texts = res["rec_texts"]
        scores = res["rec_scores"]
        boxes = res["rec_boxes"]

        # 只识别单数字：大招点数恒为 0~9，多位数字（如 720p 小格的 "186"/"456"）是噪声，
        # 单数字正则直接拒绝。has_text 只记"格内检测到文本框"（检测层面），用于区分
        # "真就绪"（有框无数字）与"数字漏检"（框都没有）。
        per_cell: dict[int, list] = {}
        has_text: set[int] = set()
        mangled: set[int] = set()  # 框内读出多位数字（不可能的大招点数 → 噪声/合并，需回退重读）
        for text, score, box in zip(texts, scores, boxes):
            pts = np.asarray(box, dtype=np.float32).reshape(-1, 2)
            cx = float(pts[:, 0].mean())
            cy = float(pts[:, 1].mean())
            idx = min(range(len(cells)), key=lambda i: (cells[i][1] - cx) ** 2 + (cells[i][2] - cy) ** 2)
            has_text.add(idx)
            normalized = self._normalize_digits(str(text))
            if re.fullmatch(r"\d{2,}", normalized):
                mangled.add(idx)
            if not re.fullmatch(self._ult_re, normalized):
                continue
            per_cell.setdefault(idx, []).append((cx, normalized, float(score)))

        results: list[tuple[int | None, float]] = [(None, 0.0)] * len(rois)
        for idx, dets in per_cell.items():
            for _cx, normalized, score in dets:
                if score > results[idx][1]:
                    results[idx] = (int(normalized), score)

        # 单格回退：重试三类格子——
        #   (a) 批量读到低置信度单数字（<RETRY_CONF）；
        #   (b) 格内什么都没检测到（has_text 空）：可能是 3x 漏检的真实数字；
        #   (c) 框内读出多位数字（mangled）：真实单数字被噪声读成多位，需回退。
        # 格内检测到非数字文本 → 真"就绪"（无数字可读），跳过。回退与 KDA 一致：2x 先、
        # 空/低置信再 5x。多数玩家处于就绪态时回退预算避免拖慢实时循环。
        fallback = 0
        for i, (v, c) in enumerate(results):
            if v is not None and c >= self._RETRY_CONF:
                continue
            if i in has_text and i not in mangled:
                continue  # 有框但非数字 → 就绪
            if fallback >= self._MAX_SINGLE_FALLBACK:
                continue
            v2, c2 = self._ocr_ult(self._upscale(rois[i], self._UPSCALE))
            fallback += 1
            if (v2 is None or c2 < self._RETRY_CONF) and fallback < self._MAX_SINGLE_FALLBACK:
                v5, c5 = self._ocr_ult(self._upscale(rois[i], self._FALLBACK_UPSCALE))
                fallback += 1
                if c5 > c2:
                    v2, c2 = v5, c5
            if c2 > c:
                results[i] = (v2, c2)
        return results

    def _ocr_ult(self, frame) -> tuple[int | None, float]:
        """单格大招识别：只识别单数字。未就绪显示单个数字（当前点数 0~9），就绪无数字。
        返回 (当前点数|None, 置信度)；无数字 → None（就绪）。"""
        if frame is None:
            return None, 0.0
        best, best_conf = None, 0.0
        for text, score in self.predict_texts(frame):
            normalized = self._normalize_digits(str(text))
            if not re.fullmatch(self._ult_re, normalized):
                continue
            if score > best_conf:
                best, best_conf = int(normalized), float(score)
        return best, best_conf

