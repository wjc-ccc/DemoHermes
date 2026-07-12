# procedural — L3 程序性记忆（Skills）

存储 Agent 学会的可复用操作流程，类似 Cursor 的 Skills 机制。

## 文件说明

| 文件 | 职责 |
|------|------|
| `store.py` | Skill 文件持久化与 SQLite 索引 |
| `loader.py` | 三级渐进加载：name → params → body（按需展开，节省 token） |
| `registry.py` | Skill 注册表，管理所有可用 Skill |
| `__init__.py` | 包初始化 |

## Skill 目录

| 目录 | 用途 |
|------|------|
| `skills/`（项目根） | 内置/种子 Skill 模板 |
| `data/skills/` | 运行时由 Agent 自动创建或优化的 Skill |

## 与其他模块的关系

- `evolution/skill_creator.py` — 从复杂任务自动提取 Skill
- `evolution/skill_optimizer.py` — 使用过程中自我修正
- `tools/memory_tools.py` 的 `search_skill` — Agent 主动搜索 Skill
