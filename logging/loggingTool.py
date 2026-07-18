"""
统一日志配置模块

入口文件（cli / api）启动时调用一次 setup_logging()，
其他业务模块只需：

    import logging
    logger = logging.getLogger(__name__)
    logger.info("...")

配置来源：项目根目录 config.yml（LOG_PATH / LOG_LEVEL）

运行测试：python logging/loggingTool.py
"""
from __future__ import annotations

import logging as _stdlib_logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config.yml"
DEFAULT_LOG_PATH = ROOT / "data" / "logs" / "runtime.log"
DEFAULT_LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
LOG_DATE_FORMAT = "%H:%M:%S"

_CONFIGURED = False


def _parse_log_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    return getattr(_stdlib_logging, str(level).upper(), _stdlib_logging.INFO)


def load_log_config(config_path: Path | None = None) -> dict[str, Any]:
    """从 config.yml 读取日志配置。"""
    path = config_path or DEFAULT_CONFIG_PATH
    config: dict[str, Any] = {
        "LOG_PATH": "data/logs/runtime.log",
        "LOG_LEVEL": DEFAULT_LOG_LEVEL,
    }

    if not path.exists():
        return config

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if key in config:
            config[key] = value

    return config


def _resolve_log_path(raw_path: str | Path | None) -> Path | None:
    if not raw_path:
        return None
    path = Path(raw_path)
    return path if path.is_absolute() else ROOT / path


def setup_logging(
    *,
    level: str | int | None = None,
    log_file: str | Path | None = None,
    console: bool = True,
    config_path: Path | None = None,
    force: bool = True,
) -> None:
    """
    初始化全局日志（整个进程只调用一次）。

    参数优先级：函数参数 > config.yml > 默认值。
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    cfg = load_log_config(config_path)
    resolved_level = _parse_log_level(level or cfg["LOG_LEVEL"])
    file_target = _resolve_log_path(
        log_file if log_file is not None else cfg["LOG_PATH"]
    )

    formatter = _stdlib_logging.Formatter(fmt=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    handlers: list[_stdlib_logging.Handler] = []

    if console:
        stream_handler = _stdlib_logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        handlers.append(stream_handler)

    if file_target:
        file_target.parent.mkdir(parents=True, exist_ok=True)
        file_handler = _stdlib_logging.FileHandler(file_target, encoding="utf-8")
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    _stdlib_logging.basicConfig(level=resolved_level, handlers=handlers, force=force)
    _CONFIGURED = True


def get_logger(name: str | None = None) -> _stdlib_logging.Logger:
    """获取命名 logger，等价于 logging.getLogger(name)。"""
    return _stdlib_logging.getLogger(name)


def test() -> None:
    """验证日志级别过滤、文件写入、模块名显示。"""
    setup_logging(force=True)
    logger = get_logger("loggingTool.test")

    logger.debug("DEBUG：默认不可见（当前级别 INFO）")
    logger.info("INFO：正常输出")
    logger.warning("WARNING：正常输出")
    logger.error("ERROR：正常输出")

    cfg = load_log_config()
    log_file = _resolve_log_path(cfg["LOG_PATH"])
    print(f"当前配置: {cfg}")
    print(f"日志文件: {log_file}")


if __name__ == "__main__":
    test()
