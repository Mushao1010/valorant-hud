"""ctypes 窗口枚举 + HWND → 屏幕矩形。

DPI 感知必须先于 mss 创建和 GetWindowRect，否则 125%/150% 缩放下
GetWindowRect 返回逻辑像素、mss 抓物理像素，产生坐标错位。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import NamedTuple

user32 = ctypes.WinDLL("user32", use_last_error=True)


def set_dpi_awareness() -> bool:
    """进程级 DPI 感知，只能设置一次。返回是否成功。"""
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        # PROCESS_PER_MONITOR_DPI_AWARE = 2
        shcore.SetProcessDpiAwareness(2)
        return True
    except Exception:
        try:
            user32.SetProcessDPIAware()
            return True
        except Exception:
            return False


class WindowInfo(NamedTuple):
    hwnd: int
    title: str
    pid: int


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_long),
        ("y", ctypes.c_long),
    ]


_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

_GWL_STYLE = -16
_WS_CAPTION = 0x00C00000  # WS_BORDER | WS_DLGFRAME（有标题栏的普通窗口化）

user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
user32.GetClientRect.restype = wintypes.BOOL
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(_POINT)]
user32.ClientToScreen.restype = wintypes.BOOL
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL


def _get_title(hwnd) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _get_pid(hwnd) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def list_windows() -> list[WindowInfo]:
    """枚举所有可见、有标题的顶层窗口。"""
    result: list[WindowInfo] = []

    @_WNDENUMPROC
    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _get_title(hwnd)
        if not title:
            return True
        result.append(WindowInfo(int(hwnd), title, _get_pid(hwnd)))
        return True

    user32.EnumWindows(_cb, 0)
    return result


def find_windows(keyword: str | None = None, pid: int | None = None) -> list[WindowInfo]:
    """按标题关键字 / 进程 ID 过滤窗口列表。"""
    windows = list_windows()
    if keyword:
        kw = keyword.lower()
        windows = [w for w in windows if kw in w.title.lower()]
    if pid is not None:
        windows = [w for w in windows if w.pid == pid]
    return windows


def window_rect(hwnd) -> dict | None:
    """返回窗口屏幕矩形 {left, top, width, height}，失败返回 None。"""
    rect = _RECT()
    if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return None
    return {
        "left": rect.left,
        "top": rect.top,
        "width": rect.right - rect.left,
        "height": rect.bottom - rect.top,
    }


def focus_window(hwnd) -> bool:
    return bool(user32.SetForegroundWindow(wintypes.HWND(hwnd)))


def client_rect(hwnd) -> dict | None:
    """返回窗口客户区屏幕矩形 {left, top, width, height}，失败返回 None。

    游戏窗口化（有边框）时客户区是游戏画面本身，去掉标题栏/边框。
    """
    h = wintypes.HWND(hwnd)
    crect = _RECT()
    if not user32.GetClientRect(h, ctypes.byref(crect)):
        return None
    origin = _POINT(0, 0)
    if not user32.ClientToScreen(h, ctypes.byref(origin)):
        return None
    return {
        "left": origin.x,
        "top": origin.y,
        "width": crect.right - crect.left,
        "height": crect.bottom - crect.top,
    }


def validate_game_window(hwnd) -> str | None:
    """按运行约束校验游戏窗口，返回错误信息，合法返回 None。

    约束：只允许「窗口化（有边框）」模式 + 16:9（1920×1080）分辨率；
    无畏契约不支持无边框窗口（会强制全屏）。
    """
    crect = client_rect(hwnd)
    if crect is None or crect["width"] <= 0 or crect["height"] <= 0:
        return "无法获取窗口客户区尺寸，请确认游戏窗口有效"
    w, h = crect["width"], crect["height"]
    ratio = w / h
    if not (16 / 9 * 0.97 <= ratio <= 16 / 9 * 1.03):
        return f"窗口比例 {w}x{h} 非 16:9，请将游戏设为「窗口化（有边框）」并保持 1920×1080（16:9）"
    style = user32.GetWindowLongW(wintypes.HWND(hwnd), _GWL_STYLE)
    if not (style & _WS_CAPTION):
        return "窗口像是全屏/无边框（无标题栏），请改用「窗口化（有边框）」模式运行游戏"
    return None
