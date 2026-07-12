# model — LLM 模型接入层

统一封装不同 LLM 提供商，对上层（core/loop）暴露一致的 `chat()` 接口。

## 文件说明

| 文件 | 职责 | 状态 |
|------|------|------|
| `BaseModel.py` | 抽象基类，定义 `chat(messages) -> str` | ✅ 已实现 |
| `deepseek.py` | DeepSeek 模型接入（OpenAI SDK 兼容） | ✅ 测试脚本可用 |
| `claude_sdk.py` | Claude Agent SDK Provider | 🔲 待实现 |
| `registry.py` | 模型注册表，按名称获取对应 Provider | 🔲 待实现 |
| `__init__.py` | 包初始化 | — |

## 配置来源

所有 API Key 和模型名从 `Agent/Config.py` 读取，对应 `.env` 中的：
- `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL`
- `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL`
- `MODEL`（默认 `deepseek-v4-flash`）
