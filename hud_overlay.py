"""HUD 全屏透明 Overlay：整合 hud_overlay.py 的布局与 overlay_alone.ui 的单卡设计。

- 全屏透明置顶窗口（hud_overlay.py）：两侧各 5 张玩家卡片，贴底部排列，按屏幕等比缩放。
- 单卡设计（overlay_alone.ui）：backdrop + 头像 + 武器 + 护甲 + NAME + 经济 + 击杀/死亡/助攻，
  卡片设计尺寸 370×130，backdrop 拉伸填满，元素用模板里的相对坐标。
- 四种情况：进攻左(atk_l) / 进攻右(atk_r) / 防守左(def_l) / 防守右(def_r)。
- 镜像规则：右侧卡片的头像与武器相对左侧模板水平翻转（weapon_m 文件夹已删除，省内存）——
  左卡片武器 = 翻转 weapon/（等效模板里的 weapon_m），右卡片武器 = weapon/ 原样；
  右侧卡片头像 = 翻转 icon/。
- 文字：NAME 左对齐（左卡片）/ 右对齐（右卡片），经济与击杀/死亡/助攻居中对齐，
  字体 "腾讯三角洲曲线体 Bold"，12pt 粗体。
- 键位：A 切换阵营回退值（无 side 数据的演示模式），Esc 退出。
- 数据源：`--url ws://主机:端口` 连接发送端 WebSocket（receiver.ws_client），每帧
  game_state_received → set_players 更新；缺省用占位数据演示。
- 阵营：左列固定 1~5 号、右列固定 6~10 号，每张卡的 atk/def 由该玩家数据里的 side 决定。
- 存活：玩家数据 alive=false 时整卡压成黑白色，并播放自上而下的缓动过渡动画（复活反向恢复）。
"""

from __future__ import annotations

import os
import sys

import numpy as np

from PySide6.QtCore import QEasingCurve, QRect, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QFont, QImage, QPainter, QPainterPath, QPixmap, QTransform
from PySide6.QtWidgets import QApplication, QLabel, QWidget

ROOT = os.path.dirname(os.path.abspath(__file__))
IMG = os.path.join(ROOT, "images")

CARD_W, CARD_H = 340, 90  # 卡片设计尺寸，= overlay_alone.ui 里 backdrop 的尺寸

# 元素相对卡片左上角的矩形 (x, y, w, h)，取自 overlay_alone.ui（坐标减去 backdrop 偏移 50,100）
# 大招进度球放屏幕外侧下角、爆能器放屏幕内侧下角（左卡片：球左下/爆能器右下；
# 右卡片经 _mirror_rect 镜像后反过来）。位置是初值，用户后续微调。
_ELEMS = {
    "avatar": (0, 0, 61, 61),
    "name": (45, 62, 131, 31),
    "weapon": (110, 20, 141, 40),
    "credits": (180, 62, 61, 31),
    "kda": (252, 62, 61, 31),
    "armor": (280, 22, 35, 35),
    "ult_orb": (-5, 44, 50, 50),    # 大招进度球（屏幕外侧下角）
    "ult_icon": (8, 57, 25, 25),  # 球内干员大招图标（不镜像）
    "spike": (314, 63, 26, 26),  # 爆能器图标（屏幕内侧下角）
}

# 四种情况 -> backdrop 相对路径
_CASES = {
    "atk_l": "backdrop/atk_l.png",
    "atk_r": "backdrop/atk_r.png",
    "def_l": "backdrop/def_l.png",
    "def_r": "backdrop/def_r.png",
}

# 死亡时 backdrop 换成的"缩短"效果图（case -> (相对路径, 是否镜像补位)）。
# 用户只提供 atkdie_l（进攻左）/ defdie_r（防守右）两张，右列 atk 卡用 atkdie_l 镜像、
# 左列 def 卡用 defdie_r 镜像；两张图统一 159 宽 = 死亡缩短的目标宽度。
DEATH_CASES = {
    "atk_l": ("backdrop/atkdie_l.png", False),
    "atk_r": ("backdrop/atkdie_l.png", True),
    "def_r": ("backdrop/defdie_r.png", False),
    "def_l": ("backdrop/defdie_r.png", True),
}
DEATH_W = 159  # 死亡 backdrop 横向缩短的目标宽度（CARD_W=340 → 159）

# 死亡迁移后 KDA/经济的显示位置：死亡缩短卡（DEATH_W=159 宽）内向屏幕两侧分布，
# KDA 靠左、经济靠右（原武器位 (110,20,141,40) 右缘 251 超出 159 宽死亡卡，文字落在 backdrop 外，
# 故移到两侧、收进死亡卡内）。左卡直接生效，右卡自动镜像。微调位置只改这两行。
_NEW_KDA = (80, 13, 64, 34)
_NEW_CREDITS = (80, 33, 64, 34)


def _mirror_rect(rect):
    """把元素矩形沿卡片中心线镜像（右卡片用）。"""
    x, y, w, h = rect
    return (CARD_W - x - w, y, w, h)


def _merge_area(a, b):
    """两个矩形合并成外接矩形 (x, y, w, h)。"""
    l, t = min(a[0], b[0]), min(a[1], b[1])
    r = max(a[0] + a[2], b[0] + b[2])
    bm = max(a[1] + a[3], b[1] + b[3])
    return (l, t, r - l, bm - t)


def _flip(pm: QPixmap) -> QPixmap:
    """水平翻转（镜像）。"""
    return pm.transformed(QTransform().scale(-1, 1))


def _grayscale(pm: QPixmap) -> QPixmap:
    """把卡片整张压成黑白（保留 alpha，覆盖 backdrop/图标/文字）。"""
    img = pm.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    w, h = img.width(), img.height()
    ptr = img.bits()
    arr = np.frombuffer(ptr, np.uint8).reshape(h, w, 4).copy()
    g = (0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]).astype(np.uint8)
    arr[:, :, 0] = g
    arr[:, :, 1] = g
    arr[:, :, 2] = g
    out = QImage(arr.data, w, h, arr.strides[0], QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(out.copy())


class PlayerCard:
    """单张玩家卡片：把一名玩家的数据按模板设计画成 QPixmap（370×130）。"""

    _cache: dict = {}
    _ult_paths: dict = {}  # 干员名 → images/ult 大招图标相对路径（找不到为 None），带缓存

    def __init__(self, case: str, font: QFont | None = None):
        assert case in _CASES, case
        self.case = case
        self.is_right = case in ("atk_r", "def_r")
        self.rects = {k: (_mirror_rect(v) if self.is_right else v) for k, v in _ELEMS.items()}
        self._backdrop = self._load(_CASES[case])
        self._death_bg = self._load_death(case)
        self._new_kda = _mirror_rect(_NEW_KDA) if self.is_right else _NEW_KDA
        self._new_credits = _mirror_rect(_NEW_CREDITS) if self.is_right else _NEW_CREDITS
        self._new_area = _merge_area(self._new_kda, self._new_credits)
        self._font = font or QFont("字体圈伟君黑 W2", 12)

    def set_font(self, font: QFont) -> None:
        """更换文字字体（下次 render 生效）。"""
        self._font = font

    @classmethod
    def _load(cls, rel: str) -> QPixmap:
        pm = cls._cache.get(rel)
        if pm is None:
            pm = QPixmap(os.path.join(IMG, rel))
            if pm.isNull():
                raise RuntimeError(f"图片缺失: {rel}")
            cls._cache[rel] = pm
        return pm

    @classmethod
    def _load_death(cls, case: str) -> QPixmap:
        """加载死亡缩短 backdrop（按 case 取图，右列/左列缺失时镜像补位），带缓存。"""
        rel, flip = DEATH_CASES[case]
        key = ("death", rel, flip)
        pm = cls._cache.get(key)
        if pm is None:
            pm = cls._load(rel)
            if flip:
                pm = _flip(pm)
            cls._cache[key] = pm
        return pm

    @classmethod
    def _ult_icon_rel(cls, agent: str) -> str | None:
        """干员 → images/ult 大招图标相对路径（如 "ult/Mik_8.png"），找不到返回 None。"""
        if agent in cls._ult_paths:
            return cls._ult_paths[agent]
        from recognition.ult_costs import find_ult
        hit = find_ult(os.path.join(IMG, "ult"), agent)
        rel = f"ult/{hit[0]}" if hit else None
        cls._ult_paths[agent] = rel
        return rel

    def _new_layer(self) -> QPixmap:
        pm = QPixmap(CARD_W, CARD_H)
        pm.fill(Qt.GlobalColor.transparent)
        return pm

    def render(self, data: dict) -> QPixmap:
        """正常态完整卡（backdrop + 保留元素 + 原位经济/KDA），供导出/预览。"""
        layers = self.render_layers(data)
        out = self._new_layer()
        p = QPainter(out)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawPixmap(0, 0, layers["bg"])
        p.drawPixmap(0, 0, layers["static"])
        p.drawPixmap(0, 0, layers["ult"])
        p.drawPixmap(0, 0, layers["weapon"])
        p.drawPixmap(0, 0, layers["spike"])
        p.drawPixmap(0, 0, layers["credits_orig"])
        p.drawPixmap(0, 0, layers["kda_orig"])
        p.end()
        return out

    def render_layers(self, data: dict) -> dict:
        """分层渲染：各元素画到独立透明层，供死亡动画按阶段独立合成/裁剪。

        层：
          bg            backdrop（存活图，上层；死亡时按 shrink 向屏幕外侧过渡移除）
          static        头像 + 名字（死亡全程保留）
          ult           大招进度球 + 球内图标（独立层，升级/就绪时以球心为轴扫动过渡）
          weapon        武器 + 护甲（死亡时中央向两侧擦除）
          spike         爆能器（死亡时上→下消失、复活时下→上出现）
          credits_orig  经济（原位置，死亡时擦除）
          kda_orig      KDA（原位置，死亡时擦除）
          credits_new   经济（迁移到死亡卡内，死亡后自下而上浮现）
          kda_new       KDA（迁移到死亡卡内，死亡后自下而上浮现）
        """
        layers = {}
        # bg
        pm = self._new_layer()
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawPixmap(0, 0, CARD_W, CARD_H, self._backdrop)
        p.end()
        layers["bg"] = pm
        # static：头像（右卡镜像） + 名字（死亡全程保留；护甲/爆能器拆独立层）
        pm = self._new_layer()
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        agent = data.get("agent")
        if agent:
            avatar = self._load(f"icon/{agent}_icon.png")
            if self.is_right:
                avatar = _flip(avatar)
            self._draw_icon(p, self.rects["avatar"], avatar)
        p.setFont(self._font)
        p.setPen(Qt.GlobalColor.white)
        name_flags = (Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter) if self.is_right else (
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        p.drawText(QRect(*self.rects["name"]), int(name_flags), data.get("name") or "")
        p.end()
        layers["static"] = pm
        # ult：大招进度球 + 球内干员大招图标（独立层，供升级/就绪时以球心为轴的扫动过渡；
        # 球不镜像；就绪用 ult_ready.png，否则按点数进度取 orb 模板）
        pm = self._new_layer()
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        ult_ready = data.get("ult_ready")
        ult_current = data.get("ult_current")
        ult_max = data.get("ult_max")
        side = data.get("side")
        orb_rel = None
        if ult_ready:
            orb_rel = "other/ult_ready.png"
        elif ult_max and ult_current is not None and side in ("attack", "defense"):
            level = max(0, min(int(ult_current), int(ult_max) - 1))
            if level == 0:
                orb_rel = f"other/{int(ult_max)}_0.png"
            else:
                side_tag = "atk" if side == "attack" else "def"
                orb_rel = f"other/{int(ult_max)}_{level}_{side_tag}.png"
        if orb_rel:
            self._draw_icon(p, self.rects["ult_orb"], self._load(orb_rel))
            if agent:
                icon_rel = self._ult_icon_rel(agent)
                if icon_rel:
                    self._draw_icon(p, self.rects["ult_icon"], self._load(icon_rel))
        p.end()
        layers["ult"] = pm
        # weapon：左卡片翻转 weapon/，右卡片用 weapon/ 原样；护甲同层，死亡时随武器中央向两侧擦除
        pm = self._new_layer()
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        weapon = data.get("weapon")
        if weapon:
            wp = self._load(f"weapon/{weapon}_icon.png")
            if not self.is_right:
                wp = _flip(wp)
            self._draw_icon(p, self.rects["weapon"], wp)
        armor = data.get("armor")
        if armor:
            self._draw_icon(p, self.rects["armor"], self._load(f"armor/{armor}.png"))
        p.end()
        layers["weapon"] = pm
        # spike：爆能器（不镜像；死亡时上→下消失、复活时下→上出现）
        pm = self._new_layer()
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if data.get("alive", True) and data.get("is_spike"):
            self._draw_icon(p, self.rects["spike"], self._load("other/Spike.png"))
        p.end()
        layers["spike"] = pm
        # 经济 / KDA：原位 + 死亡迁移位（死亡卡内居中，KDA 上/经济下，位置见 _NEW_KDA/_NEW_CREDITS）
        for key, rect, text_key in (("credits_orig", self.rects["credits"], "credits"),
                                    ("kda_orig", self.rects["kda"], "kda"),
                                    ("credits_new", self._new_credits, "credits"),
                                    ("kda_new", self._new_kda, "kda")):
            pm = self._new_layer()
            pp = QPainter(pm)
            pp.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            pp.setFont(self._font)
            pp.setPen(Qt.GlobalColor.white)
            pp.drawText(QRect(*rect), int(Qt.AlignmentFlag.AlignCenter), data.get(text_key) or "")
            pp.end()
            layers[key] = pm
        return layers

    @staticmethod
    def _draw_icon(p: QPainter, rect, pm: QPixmap) -> None:
        """等比缩放到矩形内并居中。"""
        scaled = pm.scaled(
            rect[2], rect[3],
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = rect[0] + (rect[2] - scaled.width()) // 2
        y = rect[1] + (rect[3] - scaled.height()) // 2
        p.drawPixmap(x, y, scaled)


def _demo_data() -> list[dict]:
    """占位数据（按 slot 1~10，1~5 为进攻方），含大招/爆能器示例以预览新元素。

    大招状态覆盖：就绪球 / 0 点 / 中段点数（atk/def 不同 orb）；爆能器在进攻方槽 3。
    干员与上限取自 images/ult 真实模板名，球内大招图标可正常加载。
    """
    agents = ["Astra", "Breach", "Brimstone", "Chamber", "Clove",
              "Cypher", "Deadlock", "Fade", "Gekko", "Harbor"]
    weapons = ["Vandal", "Phantom", "Operator", "Spectre", "Ghost",
               "Classic", "Sheriff", "Judge", "Marshal", "Melee"]
    armors = ["Heavy_Armor", "Light_Armor", "Regen_Shield"] * 4
    credits = [4500, 3900, 3200, 2800, 1500, 4000, 5000, 2600, 900, 4700]
    # (干员, 阵营, 大招上限, 当前点数或 None=就绪, 是否爆能器携带者)
    specs = [
        ("Astra", "attack", 7, None, False),
        ("Breach", "attack", 8, 0, False),
        ("Brimstone", "attack", 8, 4, True),
        ("Chamber", "attack", 8, 7, False),
        ("Clove", "attack", 8, 2, False),
        ("Cypher", "defense", 7, 1, False),
        ("Deadlock", "defense", 7, None, False),
        ("Fade", "defense", 8, 6, False),
        ("Gekko", "defense", 8, 3, False),
        ("Harbor", "defense", 7, 0, False),
    ]
    return [
        {
            "slot": i,
            "name": f"PLAYER {i}",
            "agent": spec[0],
            "side": spec[1],
            "weapon": weapons[i - 1],
            "armor": armors[i - 1],
            "credits": f"{credits[i - 1]:,}",
            "kda": f"{(i * 7) % 5}/{i % 4}/{(i * 3) % 6}",
            "alive": True,
            "ult_ready": spec[3] is None,
            "ult_current": spec[3],
            "ult_max": spec[2],
            "is_spike": spec[4],
        }
        for i, spec in enumerate(specs, start=1)
    ]


class HUDOverlay(QWidget):
    REF_W, REF_H = 1920, 1080
    SPACING = 30
    MARGIN_BOTTOM = 50
    MARGIN_SIDE = 0
    DEATH_ANIM_MS = 500                    # 死亡/复活灰度过渡时长
    DEATH_EASING = QEasingCurve.Type.InOutCubic
    ULT_SWEEP_MS = 450                     # 大招升级/就绪扫动过渡时长（秒针走一圈 0→360°）
    ULT_SWEEP_EASING = QEasingCurve.Type.Linear  # 秒针匀速走动

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._attack_left = True
        self._data = _demo_data()
        self._cards: dict[str, PlayerCard] = {}
        self._slots: list[dict] = []
        # 布局参数（GUI「设置HUD布局」可调，默认与 config.hud 一致）
        self.margin_side = 0
        self.scale = 1.0
        self.spacing = 30
        self.margin_bottom = 50
        self._font = QFont("字体圈伟君黑 W2", 12)
        self._build()
        self._apply_data()

    def set_layout(self, *, margin_side=None, scale=None, spacing=None, margin_bottom=None) -> None:
        """更新布局参数并即时重排（改任一参数后调用，正在显示的 HUD 立刻生效）。"""
        if margin_side is not None:
            self.margin_side = margin_side
        if scale is not None:
            self.scale = scale
        if spacing is not None:
            self.spacing = spacing
        if margin_bottom is not None:
            self.margin_bottom = margin_bottom
        self._build()
        self._apply_data()

    def set_font(self, font: QFont) -> None:
        """更换 HUD 文字字体并即时重渲（正在显示的 HUD 立刻生效）。"""
        self._font = font
        for card in self._cards.values():
            card.set_font(font)
        self._apply_data()

    # ---------------- 数据接入 ----------------

    def set_players(self, players: list, spike_carrier: str | None = None) -> None:
        """接收端数据：GameState.players（PlayerData 列表）按 slot 排成 10 人并更新卡片。

        spike_carrier 为爆能器携带者干员名，匹配的进攻方卡片画爆能器图标
        （爆能器只属于进攻方；攻守方可能出现同名干员，必须限定 side=="attack"，
        否则防守方同干员也会误画图标）。
        """
        by_slot = {p.slot: p for p in players}
        self._data = [self._card_dict(by_slot.get(i)) for i in range(1, 11)]
        for d in self._data:
            d["is_spike"] = (
                bool(d.get("agent"))
                and d.get("side") == "attack"
                and d["agent"] == spike_carrier
            )
        self._apply_data()

    @staticmethod
    def _card_dict(p) -> dict:
        """把 PlayerData 转成 PlayerCard.render 需要的 dict；缺字段用 None/空串，护甲名去空格。"""
        if p is None:
            return {"name": "", "agent": None, "weapon": None, "armor": None,
                    "credits": "", "kda": "", "side": None, "alive": True}
        parts = [None if v is None else str(v) for v in (p.kills, p.deaths, p.assists)]
        kda = "" if all(v is None for v in parts) else "/".join("—" if v is None else v for v in parts)
        return {
            "name": p.name or "",
            "agent": p.agent or None,
            "weapon": p.weapon or None,
            "armor": p.armor.replace(" ", "_") if p.armor else None,
            "credits": f"{p.credits:,}" if p.credits is not None else "",
            "kda": kda,
            "side": p.side or None,
            "alive": True if p.alive is None else bool(p.alive),
            "ult_ready": p.ult_ready,
            "ult_current": p.ult_current,
            "ult_max": p.ult_max,
        }

    # ---------------- 布局 ----------------

    def _build(self) -> None:
        """一次性建立 10 个插槽的 QLabel（只建一次；内容由 _apply_data 更新）。"""
        for s in self._slots:
            s["label"].deleteLater()
        self._slots = []
        self._cards = {}

        card_w = int(CARD_W * self.scale)
        card_h = int(CARD_H * self.scale)
        total_h = 5 * card_h + 4 * self.spacing
        start_y = self.REF_H - total_h - self.margin_bottom
        left_x = self.margin_side
        right_x = self.REF_W - self.margin_side - card_w

        for col in range(2):
            for row in range(5):
                slot_num = row + 1 if col == 0 else row + 6
                label = QLabel(self)
                label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                self._slots.append({
                    "slot": slot_num, "col": col,
                    "label": label,
                    "ref_x": left_x if col == 0 else right_x,
                    "ref_y": start_y + row * (card_h + self.spacing),
                    "card": None, "case": None,
                    "layers": None,
                    "alive": True, "progress": 0.0,
                    "shrink": 0.0, "erase": 0.0, "reveal": 0.0,
                    # 爆能器过渡（死亡上→下消失/复活下→上出现）：fade=1 直接显示，0→1 为过渡进度
                    "fade_spike": 1.0, "old_spike": None, "spike_appear_bottom": False,
                    # 大招扫动过渡（升级/就绪时以球心为轴）：sweep=1 直接显示，0→1 为扫过角度进度
                    "ult_state": None, "ult_sweep": 1.0, "old_ult": None, "ult_anim": None,
                    "ult_agent": None,  # 当前球属于哪个干员：换人时旧球一律作废（见 _apply_data）
                    "anims": [],
                })

    def _case_for(self, slot_num: int, side) -> str:
        """左列固定 1~5、右列固定 6~10；atk/def 由 side 决定，未知回退 A 键。"""
        col = 0 if slot_num <= 5 else 1
        if side not in ("attack", "defense"):
            side = "attack" if self._attack_left else "defense"
        return ("atk" if side == "attack" else "def") + ("_l" if col == 0 else "_r")

    def _apply_data(self) -> None:
        """按 self._data 更新各插槽：换卡/重渲/alive 变化触发死亡动画/刷新 label。"""
        for s in self._slots:
            data = self._data[s["slot"] - 1] if s["slot"] <= len(self._data) else {}
            case = self._case_for(s["slot"], data.get("side"))
            alive = bool(data.get("alive", True))
            if s["case"] != case:
                card = self._cards.get(case)
                if card is None:
                    card = PlayerCard(case, self._font)
                    self._cards[case] = card
                s["card"], s["case"] = card, case
                for a in s["anims"]:
                    a.stop()
                s["anims"] = []
                s["progress"] = s["shrink"] = s["erase"] = s["reveal"] = 0.0
                s["fade_spike"] = 1.0
                s["old_spike"] = None
                s["spike_appear_bottom"] = False
                s["ult_state"] = None
                s["ult_sweep"] = 1.0
                s["old_ult"] = None
                s["ult_anim"] = None
                s["ult_agent"] = None
                s["alive"] = True
            prev_layers = s["layers"]  # 变化前各层（供爆能器/大招过渡捕获"旧内容"）
            agent = data.get("agent")
            s["layers"] = s["card"].render_layers(data)
            # 大招状态变化（点数升级 / 就绪 / 上限或阵营变化）→ 以球心为轴的扫动过渡。
            # old_ult 只能取自「同一个干员」的上一帧：干员/占位者变了（开始读取时有人已死、
            # 重排换人、接收端跨局残留）就把旧球整个作废直接显示新球——否则死亡动画打断扫动
            # 后上一位干员的球会永久残留在死亡卡（头像已是新人、球内却是别人图标）。
            new_ult = (data.get("ult_ready"), data.get("ult_current"), data.get("ult_max"), data.get("side"))
            if agent != s["ult_agent"]:
                self._cancel_ult_sweep(s)
                s["ult_state"] = None  # 新干员的球状态从零记，不继承上一位的
            if new_ult != s["ult_state"]:
                if s["ult_state"] is not None and prev_layers is not None:
                    s["old_ult"] = prev_layers.get("ult")
                    self._start_ult_sweep(s)
                s["ult_state"] = new_ult
            s["ult_agent"] = agent
            if alive != s["alive"]:
                s["alive"] = alive
                self._start_death(s, dying=not alive, prev_layers=prev_layers)
            self._update_label(s)

    def _animate(self, s: dict, field: str, to: float, ms: int, easing=None, done=None) -> QVariantAnimation:
        """把 s[field] 从当前值缓动到 to；valueChanged→_update_label，finished→置终值+回调。"""
        anim = QVariantAnimation(self)
        anim.setStartValue(s[field])
        anim.setEndValue(to)
        anim.setDuration(ms)
        anim.setEasingCurve(easing or self.DEATH_EASING)
        anim.valueChanged.connect(lambda v, slot=s, f=field: self._on_anim_field(slot, f, v))
        anim.finished.connect(lambda a=anim, slot=s, f=field, tv=to, cb=done: self._on_anim_done(slot, a, f, tv, cb))
        s["anims"].append(anim)
        anim.start()
        return anim

    def _on_anim_field(self, s: dict, field: str, value) -> None:
        s[field] = float(value)
        self._update_label(s)

    def _on_anim_done(self, s: dict, anim, field: str, to: float, done) -> None:
        s[field] = float(to)
        try:
            s["anims"].remove(anim)
        except ValueError:
            pass
        anim.deleteLater()
        if done is not None:
            done()
        self._update_label(s)

    def _start_death(self, s: dict, dying: bool, prev_layers=None) -> None:
        """死亡/复活的顺序动画链。

        死亡（alive→False）：存活 backdrop 向屏幕外侧过渡移除（shrink 0→1）露出底层
        固定 159 宽的死亡 backdrop + 整卡灰度 500ms 并行、武器/护甲/原位经济/KDA
        从中央向两侧擦除 300ms、爆能器从上至下消失 300ms；擦除结束后等 100ms 间隔，
        再让迁移后的 KDA/经济（死亡卡内，KDA 上/经济下）自下而上浮现 200ms。
        复活为整体倒放：新位先消失(200ms) → 隔 100ms → 原位从两侧合拢出现 +
        存活 backdrop 重新展开覆盖 + 爆能器从下至上出现 + 回彩(500ms)。
        用 _seq 标记每次翻转，防止快速连续翻转时残留的 singleShot 误启动。
        """
        for a in s["anims"]:
            a.stop()
        s["anims"] = []
        # 死亡/复活打断所有动画时一并作废未完成的大招扫动与旧球：旧球属于上一位干员、
        # 扫动又被 stop 停在 f≈0 的话，球内图标会永久是别人（头像已是本次死亡的新数据）。
        self._cancel_ult_sweep(s)
        s["_seq"] = s.get("_seq", 0) + 1
        seq = s["_seq"]
        out_cubic = QEasingCurve.Type.OutCubic
        if dying:
            # 爆能器：旧内容层（死亡前）→ 新内容空层，fade 0→1 上→下消失
            if prev_layers:
                s["old_spike"] = prev_layers["spike"]
            s["spike_appear_bottom"] = False
            s["fade_spike"] = 0.0
            self._animate(s, "progress", 1.0, self.DEATH_ANIM_MS)
            self._animate(s, "shrink", 1.0, self.DEATH_ANIM_MS)
            self._animate(s, "erase", 1.0, 300, out_cubic)
            self._animate(s, "fade_spike", 1.0, 300, out_cubic)

            def _reveal(slot=s, k=seq):
                if slot["_seq"] != k or slot["alive"]:
                    return
                self._animate(slot, "reveal", 1.0, 200, out_cubic)

            QTimer.singleShot(400, _reveal)
        else:
            # 爆能器：旧内容层（死亡态空层）→ 新内容层，fade 0→1 从下至上出现
            if prev_layers:
                s["old_spike"] = prev_layers["spike"]
            s["spike_appear_bottom"] = True
            s["fade_spike"] = 0.0
            self._animate(s, "reveal", 0.0, 200, out_cubic)
            self._animate(s, "fade_spike", 1.0, 300, out_cubic)

            def _restore(slot=s, k=seq):
                if slot["_seq"] != k or not slot["alive"]:
                    return
                self._animate(slot, "erase", 0.0, 300, out_cubic)
                self._animate(slot, "shrink", 0.0, self.DEATH_ANIM_MS)
                self._animate(slot, "progress", 0.0, self.DEATH_ANIM_MS)

            QTimer.singleShot(300, _restore)

    def _cancel_ult_sweep(self, s: dict) -> None:
        """作废未完成的大招扫动与旧球：换人/死亡打断时丢弃残留，直接显示当前新球。

        否则扫动 anim 被 _start_death 的 stop() 掐在 f≈0 时 old_ult 仍残留 →
        合成永远画上一位干员的球（头像已是新人、球内却是别人图标）。见 _apply_data/_start_death。
        """
        a = s.get("ult_anim")
        if a is not None:
            try:
                s["anims"].remove(a)
            except ValueError:
                pass
            a.stop()
            a.deleteLater()
        s["ult_anim"] = None
        s["old_ult"] = None
        s["ult_sweep"] = 1.0

    def _start_ult_sweep(self, s: dict) -> None:
        """大招升级/就绪：以球心为轴的秒针扫动过渡（旧球保持，新球被手柄扫过露出）。

        ult_sweep ∈ [0,1] 对应扫过角度 0..360°（从 12 点方向顺时针走一圈）。
        """
        anim = s.get("ult_anim")
        if anim is not None:
            try:
                s["anims"].remove(anim)
            except ValueError:
                pass
            anim.stop()
            anim.deleteLater()

        def _done(slot=s):
            slot["old_ult"] = None
            slot["ult_anim"] = None

        s["ult_sweep"] = 0.0
        s["ult_anim"] = self._animate(s, "ult_sweep", 1.0, self.ULT_SWEEP_MS, self.ULT_SWEEP_EASING, done=_done)

    # ---------------- 显示 ----------------

    def _draw_swap(self, p: QPainter, s: dict, layer_key: str, fade_key: str, appear_bottom: bool = False) -> None:
        """元素过渡合成：旧内容（old_<layer>）上→下消失、新内容（当前层）出现。

        fade_* ∈ [0,1]：0 = 只显示旧内容，1 = 只显示新内容；中间按 clip 分界。
        旧内容消失（上→下）：可见底部 [f*H, H]，顶部先消失、向下蔓延；
        新内容出现方向由 appear_bottom 决定：
          False → 顶部先现 [0, f*H]（更换场景，"旧上→下消失 + 新上→下出现"）；
          True  → 底部先现 [H-f*H, H]（复活场景，反之，"从下至上出现"）。
        """
        f = s[fade_key]
        old = s.get(f"old_{layer_key}")
        cur = s["layers"][layer_key]
        if f <= 0.001:
            p.drawPixmap(0, 0, old if old is not None else cur)
            return
        if f >= 0.999:
            p.drawPixmap(0, 0, cur)
            return
        hh = int(CARD_H * f)
        # 旧内容：底部可见（顶部先消失）；无旧层则此区域留空
        if old is not None and hh < CARD_H:
            p.setClipRect(0, hh, CARD_W, CARD_H - hh)
            p.drawPixmap(0, 0, old)
        # 新内容：上→下（顶部）或 下→上（底部）出现
        if appear_bottom:
            p.setClipRect(0, CARD_H - hh, CARD_W, hh)
        else:
            p.setClipRect(0, 0, CARD_W, hh)
        p.drawPixmap(0, 0, cur)
        p.setClipping(False)

    def _draw_ult_sweep(self, p: QPainter, s: dict, new_lyr) -> None:
        """大招扫动合成：以球心为轴，旧球保持在外，新球只在手柄扫过的扇区可见。

        sweep ∈ [0,1] → 扫过角度 0..360°；从 12 点方向顺时针走一圈（秒针走动），
        手柄扫到哪，哪就露出新等级球。

        实测 QPainterPath.arcTo 角度约定：0°=3 点方向、正向=逆时针（y 向上数学约定）。
        故 12 点 = +90°，顺时针 = 负扫角：arcTo(rect, 90.0, -sweep*360°)。
        """
        ox, oy, ow, oh = s["card"].rects["ult_orb"]
        old = s.get("old_ult")
        if old is not None:
            p.drawPixmap(0, 0, old)
        f = s.get("ult_sweep", 0.0)
        if f <= 0.001:
            return
        path = QPainterPath()
        # QPainterPath 无 addPie，用 圆心→弧→闭合 拼出扇形 wedge
        path.moveTo(ox + ow / 2.0, oy + oh / 2.0)
        path.arcTo(ox, oy, ow, oh, 90.0, -f * 360.0)
        path.closeSubpath()
        p.setClipPath(path)
        p.drawPixmap(0, 0, new_lyr)
        p.setClipping(False)

    def _composite(self, s: dict) -> QPixmap:
        """按动画状态分层合成：backdrop 移除 + 原位元素擦除 + 新位浮现 + 灰度。

        shrink：存活 backdrop 向屏幕外侧过渡移除（左列左对齐收右缘、右列右对齐收左缘），
          shrink>0 时露出底层固定 159 宽的死亡 backdrop（不拉伸，屏幕外侧对齐）；
        erase ：武器/护甲/原位经济/KDA 从画面中央向两侧擦除（中间带被裁掉，两侧保留）；
        fade_spike：爆能器过渡合成（死亡上→下消失 / 复活下→上出现，见 _draw_swap）；
        reveal：迁移到死亡卡内的 KDA/经济自下而上浮现；
        progress：整卡（存活+死亡两层 backdrop）自上而下同步压灰。
        """
        layers = s["layers"]
        shrink = s["shrink"]
        erase = s["erase"]
        reveal = s["reveal"]
        t = s["progress"]
        out = QPixmap(CARD_W, CARD_H)
        out.fill(Qt.GlobalColor.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        # 1. backdrop：死亡 backdrop（159 宽，固定屏幕外侧，不拉伸）作底层；
        #    存活 backdrop 作上层，shrink 0→1 时向屏幕外侧过渡移除，露出底下死亡 backdrop；
        #    两者的灰度在合成后整卡同步压灰（见末尾 progress）。右卡死亡图已镜像，右对齐绘制。
        if shrink > 0.001:
            dbx = 0 if s["col"] == 0 else CARD_W - DEATH_W
            p.drawPixmap(dbx, 0, DEATH_W, CARD_H, s["card"]._death_bg)
        lw = int(CARD_W * (1 - shrink))
        if lw > 0:
            if s["col"] == 0:
                p.setClipRect(0, 0, lw, CARD_H)
            else:
                p.setClipRect(CARD_W - lw, 0, lw, CARD_H)
            p.drawPixmap(0, 0, layers["bg"])
            p.setClipping(False)
        # 2. static（头像/名字）——原位，全程保留；大招球独立层随后按扫动过渡绘制
        p.drawPixmap(0, 0, layers["static"])
        ult = layers.get("ult")
        if ult is not None:
            if s.get("ult_sweep", 1.0) >= 0.999 or s.get("old_ult") is None:
                p.drawPixmap(0, 0, ult)
            else:
                self._draw_ult_sweep(p, s, ult)
        # 3. 武器+护甲（同层）：死亡时从画面中央向两侧擦除（中间带被裁掉，两侧各画一次）
        if erase <= 0.001:
            p.drawPixmap(0, 0, layers["weapon"])
        elif erase < 0.999:
            d = int(erase * CARD_W / 2)
            left = max(0, CARD_W // 2 - d)
            right = min(CARD_W, CARD_W // 2 + d)
            p.setClipRect(0, 0, left, CARD_H)
            p.drawPixmap(0, 0, layers["weapon"])
            p.setClipRect(right, 0, CARD_W - right, CARD_H)
            p.drawPixmap(0, 0, layers["weapon"])
            p.setClipping(False)
        # 4. 爆能器：过渡合成（死亡上→下消失 / 复活下→上出现）
        self._draw_swap(p, s, "spike", "fade_spike", s.get("spike_appear_bottom", False))
        # 5. 原位经济/KDA：死亡时从画面中央向两侧擦除（中间带被裁掉，两侧各画一次）
        if erase <= 0.001:
            p.drawPixmap(0, 0, layers["credits_orig"])
            p.drawPixmap(0, 0, layers["kda_orig"])
        elif erase < 0.999:
            d = int(erase * CARD_W / 2)
            left = max(0, CARD_W // 2 - d)
            right = min(CARD_W, CARD_W // 2 + d)
            p.setClipRect(0, 0, left, CARD_H)
            p.drawPixmap(0, 0, layers["credits_orig"])
            p.drawPixmap(0, 0, layers["kda_orig"])
            p.setClipRect(right, 0, CARD_W - right, CARD_H)
            p.drawPixmap(0, 0, layers["credits_orig"])
            p.drawPixmap(0, 0, layers["kda_orig"])
            p.setClipping(False)
        # 6. 迁移后的 KDA/经济（死亡卡内，KDA 上/经济下）：自下而上浮现
        if reveal > 0.001:
            nx, ny, nw, nh = s["card"]._new_area
            vh = max(0, int(nh * reveal))
            if vh > 0:
                p.setClipRect(nx, ny + nh - vh, nw, vh)
                p.drawPixmap(0, 0, layers["credits_new"])
                p.drawPixmap(0, 0, layers["kda_new"])
                p.setClipping(False)
        p.end()
        # 7. 灰度：整卡（存活+死亡 backdrop 两层）自上而下同步压灰
        if t <= 0.001:
            return out
        gray = _grayscale(out)
        if t >= 0.999:
            return gray
        r = QPixmap(CARD_W, CARD_H)
        r.fill(Qt.GlobalColor.transparent)
        q = QPainter(r)
        q.drawPixmap(0, 0, out)
        q.setClipRect(0, 0, CARD_W, int(CARD_H * t))
        q.drawPixmap(0, 0, gray)
        q.end()
        return r

    def _update_label(self, s: dict) -> None:
        scale_x = self.width() / self.REF_W
        scale_y = self.height() / self.REF_H
        scale = min(scale_x, scale_y)
        lab = s["label"]
        lab.setPixmap(self._composite(s).scaled(
            int(CARD_W * self.scale * scale), int(CARD_H * self.scale * scale),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))
        lab.adjustSize()
        lab.move(int(s["ref_x"] * scale_x), int(s["ref_y"] * scale_y))
        lab.show()

    def _relayout(self) -> None:
        """屏幕尺寸变化时重摆全部卡片（showEvent 调用）。"""
        for s in self._slots:
            self._update_label(s)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._relayout()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            QApplication.quit()
        elif event.key() == Qt.Key.Key_A:
            self._attack_left = not self._attack_left
            self._build()
            self._apply_data()

    # ---------------- 导出（供预览/验证） ----------------

    def export(self, outdir: str) -> None:
        """导出 4 张单卡 + 2 张整屏布局 PNG（攻击左/攻击右），供查看设计效果。"""
        os.makedirs(outdir, exist_ok=True)
        data = self._data[0]
        for case in _CASES:
            PlayerCard(case).render(data).save(os.path.join(outdir, f"card_{case}.png"))
        for attack_left, tag in ((True, "atk_left"), (False, "atk_right")):
            self._attack_left = attack_left
            self._build()
            self._apply_data()
            # 渲染整屏到与 showEvent 一致的坐标
            canvas = QPixmap(self.REF_W, self.REF_H)
            canvas.fill(Qt.GlobalColor.transparent)
            p = QPainter(canvas)
            for s in self._slots:
                p.drawPixmap(int(s["ref_x"]), int(s["ref_y"]), self._composite(s))
            p.end()
            canvas.save(os.path.join(outdir, f"hud_{tag}.png"))
        print("导出到", outdir)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    overlay = HUDOverlay()
    from config import load_config
    from PySide6.QtGui import QFont

    hud = load_config().hud
    overlay.set_layout(
        margin_side=hud.margin_side, scale=hud.scale,
        spacing=hud.spacing, margin_bottom=hud.margin_bottom,
    )
    if hud.font:
        f = QFont()
        if f.fromString(hud.font):
            overlay.set_font(f)
    if "--export" in sys.argv:
        idx = sys.argv.index("--export")
        outdir = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else os.path.join(ROOT, "_hud_export")
        overlay.export(outdir)
        sys.exit(0)
    if "--url" in sys.argv:
        idx = sys.argv.index("--url")
        url = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else "ws://127.0.0.1:8765"
        from receiver.ws_client import WsClient
        client = WsClient()
        client.game_state_received.connect(lambda gs: overlay.set_players(gs.players, gs.spike_carrier))
        client.error_occurred.connect(lambda msg: print("WS 错误:", msg))
        client.connect_to(url)
    overlay.showFullScreen()
    sys.exit(app.exec())
