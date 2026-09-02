"""数据模型：保证 JSON 里每个字段始终存在（spec §13）。

任何字段识别失败都必须输出 {"value": null, "confidence": 实际置信度}，
不允许省略字段、空字符串或 "unknown"。
"""

from __future__ import annotations

# 每个玩家固定出现的字段顺序
FIELD_KEYS = (
    "name",
    "agent",
    "side",
    "alive",
    "kills",
    "deaths",
    "assists",
    "credits",
    "weapon",
    "armor",
    "ult",
)


def field(value, confidence: float) -> dict:
    return {"value": value, "confidence": round(float(confidence), 4)}


def empty_field() -> dict:
    return {"value": None, "confidence": 0.0}


def make_player(slot: int, values: dict | None = None) -> dict:
    """构造单个玩家记录。values 为 {字段: (value, confidence)}，缺失字段补空值。"""
    player = {"slot": slot}
    values = values or {}
    for key in FIELD_KEYS:
        if key in values:
            v, c = values[key]
            player[key] = field(v, c)
        else:
            player[key] = empty_field()
    return player


def make_scoreboard(
    frame_id: int, timestamp: float, players: list[dict], spike: str = "none", version: int = 1
) -> dict:
    """spike: 爆能器所属进攻方干员名（"atk_干员"），无爆能器为 "none"。"""
    return {
        "version": version,
        "timestamp": timestamp,
        "frame_id": frame_id,
        "players": players,
        "spike": spike,
    }
