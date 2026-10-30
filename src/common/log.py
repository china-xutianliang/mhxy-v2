"""日志：同时写文件和内存环形缓冲（测试台日志页读取）。不记录任何密码。"""
from __future__ import annotations

import collections
import logging
import os
import threading
import time

from common.config import runtime_dir

_BUFFER: collections.deque = collections.deque(maxlen=5000)
_LOCK = threading.Lock()
_listeners: list = []


class _BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        line = self.format(record)
        with _LOCK:
            _BUFFER.append(line)
        for fn in list(_listeners):
            try:
                fn(line)
            except Exception:  # noqa: BLE001
                pass


_configured = False


def get_logger(name: str = "mhxy") -> logging.Logger:
    global _configured
    logger = logging.getLogger("mhxy")
    if not _configured:
        _configured = True
        logger.setLevel(logging.DEBUG)
        fmt = logging.Formatter("%(asctime)s %(levelname)s [%(slave)s] %(message)s", "%H:%M:%S")
        path = os.path.join(runtime_dir(), f"mhxy_{time.strftime('%Y%m%d')}.log")
        fh = logging.FileHandler(path, encoding="utf-8")
        bh = _BufferHandler()
        for h in (fh, bh):
            h.setFormatter(fmt)
            h.addFilter(_SlaveFilter())
            logger.addHandler(h)
    return logger


class _SlaveFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "slave"):
            record.slave = "-"
        return True


def log(msg: str, level: int = logging.INFO, slave: str = "-") -> None:
    get_logger().log(level, msg, extra={"slave": slave})


def add_listener(fn) -> None:
    _listeners.append(fn)


def recent(n: int = 500) -> list[str]:
    with _LOCK:
        return list(_BUFFER)[-n:]
