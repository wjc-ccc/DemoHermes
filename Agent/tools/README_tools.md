# tools — Agent 工具系统

Agent 在对话中可以调用的"手脚"，扩展其能力边界。

## 文件说明

| 文件 | 职责 |
|------|------|
| `base.py` | Tool 抽象基类，定义统一接口（name、schema、execute） |
| `registry.py` | 工具注册表：收集 schema、分发调用、检查可用性 |
| `file_tools.py` | 文件操作：read_file、write_file、search_files 等 |
| `bash_tools.py` | 终端操作：执行 shell 命令 |
| `memory_tools.py` | 记忆操作：recall_memory、save_memory、search_skill |
| `__init__.py` | 包初始化 |

## 调用流程

```
LLM 返回 tool_call → registry 分发 → 具体 Tool.execute() → 结果回注上下文
```

## 与其他模块的关系

- `prompt/templates/tools_guide.md` — 告诉 LLM 如何使用这些工具
- `core/events.py` — 每次 tool_call 产生事件，供可视化展示
