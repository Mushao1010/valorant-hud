"""原子 JSON 写入：先写 tmp 再 os.replace，避免 GUI 读到半截文件。

os.replace 在 Windows 底层是 MoveFileEx(REPLACE_EXISTING)，目标文件被
另一个进程瞬时占用（杀毒/Defender 扫描、编辑器打开、多实例）时会抛
WinError 5/32 拒绝访问。这里对瞬时占用做短重试；重试耗尽则跳过本帧写入
并告警，而不是把异常抛回识别循环导致整个会话停止。
"""

from __future__ import annotations

import errno
import json
import os
import sys
import time

# 瞬时占用类错误：errno（跨平台）或 Windows 错误码（winerror）
_TRANSIENT_ERRNO = (errno.EACCES, errno.EPERM, errno.EBUSY)
_TRANSIENT_WIN = (5, 32, 33)  # 拒绝访问 / 共享冲突 / 文件被占用


def _is_transient(exc: OSError) -> bool:
    win = getattr(exc, "winerror", None)
    if win is not None and win in _TRANSIENT_WIN:
        return True
    return exc.errno in _TRANSIENT_ERRNO


class JsonWriter:
    def __init__(self, path: str, retries: int = 8, delay: float = 0.05):
        self.path = path
        self.retries = retries
        self.delay = delay

    def write(self, data: dict) -> None:
        abs_path = os.path.abspath(self.path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        tmp_path = abs_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        for attempt in range(self.retries):
            try:
                os.replace(tmp_path, abs_path)
                return
            except OSError as exc:
                if not _is_transient(exc):
                    raise
                if attempt == self.retries - 1:
                    # 持续被占用：跳过本帧（下帧会再写），别让识别循环崩溃
                    print(
                        f"[json_writer] 写入 {abs_path} 持续被占用，跳过本帧: {exc}",
                        file=sys.stderr,
                    )
                    return
                time.sleep(self.delay)
