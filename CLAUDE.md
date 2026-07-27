# DemoCursor

自主 Agent 框架 Demo：复现 Cursor 的 Agent 能力，增强可观测性与自我进化。

## 怎么跑

- 后端 / CLI：`.venv/Scripts/python.exe main.py`（默认 HTTP :8000，供 frontier 前端；`--cli` 终端对话）
- 前端：`.venv/Scripts/python.exe frontier/main.py`
- 测试：`.venv/Scripts/python.exe -m pytest -q`——必须用 venv 解释器；系统 anaconda 是 Pydantic v1，会产生伪 Red

## 技术栈

Python 3.11 · Pydantic v2（>=2,<3，不维护 v1 兼容层）· SQLite（WAL + busy_timeout，单进程）· pytest

## 目录与约定

- `Agent/`（大写；Linux CI 区分大小写，import 与测试路径必须保持 `Agent`）：
  core（AgentLoop / ContextBuilder / ContextBudget）、gateway（消息枢纽 / SessionStore）、
  memory（Hot 精选 / Daily / SQLite transcript / Provider / Reviewer）、channel、model、tools、skills、evolution
- `tests/`：pytest（pytest.ini：testpaths=tests, pythonpath=.）；Memory 测试在 `tests/memory/`
- `docs/`：aiDocs（方案/计划）、errorDocs（debug 日志）、timeDocs（每日进度）、testDocs（阶段测试）、recognitionDocs（架构认知）
- 提交纪律：只在非 main 分支 commit，不 push

## 当前状态（2026-07-27）

- Memory 架构 TDD（`docs/aiDocs/7.23 Memory架构TDD方案.md`）Slice 1–14 完成且验收通过：§14 全部 17 条标准 PASS，125 tests 全绿
- 开发分支 `feat/memory-tdd-20260725`，尚未合并 main
- 下一步：Slice 15 环境快照（方案明确后置）；方案 §17 列有 4 个已知缺口作为后续候选
