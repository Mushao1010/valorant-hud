"""计分板预览对话框（preview_dialog.ui）。

- 显示「失焦前约 1 秒」的稳定帧：游戏窗口失焦后 Tab 不再注入游戏进程，计分板收起，
  此时现抓的帧没有计分板。故优先取缓存中、游戏前台时的帧并回退 1 秒
  （见 capture.remember_good_frame/get_last_good_frame），无缓存时再现抓一帧兜底。
- 打开期间每 300ms 重读缓存：识别在跑、缓存持续有前台帧入队时预览跟随「1 秒前」帧；
  游戏失焦后缓存冻结，预览保持最后一帧计分板静态画面（不闪烁）。
- 不跑识别也能预览（own_capture=True，用户先预览、再开始读取计分板）：构造时先
  `_fill_cache_burst`——把游戏切前台+按住 Tab 展开计分板，连续采集约 2s 填满 30 帧
  「失焦前」缓存，再松开 Tab 让游戏失焦（缓存冻结在失焦时刻）；随后 `_grab_frame`
  直接取缓存中「失焦前约 1 秒」的稳定计分板帧并固定显示。无识别时缓存不推进，
  画面不跳动。窗口置顶（WindowStaysOnTopHint）。
- 画一个 91:47 黄色框标注计分板读取范围：框内图像原样（透明度 1），
  框外叠加 30% 黑幕压暗（等效图像透明度 0.7）。
- 按钮调整框位置/缩放（保持 91:47 比例），「恢复默认」回到 config.layout.scoreboard_rect，
  「完成」把当前框写回 scoreboard_rect（归一化两点坐标），识别范围随之改变。
"""

from __future__ import annotations

import time

import cv2

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QDialog, QMessageBox, QSizePolicy

from ui_preview_dialog import Ui_Form as Ui_Preview
from capture import set_dpi_awareness
from capture.capture import (
    Capture,
    capture_window_printwindow,
    capture_window_screen,
    get_last_good_frame,
    is_mostly_black,
    remember_good_frame,
)
from capture.input_control import press_tab_hold, release_tab
from config import load_config, save_layout

_REFRESH_MS = 300  # 打开期间重读缓存间隔（失焦后无新帧自动保持不变）
_FILL_CACHE_S = 2.0  # own_capture 填缓存时长：20fps×2s≈40帧，缓存保留最近30帧≈1.5s

_RATIO_W, _RATIO_H = 91, 47     # 黄框宽高比
_MOVE_STEP_PX = 1               # 每次移动的步长（1 帧像素）
_ZOOM_STEP_PX = 1               # 每次缩放的高度步长（1 帧像素）
_AUTOREPEAT_DELAY = 300         # 长按连发初始延迟（ms）
_AUTOREPEAT_INTERVAL = 60       # 长按连发间隔（ms）
_MIN_H = 0.02                   # 框高下限（归一化）
_MAX_H = 0.95                   # 框高上限（归一化）
_OUTSIDE_DIM_ALPHA = 77         # 框外黑幕 alpha（30% 压暗 → 图像等效透明度 0.7）
_BORDER_COLOR = QColor(255, 230, 0)


class ScoreboardPreviewDialog(QDialog):
    def __init__(self, hwnd: int, parent=None, own_capture: bool = False):
        super().__init__(parent)
        self.ui = Ui_Preview()
        self.ui.setupUi(self)
        # setPixmap 会让 QLabel 的 minimumSizeHint 等于 pixmap 大小，而 pixmap 又等于当前 label 尺寸，
        # 导致窗口放大后最小尺寸被顶高、无法缩小。用 Ignored 策略让布局忽略该 label 的尺寸提示，
        # 大小完全由 stretch=99 分配；再给对话框一个合理的最小尺寸兜底。
        self.ui.label.setSizePolicy(QSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored))
        self.setMinimumSize(420, 300)
        self._hwnd = hwnd
        self._own_capture = own_capture
        if own_capture:
            # 识别未在跑（先预览、后开始读取计分板）：先填满「失焦前」缓存，
            # 之后 _grab_frame 直接取「失焦前约 1 秒」的稳定计分板帧，预览固定不跳动。
            set_dpi_awareness()
            self._fill_cache_burst()
        # 预览置顶：own_capture 填缓存把游戏切前台、识别在跑时用户点回游戏，预览都保持可见。
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self._frame = self._grab_frame()
        if self._frame is None:
            QMessageBox.warning(
                self, "预览",
                "无法读取游戏窗口画面。请确认已选择游戏窗口、游戏为窗口化(有边框)运行且未最小化。",
            )
            self.reject()
            return
        self._fh, self._fw = self._frame.shape[:2]
        # 归一化框宽高比：91:47 是按画面像素算的，(x2-x1)/(y2-y1) 需乘 fh/fw 折算
        self._ratio = _RATIO_W / _RATIO_H * self._fh / self._fw
        self._box = self._default_box()
        self._wire()
        self._render()
        # 打开期间周期重读缓存：识别在跑时预览跟随「失焦前 1 秒」最新帧；
        # 游戏失焦后缓存冻结，预览保持最后一张计分板静态画面。
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_frame)
        self._timer.start(_REFRESH_MS)

    def _fill_cache_burst(self) -> None:
        """own_capture：把游戏切前台+按住 Tab 展开计分板，连续采集约 2s 填满
        「失焦前」缓存，再松开 Tab 让游戏失焦（此后缓存冻结在失焦时刻附近）。
        之后 _grab_frame 取缓存中「失焦前约 1 秒」的稳定计分板帧。"""
        if not press_tab_hold(self._hwnd):
            return
        # auto：PrintWindow 对 DX11 游戏恒黑时自动回退 mss 截屏，好帧缓存才能填满
        cap = Capture(self._hwnd, fps_cap=20.0, method="auto")
        cap.start()
        try:
            time.sleep(_FILL_CACHE_S)
        finally:
            cap.stop()
            release_tab()

    def closeEvent(self, event) -> None:
        """关闭后把焦点切回父窗口（own_capture 填缓存时焦点曾切到游戏）。"""
        super().closeEvent(event)
        parent = self.parent()
        if parent is not None:
            parent.activateWindow()
            parent.raise_()

    # ---------------- 抓帧与框 ----------------

    def _grab_frame(self):
        """取一帧：优先用识别循环缓存的「失焦前约 1 秒」稳定帧（计分板可见）；
        无缓存时现抓一帧兜底。PrintWindow 对 DX11 游戏（无畏契约/Vanguard）恒黑，
        黑帧回退 mss 截屏（抓屏幕实际画面）；截屏也黑才视为取帧失败，弹警告而非黑屏。"""
        cached = get_last_good_frame(self._hwnd)
        if cached is not None:
            return cached
        set_dpi_awareness()
        frame = capture_window_printwindow(self._hwnd)
        if frame is None or is_mostly_black(frame):
            frame = capture_window_screen(self._hwnd)
        if frame is not None and not is_mostly_black(frame):
            remember_good_frame(self._hwnd, frame)
            return frame
        return None

    def _refresh_frame(self) -> None:
        """定时重读缓存：有新帧且尺寸不变则重渲染；无新帧/尺寸变化则保持。"""
        frame = get_last_good_frame(self._hwnd)
        if frame is None or frame is self._frame:
            return
        if frame.shape[:2] != (self._fh, self._fw):
            return  # 尺寸变化的帧忽略，避免框/比例错位
        self._frame = frame
        self._render()

    def _default_box(self) -> tuple[float, float, float, float]:
        """默认框 = config.scoreboard_rect，经 window_rect 换算到帧坐标、按 91:47 折算比例、保持中心。"""
        cfg = load_config()
        layout = cfg.layout or {}
        win = layout.get("window_rect")
        if not win or len(win) != 4 or win[2] <= win[0] or win[3] <= win[1]:
            win = [0.0, 0.0, 1.0, 1.0]
        sb = layout.get("scoreboard_rect")
        if not sb or len(sb) != 4 or sb[2] <= sb[0] or sb[3] <= sb[1]:
            sb = (0.25, 0.30, 0.65, 0.67)
        ww, wh = win[2] - win[0], win[3] - win[1]
        fx1 = (sb[0] - win[0]) / ww
        fy1 = (sb[1] - win[1]) / wh
        fx2 = (sb[2] - win[0]) / ww
        fy2 = (sb[3] - win[1]) / wh
        cx = (fx1 + fx2) / 2
        cy = (fy1 + fy2) / 2
        h = fy2 - fy1
        w = h * self._ratio
        return self._clamp(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)

    def _clamp(self, x1, y1, x2, y2) -> tuple[float, float, float, float]:
        """框整体保持在 [0,1] 画面内（尺寸不变，只平移）。"""
        w, h = x2 - x1, y2 - y1
        if x1 < 0:
            x1, x2 = 0.0, w
        elif x2 > 1:
            x1, x2 = 1.0 - w, 1.0
        if y1 < 0:
            y1, y2 = 0.0, h
        elif y2 > 1:
            y1, y2 = 1.0 - h, 1.0
        return (x1, y1, x2, y2)

    def _move(self, dx_px: int, dy_px: int) -> None:
        dx, dy = dx_px / self._fw, dy_px / self._fh
        x1, y1, x2, y2 = self._box
        self._box = self._clamp(x1 + dx, y1 + dy, x2 + dx, y2 + dy)
        self._render()

    def _zoom(self, dh_px: int) -> None:
        """围绕中心缩放（高 ±1px），保持 91:47 比例，钳制尺寸与越界。"""
        x1, y1, x2, y2 = self._box
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        h = min(max((y2 - y1) + dh_px / self._fh, _MIN_H), _MAX_H)
        w = h * self._ratio
        self._box = self._clamp(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
        self._render()

    def _reset(self) -> None:
        self._box = self._default_box()
        self._render()

    def resizeEvent(self, event) -> None:
        """窗口/布局变化后重渲染，预览随窗口大小自适应（新版 label 无固定 720×405 最小尺寸）。"""
        super().resizeEvent(event)
        if self._frame is not None:
            # 延迟到布局更新完，确保 lab.width()/height() 是最终几何
            QTimer.singleShot(0, self._render)

    # ---------------- 渲染 ----------------

    def _render(self) -> None:
        rgb = cv2.cvtColor(self._frame, cv2.COLOR_BGR2RGB)
        img = QImage(rgb.data, self._fw, self._fh, rgb.strides[0], QImage.Format.Format_RGB888)
        base = QPixmap.fromImage(img.copy())

        lab = self.ui.label
        lw, lh = lab.width(), lab.height()
        scale_d = min(lw / self._fw, lh / self._fh)
        dw, dh = int(self._fw * scale_d), int(self._fh * scale_d)
        ox, oy = (lw - dw) // 2, (lh - dh) // 2
        base = base.scaled(dw, dh, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)

        x1, y1, x2, y2 = self._box
        # 框坐标在图像(dw×dh)坐标系内；ox/oy 只用于把 out 合成进 canvas 一次，不能再加进 box（否则偏移双加）
        box = (int(x1 * dw), int(y1 * dh), int((x2 - x1) * dw), int((y2 - y1) * dh))

        # 框外黑幕：整幅盖 30% 黑，框内区域清空（框内透明度 1、框外 0.7）
        veil = QPixmap(dw, dh)
        veil.fill(Qt.GlobalColor.transparent)
        p = QPainter(veil)
        p.fillRect(0, 0, dw, dh, QColor(0, 0, 0, _OUTSIDE_DIM_ALPHA))
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        p.fillRect(*box, Qt.GlobalColor.transparent)
        p.end()

        out = QPixmap(dw, dh)
        out.fill(Qt.GlobalColor.transparent)
        p = QPainter(out)
        p.drawPixmap(0, 0, base)
        p.drawPixmap(0, 0, veil)
        p.setPen(QPen(_BORDER_COLOR, 2))
        p.drawRect(*box)
        p.end()

        canvas = QPixmap(lw, lh)
        canvas.fill(Qt.GlobalColor.black)
        p = QPainter(canvas)
        p.drawPixmap(ox, oy, out)
        p.end()
        lab.setPixmap(canvas)

    # ---------------- 按钮 ----------------

    def _wire(self) -> None:
        for b in (self.ui.pushButton, self.ui.pushButton_2, self.ui.pushButton_3,
                  self.ui.pushButton_4, self.ui.pushButton_5, self.ui.pushButton_6):
            b.setAutoRepeat(True)
            b.setAutoRepeatDelay(_AUTOREPEAT_DELAY)
            b.setAutoRepeatInterval(_AUTOREPEAT_INTERVAL)
        self.ui.pushButton.clicked.connect(lambda: self._zoom(_ZOOM_STEP_PX))        # 放大
        self.ui.pushButton_2.clicked.connect(lambda: self._zoom(-_ZOOM_STEP_PX))     # 缩小
        self.ui.pushButton_3.clicked.connect(lambda: self._move(0, -_MOVE_STEP_PX))  # 上移
        self.ui.pushButton_4.clicked.connect(lambda: self._move(-_MOVE_STEP_PX, 0))  # 左移
        self.ui.pushButton_5.clicked.connect(lambda: self._move(_MOVE_STEP_PX, 0))   # 右移
        self.ui.pushButton_6.clicked.connect(lambda: self._move(0, _MOVE_STEP_PX))   # 下移
        self.ui.pushButton_7.clicked.connect(self._reset)                            # 恢复默认
        self.ui.pushButton_8.clicked.connect(self._finish)                           # 完成

    def _finish(self) -> None:
        cfg = load_config()
        layout = cfg.layout or {}
        win = layout.get("window_rect")
        if not win or len(win) != 4 or win[2] <= win[0] or win[3] <= win[1]:
            win = [0.0, 0.0, 1.0, 1.0]
        ww, wh = win[2] - win[0], win[3] - win[1]
        x1, y1, x2, y2 = self._box
        sb = [win[0] + x1 * ww, win[1] + y1 * wh, win[0] + x2 * ww, win[1] + y2 * wh]
        layout["scoreboard_rect"] = [round(v, 6) for v in sb]
        save_layout(layout)
        self.accept()
