"""窗口采集 + 后台采集线程 + deque(maxlen=20) 帧缓存。

- 采集线程以 ≤ fps_cap（默认 20）持续抓帧，新帧入队、旧帧自动丢弃。
- 识别循环用 get_latest() 取最新帧，绝不在旧帧上做 OCR（见需求 §3.1）。

采集方式（cfg.capture.method）：
- printwindow：PrintWindow(PW_RENDERFULLCONTENT) 读取窗口**自身内容**，
  即使被其它窗口遮挡也正确（不再抓顶层屏幕），并裁剪到客户区（去标题栏）。
- screen：mss 抓客户区屏幕矩形（旧逻辑，谁在最上就抓谁）。
- auto（默认）：先 PrintWindow，返回 None 或全黑时回退 screen。
"""

from __future__ import annotations

import ctypes
import threading
import time
from collections import deque
from ctypes import wintypes

import cv2
import mss
import numpy as np

from .window_finder import client_rect

_PW_RENDERFULLCONTENT = 0x00000002

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
_user32.GetWindowRect.restype = wintypes.BOOL
_user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
_user32.GetClientRect.restype = wintypes.BOOL
_user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(_POINT)]
_user32.ClientToScreen.restype = wintypes.BOOL
_user32.GetWindowDC.argtypes = [wintypes.HWND]
_user32.GetWindowDC.restype = wintypes.HDC
_user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_user32.ReleaseDC.restype = ctypes.c_int
_user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
_user32.PrintWindow.restype = wintypes.BOOL
_user32.GetForegroundWindow.argtypes = []
_user32.GetForegroundWindow.restype = wintypes.HWND
_gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
_gdi32.CreateCompatibleDC.restype = wintypes.HDC
_gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC,
    ctypes.POINTER(_BITMAPINFO),
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.HANDLE,
    wintypes.UINT,
]
_gdi32.CreateDIBSection.restype = wintypes.HBITMAP
_gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi32.SelectObject.restype = wintypes.HGDIOBJ
_gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi32.DeleteObject.restype = wintypes.BOOL
_gdi32.DeleteDC.argtypes = [wintypes.HDC]
_gdi32.DeleteDC.restype = wintypes.BOOL


def capture_window_printwindow(hwnd) -> np.ndarray | None:
    """PrintWindow 读取窗口自身内容并裁剪到客户区，返回 BGR ndarray 或 None。

    PW_RENDERFULLCONTENT 让窗口把自身内容渲染进 DC，与遮挡无关；
    结果再按客户区偏移裁剪，去掉窗口化（有边框）的标题栏/边框。
    """
    h = wintypes.HWND(hwnd)
    wrect = _RECT()
    if not _user32.GetWindowRect(h, ctypes.byref(wrect)):
        return None
    w, hgt = wrect.right - wrect.left, wrect.bottom - wrect.top
    if w <= 0 or hgt <= 0:
        return None

    crect = _RECT()
    _user32.GetClientRect(h, ctypes.byref(crect))
    cw, ch = crect.right - crect.left, crect.bottom - crect.top
    origin = _POINT(0, 0)
    _user32.ClientToScreen(h, ctypes.byref(origin))
    ox, oy = origin.x - wrect.left, origin.y - wrect.top

    hdc_w = _user32.GetWindowDC(h)
    if not hdc_w:
        return None
    hdc_m = None
    bmp = None
    try:
        hdc_m = _gdi32.CreateCompatibleDC(hdc_w)
        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -hgt  # 顶向下
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        buf_ptr = ctypes.c_void_p()
        bmp = _gdi32.CreateDIBSection(hdc_m, ctypes.byref(bmi), 0, ctypes.byref(buf_ptr), None, 0)
        if not bmp or not buf_ptr.value:
            return None
        _gdi32.SelectObject(hdc_m, bmp)
        if not _user32.PrintWindow(h, hdc_m, _PW_RENDERFULLCONTENT):
            _user32.PrintWindow(h, hdc_m, 0)
        arr = np.ctypeslib.as_array((ctypes.c_ubyte * (w * hgt * 4)).from_address(buf_ptr.value))
        frame = arr.reshape(hgt, w, 4)[:, :, :3].copy()  # BGRA -> BGR
        # 裁剪客户区（去标题栏/边框）
        if cw > 0 and ch > 0:
            oy = max(0, min(oy, hgt))
            ox = max(0, min(ox, w))
            frame = frame[oy : oy + ch, ox : ox + cw]
        return frame
    finally:
        if bmp:
            _gdi32.DeleteObject(bmp)
        if hdc_m:
            _gdi32.DeleteDC(hdc_m)
        _user32.ReleaseDC(h, hdc_w)


def capture_window_screen(hwnd) -> np.ndarray | None:
    """mss 抓窗口客户区屏幕矩形（BGR，uint8）。

    供 PrintWindow 对 DX11 游戏（如无畏契约/Vanguard）返回黑帧时回退用——
    那类窗口 PrintWindow 始终黑，只能抓屏幕上实际显示的画面。
    """
    bbox = client_rect(hwnd)
    if bbox is None:
        return None
    try:
        with mss.mss() as sct:
            shot = sct.grab(bbox)
            return np.array(shot)[:, :, :3]
    except Exception:
        return None


def is_mostly_black(frame, threshold: float = 8.0) -> bool:
    if frame is None:
        return True
    return float(np.mean(frame)) < threshold


# 「失焦前 1 秒帧」缓存：游戏窗口失焦后 Tab 不再注入游戏进程，计分板收起，
# 此时抓到的画面没有计分板。识别循环只在游戏为前台窗口时把帧写入本环形缓冲
# （带时间戳），预览等场景取「最后一帧前台画面再往前约 1 秒」那一帧静态显示——
# 最后一帧可能正处于失焦/计分板切换的不稳定状态，1 秒前更稳定。
_GOOD_WINDOW = 30         # 缓存最近 30 帧前台画面（20fps≈1.5s），环形覆盖
_LOOKBACK_S = 1.0         # 取失焦前 1 秒的帧
_last_good_lock = threading.Lock()
_last_good: deque = deque(maxlen=_GOOD_WINDOW)  # (hwnd, time.monotonic, frame)


def _is_foreground(hwnd: int) -> bool:
    """该窗口当前是否为前台窗口（游戏是否持有焦点）。"""
    fg = _user32.GetForegroundWindow()
    return bool(fg) and int(fg) == int(hwnd)


def remember_good_frame(hwnd: int, frame: np.ndarray | None) -> None:
    """记录游戏窗口前台时的一帧（供失焦后回退显示）。失焦/黑帧不计。

    黑帧必须过滤：PrintWindow 对 DX11 游戏在最小化/被遮挡/独占全屏时返回纯黑帧，
    printwindow 采集模式没有黑帧回退，不拦的话「好帧」缓存会被黑帧污染，预览直接黑屏。
    """
    global _last_good
    if frame is None or is_mostly_black(frame) or not _is_foreground(hwnd):
        return
    with _last_good_lock:
        _last_good.append((int(hwnd), time.monotonic(), frame))


def get_last_good_frame(hwnd: int | None = None) -> np.ndarray | None:
    """取「失焦前约 1 秒」的稳定帧；hwnd 指定时只返回该窗口的缓存，跨窗口不串帧。

    以最后一帧前台画面时间为失焦时刻，向前回退 _LOOKBACK_S，取时间戳最接近
    的那一帧；缓冲不足 1 秒时退回最早的一帧。
    """
    with _last_good_lock:
        if not _last_good:
            return None
        frames = [f for f in _last_good if hwnd is None or f[0] == int(hwnd)]
        if not frames:
            return None
        target = frames[-1][1] - _LOOKBACK_S
        best = min(frames, key=lambda f: abs(f[1] - target))
        return best[2]


class Capture:
    """实时窗口采集。创建前必须先调用 set_dpi_awareness()。"""

    def __init__(self, hwnd: int, fps_cap: float = 20.0, cache_size: int = 20, method: str = "auto"):
        self.hwnd = hwnd
        self.fps_cap = max(1.0, fps_cap)
        self.method = method
        self._cache: deque = deque(maxlen=max(1, cache_size))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._seq = 0  # 采集序号：每成功入队一帧 +1，识别循环用它判断是否有新帧
        self._sct = mss.mss()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None
        self._sct.close()

    def grab_frame(self) -> np.ndarray | None:
        """抓取当前一帧（BGR，uint8，客户区画面）。失败返回 None。

        PrintWindow 返回 None/黑帧时回退 mss 截屏（DX11 游戏如无畏契约 PrintWindow
        恒黑，只能抓屏幕实际画面）。非黑帧都尝试记入「最后一帧好画面」缓存——
        remember 内部再按前台窗口判断，屏幕回退帧只在游戏为前台（最上、无重叠）时
        才可能记入，否则被前台判断挡住，不会混入重叠的 GUI。
        """
        if self.method in ("printwindow", "auto"):
            frame = capture_window_printwindow(self.hwnd)
            if frame is None or is_mostly_black(frame):
                frame = self._grab_screen()
        else:
            frame = self._grab_screen()
        # remember 内部再过滤黑帧/非前台，黑帧或失焦时自动不计
        remember_good_frame(self.hwnd, frame)
        return frame

    def _grab_screen(self) -> np.ndarray | None:
        """mss 抓客户区屏幕矩形（回退/显式 screen 路径）。"""
        bbox = client_rect(self.hwnd)
        if bbox is None:
            return None
        try:
            shot = self._sct.grab(bbox)
            return np.array(shot)[:, :, :3]
        except Exception:
            return None

    def _run(self) -> None:
        interval = 1.0 / self.fps_cap
        while not self._stop.is_set():
            start = time.perf_counter()
            frame = self.grab_frame()
            if frame is not None:
                with self._lock:
                    self._seq += 1
                    self._cache.append(frame)
            elapsed = time.perf_counter() - start
            time.sleep(max(0.0, interval - elapsed))

    def get_latest(self) -> np.ndarray | None:
        """返回缓存中最新的帧（识别循环专用）。"""
        with self._lock:
            if not self._cache:
                return None
            return self._cache[-1]

    def get_latest_seq(self) -> int:
        """最近入队帧的序号：新帧到来会递增，识别循环用它跳过未变化帧。"""
        with self._lock:
            return self._seq

    def frame_count(self) -> int:
        with self._lock:
            return len(self._cache)


class StaticImageSource:
    """离线图片帧源：固定返回同一张图片（BGR）。用于 --image 模式验证。"""

    def __init__(self, path: str):
        frame = cv2.imread(path, cv2.IMREAD_COLOR)
        if frame is None:
            raise FileNotFoundError(f"无法读取图片: {path}")
        self._frame = frame
        self._seq = 0

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def get_latest(self) -> np.ndarray:
        self._seq += 1  # 静态图每次取都视为「新帧」，离线模式才能按 max_frames 跑 N 帧
        return self._frame

    def get_latest_seq(self) -> int:
        return self._seq

    def frame_count(self) -> int:
        return 1
