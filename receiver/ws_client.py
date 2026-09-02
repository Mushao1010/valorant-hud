"""WebSocket 客户端封装：QWebSocket 异步接收 + 自动重连。

不阻塞 GUI：I/O 由 Qt 事件循环处理，各信号在主线程回调，
比「QThread + websocket-client」更简单可靠，无需跨线程同步。
"""

from __future__ import annotations

import json

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QAbstractSocket
from PySide6.QtWebSockets import QWebSocket

from .game_state import StatsHoldover, parse_scoreboard

_BASE_DELAY_MS = 1000
_MAX_DELAY_MS = 30000


class WsClient(QObject):
    connection_state = Signal(str)        # 「连接中... / 已连接 / 已断开 / 已手动断开 / N 秒后重连」
    game_state_received = Signal(object)  # GameState
    error_occurred = Signal(str)          # JSON 解析失败等数据错误

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ws = QWebSocket()
        self._url: QUrl | None = None
        self._attempt = 0
        self._closing = False
        self._holdover = StatsHoldover()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._open)
        self._ws.connected.connect(self._on_connected)
        self._ws.disconnected.connect(self._on_disconnected)
        self._ws.textMessageReceived.connect(self._on_text)
        self._ws.errorOccurred.connect(self._on_error)

    @property
    def is_connected(self) -> bool:
        return self._ws.state() == QAbstractSocket.ConnectedState

    def connect_to(self, url: str) -> None:
        """连接到新地址：重置重连计数，立即打开。"""
        self._closing = False
        self._attempt = 0
        self._timer.stop()
        self._url = QUrl(url)
        self._ws.abort()  # 立即断开旧连接（若有），避免 open 与旧状态冲突
        self._open()

    def disconnect(self) -> None:
        """手动断开：不再自动重连。"""
        self._closing = True
        self._timer.stop()
        self._ws.close()
        self.connection_state.emit("已手动断开")

    def stop(self) -> None:
        self._closing = True
        self._timer.stop()
        self._ws.close()

    # ---------------- 内部 ----------------

    def _open(self) -> None:
        if self._url is None or self._closing:
            return
        self._timer.stop()
        self.connection_state.emit("连接中...")
        self._ws.open(self._url)

    def _schedule_reconnect(self) -> None:
        if self._closing or self._timer.isActive():
            return
        delay = min(_MAX_DELAY_MS, _BASE_DELAY_MS * (2 ** min(self._attempt, 5)))
        self._attempt += 1
        self.connection_state.emit(f"{delay // 1000} 秒后重连")
        self._timer.start(delay)

    def _on_connected(self) -> None:
        self._attempt = 0
        self._timer.stop()
        # 新连接视为新会话，清空 KDA/经济沿用缓存，防止跨局串数据
        self._holdover.reset()
        self.connection_state.emit("已连接")

    def _on_disconnected(self) -> None:
        self.connection_state.emit("已断开")
        self._schedule_reconnect()

    def _on_error(self, error) -> None:
        # 连接错误：状态标签已反映，交给重连逻辑，不当数据错误上报（避免刷屏）
        self._schedule_reconnect()

    def _on_text(self, message: str) -> None:
        try:
            gs = parse_scoreboard(json.loads(message))
        except Exception as exc:
            self.error_occurred.emit(f"解析失败: {exc}")
            return
        # KDA/经济空值沿用上次非空值（识别瞬时失败不闪空）
        self._holdover.apply(gs)
        self.game_state_received.emit(gs)
