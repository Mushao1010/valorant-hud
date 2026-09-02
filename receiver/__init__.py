"""接收端：WebSocket 接收 + JSON 解析 + GameState + 调试窗口。

本阶段只做第一阶段调试窗口，后续在 Overlay 阶段复用 GameState/WsClient。
"""

from .game_state import GameState, PlayerData, parse_scoreboard
from .ws_client import WsClient

__all__ = ["GameState", "PlayerData", "parse_scoreboard", "WsClient"]
