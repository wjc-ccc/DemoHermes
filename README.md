## 本项目核心为构建 Demo Cursor

自主 Agent 框架，目标复现 Cursor 的 Agent 能力并增强可观测性与自我进化。

### 初步计划目标（截止日期 7.13）

- 通过 claude-agent-sdk 复现和使用 Claude 的所有能力
- 可视化所有步骤流程和完整上下文，增强 Agent 可复现性
- 实现自主进化（记忆整理、Skill 自动创建与优化）

### 项目结构速览

```
DemoCursor/
├── agent/              # 核心代码包 → Agent/README_Agent.md
│   ├── core/           # 主循环、会话、上下文 → README_core.md
│   ├── memory/         # 三层记忆系统 → README_memory.md
│   ├── model/          # LLM 接入 → README_model.md
│   ├── tools/          # 工具系统 → README_tools.md
│   ├── prompt/         # 提示词构建 → README_prompt.md
│   ├── evolution/      # 自我进化 → README_evolution.md
│   └── gateway/        # CLI / API 入口 → README_gateway.md
├── data/               # 运行时数据 → README_data.md
├── skills/             # 内置 Skill 种子 → README_skills.md
├── logging/            # 日志工具 → README_Logging.md
├── docs/               # 开发进度 → README_docs.md
```

每个目录下都有 `README_目录名.md`，介绍该目录的职责和文件说明。
