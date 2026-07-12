# hot — L1 热记忆

始终注入 LLM 上下文的"短期常驻记忆"，以 Markdown 文件存储。

## 文件说明

| 文件 | 职责 |
|------|------|
| `store.py` | 读写 `data/memories/USER.md` 和 `MEMORY.md` |
| `curator.py` | 字符上限控制、内容合并与去重，防止文件膨胀 |
| `__init__.py` | 包初始化 |

## 对应数据文件

| 数据文件 | 内容 |
|----------|------|
| `data/memories/USER.md` | 用户画像（偏好、背景、习惯） |
| `data/memories/MEMORY.md` | Agent 长期笔记（项目上下文、重要结论） |

## 特点

- 会话开始时由 `prompt/snapshot.py` 冻结快照，保证本轮 prompt 稳定
- 由 `evolution/memory_curator.py` 定期自动整理
