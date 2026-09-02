"""配置加载与校验。

所有识别参数（布局、模板路径、阈值、采集/OCR 参数）都放在 config/config.json。
模块提供带默认值的 dataclass，缺失字段自动补默认值。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field

if getattr(sys, "frozen", False):
    # PyInstaller onedir：sys._MEIPASS 指向 _internal，数据/输出都在其中（可写、随文件夹整体分发）
    PROJECT_ROOT = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    CONFIG_DIR = os.path.join(PROJECT_ROOT, "config")
else:
    PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")


@dataclass
class CaptureConfig:
    window_title_keyword: str = "VALORANT"
    fps_cap: float = 20.0
    cache_size: int = 20
    # 采集方式：auto(PrintWindow 优先，全黑回退屏幕) / printwindow / screen
    method: str = "auto"


@dataclass
class RecognitionConfig:
    real_time_fps: float = 4.0
    # 通用门控（名称/阵营等），各字段用各自阈值避免误杀
    confidence_threshold: float = 0.8
    # 干员模板匹配置信度天然低于名称 OCR，单独低阈值门控（spec §6.4）
    agent_threshold: float = 0.5
    # 干员匹配用头像 ROI 在紧头像框基础上每边外扩的比例（相对 ROI 高）。
    # 实测紧框只有 ~35px，模板无对准余量导致误配/低置信；外扩 0.15 后 9/9 正确且最低分≥0.63。
    avatar_pad_frac: float = 0.15
    # 数字 OCR：小数字/暗区置信度偏低但通常正确
    digit_threshold: float = 0.5
    # 存活：武器判活的头像亮度置信门限。真存活头像 V≥106→conf 1.0，远高于门限；
    # 门限 0.7 对应 V≈99，可拦武器格混入相邻行存活玩家武器的误判活
    # （实测混入时头像 V 仅 82~96，暧昧区）。
    alive_threshold: float = 0.7
    # 存活检测用头像 ROI 的行内收缩比例（相对 ROI 高，上下各收）。
    # 按真实行高校准后，头像 ROI 顶部会含 1-2px 非头像主体的亮边行，
    # 抖动下该行 V 波动拉低均值 → 真存活偶发跌破门限（HUD 闪烁）。
    # 实测 720p 收 2px / 1080p 收 3px（≈0.09×行高）后真存活 0 翻转。
    alive_pad_frac: float = 0.09
    # 武器/护甲模板匹配：真实武器 ≥0.84，空槽误配 ≤0.66
    weapon_threshold: float = 0.7
    armor_threshold: float = 0.5
    digit_regex: str = r"^\d{1,3}$"
    credits_regex: str = r"^\d{1,5}$"
    strip_chars: str = ", "
    # 爆能器检测：计分板左侧竖条（帧归一化 [x1,y1,x2,y2]，
    # 由 1080p 坐标 x531-571 / y338-739 换算而来）
    spike_region: list = field(default_factory=lambda: [0.276563, 0.312963, 0.297396, 0.684259])
    spike_template: str = "Spike.png"
    spike_threshold: float = 0.6


@dataclass
class OCRConfig:
    device: str = "gpu"
    detection_model: str = "PP-OCRv6_small_det"
    recognition_model: str = "PP-OCRv6_small_rec"
    use_doc_orientation_classify: bool = False
    use_doc_unwarping: bool = False
    use_textline_orientation: bool = False
    warmup_calls: int = 3
    batch_rois: bool = True
    # DB 检测器 box 置信度阈值：默认 0.6 会漏掉低置信度的数字框（实测 30 格合成图只检出
    # 14/30）。降到 0.3 提升到 ~29/30；误检框被数字正则过滤，不产生错值。
    det_box_thresh: float = 0.3


@dataclass
class TemplatesConfig:
    agents_dir: str = "images/icon"
    # 干员匹配专用覆写模板目录（同名模板覆盖 agents_dir，仅用于匹配，不影响头像渲染）：
    # 展示头像与匹配模板要分开时用——如 Brimstone 匹配用实机紧框提取的模板，
    # 但 HUD 头像仍显示 images/icon 的美术图，两者不能互相污染。
    agents_override_dir: str = "images/agents_tmpl"
    weapons_dir: str = "images/weapon"
    armor_dir: str = "images/armor"
    ult_dir: str = "images/ult"
    other_dir: str = "images/other"
    agents_suffix: str = "_icon"
    weapons_suffix: str = "_icon"
    armor_suffix: str = ""
    scale_factors: list = field(default_factory=lambda: [0.8, 0.9, 1.0, 1.1, 1.2])
    match_threshold: float = 0.8


@dataclass
class SideConfig:
    # OpenCV HSV 的 H 范围是 0~179：红 ≈ 0~8 与 170~179；防守方实测为绿(~70-90)，兼顾蓝
    attack_hue_ranges: list = field(default_factory=lambda: [[0, 8], [170, 179]])
    defense_hue_ranges: list = field(default_factory=lambda: [[70, 135]])


@dataclass
class AliveConfig:
    # 存活判据：头像 ROI 的 HSV V 均值。实测死亡 ≤82、存活 ≥94，区间内视为歧义
    dead_max_v: int = 82
    alive_min_v: int = 94


@dataclass
class JsonOutputConfig:
    scoreboard: str = "output/scoreboard.json"
    init: str = "output/init.json"


@dataclass
class HudConfig:
    """接收端 HUD 覆盖层布局参数（用户可调，持久化到 config.json 的 hud 节）。"""
    margin_side: int = 0      # 左右距离：卡片列与屏幕左右边缘的间距（参考 1920 宽）
    scale: float = 1.0        # 缩放大小：卡片显示倍率
    spacing: int = 30         # 行间隙：相邻两行的垂直间距
    margin_bottom: int = 50   # 底部距离：卡片列与屏幕底边的间距
    font: str | None = None   # HUD 文字字体（QFont.toString()，None=默认"字体圈伟君黑 W2",12）


@dataclass
class AppConfig:
    version: int = 1
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    recognition: RecognitionConfig = field(default_factory=RecognitionConfig)
    ocr: OCRConfig = field(default_factory=OCRConfig)
    templates: TemplatesConfig = field(default_factory=TemplatesConfig)
    side: SideConfig = field(default_factory=SideConfig)
    alive: AliveConfig = field(default_factory=AliveConfig)
    json_output: JsonOutputConfig = field(default_factory=JsonOutputConfig)
    hud: HudConfig = field(default_factory=HudConfig)
    layout: dict | None = None

    def resolve(self, rel_path: str) -> str:
        """相对路径基于项目根目录解析为绝对路径。"""
        if os.path.isabs(rel_path):
            return rel_path
        return os.path.join(PROJECT_ROOT, rel_path)


def _fill(cls, data):
    data = data or {}
    return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def default_config() -> AppConfig:
    return AppConfig()


def load_config(path: str = DEFAULT_CONFIG_PATH) -> AppConfig:
    """读取 config.json 并填充默认值。文件缺失时自动生成默认配置。"""
    if not os.path.exists(path):
        write_default_config(path)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    cfg = AppConfig(
        version=data.get("version", 1),
        capture=_fill(CaptureConfig, data.get("capture")),
        recognition=_fill(RecognitionConfig, data.get("recognition")),
        ocr=_fill(OCRConfig, data.get("ocr")),
        templates=_fill(TemplatesConfig, data.get("templates")),
        side=_fill(SideConfig, data.get("side")),
        alive=_fill(AliveConfig, data.get("alive")),
        json_output=_fill(JsonOutputConfig, data.get("json_output")),
        hud=_fill(HudConfig, data.get("hud")),
        layout=data.get("layout") or None,
    )
    return cfg


def _write(cfg: AppConfig, path: str) -> None:
    payload = {
        "version": cfg.version,
        "capture": _asdict(cfg.capture),
        "recognition": _asdict(cfg.recognition),
        "ocr": _asdict(cfg.ocr),
        "templates": _asdict(cfg.templates),
        "side": _asdict(cfg.side),
        "alive": _asdict(cfg.alive),
        "json_output": _asdict(cfg.json_output),
        "hud": _asdict(cfg.hud),
        "layout": cfg.layout,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def write_default_config(path: str = DEFAULT_CONFIG_PATH) -> None:
    """写入一份带默认值的 config.json（layout 为 null，待标定）。"""
    _write(default_config(), path)


def save_layout(layout: dict, path: str = DEFAULT_CONFIG_PATH) -> None:
    """把新的 layout 写回 config.json（保留其它配置）。"""
    cfg = load_config(path)
    cfg.layout = layout
    _write(cfg, path)


def save_hud(hud: HudConfig, path: str = DEFAULT_CONFIG_PATH) -> None:
    """把新的 HUD 布局参数写回 config.json（保留其它配置）。"""
    cfg = load_config(path)
    cfg.hud = hud
    _write(cfg, path)


def _asdict(obj) -> dict:
    return {k: v for k, v in obj.__dict__.items()}


# ---------------- 布局坐标换算 ----------------

_SIDE_ONLY_SLOTS = (1, 6)


def validate_layout(layout) -> str | None:
    """返回错误信息，合法返回 None。"""
    if not layout:
        return "未标定计分板布局（config.layout 为空），请先用 calibration/roi_calibrator.py 标定"
    sb = layout.get("scoreboard_rect")
    if not sb or len(sb) != 4 or sb[2] <= sb[0] or sb[3] <= sb[1]:
        return "未框选计分板区域，请先框选计分板"
    if not layout.get("rows"):
        return "未添加水平分隔线（玩家行），请先添加横线"
    if not layout.get("cols") or not layout.get("col_fields"):
        return "未添加垂直分隔线（字段列），请先添加竖线并指定字段"
    row_slots = layout.get("row_slots")
    if row_slots is not None:
        if not isinstance(row_slots, list) or len(row_slots) != len(layout.get("rows", [])) + 1:
            return "玩家行配置与行数不一致，请重新在标定工具中指定玩家行"
        seen = set()
        for v in row_slots:
            if v == 0:
                continue
            if not isinstance(v, int) or v < 1 or v > 10:
                return f"无效的玩家行号 {v}，应为 1~10 或 0（非玩家行）"
            if v in seen:
                return f"玩家 {v} 被分配到多行，请每行只选一个玩家"
            seen.add(v)
        if not seen:
            return "尚未分配任何玩家行，请为计分板中的玩家行指定玩家 1~10"
    return None


def resolve_rect(layout, frame_shape, slot: int, field: str) -> list[int] | None:
    """按归一化 layout 计算 (slot, field) 在当前帧的像素矩形 [x1,y1,x2,y2]，无则 None。

    layout 各矩形均为 0~1 归一化坐标：window_rect/scoreboard_rect 相对参考图，
    rows/cols 相对计分板（0~1），col_fields 第 i 项为第 i 列字段名。
    运行时按当前帧尺寸等比缩放，窗口缩放后区域仍然正确。
    """
    if not layout:
        return None
    sb = layout.get("scoreboard_rect")
    if not sb or len(sb) != 4 or sb[2] <= sb[0] or sb[3] <= sb[1]:
        return None
    win = layout.get("window_rect")
    if not win or len(win) != 4 or win[2] <= win[0] or win[3] <= win[1]:
        win = [0.0, 0.0, 1.0, 1.0]
    fh, fw = frame_shape[:2]
    if fh <= 0 or fw <= 0:
        return None

    # 计分板在帧内的像素矩形
    ww = win[2] - win[0]
    wh = win[3] - win[1]
    x1 = (sb[0] - win[0]) / ww * fw
    y1 = (sb[1] - win[1]) / wh * fh
    x2 = (sb[2] - win[0]) / ww * fw
    y2 = (sb[3] - win[1]) / wh * fh
    if x2 <= x1 or y2 <= y1:
        return None

    if field == "side_avatar" and slot not in _SIDE_ONLY_SLOTS:
        return None
    col_fields = layout.get("col_fields") or []
    col = None
    for i, f in enumerate(col_fields):
        if f == field:
            col = i
            break
    if col is None:
        return None

    cb = [0.0] + [float(c) for c in (layout.get("cols") or [])] + [1.0]
    rb = [0.0] + [float(r) for r in (layout.get("rows") or [])] + [1.0]
    if col >= len(cb) - 1:
        return None
    row_slots = layout.get("row_slots")
    if row_slots:
        try:
            row = row_slots.index(slot)
        except ValueError:
            return None
    else:
        row = slot - 1
    if row < 0 or row >= len(rb) - 1:
        return None

    lx = x1 + (x2 - x1) * cb[col]
    rx = x1 + (x2 - x1) * cb[col + 1]
    ty = y1 + (y2 - y1) * rb[row]
    by = y1 + (y2 - y1) * rb[row + 1]
    lx, rx = sorted((lx, rx))
    ty, by = sorted((ty, by))
    return [int(lx), int(ty), int(rx), int(by)]


def crop_roi(frame, roi):
    """按 (x1, y1, x2, y2) 裁剪帧。roi 为空时返回 None。"""
    if not roi or len(roi) != 4:
        return None
    x1, y1, x2, y2 = (int(v) for v in roi)
    h, w = frame.shape[:2]
    x1, x2 = max(0, min(x1, w)), max(0, min(x2, w))
    y1, y2 = max(0, min(y1, h)), max(0, min(y2, h))
    if x2 - x1 <= 0 or y2 - y1 <= 0:
        return None
    return frame[y1:y2, x1:x2]
