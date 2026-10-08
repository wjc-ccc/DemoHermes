## 本项目核心为构建 Demo Cursor

自主 Agent 框架，目标复现 Cursor 的 Agent 能力并增强可观测性与自我进化。

### 当前状态（2026-09-28）

- CLI / HTTP（frontier 前端）双入口；Gateway 双队列调度 + AgentLoop 工具内循环
- Memory 使用有界 `MEMORY.md` 手册、最多三张关键词经验卡片和按需 `memory_search` / `memory_read` / `memory_edit` 工具；完成对话按 1、2、3、4、5 轮渐进批处理，校验通过的卡片直接可用。
- 统一持久化入口 `RuntimeStorage.open`，运行库 `data/agent.db` 保留原文、卡片版本、手册版本、作业和 Trace。详见 [SQLite 与 Memory 存储](docs/sqlite-memory-storage.md) 和 [当前 Memory 运行链路](docs/recognitionDocs/memory.md)。
- 历史设计方案保留在 `docs/aiDocs/`，其中旧 Hot/Daily/Provider 方案不代表当前生产路径。

架构整理从 [模块架构总览](docs/模块架构总览.md) 开始：按感知、推理、执行、反思、记忆、治理、协作七个能力视角，对照当前代码说明运行链路、模块接口和实现状态。细节入口见 [架构认知文档](docs/recognitionDocs/README.md)。

### 项目结构速览

```
DemoCursor/
├── main.py             # 一键启动：默认 HTTP :8000（frontier 后端）；--cli 终端模式
├── frontier/           # 可视化前端
├── agent/              # 核心代码包（小写；import 必须保持 agent）
│   ├── core/           # AgentLoop、PrecisionBuilder、ContextBudget、五层 Memory 领域
│   ├── gateway/        # 消息枢纽、SessionStore（dict + SQLite adapter）
│   ├── storage/sqlite/ # RuntimeStorage 与会话/记忆/Job/Trace Store
│   ├── channel/        # 平台适配（CLI / frontier / 飞书 / 微信 / QQ）
│   ├── model/          # LLM 接入（claude / deepseek）
│   ├── tools/          # 工具注册；Memory 搜索、读取与编辑
│   ├── skills/         # Skill 加载与注册
│   ├── prompt/         # 系统提示词
│   └── evolution/      # 与 Memory 分离的 Skill 自我进化
├── tests/              # pytest 入口；tests/memory/ 为 Memory TDD 测试
├── data/               # 运行时数据（记忆文件、日志）
├── logging/            # 日志工具
├── docs/               # 开发文档 → docs/README_docs.md
```
