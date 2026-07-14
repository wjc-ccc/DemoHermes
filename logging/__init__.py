"""
项目日志包：保留标准库 logging API，并提供 setup_logging / get_logger。

入口文件：
    from logging.loggingTool import setup_logging
    setup_logging()

业务文件：
    import logging
    logger = logging.getLogger(__name__)
"""
import importlib.util
import sys
from pathlib import Path

from .loggingTool import get_logger, setup_logging


def _load_stdlib_logging():
    """从标准库路径直接加载 logging，避免与当前包名冲突。"""
    candidates = [
        Path(sys.base_prefix) / "Lib" / "logging" / "__init__.py",
        Path(sys.base_prefix) / "lib" / "logging" / "__init__.py",
    ]
    for path in candidates:
        if not path.exists():
            continue
        spec = importlib.util.spec_from_file_location("_stdlib_logging", path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    raise ImportError("无法定位标准库 logging 模块")


_stdlib_logging = _load_stdlib_logging()

for _name in dir(_stdlib_logging):
    if _name.startswith("_"):
        continue
    if _name not in ("setup_logging", "get_logger"):
        globals()[_name] = getattr(_stdlib_logging, _name)

__all__ = ["setup_logging", "get_logger", *getattr(_stdlib_logging, "__all__", [])]
