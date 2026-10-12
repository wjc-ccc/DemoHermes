# Perception 感知模块命名

感知模块统一命名为 `perception` / `Perception`，代码与当前项目数据不再使用旧模块名称。

本次修改覆盖：

- 实现包 `agent/core/perception/`，以及 `perception_engine.py`、`perception_admission.py`、`perception_anchor.py`、`perception_judge.py`、`perception_trace.py`。
- 数据契约包 `agent/types/perception/`，包括 `PerceptionConfig`、`PerceptionResult`、`PerceptionMetrics`、`PerceptionRecord` 和全部 `Perception*Trace` / `Perception*Snapshot` 类型；实现入口改为 `PerceptionBuilder`。
- Runtime、AgentLoop、Action、Memory、Gateway 的导入、装配参数与实例属性：`perception`、`perception_trace`。
- TurnRecord、SQLite 读写及调试查询中的 `perception_trace`、`has_perception_trace`、`perception_metrics`；Trace 模块 ID 为 `perception`，展示名称为 `Perception`。
- 前端 `PerceptionContextDialog.vue`、`getPerceptionContext`、`PerceptionTraceDetail`、Trace 页面及 Loop 详情组件。
- 测试文件和测试辅助装配、在线探针、评测运行器与汇总脚本、架构文档、模块规划和审计文档。审计清单按新源码重新生成。

数据库新增 `011_perception_naming.sql`。RuntimeStorage 打开数据库时，通过现有迁移机制把 `turns.turn_json.precision_trace` 转为 `perception_trace`，保留完整 Trace 内容；已存在新字段时保留新字段。运行时代码只使用新名称，没有旧模块别名或双字段读取。迁移不会扫描或改写用户消息正文。

本次补齐全部当前项目引用：

- 分类和记忆评测的 `precision`、`card_precision` 等查准率指标，以及样本中的 `decimal precision mismatch` 错误原文。
- 题库统一使用 `target="perception"`、`perception_behavior`、`perception_core_benchmark`、`perception_core_pilot`、`formal_perception_*` / `pilot_perception_*` 及对应 JSON 文件名。场景 ID、来源引用、发布清单、360 条黄金问题和事实时间线已同步更新；题库与问题指纹按新内容重算。
- 既有 SQL 迁移和真实评测运行目录中的源代码快照、结果、清单、哈希及图像。命名变更改变当前代码指纹；之后的真实评测必须建立新的运行版本，不能继续使用旧清单宣称生产代码未变化。
- `graphify-out/` 的模块路径、节点 ID、边引用、标签和报告同步更名；失效 AST 缓存移入临时备份。文档引用的本地评测目录、派生报告和图像文件名同步更新。

评测报告的模块键和 target 分组键均为 `perception`。数据库新增 `012_perception_eval_naming.sql` 与配套迁移代码，在同一事务中更新评测目标约束、题库 / 场景 / 问题 / 结果关联和内容哈希；提交前检查外键，冲突时回滚。一次性迁移和迁移测试必须识别旧库字段和标识，运行代码只接受新名称。

真实模型调用原文、真实回答和原实验冻结记录保留原始内容；更名不表示重新运行模型评测。当前题库与代码产生新指纹，后续真实评测需建立新的运行版本。旧内容与指纹映射保存在 `.pytest_tmp/` 的备份中。

完整更名后的验证：492 项后端测试与 11 项前端测试通过，TypeScript 类型检查和生产构建通过；正式 / 试运行题库导出与生成器的指纹一致；360 条黄金问题已重新绑定哈希；6 份图谱没有悬空节点引用；新命名的文档链接均有效。评测图像使用原有统计值重新导出，图像清单记录新报告与绘图代码的哈希。
