"""PySide6 QThread：由 GUI「开始读取计分板」按钮驱动。

流程：设置 DPI → 窗口预检（只允许窗口化 16:9）→ 启动采集 → OCR 预热 →
Tab 看门狗（游戏前台时按住 Tab 展开计分板，切回自动恢复）→
初始化识别（名称/干员）→ 实时识别写 scoreboard.json → 停止时清理。
异常捕获后发信号回 GUI，不冻结界面。
"""

from __future__ import annotations

import traceback

from PySide6.QtCore import QThread, Signal, Slot

from config import load_config
from capture import Capture, set_dpi_awareness
from capture.input_control import TabWatchdog
from capture.window_finder import validate_game_window
from recognition import RecognitionEngine

# Tab 按下后等待计分板渲染的缓冲：按键注入到游戏真正画出计分板有几帧延迟，
# 立即读会拿到计分板出现前的空画面（名称/干员首读全空）。
_TAB_SETTLE_S = 0.4


class RecognitionWorker(QThread):
    status = Signal(str)
    init_done = Signal(object)  # 初始化结果 players 列表
    error = Signal(str)
    frame_time = Signal(float)  # 每帧处理时长 ms
    frame_state = Signal(object)  # 每帧识别结果 scoreboard dict（供发送端每帧广播）

    def __init__(self, hwnd: int, config_path: str | None = None, parent=None):
        super().__init__(parent)
        self.hwnd = hwnd
        self.config_path = config_path
        self._engine: RecognitionEngine | None = None
        self._source: Capture | None = None

    def run(self) -> None:
        self._tab: TabWatchdog | None = None
        try:
            set_dpi_awareness()
            cfg = load_config(self.config_path) if self.config_path else load_config()

            msg = validate_game_window(self.hwnd)
            if msg:
                raise RuntimeError(msg)

            self.status.emit("初始化采集...")
            self._source = Capture(
                self.hwnd,
                fps_cap=cfg.capture.fps_cap,
                cache_size=cfg.capture.cache_size,
                method=cfg.capture.method,
            )
            self._source.start()

            # 等待首帧（采集线程就绪）
            for _ in range(100):
                if self._source.get_latest() is not None:
                    break
                self.msleep(50)

            self._engine = RecognitionEngine(cfg, self._source)
            self._engine.on_frame = self._emit_frame
            self.status.emit("OCR 预热中...")
            self._engine.warmup()

            self.status.emit("按住 Tab 读取计分板...")
            self._tab = TabWatchdog(self.hwnd)
            self._tab.start()
            # 等计分板真正渲染出来再首读：立即读只会拿到按 Tab 前的空画面
            self.msleep(int(_TAB_SETTLE_S * 1000))
            try:
                self.status.emit("初始化识别...")
                init_players = self._engine.run_initialization()
                self.init_done.emit(init_players)
                self.status.emit("实时识别中...")
                self._engine.run()
            finally:
                self._tab.stop()
                self._tab = None
        except Exception as exc:  # noqa: BLE001 - 顶层兜底，回传 GUI
            self.error.emit(f"{exc}\n{traceback.format_exc()}")
        finally:
            if self._source is not None:
                self._source.stop()
                self._source = None

    def _emit_frame(self, frame_id: int, process_ms: float, scoreboard: dict) -> None:
        self.frame_time.emit(process_ms)
        self.frame_state.emit(scoreboard)

    @Slot(object)
    def set_roster(self, agents: list | None) -> None:
        """GUI 线程调用，经 Qt 排队到本线程执行，传给引擎限制实时干员匹配。"""
        if self._engine is not None:
            self._engine.set_roster(agents)

    @Slot()
    def refresh_tab(self) -> bool:
        """GUI 点「Tab 刷新」：重新按下 Tab 展开计分板（手动 Tab 收起后兜底）。
        返回是否已直接按下（False 时看门狗会在游戏回到前台后自动补按）。"""
        if self._tab is not None:
            return self._tab.refresh()
        return False

    def stop(self) -> None:
        """请求停止（线程安全）：通知引擎退出循环，最后松开 Tab。"""
        if self._engine is not None:
            self._engine.stop()
