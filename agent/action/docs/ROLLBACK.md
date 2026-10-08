# Git 回退

实施基线：2813ca8。已有 PROJECT.md、memory_review_worker.py、logging 缓存、.coverage、graphify-out 不属于此次修改。

交付分组：基础账本 → 工具适配 → 可选 Loop 接入 → 计划/提示链。每组提交只包含本次文件与测试。

先停止接收新任务，对 started/unknown 动作查证，再关闭 action 或逆序 git revert 接入提交。保留 action 表及历史，不删库，不修改已应用迁移，不用 reset --hard。

旧路径不恢复 action 创建的未完成计划。Git 回退不撤销已经发生的业务副作用；手册或技能内容恢复须走各自受控业务入口。

## 本次提交

| 顺序 | 提交 | 内容 |
| --- | --- | --- |
| 1 | 3a34420 | 基础账本、契约、可选迁移 |
| 2 | f95bf66 | 工具适配、PRE/POST、延迟提交回执 |
| 3 | a75d106 | 可选 Loop/Gateway/CLI 接入 |
| 4 | 本文所属计划提交，见下方定位命令 | 计划、提示链、闸门、崩溃验收及文档 |

第四组准确提交可用 `git log -1 --format=%h -- agent/action/plan/runner.py` 定位；其标题为 `feat(action): add durable plans artifact gates and crash recovery`。

若只关闭控制层，重启时省略 --use-action 或使用 --no-use-action。停止入口后先列出 `runtime.ledger.list(unfinished=True)`，对 UNKNOWN 调用 recover 并复核；未查清的任务不进入旧执行路径。

需要代码回退时先撤销第 4 组，再撤销第 3 组；基础模块可保留而不启用。完全移除继续逆序撤销第 2、1 组。所有原 schema 文件未改写，旧启动器忽略 action 新表与额外迁移记录。撤销源码不自动删除 action 数据。

共享的 agent_loop.py/main.py 同期有上下文准入修改，此次按 action 改动分块提交。回退遇到相邻行冲突时按 ACTION-HOOK 注释处理，保留上下文准入与其他既有修改。
