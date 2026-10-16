# 经验卡片：统计、事后反馈与场景合并

实现日期：2026-10-16。

保持现有异步提取、SQLite 和关键词召回。只新增 `memory_card_decisions` 一张表；卡片正文、证据和版本仍使用原表。新增迁移为 `013_memory_card_decisions.sql`，在下一次正常打开运行存储时执行；不要修改已经应用的旧迁移。

## 卡片协议与检索

现有经验字段继续描述适用条件、做法、观察结果及限制。新增：

| 字段 | 含义 |
| --- | --- |
| `source_message_ids` | 由已有证据关系计算的来源消息列表，不额外保存关系 |
| `used_count` | 当前主模型实际看到卡片的轮次数，同一正文代际、同轮只记一次 |
| `judge_count` | 已计入统计的使用判定数，包含未知结果 |
| `effective_count` | 有证据落实建议的次数 |
| `helpful_count` / `harmful_count` | 落实后有明确正向／负向结果支持的次数 |
| `recall_enabled` | 是否允许自动召回；关闭后仍可搜索、读取 |

成熟度不落数据库：`(helpful_count - harmful_count) / (helpful_count + harmful_count + 3)`。默认排序为 `关键词相关度 × (1 + 0.5 × 成熟度)`；同分优先给曝光少的卡机会。明确未采用至少五次且没有正向结果的卡降权，未知不作为未采用。

自动召回继续最多三张，总计六百估算 tokens。候选选择与最终围栏使用同一渲染预算，避免合并卡被预算移除后原卡也被隐藏。`recall_enabled=false` 不影响按需搜索。

按来源消息反查卡片：

```json
{"source_message_ids": ["原始message_id"], "source": "card"}
```

可同时提供 `query` 缩小范围。查询受运行时 scope 隔离，使用现有 `memory_evidence` 索引。工具证据仍通过 `tool:` 引用保留，不用消息来源列表替代。

## 曝光和唯一决策表

Loop 只在实际模型请求成功、经验段确实保留时暂存曝光。手动 `memory_read` 也只在真实读回片段完整进入后续模型请求时记曝光；搜索摘要、被裁剪的工具结果、没有后续模型请求的读卡不计。

曝光暂存在当前用户消息 metadata，SessionStore 将原始事实、卡片修改、曝光计数、待评记录和反馈 outbox 同事务提交。提交失败时一起回滚，避免崩溃产生没有来源轮次的计数。

`memory_card_decisions` 的主要字段：

| 字段 | 含义 |
| --- | --- |
| `id` / `dedupe_key` | 判定身份与幂等键 |
| `scope_key` | 归属隔离 |
| `card_id` / `card_revision` | 当时实际展示的卡片版本 |
| `turn_id` / `model_key` | 来源轮次与受指导主模型；合并可无轮次 |
| `decision_type` | `usage` 或 `merge` |
| `result_json.context` | 当时卡片快照、实际可见正文和曝光位置；合并时保存父版本组合 |
| `result_json.judgment` | JEV 标签、证据引用和评判器身份；归纳时也记录生成结果和可得用量 |
| `created_at` / `judged_at` / `counted_at` | 创建、判定及已应用统计的时间 |

`judgment.adopted` 为 `yes/no/unknown`；`effect` 为 `helpful/harmful/neutral/unknown`。采用和收益必须分别判断；工具成功状态、正常结束、模型自称成功都不能直接给卡加分。判官看到的是当时实际展示的正文及曝光后的执行证据，不会用未展示的完整卡片补齐建议。

后台通过已有 jobs 处理 `memory.feedback`，每批最多三张、每卡两题；超过三张会持久安排续批。网络错误使用原有租约、退避和有限重试，不写成负面结果。失败和中断轮次也保留可观察的工具证据。

同一次反馈重跑不会重复计数；复评按旧贡献与新贡献的差额更新。统计变化不推进卡片 revision；正文或适用含义改变清零统计，证据补充延续同一正文代际。切换主模型时从同一决策表重建统计，旧模型结果不直接作为新模型的成熟度。

默认有五个明确正负结果且成熟度不高于 `-0.25` 时关闭自动召回。JEV 做观察性归因，不能证明卡片造成了结果改善；真实增益可另外抽样做有卡／无卡对照。

## 可选 embedding 与单级场景合并

`.env` 配置以下三项后启用后台向量维护：

```dotenv
MEMORY_EMBEDDING_BASE_URL=https://your-provider.example/v1
MEMORY_EMBEDDING_API_KEY=填写到本地环境文件
MEMORY_EMBEDDING_MODEL=填写模型名称
```

不需要新的向量数据库。缓存放在卡片 `metadata.embedding`，绑定模型、正文 hash 和语义 hash；计数或补证据不重新生成向量。小规模在 SQLite 读取向量，计算同 scope 的 Top 5 近邻。

向量只找候选，JEV 判定 `duplicate/complementary/conflict/unrelated`。重复只给保留卡补证据、不转移统计；冗余卡保留原文和统计但退出自动召回，仍可搜索。至少三张原子卡全组条件兼容才生成场景卡，并再次检查新摘要没有新增结论、遗漏限制或改写失败观察。场景继续是一张普通经验卡，父卡身份和内容 hash 放在 `metadata.merged_from`。

场景的全部统计从零开始，父卡保留。第一版只做原子卡到场景卡的单级归纳，不把场景和自己的来源再次归纳。相同父版本组合的判定、失败或拒绝结果复用，避免每次后台轮询重新付费。embedding 失败有持久占位、有限重试和退避。

检索中最多一个合并试用卡。只有它实际入选才抑制对应父卡；超预算或不匹配时父卡正常参与。五个明确正负结果后成熟度至少 `0.5` 才继续优先使用，否则关闭该合并卡。十次曝光仍缺少足够结果时暂停继续曝光，等待已排队反馈；反馈完成仍不足则退出试用。不会因此删除或扣罚父卡。

父卡正文、条件、有效期或有效状态改变，旧合并表示停止自动召回；父卡仅增加计数或来源不影响它。

在线召回仍使用关键词。当前向量缓存可供以后接入语义召回，但本次没有增加每轮 query embedding。

## 配置与验证

架构超参数集中于 `MemoryExperienceConfig`，可通过 `RuntimeConfig.memory_experience_config` 注入。`enabled=False` 关闭新曝光与维护链路。当前切分宽度、原文覆盖、提取模型和轮次调度保持原配置。

事后 JEV 默认复用 `JEVMODEL_API_KEY/JEV_BASE_URL/JEV_MODEL`，也可通过 `MEMORY_JEV_API_KEY/BASE_URL/MODEL` 单独设置。未配置 JEV 时保留待评作业，不产生评判请求；未配置完整 embedding 三项时不产生向量或合并请求。

新增测试覆盖实际排序、预算回退、来源反查、自动与手动曝光、事务回滚、续批反馈、重复判定、模型隔离、正文代际、向量缓存、关系冲突、保真拒绝、父版本变化与合并退回。所有新测试使用临时数据库和 fake／MockTransport，不调用真实付费模型。
