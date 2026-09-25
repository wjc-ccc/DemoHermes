## 本项目核心为构建 Demo Cursor

自主 Agent 框架，目标复现 Cursor 的 Agent 能力并增强可观测性与自我进化。

### 当前状态（2026-09-24）

- CLI / HTTP（frontier 前端）双入口；Gateway 双队列调度 + AgentLoop 工具内循环
- Memory 已重构为五层领域模型：普通 Markdown 全量手册 + 独立 SQLite 事实/卡片/Trace + outbox 后台 Reviewer；旧 marker、Hot/Daily 与 Provider 总管链已移除
- 统一持久化入口 RuntimeStorage.open，运行库 data/agent.db 共 17 张表；Memory 三态支持显式读写和前端只读展示，详见 [SQLite 与 Memory 三态](docs/sqlite-memory-states.md)。
- 当前实施边界和文件职责见 `ai_memory_plan.md`；历史方案保留在 `docs/aiDocs/`

### 项目结构速览

```
DemoCursor/
├── main.py             # 一键启动：默认 HTTP :8000（frontier 后端）；--cli 终端模式
├── frontier/           # 可视化前端
├── agent/              # 核心代码包（小写；import 必须保持 agent）
│   ├── core/           # AgentLoop、PrecisionBuilder、ContextBudget、五层 Memory 领域
│   ├── gateway/        # 消息枢纽、SessionStore（dict + SQLite adapter）
│   ├── storage/sqlite/ # RuntimeStorage 与会话/三态/记忆/Job/Trace Store
│   ├── channel/        # 平台适配（CLI / frontier / 飞书 / 微信 / QQ）
│   ├── model/          # LLM 接入（claude / deepseek）
│   ├── tools/          # 工具注册；Memory 召回工具留待下一阶段
│   ├── skills/         # Skill 加载与注册
│   ├── prompt/         # 系统提示词
│   └── evolution/      # 与 Memory 分离的 Skill 自我进化
├── tests/              # pytest 入口；tests/memory/ 为 Memory TDD 测试
├── data/               # 运行时数据（记忆文件、日志）
├── logging/            # 日志工具
├── docs/               # 开发文档 → docs/README_docs.md
```
