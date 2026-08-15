"""
PathLockRegistry — 提供统一的对于所有文件的锁定和释放功能

- 同一规范化文件路径共享一把 threading.RLock，多个 MemoryManager 实例、多个线程写同一文件时互斥；
- 锁必须覆盖完整 read → validate → patch → backup/compact → replace；

"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class PathLockRegistry:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}

    def _lock_for(self, path: Path) -> threading.RLock:
        key = str(path.resolve())
        with self._guard:
            return self._locks.setdefault(key, threading.RLock())

    ### 核心函数 持有者处理文件前 先调用hold获取锁
    @contextmanager
    def hold(self, path: Path) -> Iterator[None]:
        lock = self._lock_for(path)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()


# 模块级单例：进程内所有 Manager/Store 共用
REGISTRY = PathLockRegistry()
