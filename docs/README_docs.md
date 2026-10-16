# 文档目录

文档按用途阅读，判断当前行为时以代码和“当前架构”文档为准。

## 当前架构

先读 [AgentRuntime 运行架构](runtime-architecture.md)，核对实际装配与请求主线。

1. [模块架构总览](模块架构总览.md)：感知、推理、执行、反思、记忆、治理、协作的运行链路、代码落点和现状。
2. [架构认知索引](recognitionDocs/README.md)：types、channel、gateway、agent loop、tool/skill、memory 的详细接口与职责。
3. [SQLite 与 Memory 存储](sqlite-memory-storage.md)：数据库结构与升级规则。具体运行路径以 [Memory 运行链路](recognitionDocs/memory.md) 为准。
4. [经验卡片反馈与场景合并](memory-card-feedback.md)：单卡成熟度、唯一决策表、异步JEV和可退回的向量近邻合并。

## 历史与过程记录

| 目录 | 用途 |
| --- | --- |
| `aiDocs/` | 带日期的设计方案、实施总结与演进计划；可能含未实现或已替换的方案 |
| `timeDocs/` | 每日计划和进度 |
| `errorDocs/` | 开发中遇到的问题和排查日志 |
| `testDocs/` | 各阶段测试记录 |

修改功能时先核对 `agent/runtime.py:build_runtime` 的实际装配、相应模块的 Interface 和测试，再更新“当前架构”。新增 AI 方案应带日期和简要标题，只放在 `aiDocs/`；不要把历史方案直接写成生产行为。
