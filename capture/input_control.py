"""ctypes SendInput 键盘控制：长按/松开 Tab + 前台监控看门狗。

计分板（Tab 键）在游戏里是按住才显示。点「开始读取计分板」后持续按住
Tab（不中途松开），直到停止时松开。发送按键前先把游戏窗口设为前台，
并等焦点真正到位再发键（SetForegroundWindow 是异步的）。
看门狗只做兜底：检测到游戏在前台但 Tab 未按住时补按。
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes

from .window_finder import focus_window

user32 = ctypes.WinDLL("user32", use_last_error=True)

_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_VK_TAB = 0x09


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    # 必须含 mi/ki/hi 全部成员：sizeof(INPUT) 取决于 union 最大成员（MOUSEINPUT=32），
    # 只有 ki 会让 sizeof=32 而非真实的 40，SendInput 要求 cbSize==sizeof(INPUT)，
    # 传 32 会返回 ERROR_INVALID_PARAMETER(87)，按键注入不进去（实测 2026-08-10）。
    _fields_ = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = wintypes.SHORT


def _send_tab(keyup: bool) -> None:
    extra = ctypes.c_ulong(0)
    inp = _INPUT()
    inp.type = _INPUT_KEYBOARD
    inp.union.ki.wVk = _VK_TAB
    inp.union.ki.wScan = 0
    inp.union.ki.dwFlags = _KEYEVENTF_KEYUP if keyup else 0
    inp.union.ki.time = 0
    inp.union.ki.dwExtraInfo = ctypes.pointer(extra)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


def _tab_is_down() -> bool:
    """查询系统 Tab 键当前是否按下（真实状态，反映 SendInput 注入与用户键盘）。"""
    return bool(user32.GetAsyncKeyState(_VK_TAB) & 0x8000)


def _wait_foreground(hwnd: int, timeout: float = 1.0) -> bool:
    """轮询等待 hwnd 成为前台窗口（SetForegroundWindow 是异步的）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        h = user32.GetForegroundWindow()
        if h and int(h) == hwnd:
            return True
        time.sleep(0.02)
    return False


def press_tab_hold(hwnd: int | None = None) -> bool:
    """把窗口切到前台并按下 Tab（不松开）。返回是否成功按下。

    SetForegroundWindow 是异步的：切换后必须等焦点真正到位再发键，
    否则 Tab 会落到切换前的窗口（如 GUI）。Windows 前台锁可能拒绝第一次
    切换，二次调用通常能生效；超时仍失败则放弃，交给看门狗在游戏真正
    前台时补按。
    """
    if hwnd is not None:
        focus_window(hwnd)
        time.sleep(0.1)
        focus_window(hwnd)  # 二次尝试：SetForegroundWindow 常需调用两次才生效
        if not _wait_foreground(hwnd, timeout=2.0):
            return False
    _send_tab(keyup=False)
    return True


def release_tab() -> None:
    _send_tab(keyup=True)


class TabWatchdog:
    """持续按住 Tab，直到 stop() 才松开；只在游戏前台但未按住时兜底补按。"""

    def __init__(self, hwnd: int, interval: float = 0.2):
        self.hwnd = hwnd
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tab_down = False

    def start(self) -> None:
        """切游戏到前台并按下 Tab（持续按住），启动兜底监控线程。"""
        self._stop.clear()
        self._tab_down = press_tab_hold(self.hwnd)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        # 只在游戏前台且 Tab 未真正按住时补按；失焦不松开、不向 GUI 发键。
        # 用 GetAsyncKeyState 查真实按键状态：用户手动按 Tab 会发 keyup 抵消
        # 程序注入的 keydown，自记 _tab_down 会失同步（计分板收起后看门狗不补按），
        # 查真实状态即可让看门狗在 Tab 松开后自动恢复。
        while not self._stop.wait(self._interval):
            hwnd = user32.GetForegroundWindow()
            if bool(hwnd) and int(hwnd) == self.hwnd and not _tab_is_down():
                _send_tab(keyup=False)

    def stop(self) -> None:
        """停止并松开 Tab。"""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        # 用真实按键状态判断是否要松：自记标志可能被手动 keyup / 刷新失败清掉，
        # 用 GetAsyncKeyState 保证不残留按住（若 Tab 实际未按住则无需发 keyup）。
        if _tab_is_down():
            _send_tab(keyup=True)
        self._tab_down = False

    def refresh(self) -> bool:
        """重新按下 Tab（兜底：手动 Tab 松手让计分板收起后重开）。返回是否已直接按下。

        与「开始读取」等效：切游戏到前台后先发 keyup 再发 keydown，强制产生
        「松开→按下」边沿。若系统 Tab 仍处于被注入的按下状态，重复 keydown
        不产生边沿、游戏不重新显示计分板（开始读取是首次按下有边沿才显示）。
        """
        if not press_tab_hold(self.hwnd):
            self._tab_down = False
            return False
        _send_tab(keyup=True)
        time.sleep(0.05)
        _send_tab(keyup=False)
        self._tab_down = True
        return True
