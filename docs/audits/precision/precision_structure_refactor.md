# Precision 职责归并记录

日期：2026-10-10。本次为结构重构，保留现有分级、预算、压缩、P3 和 Trace 行为。

## 文件职责

| 文件 | 归并后的职责 |
| --- | --- |
| precision/errors.py | 唯一定义预算超限、Anchor 构建、Anchor 压缩及判官错误；保留原构造参数、错误码及属性 |
| precision/percision_engine.py | 加载、准入、优先级与预算策略、最终组装和指标；调用 Anchor 模块完成专属操作 |
| precision/percision_anchor.py | 模型 Adapter、JSON 解析、五槽内容、覆盖账本、digest、两次构建尝试、校验及上下文投影 |
| precision/context_item.py | 内部 ContextItem、共用 HistoryTurn/AnchorContent 和加载阶段状态 |
| precision/priority.py | 保持轻量共享枚举，公开协议不加载业务实现 |
| admission / judge / trace | 保留各自实现与职责 |

原 core/context_budget.py 已删除，生产与测试引用均迁移到 precision/errors.py。没有增加旧路径兼容文件。

Engine 从 1218 行缩减到约 830 行。15 个 Anchor 辅助函数及覆盖/重试操作迁入现有 Anchor 模块，没有增加新的业务文件。两种校验仍独立：validate_anchor_content 验证新生成内容与事实来源；validate_stored_anchor 验证缓存与当前历史的覆盖关系。

五槽名/显示标题由纯类型模块 agent/types/context.py 的 ANCHOR_SECTIONS 统一提供，内容投影、遍历、旧格式解析和文本渲染使用同一份定义。Anchor 提示词、缓存字段及序列化格式保持一致。

## 验证

- 47 项 Precision、Admission、Anchor、全文快照及类型契约测试通过。
- 12 个纯迁移 helper 的 AST 函数体与重构前一致（只调整函数名引用）。
- 五槽均有事实时，渲染文本与重构前逐字一致。
- 预算异常默认/自定义消息、args 和 error_code 与重构前一致。
- 331 项相关 Python 回归通过，覆盖 Core、Memory、Action、Eval、Evolution、Gateway 与 SQLite；本次未处理复查报告中的行为问题。
