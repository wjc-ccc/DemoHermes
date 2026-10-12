# Memory 基础题库：两个历史版本与字段定义

构建日期：2026-10-08。所有示例均为虚构数据。以下记录基础版本；2026-10-11新增的48个长对话场景及96道问题见 [长对话评测](long-dialogue-evaluation.md)。

已保存到项目 `data/agent.db`：

| 案例集 | 内容版本 | 场景数 | 后续提问数 | 用途 |
|---|---|---:|---:|---|
| memory_behavior | 1.0.0 | 23 | 4 | 原有基础回归案例的快照；多数只检验提取与检索 |
| memory_behavior | 2.0.0 | 20 | 40 | 日常多会话黄金问题；每个场景两道自然提问 |

这两个历史版本共43个场景、44个后续提问。第一版没有后续问题的基础用例仍完整保存并可回放，不人为给它们补造提问。

## 三种版本的关系

- `version` / `dataset_version`：**考题内容版本**。基础为 `1.0.0`，日常黄金问题为 `2.0.0`。同一 `case_id` 可以存在于不同版本中，各自保存自己的历史和答案。
- `schema_version`：**示例字段契约版本**，本次为 `1`。
- `extractor_version`：**实际运行的记忆提取器版本**，由运行报告单独记录，不决定考题答案。

发布时保存内容指纹。同版同内容重复导入不会重复插入，同版不同内容会拒绝；修改已发布的考题需使用新内容版本。运行报告包含案例集与单例指纹，便于核对是否使用了同一份标准。

## 数据库表

| 表 | 一行表示什么 | 版本关联 |
|---|---|---|
| `eval_datasets` | 一份已经发布的案例集 | 主键为 dataset_id + dataset_version |
| `eval_cases` | 一个场景：历史、预期记忆、应忽略内容 | dataset_id + dataset_version + case_id |
| `eval_questions` | 场景中的一个黄金问题 | dataset_id + dataset_version + case_id + question_id |

数据库通过外键关联三张表。常用筛选字段直接作为列保存，其余完整定义保存在 `dataset_json`、`case_json`、`question_json`。案例行的 JSON 不重复保存问题列表，问题独立存在第三张表，读取时再组装。`010_storage_contract.sql` 完成旧表名迁移，新增 `eval_runs` 和 `eval_results` 保存实际运行。

这三张表属于评测标准，不进入用户卡片、对话历史、原文检索或自动经验召回。参考答案不会被模型读取。后台只读表目录已加入中文说明，可按版本、场景和难度筛选。

## 每个场景的字段

| 字段 | 含义 |
|---|---|
| `case_id` | 同一版本内唯一的场景 ID |
| `title` / `description` | 场景名称和说明 |
| `scenario` | 使用场景，例如沟通、通勤、学习、购物；**不是记忆卡片类别** |
| `difficulty` | `basic` 基础，或 `multi_session` 多会话场景 |
| `abilities` | 要检验的能力，如条件偏好、时间变化、跨会话联系、经验边界、信息不足 |
| `history` | 按时间顺序排列的会话；每段有 session_id、occurred_at、turns |
| `history[].turns[]` | turn_id 和 user_text；提取证据必须引用这些实际轮次 |
| `expected_memories` | 该场景值得保存的事实或经验及其证据 |
| `ignored_information` | 不应长期提取的临时事项、他人信息、未验证打算等 |
| `forbidden_memories` | 明确不能生成的错误卡片，包含说明、类别限制和匹配词组 |
| `questions` | 对应黄金问题，数据库独立存为问题行 |
| `strict_card_count` | 是否要求精确卡片数量。基础版保留原断言；日常版允许合理拆分，但必需事实与禁止内容仍检查。遗忘后的空状态仍要求零张有效卡 |
| `content_hash` | 数据库保存的定义指纹，运行报告同时记录 |

历史包含闲聊、临时请求和例外。评测问题在新会话提出，不在问题里提示检索工具。

## 每条预期记忆的字段

| 字段 | 含义 |
|---|---|
| `memory_id` | 场景内的标准事实 ID，不是运行时生成的数据库卡片 ID |
| `kind` | profile、stable_preference、ongoing_fact、reusable_experience 四类之一 |
| `claim` | 以自然语言描述应该保留的含义 |
| `anchors` | 自动检查的关键含义词组；每组支持同义备选 |
| `query` | 检验该记忆是否可以查回的关键词查询 |
| `conditions` | 适用范围，如手机阅读、本次购机、加班期间 |
| `evidence_turn_ids` | 支持这条记忆的历史轮次 ID |
| `valid_from` / `valid_until` | 可选的有效期标准；不会凭空推断日期 |
| `outcome_status` | 经验的 success、failure、mixed 或 observed；其他类别为空 |

这些是评测预期，不能直接写入生产卡片冒充模型提取结果。

## 每个黄金问题的字段

| 字段 | 含义 |
|---|---|
| `question_id` | 场景内唯一的问题 ID |
| `text` | 用户在新会话中的自然请求 |
| `asked_at` | 提问的逻辑时间，包含时区 |
| `history_session_count` | 该题允许看到历史的哪个阶段；为空时使用完整历史 |
| `expected_memory_ids` | 回答本题需要联系哪些预期事实；可以为空，表示主要依靠原始历史或应承认未知 |
| `evidence_turn_ids` | 回答依据；必须存在于允许读取的历史阶段中，不能引用未来信息 |
| `expected_action` | 回答、制定计划、草拟内容、查询历史或补问信息 |
| `reference_answer` | 开发者预先确定的参考处理方式；不要求逐字复述 |
| `required_meaning` | 必须满足的语义标准 |
| `forbidden_meaning` | 禁止推断、错误使用旧值、越过经验边界等 |
| `answer_contains` / `answer_lacks` | 可自动检查的少量关键含义及禁止字面表达 |
| `required_tools` | 只有特定路径本身是验收要求时才设置。新版不固定工具调用顺序 |
| `requires_semantic_review` | 是否需要完整语义复核；新版 40 题全部为 true |

自动关键词命中不等于语义正确。自动检查通过后，新版会标成 `review_status=pending`，保留实际回答、来源映射和上述标准；未完成语义审核前不会报告最终通过。CLI 在全部自动检查通过但仍需复核时返回退出码 2。

## 新版 20 个场景

| 场景 ID | 要检验的日常逻辑 |
|---|---|
| conditional_reply | 手机简版、电脑排查详版和临时英文可以同时存在 |
| meeting_and_report | 内部例会改期，客户周报不改；组合手机偏好和汇报经验 |
| study_time_window | 两周加班期间限制学习时间，到期后先确认新预算 |
| travel_budget_update | 新预算取代旧预算，火车票另算的例外保留 |
| food_and_exception | 本人口味、送同事的蛋糕、一次聚餐分别理解 |
| keys_location | 查新的固定位置，一次在口袋找到不改变规则 |
| commute_conditions | 新办公室、正常地铁、雨天打车共同参与安排 |
| walk_routine | 一次取消不删除长期安排，不推算未记录的距离 |
| laptop_constraints | 同时满足价格、重量、内存；不泛化到其他消费 |
| gift_vs_self | 原始送礼记录可以查回，但不成为本人的口味 |
| reading_progress | 打算读第三章不等于已经读完，进度从原文查回 |
| two_classes | 绘画课改期不会覆盖摄影活动 |
| packing_experience | 搬家拆箱经验不能直接声称适用于所有电子文件 |
| failed_layout | 手机双栏阅读失败，未试建议不成为成功经验 |
| photo_sync_mixed | 缩略图成功不证明原图备份成功，不能据此确认可删 |
| unadopted_app | 对建议有兴趣不等于已经采用或迁移 |
| forget_current_project | 遗忘后不恢复旧项目，也不基于它安排当前工作 |
| cross_topic_reference | “前者、后者”跨会话对应正确事项，局部操作不等于整体完成 |
| quoted_opinion | 本人陈述优先于朋友的猜测，节日主题只是临时例外 |
| missing_address | 大概居住区域不能补成门牌，地铁偏好不能推出驾照状态 |

## 一个问题实例

场景 `meeting_and_report` 的历史包括：用户负责 Harbor、内部例会原为周五四点、客户周报仍周五、手机先看短版、三部分汇报结构得到一次认可，以及例会从10月12日起改为周四十点。

问题 `after`：

> 今天是10月14日，帮我准备明天的例会材料，我先在手机上看。

标准：联系项目、改期生效时间、手机偏好和汇报经验；先给“已完成、阻塞、下一步”的简版框架。本周实际进度没有提供时，应查资料或询问；不能把旧经验当成本周已经完成的事情，也不能改客户周报时间。

## 查看、导出和运行

列出数据库已保存版本，不调用模型：

```powershell
.venv\Scripts\python.exe -m agent.eval.datasets.memory_dataset --db data/agent.db list
```

导出完整新版：

```powershell
.venv\Scripts\python.exe -m agent.eval.datasets.memory_dataset --db data/agent.db export --version 2.0.0 --output data/evals/golden-v2-export.json
```

按数据库版本运行真实模型评测，使用新的运行目录：

```powershell
.venv\Scripts\python.exe -m agent.eval.memory_behavior --live --dataset-db data/agent.db --dataset-version 2.0.0 --cases meeting_and_report --output-dir data/evals/golden-v2-meeting-run
```

省略 `--cases` 会执行新版 40 个问题。两个问题各自在隔离用户下重放同一历史，避免第一题回答进入第二题的历史。问题、标准答案和实际回答都有各自位置；模型输入只含历史、日期上下文和自然问题。

数据日期作为对话上下文提供给模型，**不修改实际 SQLite 时钟**。跨日期物理过期的自动断言仍需可控时钟测试配合；不能仅靠这批黄金问题证明全部过期行为正确。

源文件：原基础版为 `tests/eval/datasets/memory_behavior.json`，日常版为 `tests/eval/datasets/memory_golden_v2.json`。规范类型在 `agent/types/eval/datasets.py`。

本次已完成案例编写、发布、版本存取及评测转换测试。新版 40 题尚未进行真实模型全量运行，数据库中的内容是评测标准，不是模型已通过的结果。

## 2026-10-08写入审计

本次新增迁移 `006_memory_eval_datasets.sql`，未修改已应用的旧迁移。

写入前 SQLite 备份为 `data/agent.pre_eval_datasets.09e81e32.db`。写入后完整性为 `ok`，无外键错误。原有会话 8、轮次 16、消息 32、工具记录 0、卡片 2、调度轮次 16，写入前后数量一致。
