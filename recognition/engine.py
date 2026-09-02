"""识别引擎：初始化阶段 + 实时循环。

- 初始化阶段：一次识别 10 人 name（OCR）+ agent（模板匹配），写 output/init.json。
  之后不再识别名称/干员（spec §5.2 / §6.4），错误由 GUI 用户直接修改。
- 实时循环：每次取最新帧（绝不处理旧缓存帧，spec §3.1），
  OCR 数字（kills/deaths/assists/credits）+ OpenCV（阵营/存活/武器/护甲）→ 写 scoreboard.json。
- 帧源：实时窗口采集（Capture）或离线图片（StaticImageSource，--image 模式）。
"""

from __future__ import annotations

import threading
import time

import numpy as np

from config import AppConfig, crop_roi, resolve_rect, validate_layout
from capture import Capture, StaticImageSource
from ocr import PaddleOcrEngine
from opencv import AgentMatcher, WeaponMatcher, ArmorMatcher, SideDetector, AliveDetector, SpikeMatcher
from output import JsonWriter
from .scoreboard import make_player, make_scoreboard
from .ult_costs import resolve_ult, ult_entries

_KDA_KEYS = ("kills", "deaths", "assists")
# 单格补读数量上限：超过则回退整批合成图。单格补读约 13ms/格（硬格 2 次 predict），
# 30 格合成图 ~116ms + 漏检回退 → 变化格 ≤12 时单格补读更快且更准
# （合成图对特定字形会读错值，如 s7d 的 12→2），超过才整批。
_KDA_MAX_SINGLE_RE = 12


class RecognitionEngine:
    # 大招点数"突变"复核帧数：正常步进（+1 涨点/到顶转就绪/就绪放完归 0）沿用 2 帧
    # 时间确认；跳涨 ≥2、下跌、过早"就绪"、就绪后非 0 等突变，需连续这么多帧读到
    # 同一新值才提交——误读是偶发抖动（实测 1→7 / 1→就绪 ~0.7%），很难连续多帧一致，
    # 真变化（双杀同帧跳涨、连点快速到账）只是延迟 ~10 帧提交，肉眼不可察。
    ULT_ABNORMAL_CONFIRM_FRAMES = 10

    def __init__(self, cfg: AppConfig, source):
        self.cfg = cfg
        self.source = source
        self.ocr = PaddleOcrEngine(
            cfg.ocr,
            digit_regex=cfg.recognition.digit_regex,
            credits_regex=cfg.recognition.credits_regex,
        )
        self.agent_matcher = AgentMatcher(cfg)
        self.weapon_matcher = WeaponMatcher(cfg)
        self.armor_matcher = ArmorMatcher(cfg)
        self.side_detector = SideDetector(cfg)
        self.alive_detector = AliveDetector(cfg)
        self.spike_matcher = SpikeMatcher(cfg)
        self._ult_dir = cfg.resolve(cfg.templates.ult_dir)
        self._ult_entries = ult_entries(self._ult_dir)
        self.scoreboard_writer = JsonWriter(cfg.resolve(cfg.json_output.scoreboard))
        self.init_writer = JsonWriter(cfg.resolve(cfg.json_output.init))
        self._stop = threading.Event()
        self._initial: list[dict] | None = None
        self._last_seq = 0  # 最近已处理帧的序号，防止识别快于采集时对同一帧重复识别
        # 大招列变化缓存：点数变化很慢，逐帧 diff 未变则复用上次结果（省掉最常见的第 3 次 predict）
        self._ult_crops: list | None = None
        self._ult_numbers: dict = {}
        # 大招值待确认：单格读偶尔误读（实测 1→7 / 1→就绪 ~0.7%），同一新值需连续
        # 多帧读到才提交，抖动噪声只闪一帧的直接撤回。值 = (读数, 已连续帧数)：
        # 正常步进（+1/到顶就绪/就绪归0）2 帧提交；突变（跳涨/下跌/过早就绪）按
        # ULT_ABNORMAL_CONFIRM_FRAMES 多帧复核——真变化只延迟几帧，误读难连续一致。
        self._ult_pending: dict[int, tuple] = {}
        # 干员大招点数上限缓存（slot → max）：上限从干员名反查，干员识别偶发失败
        # 会查不到（max=None → 预览只显示 "0"、HUD 不画大招球）。识别到一次就记住，
        # 之后偶发失败时沿用，保持 "0/8" 稳定显示。
        self._ult_max: dict[int, int] = {}
        # 物理行占用者快照（slot → 最近确认干员）：大招缓存按物理行存，若同一行换成
        # 另一个干员（计分板重排），旧行的大招值/复核基准全部作废，须按新人重读。
        self._slot_agents: dict[int, str | None] = {}
        # 最近一次 set_roster 名单（用于检测新对局 → 重置所有按行缓存的跨帧状态）
        self._roster_agents: frozenset | None = None
        # 存活值待确认：武器判活需要头像亮度明确（防相邻行武器混入误判活），但
        # 真存活头像 V 在抖动下会偶发单帧跌破门限 → 同一新值需连续两帧才提交，
        # 抖动闪一帧直接撤回（与 KDA/大招时间确认一致）。
        self._alive_committed: dict[int, tuple] = {}
        self._alive_pending: dict[int, tuple] = {}
        self._last_scoreboard: dict | None = None  # 最近一帧完整结果，坏帧整帧回滚用
        # 数字列变化缓存（KDA 与 credits 各一份）：数值只在击杀/买装备时变，逐格
        # diff 未变且置信度够则复用，省掉最贵的批量合成图 predict。
        # cache 结构: {"crops": list|None, "numbers": dict, "reliable": list}
        self._kda_cache: dict = {}
        self._cred_cache: dict = {}
        self.on_frame = None  # 可选回调 on_frame(frame_id, process_ms, scoreboard)，每帧处理完调用
        self._realtime_agent = False  # 设名单后每帧按名单匹配干员，用于计分板重排跟踪

    # ---------------- 对外接口 ----------------

    def warmup(self) -> None:
        self.ocr.warmup()

    def stop(self) -> None:
        self._stop.set()

    def set_roster(self, agents: list[str] | None) -> None:
        """设置已确认干员名单：实时干员识别只匹配名单内干员（更快更准）。

        名单为空/未设置时退回初始化阶段的干员快照，不逐帧匹配。

        名单内容变化（换对局/换计分板）时，所有按物理行缓存的跨帧状态作废——
        大招值/复核/上限、占用者快照、存活确认都基于上一局的行，须全新开局。
        """
        roster_set = frozenset(agents) if agents else None
        if roster_set is not None and roster_set != self._roster_agents:
            # 首次设定名单 / 名单变化 = 开局或换对局：物理行可能已换成新对局的人，
            # 所有按行缓存（含初始化阶段可能读出的）直接清零，首帧全新读
            self._ult_crops = None
            self._ult_numbers = {}
            self._ult_pending = {}
            self._ult_max = {}
            self._slot_agents = {}
            self._alive_committed = {}
            self._alive_pending = {}
            self._kda_cache = {}
            self._cred_cache = {}
        self._roster_agents = roster_set
        if agents:
            self.agent_matcher.set_restriction(set(agents))
            self._realtime_agent = True
        else:
            self.agent_matcher.set_restriction(None)
            self._realtime_agent = False

    def run_initialization(self) -> list[dict]:
        """初始化阶段：识别 10 人名称 + 干员，写 init.json。"""
        layout_msg = validate_layout(self.cfg.layout)
        if layout_msg:
            raise RuntimeError(layout_msg)
        frame = self.source.get_latest()
        if frame is None:
            raise RuntimeError("初始化阶段无法获取画面，请确认窗口/图片有效")
        th = self.cfg.recognition.confidence_threshold
        agent_th = self.cfg.recognition.agent_threshold
        players = []
        for slot in range(1, 11):
            name_roi = crop_roi(frame, self._roi(frame, slot, "name"))
            name_v, name_c = self.ocr.ocr_name(name_roi)
            agent_v, agent_c = self.agent_matcher.match(crop_roi(frame, self._agent_roi(frame, slot)))
            players.append(
                make_player(
                    slot,
                    {
                        "name": (name_v if name_v and name_c >= th else None, name_c),
                        "agent": (agent_v if agent_v and agent_c >= agent_th else None, agent_c),
                    },
                )
            )
        self._initial = players
        self.init_writer.write(make_scoreboard(0, time.time(), players))
        return players

    def run(self, max_frames: int | None = None) -> None:
        """实时识别循环。max_frames 用于离线图片模式（跑 N 帧后退出）。

        帧率策略：cfg.recognition.real_time_fps > 0 时按该值上限调度（处理快则 sleep 到间隔）；
        <=0 时不设帧上限，识别尽 GPU/CPU 所能多快跑多快（实际受采集帧率与实际处理耗时约束）。
        无论是否设上限，同一帧绝不重复识别：识别快于采集时跳过直到新帧到来。
        """
        if self._initial is None:
            self.run_initialization()
        fps = self.cfg.recognition.real_time_fps
        interval = 0.0 if fps <= 0 else 1.0 / max(0.5, fps)
        frame_id = 0
        while not self._stop.is_set():
            start = time.perf_counter()
            frame = self.source.get_latest()
            if frame is None:
                time.sleep(0.05)
                continue
            if self.source.get_latest_seq() == self._last_seq:
                # 尚无新帧（识别快于采集）：跳过，避免对同一帧重复 OCR/广播
                time.sleep(0.01)
                continue
            self._last_seq = self.source.get_latest_seq()
            frame_id += 1
            scoreboard = self._recognize_frame(frame, frame_id, time.time())
            self.scoreboard_writer.write(scoreboard)
            elapsed = time.perf_counter() - start
            if self.on_frame is not None:
                self.on_frame(frame_id, elapsed * 1000.0, scoreboard)
            if max_frames is not None and frame_id >= max_frames:
                break
            if interval > 0:
                time.sleep(max(0.0, interval - elapsed))

    # ---------------- 内部 ----------------

    def _roi(self, frame, slot: int, key: str):
        return resolve_rect(self.cfg.layout, frame.shape, slot, key)

    def _agent_roi(self, frame, slot: int):
        """干员匹配用头像 ROI：紧头像框每边外扩 avatar_pad_frac，给模板留对准余量。

        存活检测仍用紧框（避免背景压低 V 均值），只有干员匹配用外扩框。
        """
        roi = self._roi(frame, slot, "avatar")
        if not roi:
            return None
        x1, y1, x2, y2 = roi
        pad = max(1, int((y2 - y1) * self.cfg.recognition.avatar_pad_frac))
        fh, fw = frame.shape[:2]
        return [max(0, x1 - pad), max(0, y1 - pad), min(fw, x2 + pad), min(fh, y2 + pad)]

    def _thresholded(self, value, confidence: float, threshold: float | None = None):
        """低于阈值 → value=None（置信度保留，spec §13.4）。"""
        if threshold is None:
            threshold = self.cfg.recognition.confidence_threshold
        if value is None or confidence < threshold:
            return None, confidence
        return value, confidence

    def _ocr_digit_single(self, crop, credits: bool) -> tuple[int | None, float]:
        """单格数字补读：2x 优先，2x 全空再 5x（与批量回退同策略）。

        用完整管线（det+rec）而非 rec-only：实测 rec-only 会对空格读出错值
        （空格被判成高置信数字），det 负责挡住这类噪声。
        """
        if crop is None:
            return None, 0.0
        v2, c2 = self.ocr.ocr_digits(self.ocr._upscale(crop, self.ocr._UPSCALE), credits=credits)
        if v2 is None or c2 < self.ocr._RETRY_CONF:
            v5, c5 = self.ocr.ocr_digits(self.ocr._upscale(crop, self.ocr._FALLBACK_UPSCALE), credits=credits)
            if c5 > c2:
                v2, c2 = v5, c5
        return v2, c2

    @staticmethod
    def _crop_changed(old, new) -> bool:
        """ROI 是否值得重读（数字/KDA/credits/大招共用的变化判定）。

        旧判定 mean(abs(diff))>10 对「只改一个数字」不敏感：数字笔画只占格子很小比例，
        均值被留白稀释——实测单字符变化 mean≈5-8，远低于 10，导致复用缓存旧值，
        经济值变化迟迟不刷新。改为「均值>10 或 显著变化像素占比>0.3%」：单字符变化
        贡献 5%+ 的显著变化像素（噪声底为 0.0%，σ=10 采集噪声也不误触发），
        既抓到小变化又不被噪声干扰。
        """
        if old is None or new is None or old.shape != new.shape:
            return True
        diff = np.abs(old.astype(np.int16) - new.astype(np.int16))
        return bool(float(np.mean(diff)) > 10 or float(np.mean(diff > 40)) > 0.003)

    def _ocr_ult_single(self, crop) -> tuple[int | None, float]:
        """单格大招读值：3x 优先，3x 全空再 5x。

        大招格子极小（720p 约 11px 宽），单格 3x 是四图实测最稳的读法：合成图 3x 会
        对这样的小格读错高置信值（img3 s6 把 1 读成 7@0.97），回退 2x 也会把 7 读成
        2@0.66 且高置信挡住 5x。用完整管线（det+rec），由 det 挡住「就绪」格的空格
        噪声。返回 (当前点数|None, 置信度)，None=就绪。
        """
        if crop is None:
            return None, 0.0
        v3, c3 = self.ocr._ocr_ult(self.ocr._upscale(crop, 3))
        if v3 is None or c3 < self.ocr._RETRY_CONF:
            v5, c5 = self.ocr._ocr_ult(self.ocr._upscale(crop, self.ocr._FALLBACK_UPSCALE))
            if c5 > c3:
                v3, c3 = v5, c5
        return v3, c3

    def _batch_numbers_cached(self, metas, crops, credits: bool, upscale: int, cache: dict) -> dict:
        """带 diff-skip 的批量数字 OCR，返回 {meta: (value, confidence)} 并更新 cache。

        首帧整批合成图；之后逐格 diff——未变且置信度够的格复用缓存，只对「变化的」
        或「之前不可靠的」格单格补读。变化格过多时回退整批（30 格合成图 ~116ms，
        单格补读超过 ~8 格反而更慢）。cache = {"crops","numbers","reliable"}。
        """
        old_crops = cache.get("crops")
        if old_crops is None:
            numbers = self._batch_numbers(metas, crops, credits=credits, upscale=upscale)
            cache["crops"] = crops
            cache["numbers"] = numbers
            cache["pending"] = {}
            cache["reliable"] = [numbers.get(m, (None, 0.0))[1] >= self.ocr._RETRY_CONF for m in metas]
            return numbers

        pending = cache["pending"]
        need: list[int] = []
        for i, (old, new) in enumerate(zip(old_crops, crops)):
            # 待确认格每帧强制重读：只有重读才能确认/撤回待提交的新值
            if self._crop_changed(old, new) or not cache["reliable"][i] or metas[i] in pending:
                need.append(i)

        if len(need) > _KDA_MAX_SINGLE_RE:
            numbers = self._batch_numbers(metas, crops, credits=credits, upscale=upscale)
            # 批量合成图对「相邻相同数字」不稳定：位移/渲染抖动把 11 读成 1（实测 ~30%），
            # 且误读置信度不低，batch 内部低置信回退不会触发 → 错误值直接写进缓存 → HUD 闪烁。
            # 只对「批量结果 ≠ 上次已确认值」的格用单格补读复核：抖动不改值时误读格复核回旧值
            # （不再闪），真实变化复核结果一致照样提交（不推迟）。复核只发生在真正变值的格上，
            # 普通帧大部分格批量读值不变，不额外花时间。
            for i, m in enumerate(metas):
                old_v = cache["numbers"].get(m, (None, 0.0))[0]
                new_v = numbers.get(m, (None, 0.0))[0]
                if new_v != old_v:
                    numbers[m] = self._ocr_digit_single(crops[i], credits=credits)
            cache["crops"] = crops
            cache["numbers"] = numbers
            cache["pending"] = {}  # 批量已提交全部格子，旧的待确认全部失效
            cache["reliable"] = [numbers.get(m, (None, 0.0))[1] >= self.ocr._RETRY_CONF for m in metas]
            return numbers

        numbers = dict(cache["numbers"])
        for i in need:
            m = metas[i]
            v = self._ocr_digit_single(crops[i], credits=credits)
            old_v = cache["numbers"].get(m, (None, 0.0))[0]
            if v[0] == old_v:
                # 与已确认值一致：更新置信度/缓存并撤销待确认（抖动噪声闪一帧即撤回）
                numbers[m] = v
                cache["numbers"][m] = v
                cache["crops"][i] = crops[i]
                cache["reliable"][i] = v[1] >= self.ocr._RETRY_CONF
                pending.pop(m, None)
            elif m in pending and pending[m][0] == v[0]:
                # 连续两帧读到同一新值 → 确认是真实变化，提交（只延迟一帧）。
                # 注意：新值可能是 None，必须 m 确实在 pending 里才当二次确认，
                # 否则第一次读到 None 会被误提交（对应格从有值变空时闪烁）。
                numbers[m] = v
                cache["numbers"][m] = v
                cache["crops"][i] = crops[i]
                cache["reliable"][i] = v[1] >= self.ocr._RETRY_CONF
                pending.pop(m, None)
            else:
                # 首次观测到新值：本帧仍显示旧值，记待确认，下帧重读复核
                pending[m] = v
                cache["crops"][i] = crops[i]  # 以新 crop 作下帧 diff 基准，强制进入 need
        return numbers

    def _batch_numbers(self, metas, crops, credits: bool, upscale: int | None = None) -> dict:
        """批量 OCR 数字 ROI，返回 {meta: (value, confidence)}，自动跳过空 ROI。"""
        valid = [(i, c) for i, c in enumerate(crops) if c is not None]
        results = [(None, 0.0)] * len(crops)
        if valid:
            idx = [i for i, _ in valid]
            valid_crops = [c for _, c in valid]
            batch = self.ocr.batch_ocr_digits(valid_crops, credits=credits, upscale=upscale)
            for i, r in zip(idx, batch):
                results[i] = r
        return {meta: r for meta, r in zip(metas, results)}

    def _ult_cost_for(self, agent: str | None) -> int | None:
        """干员 → 大招点数上限（images/ult 文件名），找不到返回 None。"""
        hit = resolve_ult(self._ult_entries, agent)
        return hit[1] if hit else None

    @staticmethod
    def _is_ult_step_plausible(old_v, new_v, max_pts) -> bool:
        """大招点数步进是否"正常"（只影响复核帧数，不拒绝）。正常序列
        1→2→…→max-1→就绪(None)→放完归 0→再逐点涨。正常 = +1 涨点 / 到顶转就绪 /
        就绪放完归 0；突变 = 跳涨 ≥2、下跌、过早"就绪"、就绪后非 0 数字。"""
        if new_v is None:
            # 数字消失 = 就绪：仅当已到/接近上限（6→就绪，max=7）正常；
            # 低点数"就绪"多为识别失败把数字读没（实测 1→就绪 ~0.7%），需多帧复核。
            if old_v is None:
                return False
            if max_pts:
                return old_v >= max_pts - 1
            return False
        if old_v is None:
            # 就绪 → 数字：正常只有放完归 0；就绪后突现非 0 数字是突变。
            return new_v == 0
        # 双方都有数字：唯一正常步进是 +1 涨点；跳涨/下跌/归 0 均突变。
        return new_v == old_v + 1

    def _ult_confirm_frames(self, old_v, new_v, slot) -> int:
        """大招变化复核帧数：正常步进沿用 2 帧时间确认；突变多帧复核。"""
        if self._is_ult_step_plausible(old_v, new_v, self._ult_max.get(slot)):
            return 2
        return self.ULT_ABNORMAL_CONFIRM_FRAMES

    def _read_ult_numbers(self, ult_crops: list, agent_read: dict | None = None) -> dict:
        """大招点数读取：diff-skip + 步进合理性守卫的时间确认，返回 slot→(值, 置信)。

        点数只在击杀/就绪时变，像素未变的格子直接复用缓存（未变重读结果必然相同）；
        变化的格子单读后进 pending 复核——正常步进（+1 涨点/到顶转就绪/就绪放完归 0）
        连续 2 帧一致即提交；突变（跳涨 ≥2/下跌/过早"就绪"）需 ULT_ABNORMAL_CONFIRM_FRAMES
        帧连续一致才提交，防单格误读造成的点数跳变（实测 1→7 / 1→就绪 ~0.7%）。

        agent_read 为本轮各行干员 (值, 置信)。某行干员与上轮确认的不同（两轮都明确）
        即判定该行换了占用者（计分板重排）：行内旧值/复核基准是上一人的，直接作废，
        本轮读数立即成为新基准，不再套用旧人的"步进合理性"（旧人点数 3 → 新人就绪
        会被误判成突变挂 10 帧复核，期间持续显示旧人点数）。
        """
        old_ults = self._ult_crops
        first_ult = old_ults is None  # 首帧无历史值，时间确认缺基准，直接提交
        ult_numbers = {}
        pending_ults = self._ult_pending
        # 占用者更换行：本轮干员明确 且 与上轮确认干员不同 → 该行换人，作废旧行状态
        fresh_slots: set[int] = set()
        if agent_read and self._slot_agents:
            for slot, (cur_agent, _) in agent_read.items():
                prev_agent = self._slot_agents.get(slot)
                if cur_agent and prev_agent and prev_agent != cur_agent:
                    fresh_slots.add(slot)
        for slot in range(1, 11):
            meta = (slot, "ult")
            new = ult_crops[slot - 1]
            old = None if old_ults is None else old_ults[slot - 1]
            if slot in fresh_slots:
                # 行换人：旧值/复核作废，本轮读数直接成为新基准（同首帧路径）
                v = self._ocr_ult_single(new)
                ult_numbers[meta] = v
                pending_ults.pop(slot, None)
                continue
            # 待确认格每帧强制重读（同数字列单格路径的时间确认）
            if not self._crop_changed(old, new) and slot not in pending_ults:
                ult_numbers[meta] = self._ult_numbers[meta]
            else:
                v = self._ocr_ult_single(new)
                old_v = self._ult_numbers.get(meta, (None, 0.0))[0]
                if first_ult:
                    ult_numbers[meta] = v
                elif v[0] == old_v:
                    # 与已确认值一致：撤销待确认（抖动噪声闪一帧即撤回）
                    ult_numbers[meta] = v
                    pending_ults.pop(slot, None)
                else:
                    # 新值与已确认值不同：pending 记 (值, 已连续帧数)。复核帧数按
                    # 步进合理性区分——正常步进 2 帧即提交；突变需多帧连续一致才提交。
                    pen = pending_ults.get(slot)
                    if pen is not None and pen[0][0] == v[0]:
                        cnt = pen[1] + 1
                        if cnt >= self._ult_confirm_frames(old_v, v[0], slot):
                            ult_numbers[meta] = v
                            pending_ults.pop(slot, None)
                        else:
                            pending_ults[slot] = (v, cnt)
                            ult_numbers[meta] = self._ult_numbers.get(meta, (None, 0.0))
                    else:
                        # 首次观测到新值：保持旧值，记待确认，下帧重读复核。
                        # 注意：新值可能是 None（空/就绪），不能把「不在 pending」当已有的 None。
                        pending_ults[slot] = (v, 1)
                        ult_numbers[meta] = self._ult_numbers.get(meta, (None, 0.0))
        self._ult_crops = ult_crops
        self._ult_numbers = ult_numbers
        if agent_read:
            # 更新占用者快照（下帧据此判断哪行换人）；值可为 None（识别失败不算换人）
            self._slot_agents = {slot: (ag if ag else None) for slot, (ag, _) in agent_read.items()}
        return ult_numbers

    def _spike_roi(self, frame):
        """爆能器竖条 ROI（帧归一化区域，见 config.recognition.spike_region）。"""
        reg = self.cfg.recognition.spike_region
        fh, fw = frame.shape[:2]
        x1, y1, x2, y2 = reg
        return crop_roi(frame, [x1 * fw, y1 * fh, x2 * fw, y2 * fh])

    def _slot_at_y(self, cy_norm: float) -> int | None:
        """爆能器竖条内归一化 y → 玩家槽位。

        竖条与计分板高度略有差异（校准来源不同），先把竖条 y 换算到
        计分板归一化坐标系，再按 rows 映射到行。非玩家行返回 None。
        """
        layout = self.cfg.layout
        win = layout.get("window_rect") or [0.0, 0.0, 1.0, 1.0]
        sb = layout.get("scoreboard_rect")
        if not sb or len(sb) != 4 or sb[2] <= sb[0] or sb[3] <= sb[1]:
            return None
        ww, wh = win[2] - win[0], win[3] - win[1]
        fy1 = (sb[1] - win[1]) / wh
        fy2 = (sb[3] - win[1]) / wh
        sy1, sy2 = self.cfg.recognition.spike_region[1], self.cfg.recognition.spike_region[3]
        frame_y = sy1 + cy_norm * (sy2 - sy1)
        sb_norm = (frame_y - fy1) / (fy2 - fy1)
        rows = layout.get("rows") or []
        rb = [0.0] + [float(r) for r in rows] + [1.0]
        row_slots = layout.get("row_slots")
        for r in range(len(rb) - 1):
            if rb[r] <= sb_norm < rb[r + 1]:
                slot = row_slots[r] if row_slots else r + 1
                return slot if slot else None
        return None

    def _detect_spike(self, frame, players: list[dict]) -> str:
        """检测爆能器：竖条内定位 → 映射玩家行 → 取攻击方干员 → "atk_干员"，无则 "none"。"""
        found, _conf, cy_norm = self.spike_matcher.match(self._spike_roi(frame))
        if not found:
            return "none"
        slot = self._slot_at_y(cy_norm)
        if slot is None or not (1 <= slot <= 10):
            return "none"
        p = players[slot - 1]
        agent = p.get("agent", {}).get("value")
        side = p.get("side", {}).get("value")
        if not agent or side != "attack":
            return "none"
        return f"atk_{agent}"

    def _recognize_frame(self, frame, frame_id: int, timestamp: float) -> dict:
        rcfg = self.cfg.recognition

        # 数字：KDA 与 credits 分开批量 OCR（不同放大倍数：KDA 2x，credits 1x）
        kda_metas, kda_crops = [], []
        for slot in range(1, 11):
            for key in _KDA_KEYS:
                kda_metas.append((slot, key))
                kda_crops.append(crop_roi(frame, self._roi(frame, slot, key)))
        cred_metas, cred_crops = [], []
        for slot in range(1, 11):
            cred_metas.append((slot, "credits"))
            cred_crops.append(crop_roi(frame, self._roi(frame, slot, "credits")))

        # KDA/credits 带 diff-skip 的批量 OCR：首帧整批合成图；之后未变的格复用缓存，
        # 只补读变化的/不可靠的格（KDA 2x，credits 数字大 1x 就够且更快）。
        kda_numbers = self._batch_numbers_cached(
            kda_metas, kda_crops, credits=False, upscale=self.ocr._UPSCALE, cache=self._kda_cache
        )
        cred_numbers = self._batch_numbers_cached(
            cred_metas, cred_crops, credits=True, upscale=1, cache=self._cred_cache
        )
        numbers = dict(kda_numbers)
        numbers.update(cred_numbers)

        # 大招：10 格逐格单读（3x 优先、空/低置信再 5x）+ diff-skip。点数只在击杀/就绪时变，
        # 像素未变的格子直接复用缓存（未变重读结果必然相同，白白花钱）；击杀帧只单读
        # 变化的那 1 格（~20ms），替代整批 3x 合成图（~84ms），压掉击杀帧暴涨。
        # 单格 3x 比合成图 3x 准：720p 下 ult 格仅 ~11px 宽，合成图会读错高置信值
        # （img3 s6 把 1 读成 7@0.97、img2 s3 把 7 读成 2@0.66），单格 3x 四图实测全对。
        ult_metas, ult_crops = [], []
        for slot in range(1, 11):
            ult_metas.append((slot, "ult"))
            ult_crops.append(crop_roi(frame, self._roi(frame, slot, "ult")))
        # 实时模式先识别本轮 10 个干员（供玩家构建复用 + 占用者跟踪）。
        # 大招缓存按物理行存，若某行换成了另一个干员，该行旧值/复核基准必须作废，
        # 否则会拿上一个占用者（如 Sage）的大招值硬套新占用者（如炼狱就绪）。
        agent_read: dict[int, tuple] = {}
        if self._realtime_agent:
            for slot in range(1, 11):
                agent_v, agent_c = self.agent_matcher.match(crop_roi(frame, self._agent_roi(frame, slot)))
                agent_read[slot] = self._thresholded(agent_v, agent_c, rcfg.agent_threshold)
        ult_numbers = self._read_ult_numbers(ult_crops, agent_read)

        # 阵营：按队聚合 5 名玩家侧色环（1~5 一队，6~10 一队）
        side_a = self._detect_side(frame, range(1, 6))
        side_b = self._detect_side(frame, range(6, 11))

        players = []
        raw_alives: dict[int, tuple] = {}
        prev_alives: dict[int, tuple] = {}
        cand_alives: dict[int, tuple] = {}
        weapon_none: dict[int, bool] = {}
        for slot in range(1, 11):
            side_team = side_a if slot <= 5 else side_b
            # 存活检测用头像 ROI：在行内上下各收 alive_pad_frac（避开头像顶/底的
            # 非主体亮边行）。干员匹配仍用紧框外扩（_agent_roi），不受影响。
            ar = self._roi(frame, slot, "avatar")
            if ar:
                pad = max(1, round((ar[3] - ar[1]) * rcfg.alive_pad_frac))
                ar = [ar[0], ar[1] + pad, ar[2], ar[3] - pad]
            avatar_roi = crop_roi(frame, ar)
            alive_v, alive_c = self.alive_detector.detect(avatar_roi)

            weapon_v, weapon_c = self.weapon_matcher.match(crop_roi(frame, self._roi(frame, slot, "weapon")))
            weapon = self._thresholded(weapon_v, weapon_c, rcfg.weapon_threshold)
            weapon_none[slot] = weapon[0] is None
            if weapon[0] is None:
                # 武器槽未识别到武器。死亡玩家武器槽为空，但武器识别也会偶发失败
                # （std/模板门控返回 (None, 0.0)）——若此时头像仍亮，多半是识别失败
                # 而非死亡（真死头像必然显著变暗 V≤dead_max_v）。仅当头明确判死才
                # 确认死亡；否则给低置信走时间确认，单帧识别失败不穿透 HUD 闪死。
                if alive_v is False:
                    conf = min(1.0, 0.5 + (rcfg.weapon_threshold - weapon_c) / (2 * rcfg.weapon_threshold))
                    raw_alive = (False, max(conf, alive_c))
                else:
                    raw_alive = (False, 0.0)
            elif alive_v is True and alive_c >= rcfg.alive_threshold:
                # 武器非空 且 头像亮度明确判活 → 存活。亮度要求有裕量
                # （真存活头像 V≥106 → conf 1.0，门限 0.7 对应 V≈99），
                # 防止武器格混入相邻行存活玩家的武器而误判活。
                conf = min(1.0, 0.5 + (weapon_c - rcfg.weapon_threshold) / (2 * (1.0 - rcfg.weapon_threshold)))
                raw_alive = (True, max(conf, alive_c))
            elif alive_v is False:
                # 武器非空但头像明确判死（V≤dead_max_v）：死亡特征压倒武器——
                # 死亡玩家头像显著偏暗，此时武器大概率是相邻行存活玩家的图标
                # 混入（行错位/紧贴间隙），不采信 → 保守判死。
                raw_alive = (False, max(alive_c, 0.5))
            else:
                # 武器非空且头像未明确判死（V>dead_max_v，含暧昧区/偏低亮）：
                # Valorant 死亡玩家武器槽为空，能匹配到武器即大概率存活；
                # 头像只用于否决「明确死」，不再要求亮度够亮。
                # 修正真活但头像偏暗的误判：实测 bug(3)s8 头像 V 收缩后 97.5
                # 略低于旧门限 99 被误判死（有完整独立武器），text(2)s2 同理。
                conf = min(1.0, 0.5 + (weapon_c - rcfg.weapon_threshold) / (2 * (1.0 - rcfg.weapon_threshold)))
                raw_alive = (True, max(conf, alive_c))
            # 时间确认：与已提交值一致→直接采用；高置信翻转（死亡武器槽变空/
            # 复活武器出现，conf≥门限）立即生效，不等两帧——武器槽变化是强信号，
            # 实测武器识别单帧 100 帧噪声 0 闪烁，2帧防抖只拖慢死亡/复活观感。
            # 仅低置信临界抖动（conf<门限，如武器识别不确定）保留两帧确认防闪烁。
            prev = self._alive_committed.get(slot, raw_alive)
            raw_alives[slot] = raw_alive
            prev_alives[slot] = prev
            if raw_alive[0] == prev[0]:
                alive = raw_alive
                self._alive_pending.pop(slot, None)
            elif raw_alive[1] >= rcfg.alive_threshold:
                alive = raw_alive
                self._alive_pending.pop(slot, None)
            elif slot in self._alive_pending and self._alive_pending[slot][0] == raw_alive[0]:
                alive = raw_alive
                self._alive_pending.pop(slot, None)
            else:
                alive = prev
                self._alive_pending[slot] = raw_alive
            self._alive_committed[slot] = alive
            cand_alives[slot] = alive
            armor_v, armor_c = self.armor_matcher.match(crop_roi(frame, self._roi(frame, slot, "armor")))

            init = self._initial[slot - 1] if self._initial else {}
            if self._realtime_agent:
                agent = agent_read.get(slot, (None, 0.0))  # 已在本帧开头统一匹配
            else:
                init_agent = init.get("agent", {})
                agent = (init_agent.get("value"), init_agent.get("confidence", 0.0))
            values = {
                "name": (init.get("name", {}).get("value"), init.get("name", {}).get("confidence", 0.0)),
                "agent": agent,
                "side": self._thresholded(*side_team),
                "alive": alive,
                "kills": self._thresholded(*numbers.get((slot, "kills"), (None, 0.0)), rcfg.digit_threshold),
                "deaths": self._thresholded(*numbers.get((slot, "deaths"), (None, 0.0)), rcfg.digit_threshold),
                "assists": self._thresholded(*numbers.get((slot, "assists"), (None, 0.0)), rcfg.digit_threshold),
                "credits": self._thresholded(*numbers.get((slot, "credits"), (None, 0.0)), rcfg.digit_threshold),
                "weapon": weapon,
                "armor": self._thresholded(armor_v, armor_c, rcfg.armor_threshold),
            }
            # 大招：无数字（就绪）→ {"ready": true}；否则 {"current", "max"}。
            # 就绪由"无数字"推断，置信度无意义置 0。死亡玩家照常显示大招点数
            # （HUD 侧已把死亡卡片压成黑白，点数球随卡片一起变黑白）。
            current, uconf = ult_numbers.get((slot, "ult"), (None, 0.0))
            max_pts = self._ult_cost_for(agent[0])
            if max_pts is None:
                # 干员识别偶发失败 → 查不到上限：沿用本 slot 上次成功查到的 max，
                # 避免预览只显示 "0"（缺 "/上限"）、HUD 因 max 缺失不画大招球。
                max_pts = self._ult_max.get(slot)
            else:
                self._ult_max[slot] = max_pts
            values["ult"] = (
                ({"ready": True}, 0.0) if current is None else ({"current": current, "max": max_pts}, uconf)
            )
            players.append(make_player(slot, values))
        # 帧级画面丢失闸门：坏帧（遮挡/撕裂/mss 黑帧穿透）会让计分板字段区整片异常，
        # 特征与真实状态冲突——真死亡是逐帧个别 slot 翻转（两队不可能同一帧死 6+），
        # 且活人的武器槽必有图标（不可能空）。两个独立的坏帧信号：
        #   1) 一帧 ≥6 个 slot 同时从「活」提交为「死」（整片变暗/武器槽清空）；
        #   2) ≥6 个 slot「活着却武器为空」（活人武器丢失，如武器列整片识别失败）。
        # 命中任一即回滚整帧到上一帧（alive/武器/数字全沿用），避免 HUD 整版闪死
        # 或武器全空。单队团灭（5 人）不会同时满足两信号。下帧恢复后自然正常判定。
        flip_dead = [
            s for s in range(1, 11)
            if cand_alives[s][0] is False and prev_alives[s][0] is True
        ]
        fake_missing = [
            s for s in range(1, 11)
            if weapon_none[s] and cand_alives[s][0] is True
        ]
        if (len(flip_dead) >= 6 or len(fake_missing) >= 6) and self._last_scoreboard is not None:
            for s in set(flip_dead) | set(fake_missing):
                self._alive_committed[s] = prev_alives[s]
                self._alive_pending.pop(s, None)
            last = self._last_scoreboard
            return make_scoreboard(frame_id, timestamp, last["players"], spike=last["spike"])
        spike = self._detect_spike(frame, players)
        scoreboard = make_scoreboard(frame_id, timestamp, players, spike=spike)
        self._last_scoreboard = scoreboard
        return scoreboard

    def _side_ring_roi(self, frame, slot: int):
        """色环列 ROI：头像列右移一个头像宽度。side_avatar 只在 1/6 号定义，
        其余槽回退到干员图标会把图标颜色混进阵营信号，这里统一取头像右侧的色环列。"""
        roi = self._roi(frame, slot, "side_avatar")
        if roi:
            return roi
        a = self._roi(frame, slot, "avatar")
        if not a:
            return None
        x1, y1, x2, y2 = a
        w = x2 - x1
        return [x2, y1, min(frame.shape[1], x2 + w), y2]

    def _detect_side(self, frame, slots) -> tuple[str | None, float]:
        rois = []
        for slot in slots:
            crop = crop_roi(frame, self._side_ring_roi(frame, slot))
            if crop is not None:
                rois.append(crop)
        return self.side_detector.detect(rois)
