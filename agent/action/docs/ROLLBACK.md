# Git 回退

实施基线：2813ca8。已有 PROJECT.md、memory_review_worker.py、logging 缓存、.coverage、graphify-out 不属于此次修改。

交付分组：基础账本 → 工具适配 → 可选 Loop 接入 → 计划/提示链。每组提交只包含本次文件与测试。

先停止接收新任务，对 started/unknown 动作查证，再关闭 action 或逆序 git revert 接入提交。保留 action 表及历史，不删库，不修改已应用迁移，不用 reset --hard。

旧路径不恢复 action 创建的未完成计划。Git 回退不撤销已经发生的业务副作用；手册或技能内容恢复须走各自受控业务入口。
