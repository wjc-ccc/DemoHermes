"""
PathLockRegistry — 进程级文件锁注册表（7.23 方案 §5.1）

- 模块级单例：同一规范化文件路径共享一把 threading.RLock，
  多个 MemoryManager 实例、多个线程写同一文件时互斥；
- 锁必须覆盖完整 read → validate → patch → backup/compact → replace；
- 原子 rename 只防半文件，真正的并发保证来自这把锁（§1.2）。

非保证（§5.2）：任意外部编辑器在 rename 窗口写入、多 Python 进程共享目录。
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
