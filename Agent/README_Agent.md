# Agent 包

Demo Cursor 的核心代码包，实现自主 Agent 的完整生命周期。

## 子目录一览

| 目录 | 职责 |
|------|------|
| `core/` | Agent 主循环、会话管理、上下文组装、事件模型 |
| `memory/` | 三层记忆系统（Hot / Episodic / Procedural） |
| `model/` | LLM 模型接入（DeepSeek、Claude SDK） |
| `tools/` | Agent 可调用的工具（文件、终端、记忆操作） |
| `prompt/` | System Prompt 分层构建与模板 |
| `evolution/` | 自我进化（记忆整理、Skill 创建与优化） |
| `gateway/` | 对外入口（CLI 终端 / HTTP API） |

## 根目录文件

| 文件 | 职责 |
|------|------|
| `Config.py` | 全局配置：从 `.env` 加载 API Key、模型名等 |
| `__init__.py` | 包初始化 |

## 数据流向（简图）

```
用户输入 → gateway → core/loop → model（思考）
                              → tools（行动）
                              → memory（持久化）
                              → evolution（后台进化）
```
