"""接收端数据模型：把发送端 JSON 整理成适合 Overlay 使用的 GameState。

协议：每个玩家 11 字段 {slot,name,agent,side,alive,kills,deaths,assists,credits,weapon,armor}，
每项为 {value, confidence}，字段永不省略，失败时 value:null（见发送端 recognition/scoreboard.py）。
本阶段只保留 value，丢弃 confidence（Overlay 阶段需要低置信度变暗时再扩展）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PlayerData:
    slot: int
    name: str | None = None
    agent: str | None = None
    side: str | None = None
    alive: bool | None = None
    kills: int | None = None
    deaths: int | None = None
    assists: int | None = None
    credits: int | None = None
    weapon: str | None = None
    armor: str | None = None
    ult_ready: bool | None = None    # 大招就绪（"就绪"，无数字）
    ult_current: int | None = None  # 大招当前点数
    ult_max: int | None = None      # 大招点数上限（=干员大招 cost）


@dataclass
class GameState:
    version: int | None = None
    timestamp: float | None = None
    frame_id: int | None = None
    players: list[PlayerData] | None = None
    spike_carrier: str | None = None  # 爆能器携带者干员名，无爆能器为 None

    def __post_init__(self) -> None:
        if self.players is None:
            self.players = []


_STAT_FIELDS = ("kills", "deaths", "assists", "credits")


class StatsHoldover:
    """KDA/经济空值沿用：某玩家某字段为 None 时，用该玩家上一次非 None 的值。

    发送端识别瞬时失败会发 value:null，接收端逐帧沿用上次成功值，避免数字闪空。
    连接重置（新一局/重连）时调用 reset() 清空，防止跨局串数据。
    """

    def __init__(self) -> None:
        self._last: dict[int, dict[str, int]] = {}

    def reset(self) -> None:
        self._last.clear()

    def apply(self, gs: GameState) -> GameState:
        for p in gs.players:
            cached = self._last.setdefault(p.slot, {})
            for field in _STAT_FIELDS:
                value = getattr(p, field)
                if value is None:
                    setattr(p, field, cached.get(field))
                else:
                    cached[field] = value
        return gs


def _value(item):
    """协议字段是 {value, confidence}；容错发送端变体直接给标量。返回 value 或 None。"""
    if isinstance(item, dict):
        return item.get("value")
    return item


def _int(item) -> int | None:
    v = _value(item)
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _bool(item) -> bool | None:
    v = _value(item)
    if v is None:
        return None
    return bool(v)


def _parse_spike_carrier(spike) -> str | None:
    """顶层 spike："atk_干员" → 干员名；"none"/空 → None。"""
    if not isinstance(spike, str):
        return None
    spike = spike.strip()
    if spike.startswith("atk_"):
        return spike[4:]
    if spike in ("", "none"):
        return None
    return spike  # 容错：直接给干员名


def parse_scoreboard(raw) -> GameState:
    """把发送端 JSON（json.loads 后的 dict）解析成 GameState。

    顶层不是对象时抛 ValueError，由调用方捕获后走 error 信号，不崩 GUI。
    """
    if not isinstance(raw, dict):
        raise ValueError("JSON 顶层必须是对象")
    players = []
    for p in raw.get("players", []):
        if not isinstance(p, dict):
            continue
        slot = _int(p.get("slot"))
        if slot is None or not (1 <= slot <= 10):
            continue
        alive = _bool(p.get("alive"))
        armor = _value(p.get("armor"))
        if alive is False:
            # 死亡玩家计分板无护甲槽（与武器槽同理），护甲识别可能误配空槽 → 过滤
            armor = None
        ult = _value(p.get("ult"))
        ult_ready = ult_current = ult_max = None
        if isinstance(ult, dict):
            if ult.get("ready"):
                ult_ready = True
            else:
                ult_current = _int(ult.get("current"))
                ult_max = _int(ult.get("max"))
        players.append(
            PlayerData(
                slot=slot,
                name=_value(p.get("name")),
                agent=_value(p.get("agent")),
                side=_value(p.get("side")),
                alive=alive,
                kills=_int(p.get("kills")),
                deaths=_int(p.get("deaths")),
                assists=_int(p.get("assists")),
                credits=_int(p.get("credits")),
                weapon=_value(p.get("weapon")),
                armor=armor,
                ult_ready=ult_ready,
                ult_current=ult_current,
                ult_max=ult_max,
            )
        )
    players.sort(key=lambda x: x.slot)
    return GameState(
        version=_int(raw.get("version")),
        timestamp=_value(raw.get("timestamp")),
        frame_id=_int(raw.get("frame_id")),
        players=players,
        spike_carrier=_parse_spike_carrier(raw.get("spike")),
    )
