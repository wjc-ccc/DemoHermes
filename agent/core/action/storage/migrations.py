"""由 RuntimeStorage 的统一迁移器加载；不自行初始化应用数据库。"""
from pathlib import Path


def schema_directory():
    return Path(__file__).with_name("schema")

