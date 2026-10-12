# 当前 Perception 正确性与冗余复查

> 文件职责归并已实施，见 [perception_structure_refactor.md](perception_structure_refactor.md)。下列行为问题仍属于后续修复范围；源码位置随归并发生变化。

检查日期：2026-10-10；对象为当前工作区（包括未提交改动）。本次未修改生产代码。

结论：唯一 build 主链、P0/P1 保护、Anchor 覆盖/digest/事实来源验证、正常路径的全文冻结已经成立；不能据此认定所有边界行为正确。现有 47 项 Perception/Admission/Anchor/全文/类型契约测试通过，以下问题由额外探针复现。当前 Action 分支已调用 _pin_readback，旧的“Action 漏钉住”问题不再列为未修复问题。

## 正确性问题

| 优先级 | 问题 | 证据与条件 | 建议 |
| --- | --- | --- | --- |
| P1 | Action 的恢复引用使用错误身份 | ActionRuntime 分配新的事实 ID；工具消息回注保留 provider ID；Admission 从回注字典读取 ID 并生成 tool:<provider-id>，Retriever 按事实 ID 查询。真实 resolver 探针：provider ID 未找到，fact ID 找到 | 模型关联 ID 与持久事实 ID 分开使用，从 Message.tool_call_ids 构造可解析的恢复引用 |
| P1 | 实际没有投影也判为误判 | _pin_readback/mark_readback_miss 只检查存量 verdict 动作。shadow 正常读回得到 missed=true；active 配合固定消息成本估算器时原文仍完整发送、saved_tokens=0，也得到 missed=true | 从本次实际发送的定案确认条目曾被 trim/defer，再计误判；模式检查本身不足以证明实际生效 |
| P1 | 重读失败仍使旧成功结果过期 | _stale_reason 只看后续调用请求，不检查执行结果。后续同文件读取失败，旧成功结果 score=0/action=defer | 用成功 observation 确认旧结果被替代；失败或未完成调用不触发 stale |
| P1 | 滚动窗口淘汰会丢失完整迭代 | window=1：active 迭代1 → other → active 迭代2，最终只剩 [2]。Gateway 最终从同一窗口获取 Trace 来持久化 | 未提交 turn 的完整记录独立于已完成 turn 的滚动统计窗口；按 turn 生命周期完成持久化后再淘汰 |
| P1 | 输入预算没有覆盖工具 schemas | Engine 只估算 messages，Loop 随后加入 schemas。探针显示 message_tokens=196、input_budget=390，另有3931字节工具 schema，仍调用模型。字节数不是实际 token 数；此结果证明 schema 没有参加同一次预算检查 | 在调用前对完整请求材料采用一致预算；工具定义不能只进入 Trace 而不进入估算 |
| P2 | Token 估算兜底不一致 | _admit/_with_estimate 的字符兜底与默认 UTF-8 字节估算不同；build 最终又直接调用原估算器。持续异常的估算器仍抛 ValueError | 一个估算入口、一种降级/失败策略，避免部分估算成功后最终绕过兜底 |
| P2 | 缺失调用 ID 时工具协议配对错误 | ToolCallRequest.id 允许默认空值；assistant 先存空 ID，普通执行随后生成 UUID，tool 回注带 UUID。探针两者不等。正常非空 provider ID 路径不受此问题影响 | 回注 assistant 之前统一规范化关联 ID，两条执行路径使用同一关联 ID |
| P2 | 轮级指标仍只反映最后成功构建 | builds 保留 judged=[3,0]，根级 judged=0；用量从0.9降至0.2，窗口p95为0.2。详情未丢失，但轮级成本/峰值口径没有从所有构建归并 | 判官计数/耗时按 turn 聚合；预算压力取该 turn 已知构建峰值；失败统计单列 |

主要位置：

- 恢复身份：building_messages.py:75/78、perception_admission.py:380/454、action/runtime.py:87、memory_retrieval.py:217。
- 读回判错：agent_loop.py:332、perception_admission.py:193。
- 过期规则：perception_admission.py:394。
- 窗口及指标：perception_trace.py:36/67/112；Gateway 提交阶段使用 perception_trace.get(turn_id)。
- 预算与估算：perception_engine.py:149/653/1151、agent_loop.py:232。
- 空关联 ID：building_messages.py 的 assistant 构造与 agent_loop.py:359。

## 冗余与可收敛内容

1. **整段历史反复深拷贝。** record 拷贝新快照、再拷贝完整 turn、再返回完整副本；生产 Engine 忽略这个返回值。record_request 更新一次工具定义，又复制历史构建。确定性探针测得1/4/8次构建分别触发5/38/124次 PerceptionBuildSnapshot 深拷贝。这是对象复制次数，不是耗时或内存用量实测。建议新快照只拷贝一次，内部历史共享，读取指定迭代时提供隔离副本。
2. **根级 metrics/records 与最新成功 build 重复。** 有真实列表、SQL筛选及调试消费者，不是死字段；建议作为只读派生摘要，builds 作为权威构建记录，避免分别维护状态。
3. **重复排序。** _grade:628 与 _build_messages:785 都排序；最终组装时保留一次即可。
4. **五槽定义重复。** _ANCHOR_SLOTS、_anchor_content、_anchor_facts、legacy 标题映射与 render_anchor 分别列举同一结构；集中槽位及标题元数据，派生遍历/投影。
5. **HistoryTurn 别名重复。** Engine 与 Admission 各声明一次，放到内部 ContextItem 类型所在模块维护一份即可。
6. **工具反馈重复。** Action 与普通路径现在都执行 record_memory_read + _pin_readback；可以形成一个成功工具反馈入口，再从实际定案检查生效状态。
7. **不能按“显式读取少”删除协议。** Judge 请求字段进入实际远程 state；序列化、SQLite、通用表格也会消费观测字段。Anchor 校验账本和 raw_message 是正确性信息。P3 保留是既定选择，尚无生产来源不等于应删除。

## 文件建议

| 文件/职责 | 建议 |
| --- | --- |
| core/context_budget.py + perception/errors.py + anchor 内的 AnchorBuildError | 优先集中错误定义到 errors.py；context_budget.py 当前仅剩预算异常。保持异常类名/错误属性，迁移所有引用 |
| Engine 的 Anchor 五槽、覆盖、digest 校验 helper | 集中到现有 perception_anchor.py，Engine 保留阶段编排。不要把两个校验合成一个：新内容校验和缓存覆盖校验有不同目的 |
| context_item.py + priority.py | 可以合并到纯类型模块，但收益较小；现有类型规范要求 Priority 分类入口轻量，合并不能反向加载 Engine/模型实现。可先只去重 HistoryTurn |
| engine / admission / judge / anchor / trace 五个业务文件 | 保持分开：分别是编排、准入策略、远程 Adapter、Anchor 逻辑、观测。拼成一个大文件不会解决冗余 |
| types/perception 的配置、结果、Judge、Trace 协议 | 保留按职责分层；运行期返回值与持久快照相似字段有不同生命周期，不宜直接混为一个对象 |

建议顺序：修复引用与实际生效判定 → 修复过期/预算及关联 ID → 保障 Trace 完整记录和正确聚合 → 减少深拷贝及重复状态 → 集中错误和 Anchor 职责。
