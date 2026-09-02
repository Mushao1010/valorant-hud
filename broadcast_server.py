"""发送端 WebSocket 广播服务（push-only，原 mock_server 核心逻辑）。

监听一个端口，把外部传入的 dict 数据 JSON 序列化后广播给所有已连接客户端。
不读文件、不轮询——由调用方每识别一帧调用一次 broadcast_data(data) 推送一帧。

由 GUI 发送端页或 mock_server.py（CLI 封装）使用。
"""

from __future__ import annotations

import json
import socket

from PySide6.QtCore import QObject, Signal
from PySide6.QtWebSockets import QWebSocketServer


def _lan_ip() -> str:
    """取本机局域网 IP（用于向接收端显示连接地址）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


class BroadcastServer(QObject):
    started = Signal(str)       # ws://ip:port
    stopped = Signal()
    failed = Signal(str)        # 监听失败等错误
    client_count = Signal(int)  # 当前已连接接收端数量

    def __init__(self, parent=None):
        super().__init__(parent)
        self._server = QWebSocketServer(
            "HUD广播", QWebSocketServer.SslMode.NonSecureMode
        )
        self._server.newConnection.connect(self._on_new_connection)
        self._clients: list = []
        self._client_slots: dict = {}  # client -> disconnected 回调，stop 时用于解除

    @property
    def is_running(self) -> bool:
        return self._server.isListening()

    def address(self) -> str:
        return f"ws://{_lan_ip()}:{self._server.serverPort()}"

    def start(self, port: int) -> bool:
        """监听端口，准备广播；已在运行时直接返回 True。失败发 failed 信号返回 False。"""
        if self.is_running:
            return True
        if not self._server.listen(port=port):
            self.failed.emit(f"监听 ws://0.0.0.0:{port} 失败（端口被占用？）")
            return False
        self.started.emit(self.address())
        return True

    def stop(self) -> None:
        if not self.is_running and not self._clients:
            return
        for c in list(self._clients):
            slot = self._client_slots.get(c)
            if slot is not None:
                c.disconnected.disconnect(slot)  # 先解除回调，避免销毁后 emit 崩溃
            c.abort()
        self._clients.clear()
        self._client_slots.clear()
        self._server.close()
        self.stopped.emit()

    def broadcast_data(self, data: dict) -> None:
        """把一帧数据 JSON 序列化并推送给所有已连接客户端。"""
        if not self._clients:
            return
        payload = json.dumps(data, ensure_ascii=False)
        for c in self._clients:
            c.sendTextMessage(payload)

    # ---------------- 内部 ----------------

    def _on_new_connection(self) -> None:
        client = self._server.nextPendingConnection()
        slot = lambda c=client: self._remove(c)  # noqa: E731
        self._client_slots[client] = slot
        self._clients.append(client)
        client.disconnected.connect(slot)
        self.client_count.emit(len(self._clients))

    def _remove(self, client) -> None:
        if client in self._clients:
            self._clients.remove(client)
        self._client_slots.pop(client, None)
        self.client_count.emit(len(self._clients))
