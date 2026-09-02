"""大招图标与点数上限查找。

读取 images/ult/{Agent}_{cost}.png 文件名，提供按干员名查找大招图标文件名与点数上限。
文件名里的干员名与 images/icon 的干员图标名可能略有出入
（如 Miks_icon ↔ Mik_8、Skye_icon ↔ Skey_8），查找按
精确 → 前缀包含 → 编辑距离≤2 逐级容错。发送端用它求 max，接收端用它找大招图标文件。
"""

from __future__ import annotations

import glob
import os

_MAX_DIST = 2  # 编辑距离容差（覆盖 Miks/Mik、Skye/Skey 这类拼写差异）


def ult_entries(ult_dir: str) -> list[tuple[str, int]]:
    """扫描目录，返回 [(干员名, 点数上限), ...]（按文件名字典序）。"""
    entries = []
    for path in sorted(glob.glob(os.path.join(ult_dir, "*.png"))):
        base = os.path.basename(path)[: -len(".png")]
        name, _, cost_s = base.rpartition("_")
        if not name or not cost_s.isdigit():
            continue
        entries.append((name, int(cost_s)))
    return entries


def _levenshtein(a: str, b: str) -> int:
    """编辑距离（用于个别拼写差异容错）。"""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[lb]


def _common_prefix_len(a: str, b: str) -> int:
    """公共前缀长度（编辑距离相同时更相似的优先）。"""
    n = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        n += 1
    return n


def resolve_ult(entries: list[tuple[str, int]], agent: str | None) -> tuple[str, int] | None:
    """在 ult_entries 结果里按干员名解析，返回 (干员名, 点数上限)，找不到返回 None。

    精确 → 前缀包含 → 编辑距离≤2。entries 可缓存，避免每帧重扫目录。
    编辑距离并列时取公共前缀更长的（如 Skye 应匹配 Skey 而非距离同为 2 的 Sage）。
    """
    if not agent or not entries:
        return None
    for name, cost in entries:
        if name == agent:
            return name, cost
    # 容错：前缀包含最可靠（Miks→Mik），编辑距离兜底（Skye→Skey）
    best = None  # (dist, -公共前缀长, name, cost)
    for name, cost in entries:
        if agent.startswith(name) or name.startswith(agent):
            return name, cost
        d = _levenshtein(agent, name)
        if d <= _MAX_DIST:
            key = (d, -_common_prefix_len(agent, name), name)
            if best is None or key < best[0]:
                best = (key, name, cost)
    if best is None:
        return None
    return best[1], best[2]


def find_ult(ult_dir: str, agent: str | None) -> tuple[str, int] | None:
    """按干员名查大招图标，返回 (图标文件名如 "Astra_7.png", 点数上限)，找不到返回 None。"""
    hit = resolve_ult(ult_entries(ult_dir), agent)
    if hit is None:
        return None
    name, cost = hit
    return f"{name}_{cost}.png", cost
