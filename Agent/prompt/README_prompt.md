# prompt — 提示词构建

负责组装发给 LLM 的 System Prompt，采用分层注入策略。

## 文件说明

| 文件 | 职责 |
|------|------|
| `builder.py` | 分层组装：identity → tools → memory → volatile |
| `snapshot.py` | 会话开始时冻结 Hot Memory 快照，保证 prompt 稳定 |
| `__init__.py` | 包初始化 |

## 模板目录 `templates/`

| 模板 | 内容 |
|------|------|
| `identity.md` | Agent 身份与行为准则 |
| `tools_guide.md` | 工具使用指引（schema 说明、调用规范） |
| `evolution_guide.md` | 自我进化相关指引 |

## 分层注入顺序

```
1. identity    — 你是谁、行为边界（固定）
2. tools       — 可用工具说明（随注册工具变化）
3. memory      — Hot 快照 + 检索到的 Episodic/Skill（每轮可能不同）
4. volatile    — 当前任务上下文、临时指令（每轮更新）
```
