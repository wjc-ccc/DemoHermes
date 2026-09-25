# DemoCursor

自主 Agent 框架 Demo：复现 Cursor 的 Agent 能力，增强可观测性与自我进化。

## 怎么跑

- 后端 / CLI：`.venv/Scripts/python.exe main.py`（默认 HTTP :8000，供 frontier 前端；`--cli` 终端对话）
- 前端开发：`cd frontier && npm install && npm run dev`（Vite :5173，proxy 读 `frontier/src/config.js`）
- 前端生产：`cd frontier && npm run build` 后 `.venv/Scripts/python.exe frontier/main.py`（托管 `dist/`）
- Memory Debug：前端 `config.enableMemoryDebug=true` + 后端 `main.py --enable-memory-debug`
- 测试：`.venv/Scripts/python.exe -m pytest -q`；前端 `cd frontier && npm test`——必须用 venv 解释器；系统 anaconda 是 Pydantic v1，会产生伪 Red

## 技术栈

Python 3.11 · Pydantic v2（>=2,<3，不维护 v1 兼容层）· SQLite（WAL + busy_timeout，单进程）· pytest

## 目录与约定

- `agent/`（小写；Linux CI 区分大小写，import 与测试路径必须保持 `agent`）：
  core（AgentLoop / ContextBuilder / ContextBudget）、gateway（消息枢纽 / SessionStore）、
  core/memory（五层领域 / Markdown 快照 / Reviewer）、storage/sqlite（Session / Task / Memory / Trace Adapter）、channel、model、tools、skills、evolution、eval
- `tests/`：pytest（pytest.ini：testpaths=tests, pythonpath=.）；Memory 测试在 `tests/memory/`
- `docs/`：aiDocs（方案/计划）、errorDocs（debug 日志）、timeDocs（每日进度）、testDocs（阶段测试）、recognitionDocs（架构认知）
- 类型与数据流：载体用自然名（`Session`/`Message`/`ToolCall`）；一次操作的输入输出用 `XxxRequest`/`XxxResult`。详见 `.cursor/rules/python-types-dataflow.mdc`（每次会话自动加载）
- 提交纪律：只在非 main 分支 commit，不 push

## 当前状态（2026-09-19）

- Memory 五层分册、普通 Markdown、SQLite 卡片/Trace、outbox Reviewer 与冻结快照接入已完成；召回/RAG 后置
- Skill 自进化：确定性 turn 事实 → `SkillRepository` 只写 draft → 后台 shadow replay 通过后才晋升；draft 不可被 `use_skill` 加载
- 生产默认不跑真实 LLM 回放（无 `runner_factory` 则技能保持 draft）；测试注入 EvalRunner 覆盖全链
- 下一步：为生产接上带真实工具注册表的 `runner_factory`，以及把历史可验证任务收成版本化 suite
