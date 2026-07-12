# templates — System Prompt 模板

存放注入 LLM 的静态模板文件，由 `prompt/builder.py` 加载。

| 模板 | 注入层级 | 内容 |
|------|----------|------|
| `identity.md` | 第 1 层 identity | Agent 身份与行为准则 |
| `tools_guide.md` | 第 2 层 tools | 工具使用指引 |
| `evolution_guide.md` | evolution 模块 | 自我进化判断准则 |

模板中使用 HTML 注释说明用途，不会被注入到 prompt 中。
