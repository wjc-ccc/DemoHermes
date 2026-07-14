# memory — 三层记忆系统

Agent 的"长期记忆"，分三层存储不同粒度的信息。

## 三层架构

| 层级 | 目录 | 存储方式 | 内容 |
|------|------|----------|------|
| L1 Hot | `hot/` | Markdown 文件 | 用户画像、长期笔记（始终注入 prompt） |
| L2 Episodic | `episodic/` | SQLite + FTS5 | 每轮对话与工具调用轨迹（按需检索） |
| L3 Procedural | `procedural/` | Skill 文件 + SQLite | 可复用的操作流程（渐进加载） |

## 根目录文件

| 文件 | 职责 |
|------|------|
| `manager.py` | 记忆编排中枢，统一调度三层读写 |
| `base.py` | MemoryStore / MemoryProvider 抽象基类 |
| `__init__.py` | 包初始化 |

## 运行时数据目录

实际数据存放在项目根 `data/` 下：
- `data/memories/USER.md` — 用户画像
- `data/memories/MEMORY.md` — Agent 长期笔记
- `data/skills/` — 运行时 Skill 文件
