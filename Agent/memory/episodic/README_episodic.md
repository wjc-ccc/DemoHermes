# episodic — L2 情景记忆

记录每轮对话与工具调用的完整轨迹，支持全文检索。

## 文件说明

| 文件 | 职责 |
|------|------|
| `db.py` | SQLite 数据库 schema 定义与连接管理 |
| `recorder.py` | 记录每轮对话内容、工具调用与结果 |
| `fts.py` | FTS5 全文检索，按关键词召回历史片段 |
| `__init__.py` | 包初始化 |

## 使用场景

- Agent 需要回忆"之前做过什么"时，通过 `tools/memory_tools.py` 的 `recall_memory` 检索
- 可视化前端通过 `core/events.py` 回放完整对话轨迹
