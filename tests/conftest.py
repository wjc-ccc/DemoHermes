"""Gateway 测试使用独立临时 SQLite，会话提交路径与 Runtime 一致。"""
import pytest

from agent.storage.sqlite.runtime_storage import RuntimeStorage


@pytest.fixture
def session_store(tmp_path):
    return RuntimeStorage.open(tmp_path / "agent.db").sessions
