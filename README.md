## 本项目核心为构建 Demo Cursor

自主 Agent 框架，目标复现 Cursor 的 Agent 能力并增强可观测性与自我进化。

### 当前状态（2026-07-27）

- CLI / HTTP（frontier 前端）双入口；Gateway 双队列调度 + AgentLoop 工具内循环
- Memory 系统（TDD Slice 1–14）已验收：USER/GROUP + MEMORY 精选记忆、daily 日志、SQLite transcript 永久记录与检索、外部 Provider 接入、后台 Reviewer 自动提炼
- 架构认知见 `docs/recognitionDocs/`；Memory 设计契约见 `docs/aiDocs/7.23 Memory架构TDD方案.md`

### 项目结构速览

```
DemoCursor/
├── main.py             # 一键启动：默认 HTTP :8000（frontier 后端）；--cli 终端模式
├── frontier/           # 可视化前端
├── Agent/              # 核心代码包（大写；import 必须保持 Agent）
│   ├── core/           # AgentLoop、ContextBuilder、ContextBudget、Session/types
│   ├── gateway/        # 消息枢纽、SessionStore（dict + SQLite adapter）
│   ├── memory/         # 精选记忆 Hot/Daily、SQLite transcript、Provider、Reviewer
│   ├── channel/        # 平台适配（CLI / frontier / 飞书 / 微信 / QQ）
│   ├── model/          # LLM 接入（claude / deepseek）
│   ├── tools/          # 工具（calculator / bash / file / memory_read / memory_edit）
│   ├── skills/         # Skill 加载与注册
│   ├── prompt/         # 系统提示词
│   └── evolution/      # 自我进化（reviewer / curator / skill_creator）
├── tests/              # pytest 入口；tests/memory/ 为 Memory TDD 测试
├── data/               # 运行时数据（记忆文件、日志）
├── logging/            # 日志工具
├── docs/               # 开发文档 → docs/README_docs.md
```
