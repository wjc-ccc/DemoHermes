"""数据库只索引 UTC 毫秒；领域 JSON 保留原本的时间表示。"""
from datetime import datetime, timezone
import json
import time


def milliseconds(value=None):
    if value is None:
        return int(time.time() * 1000)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError('timestamp must include timezone')
        return int(value.timestamp() * 1000)
    return int(value * 1000)


def utcnow():
    return datetime.now(timezone.utc)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
