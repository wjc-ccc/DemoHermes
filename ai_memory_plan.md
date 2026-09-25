> 2026-09-24 实施更新：当前表结构与 API 以 [SQLite 与 Memory 三态](docs/sqlite-memory-states.md) 为准。下文保留早期设计记录，其中旧表名与 TaskState 已被本轮 17 表方案替代。

# AI Memory 重构方案（实施稿 v4）

> 本稿实现五件事：**五层记忆边界、每层保存途径、SQLite 独立数据架构、Session → Memory 提取、冻结快照接入 Precision**。
>
> Memory 只通过 `MemorySnapshot` 接入现有 Precision，不修改 Precision 的分级算法。本稿仍不设计锚 / 账 / 集、机械态、调度态、RAG、向量检索与召回工具。

## 实施状态（2026-09-19）

- [x] SQLite 基础设施迁入 `agent/storage/sqlite/`，旧 `agent/memory/episodic/` 已删除。
- [x] 引入版本化 migration、`turns`、`memory_review_jobs` transactional outbox。
- [x] 建立五层 `MemoryLayer`、显式 `MemoryScope`、五类 `XxxItem` 与轻量 `MemoryItem` 投影。
- [x] 建立 Task、User Memory、Document Registry 与 Memory Trace 表及 Store。
- [x] 新建无 marker 的 `MarkdownBook`；支持整文档读取、哈希、token 统计和乐观并发替换。
- [x] 建立 `MemoryProposal → MemoryValidator → MemoryReviewResult` 的纯审查链路，并阻止召回内容自我强化。
- [x] 建立 `MemoryTrace / MemoryItemTrace`，区分 AI 输出字段与运行时校验字段。
- [x] 主链路已切换为 `MemoryContextLoader → MemorySnapshot`；旧 Hot/Daily/Provider、marker CRUD 与总管式 `MemoryManager` 已删除，不保留兼容层。
- [x] 将新 `SessionMemoryReviewer` 接到独立 outbox worker，并原子提交卡片、版本、证据、关键词、Trace 与 job 状态；重复事实只追加证据。
- [x] 主程序已启用 reviewer worker；Gateway 启停负责 worker 生命周期，Turn 事实提交后只发唤醒信号。
- [x] `MemoryTraceStore` 支持按 trace、session、item 查询和按时间清理，AI 字段与运行时字段分开存储。
- [x] 提供一次性 marker 扫描/迁移脚本；默认 dry-run，显式 apply 才备份、导入 SQLite 并清除旧围栏。
- [x] Debug API 与前端已切换为展示冻结的五层 Memory Context，不再读取旧 Hot/Profile/Daily 私有目录。
- [ ] 关键词 / 相似度 / 混合召回工具、强制经验加载、RAG、锚 / 账 / 集仍按本稿范围延后。

---

## 1. 这次重构要解决什么

当前问题并不只是“少一个向量库”，而是四类概念混在了一起：

1. Markdown 既被当作人类文档，又被当作带特殊标记的条目数据库。
2. Session、Turn、Message 是事实记录，却和长期记忆放在同一个 memory 目录中。
3. 用户长期记忆、任务状态、临时草稿缺少明确边界，容易相互污染。
4. 提取、校验、落库、召回、上下文注入没有可追踪的统一原子。

本轮重构的目标是先建立稳定的“地基”：

- 用五个语义层明确权威顺序和生命周期。
- Markdown 回归普通文档，不再依赖隐藏标识符切分。
- SQLite 成为独立的数据基础设施，不从属于 memory。
- Session 事实先可靠落库，再由 Memory 模块异步提取候选记忆。
- 用轻量 `MemoryItem` 投影和 `MemoryTrace` 统一观察，而不是强迫所有来源共用一张表。

---

## 2. 先划清四个不同维度

### 2.1 Layer 表示“语义权威与生命周期”

五层分别回答：这条信息属于谁、能约束谁、活多久、由谁修改。

### 2.2 Storage 表示“事实保存在哪里”

Markdown、SQLite、进程内对象、未来向量库只是保存方式。它们不是 Memory Layer。

同一层可以使用不同保存方式。例如用户层可以同时拥有：

- 一个全量加载、供人和 Agent 共同维护的简短 `MEMORY.md`；
- SQLite 中可检索、可去重、可统计的结构化长期记忆卡片。

二者职责不同，不做双向自动同步。

### 2.3 Context 表示“这一次模型实际看见什么”

某条信息属于用户层，不代表每一轮都必须加载。上下文装配属于后续运行时设计，本稿只规定来源和默认意图，不实现拼装算法。

### 2.4 Trace 表示“系统对信息做了什么”

Trace 记录写入、提取、校验、去重、召回和采用结果。它不成为新的事实来源，也不保存模型的隐藏思维过程。

---

## 3. 五层模型

代码中使用一个封闭枚举 `MemoryLayer`，存储值使用稳定英文名，不把 `0/1/2` 当业务值：

```python
class MemoryLayer(str, Enum):
    POLICY = "policy"
    PROJECT = "project"
    USER = "user"
    TASK = "task"
    SCRATCHPAD = "scratchpad"
```

层级顺序由 `memory_layer.py` 中的规则表定义：

| 顺序 | layer | 含义 | 默认生命周期 | 默认权威 |
|---|---|---|---|---|
| Layer 0 | `policy` | 组织规则、合规红线、安全边界 | 长期、版本化 | 最高 |
| Layer 1 | `project` | 项目规范、领域模型、架构、业务流程 | 跟随项目 | 次高 |
| Layer 2 | `user` | 用户 / 租户长期信息与个性化 | 跨任务长期存在 | 用户范围 |
| Layer 3 | `task` | 当前任务 / Session 的目标与进度 | 任务生命周期 | 当前任务 |
| Layer 4 | `scratchpad` | 当前轮的工具结果、中间判断和临时变量 | 单轮或短 TTL | 最低 |

这里的 `Layer 0…4` 只是权威顺序，不等于 P0/P1/P2 优先级，也不等于热 / 温 / 冷存储。

### 3.1 覆盖规则

- 低层不能修改、覆盖或删除高层事实。
- 高层可以限制低层允许采取的动作。
- 同层冲突按来源权威、版本和有效期处理。
- 临时任务需求不能自动晋升为用户长期画像。
- 自动提取器不能创建或修改 `policy`、`project` 内容。
- “在 Prompt 中写了安全规则”不等于真正的安全边界。生产写权限、客户数据外发和账单操作仍必须由工具权限、审批网关等代码机制强制执行。

---

## 4. 五个分册及各自保存途径

### 4.1 Layer 0：Policy / Management

职责：组织级规则、合规红线、数据边界、审批要求、禁止事项。

推荐保存：

- 权威正文：受管理的普通 Markdown，例如 `{policy_root}/POLICY.md`。
- 版本与审计：Git、配置发布系统或不可变版本号。
- SQLite：只保存文档注册信息、加载快照和 Trace，不复制正文作为第二份权威来源。

建议类型 `PolicyItem.type`：

- `compliance_rule`
- `security_boundary`
- `approval_rule`
- `operational_prohibition`
- `data_handling_rule`

第一版把一份文档视为一个可追踪的 `PolicyItem`。标题层级只服务人类阅读，不依赖标题或 HTML 注释把 Markdown 切成数据库条目。

修改原则：Agent 默认只读；变更必须走明确的管理入口。普通用户对话不能修改该层。

### 4.2 Layer 1：Project / Domain

职责：项目规范、领域模型、代码架构、目录说明、测试命令和业务流程。

推荐保存：

- 权威正文：项目仓库里的 `AGENTS.md`、`CLAUDE.md`、`README.md`、架构文档和领域文档。
- 版本与审计：跟随项目 Git，可 review、diff、rollback。
- SQLite：只保存文件注册、内容哈希、token 数、最近读取时间等派生元数据。

建议类型 `ProjectItem.type`：

- `project_rule`
- `architecture`
- `domain_model`
- `business_workflow`
- `repository_guide`
- `command`

Memory 不接管这些文件的 CRUD，也不把项目文档自动复制为长期记忆卡片。未来 RAG 可以为它们生成派生 chunk，但源文档仍是唯一权威。

### 4.3 Layer 2：User / Tenant

职责：用户偏好、稳定身份信息、能力画像、租户配置、长期语义事实和可复用经验。

本层分成两种互补载体：

1. **用户手册 `MEMORY.md`**
   - 内容短、稳定、高信号、默认全量加载。
   - 人和 Agent 都可以通过普通文件编辑维护。
   - 不要求固定标题、隐藏标记或条目 ID。
   - 不接受后台提取器自动追加。
   - 适合“始终希望助手知道”的总则，不适合大量事实卡片。

2. **结构化用户记忆卡片**
   - SQLite 是权威来源。
   - 后台从 Session 中提取、校验、去重后写入。
   - 支持版本、证据、冲突、失效、统计和后续检索。
   - 未来可将卡片内容投影到向量库；向量库只是索引，不是权威来源。

建议类型 `UserMemoryItem.type`：

- `identity`
- `preference`
- `capability`
- `tenant_config`
- `relationship`
- `long_term_fact`
- `reusable_experience`

可选的正交字段 `memory_kind`：

- `episodic`：发生过的事件或经历。
- `semantic`：稳定事实、概念和关系。
- `procedural`：可复用的流程或做法。

`type` 回答“这是什么业务信息”，`memory_kind` 回答“它以什么认知形态存在”，两者不混用。

### 4.4 Layer 3：Task / Session

职责：当前目标、里程碑、状态、checkpoint、已完成事项、下一步、阻塞和已排除方案。

推荐保存：

- Session / Turn / Message / ToolCall：SQLite 中的原始事实表。
- TaskStateItem：SQLite 中的任务状态表。
- Checkpoint：SQLite 中的版本化快照。

建议类型 `TaskStateItem.type`：

- `goal`
- `milestone`
- `progress`
- `checkpoint`
- `decision`
- `blocker`
- `next_step`
- `rejected_option`

Task 层不等于长期 Memory。它可以为当前任务提供连续性，但任务结束后只有通过晋升规则的内容才能进入用户层。

本稿不引入锚、账、集，也不定义调度态和机械态。当前只保留通用任务状态结构，为后续运行时设计留出位置。

### 4.5 Layer 4：Scratchpad / Turn

职责：当前一轮的工具观察、中间判断、候选方案、临时参数和未核实假设。

推荐保存：

- 第一版只存在于进程内或当前 Agent loop 对象中。
- 工具调用的客观输入输出仍由 `tool_calls` 表保存，不需要复制成 Scratchpad 数据表。
- 如将来需要崩溃恢复，再增加带 TTL 的短期快照；本轮不建永久表。

建议类型 `ScratchpadItem.type`：

- `tool_observation`
- `hypothesis`
- `candidate_plan`
- `temporary_variable`
- `intermediate_result`
- `pending_check`

Scratchpad 默认不进入长期提取。只有经过任务收束形成的明确状态才先写入 Task 层。

---

## 5. Markdown 能力如何拆

### 5.1 删除的旧语义

删除“Markdown 是条目数据库”的假设：

- 不再写入 `<!-- memory-entry:... -->`。
- 不再按特殊标识符切分条目。
- 不再提供 `upsert_entry`、`delete_entry` 一类基于隐藏 ID 的操作。
- 不再把 memory card 的 token、priority、confidence、evidence 等字段塞入 Markdown。

### 5.2 保留的新语义

Markdown 只承担“人类可读、可直接编辑、可 diff 的手册 / 规范”：

- 按普通文本读取整份文档。
- 人与 Agent 使用正常的文件编辑和 Git diff。
- 标题、列表、段落结构都只是内容，不是协议。
- 系统仅在外围记录 `path`、`layer`、`content_hash`、`token_count`、`modified_at`。
- 内容变更后通过哈希使旧加载快照失效。

### 5.3 第一版全量加载的 Markdown

推荐默认全量加载：

- 当前有效的 `POLICY.md`。
- 当前用户的简短 `MEMORY.md`。
- 当前项目声明为必读的规则文件，例如根目录 `AGENTS.md`。

项目中的大型 README、架构文档和领域文档不承诺永远全量加载；后续由 Context / RAG 设计解决。这里仅完成注册和来源划分。

### 5.4 不做自动双向同步

`MEMORY.md` 与 SQLite 用户记忆卡片不是同一内容的两个副本：

- `MEMORY.md` 是人工策展的常驻手册。
- SQLite 卡片是后台提取和检索的结构化资产。

如果未来需要“把若干卡片整理进 MEMORY.md”，应做成明确的人工审阅发布动作，而不是静默同步。

---

## 6. 统一原子：轻量投影，不统一底层表

Session、Markdown 文档、用户卡片和未来 RAG chunk 的生命周期完全不同，不应硬塞进同一张数据库表。它们只在观测、召回和上下文候选阶段投影成统一的 `MemoryItem`：

```python
@dataclass(frozen=True)
class MemoryItem:
    item_id: str
    layer: MemoryLayer
    type: str
    source_type: str
    source_ref: str
    scope: MemoryScope
    title: str | None
    content: str
    summary: str | None
    priority: int
    token_count: int
    content_hash: str
    created_at: datetime | None
    updated_at: datetime | None
    metadata: Mapping[str, JsonValue]
```

关键约束：

- `MemoryItem` 是统一投影，不是万能持久化模型。
- 各底层 Store 保留自己的领域结构。
- `priority` 是内容的稳定管理属性，可以持久化。
- `token_count` 是基于指定 tokenizer 的派生缓存，Trace 中还要记录 tokenizer 版本。
- `relevance` 依赖某一次 query，不能作为卡片静态字段；它只属于该次召回的 `MemoryItemTrace`。
- 未来 vector chunk 也只是带 `source_ref` 的派生 `MemoryItem`，不能成为独立事实源。

### 6.1 `MemoryScope`

第一版使用显式可空字段，不把多个维度编码进一个字符串：

```python
@dataclass(frozen=True)
class MemoryScope:
    organization_id: str | None = None
    tenant_id: str | None = None
    user_id: str | None = None
    project_id: str | None = None
    task_id: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
```

这能直接表达五层的归属，也为个人助手和未来多租户共用同一套结构。

---

## 7. SQLite 从 Memory 中拆出

### 7.1 定位

SQLite 是独立的本地数据基础设施，负责事务、迁移、连接和持久化。它不属于 episodic memory，也不负责判断什么值得记住。

Memory 只能通过 Store 协议访问它，不直接持有连接和 SQL。

### 7.2 目录建议

```text
agent/
  storage/
    __init__.py
    sqlite/
      __init__.py
      database.py
      migrations.py
      transaction.py
      session_store.py
      task_store.py
      memory_store.py
      memory_review_committer.py
      trace_store.py
      document_registry.py
      schema/
        001_sessions.sql
        002_tasks.sql
        003_memory.sql
        004_memory_trace.sql
      types/
        transaction_types.py
        storage_types.py
```

这里不提前抽象 PostgreSQL / MySQL 通用仓储层。先把 SQLite 做成一个深模块：外部只看到事务和 Store 接口，内部隐藏连接、SQL、迁移与锁。

### 7.3 表的归属

#### Session 事实表

```text
sessions
session_slots
turns
messages
tool_calls
```

这些表记录“发生了什么”，不属于长期记忆。

`turns` 第一版至少包含：

```text
id
session_id
sequence_no
status
started_at
completed_at
input_token_count
output_token_count
error_code
created_at
```

`messages`、`tool_calls` 使用 `turn_id` 建立明确归属。

#### Task 状态表

```text
tasks
task_state_items
task_checkpoints
```

`task_state_items` 至少包含：

```text
id
task_id
session_id                 nullable
type
title                      nullable
content_json
status
priority
source_type
source_ref                 nullable
revision
created_at
updated_at
completed_at               nullable
```

`task_checkpoints` 至少包含：

```text
id
task_id
checkpoint_no
summary
state_json
source_turn_id             nullable
created_at
```

#### 用户长期记忆表

```text
memory_items
memory_item_versions
memory_evidence
memory_keywords
memory_review_jobs
memory_extraction_runs
```

`memory_items` 第一版至少包含：

```text
id
layer                      v1 只允许 user
type
memory_kind                nullable
organization_id            nullable
tenant_id                  nullable
user_id                    nullable
project_id                 nullable
normalized_key
title                      nullable
summary
content_json
status
priority
confidence
source_authority
valid_from                 nullable
valid_until                nullable
supersedes_item_id         nullable
revision
content_hash
token_count
tokenizer_name
created_at
updated_at
last_confirmed_at          nullable
```

`status` 建议固定为：

```text
proposed
active
superseded
rejected
expired
deleted
```

`memory_item_versions` 保存每次可见变更前后的版本，不用覆盖旧事实。

`memory_evidence` 至少包含：

```text
id
memory_item_id
source_type                message / tool_call / manual / import
source_id
source_hash
evidence_excerpt           可裁剪，禁止保存隐藏推理
observed_at
created_at
```

`memory_keywords` 使用规范化关键词逐行保存，便于精确统计和关键词检索。全文检索可由 SQLite FTS5 在后续召回阶段增加。

#### 文档注册表

```text
context_documents
```

只保存外围元数据：

```text
id
layer
type
scope identifiers
path
content_hash
token_count
tokenizer_name
revision_ref               nullable
last_loaded_at             nullable
created_at
updated_at
```

正文仍以 Markdown 文件为权威来源。

#### Trace 表

```text
memory_traces
memory_item_traces
```

Trace 与业务表分离，允许独立保留周期和采样策略。

### 7.4 事务边界

一次 Turn 完成时，应在同一个 SQLite 事务中完成：

1. 写入 Turn、Messages、ToolCalls。
2. 更新 Session 游标。
3. 写入一个 `memory_review_jobs` outbox 任务。

这样可以保证“事实已经提交却永远没有提取任务”以及“提取任务存在但消息没提交”都不会发生。

Memory Reviewer 在另一个事务中领取 job、提取、校验并原子提交整批结果。

---

## 8. Session → Memory 提取逻辑

### 8.1 边界

Session Store 只记录事实并发布待审 job，不判断长期价值。

Memory 模块负责：

- 读取尚未审查的 Session 增量。
- 生成结构化候选。
- 校验候选层级、证据、冲突和敏感性。
- 对用户记忆执行 create / merge / supersede / noop。
- 必要时生成 TaskStateItem 建议。
- 写入 MemoryTrace。

提取器不允许：

- 自动写 `POLICY.md`、项目文档或 `MEMORY.md`。
- 把被召回进上下文的旧记忆再次当成新事实。
- 从模型自己的猜测中生成用户长期画像。
- 把单次任务卡顿、临时目标或中间方案直接晋升为用户事实。

### 8.2 流程

```text
Session/Turn 原始事实提交
        ↓ 同事务写 review job
Reviewer 领取租约
        ↓
读取自上次 cursor 后的新消息与工具事实
        ↓
来源过滤与去回声
        ↓
AI 生成结构化 MemoryProposal
        ↓
确定性 Validator 校验
        ↓
查重 / 合并 / 冲突 / 失效判断
        ↓
原子写入卡片、版本、证据、关键词和 Trace
        ↓
推进 review cursor，完成 job
```

### 8.3 `MemoryProposal` 字段

AI 输出只允许是提案，不直接成为数据库记录：

```text
proposal_id
target_layer               只允许 user 或 task
type
memory_kind                target_layer=user 时可用
normalized_key
title                      nullable
summary
content_json
keywords[]
confidence
source_message_ids[]
source_tool_call_ids[]
valid_from                 nullable
valid_until                nullable
proposed_mutation          create / merge / supersede / noop
reason_code
```

`reason_code` 是短的结构化原因，不要求也不保存模型 chain-of-thought。

### 8.4 晋升规则

#### 允许进入 User 层

至少满足以下之一：

- 用户明确表达稳定偏好、身份事实或长期配置。
- 同一事实在不同任务中被重复证实。
- 工具返回了可验证、长期有效的用户 / 租户事实。
- 一条任务经验已经被证明可跨任务复用。

同时必须满足：

- 有可定位证据。
- 不是敏感秘密或被策略禁止保存的内容。
- 不是纯粹的当前任务状态。
- 能映射到封闭的 `type`。
- 通过重复、冲突和有效期校验。

#### 只留在 Task 层

- 当前目标、进度、下一步、阻塞。
- 本任务内做出的决策或排除方案。
- 尚未证明可跨任务复用的经验。
- 对当前 Session 有用、但不应改变用户画像的信息。

#### 只留在 Scratchpad

- 未核实假设。
- 工具结果的临时解释。
- 候选方案和中间计算。
- 已在本轮消费完的临时变量。

#### 禁止自动晋升

- 任意内容 → Policy。
- 任意内容 → Project 文档。
- Scratchpad 未经任务收束直接 → User。
- 模型推断出的敏感属性 → User。

### 8.5 显式陈述与推断的状态

推荐默认规则：

- 用户明确陈述、风险较低且语义清晰的偏好：可直接 `active`。
- 模型推断、一次性行为归纳、能力画像：先 `proposed`。
- 多次独立证据或用户确认后：`proposed → active`。
- 与现有 active 卡片冲突：不覆盖，生成冲突结果并等待 supersede 规则或人工确认。

### 8.6 幂等与去回声

每次提取使用：

```text
input_hash = hash(session_id + cursor_range + message_hashes + extractor_version)
```

`memory_extraction_runs.input_hash` 唯一，重复执行只返回已有结果。

所有输入消息还要标记其来源：

- `user_authored`
- `assistant_authored`
- `tool_observed`
- `memory_recalled`
- `system_injected`

默认只有 `user_authored` 和可信 `tool_observed` 可以成为用户长期事实的主证据；`memory_recalled` 只能辅助查重，不能自我强化。

### 8.7 Review Job 字段

```text
id
session_id
from_turn_no
to_turn_no
trigger_type
status
lease_owner              nullable
lease_expires_at         nullable
attempt_count
extractor_version
input_hash
last_error_code          nullable
last_error_message       nullable
created_at
started_at               nullable
completed_at             nullable
```

job 状态：`pending / running / completed / retryable_failed / terminal_failed`。

---

## 9. MemoryTrace 设计

遵循项目约定，模块级 Trace 命名为 `MemoryTrace`，每个原子候选对应一个 `MemoryItemTrace`。

### 9.1 运行时 / 校验阶段可直接记录的字段

这些字段不能要求 AI 生成：

```text
trace_id
operation                  extract / validate / persist / retrieve / load
session_id / turn_id / task_id
job_id / extraction_run_id
item_id / proposal_id
source_type / source_ref
layer / type
input_hash / content_hash
token_count / tokenizer_name
validator_version
dedup_candidate_ids[]
conflict_item_ids[]
validation_codes[]
decision                   accepted / merged / superseded / rejected / skipped
latency_ms
created_at
```

未来召回阶段还可以记录：

```text
query_hash
keyword_hits
similarity_score
recency_score
final_relevance_score
loaded_into_context
context_position
```

注意：`final_relevance_score` 是一次召回的结果，不写回 `memory_items`。

### 9.2 AI 输出时一并提供的字段

```text
proposed_layer
proposed_type
proposed_memory_kind
proposed_keywords[]
self_reported_confidence
evidence_refs[]
proposed_mutation
reason_code
```

### 9.3 Trace 不记录什么

- 不记录隐藏 chain-of-thought。
- 不把完整 Prompt 和完整用户隐私无条件复制一遍。
- 不把 Trace 当作新的长期记忆来源。
- 不因为 Trace 失败而阻止 Session 原始事实落库。

---

## 10. Memory 模块代码结构

Memory 位于 `agent/core/memory/`。它不依赖 SQLite，通过 Protocol 与持久化 Adapter 分离；运行时只向 Precision 交付一次冻结的 `MemorySnapshot`。

```text
agent/core/memory/
  __init__.py
  memory_layer.py
  memory_item.py
  policy_item.py
  project_item.py
  user_memory_item.py
  task_state_item.py
  scratchpad_item.py
  memory_scope.py
  markdown_book.py
  memory_snapshot.py
  memory_context_loader.py
  memory_extractor.py
  memory_validator.py
  session_memory_reviewer.py
  memory_review_worker.py
  memory_trace.py
  types/
    __init__.py
    extraction_types.py
    memory_store_types.py
    memory_source_types.py
```

职责说明：

- `memory_layer.py`：`MemoryLayer` 和层级覆盖 / 晋升规则。层级类放在自己的模块，不塞进 `types/`。
- `*_item.py`：各层原子对象及本层字段校验，全部使用 `XxxItem` 命名。
- `memory_item.py`：跨来源的轻量统一投影。
- `memory_scope.py`：显式 Scope 值对象。
- `markdown_book.py`：整文档读取、哈希、token 统计和安全写入；不理解条目标记。
- `memory_snapshot.py`：定义单 turn 不可变快照、文档修订和加载诊断。
- `memory_context_loader.py`：定位并按 Policy → Project → User 顺序冻结普通 Markdown；这是运行时唯一的 Memory 加载 Interface。
- `memory_extractor.py`：将 Session 增量转成 `MemoryProposal`。
- `memory_validator.py`：确定性层级、证据、隐私、去重和晋升校验。
- `session_memory_reviewer.py`：纯提取审查、来源过滤、批内去重和 Trace 组装。
- `memory_review_worker.py`：连接 outbox、纯 Reviewer 与持久化 Committer；负责任务租约和有界重试，不决定记忆内容。
- `memory_trace.py`：`MemoryTrace`、`MemoryItemTrace` 及记录逻辑。
- `types/`：只放请求 / 结果 / Protocol 等接口类型，不放实体类和层级类。

### 10.1 暂不创建总管式 `MemoryManager`

第一阶段不保留一个同时管理 Markdown、SQLite、搜索、Provider 和上下文注入的“大总管”。先让模块边界稳定：

- 文档由 `MarkdownBook` 管。
- Session / Task / Memory 表由独立 Store 管。
- 提取由 `SessionMemoryReviewer` 编排。
- 全量文档装配由 `MemoryContextLoader.freeze` 管；结构化卡片召回后续再设计独立窄接口。

这样能避免在边界尚未确定时把所有行为再次塞回 `MemoryManager`。

---

## 11. 现有代码的迁移方向

本重构不做兼容别名，不保留旧路径转发；改名后直接修改所有调用方。

| 原位置 | 当前结果 |
|---|---|
| `agent/memory/episodic/*` | 已拆入 `agent/storage/sqlite/` 的独立连接、migration 与 Store |
| `agent/memory/hot/*` | 已删除；普通 Markdown 由 `core/memory/markdown_book.py` 整文档管理 |
| `agent/memory/memory_types.py` | 已按五层实体模块与 `types/` 协议目录拆分 |
| `agent/memory/memory_extractors.py` | 已收敛为 `core/memory/memory_extractor.py` |
| `agent/memory/session_reviewer.py` | 已收敛为 `core/memory/session_memory_reviewer.py` |
| `agent/memory/memory_manager.py` | 已删除；没有旧名称 alias 或转发层 |
| `agent/tools/memory_tools.py` | 已删除；未来按搜索语义重新建立 recall tools，不复用条目 CRUD 协议 |
| 旧 Evolution playbook → marker Markdown 链 | 已删除；Hermes Skill 演化保留并与 Memory 分离 |
| `agent/core/precision/` | 算法未改；只消费新的 `MemorySnapshot` 类型 |

执行时遵守：

- 不为旧名称创建 alias。
- 全仓修改 import 和调用点。
- 不修改仍然成立的现有注释。
- 只有原逻辑被删除时才删除相应注释；新逻辑只增量补充说明。

---

## 12. 开发顺序

### Phase 0：冻结本审批稿（已完成）

先确认文末决策项，再开始代码修改。

验收：五层含义、Markdown / SQLite 分工、自动晋升边界没有歧义。

### Phase 1：SQLite 基础设施独立（已完成）

1. 建立 `agent/storage/sqlite/`。
2. 引入 migration runner 和事务边界。
3. 搬迁现有 Session 表与 Store。
4. 增加明确的 `turns` 表和外键。
5. 全仓修改调用点，删除旧 `episodic` DB 路径。

验收：现有 Session 行为不变；重启后可恢复；migration 可从空库构建；没有兼容转发层。

### Phase 2：建立五层实体和保存边界（已完成）

1. 实现 `MemoryLayer`、`MemoryScope` 和五类 `XxxItem`。
2. 实现轻量 `MemoryItem` 投影。
3. 建立 `tasks`、`task_state_items`、`task_checkpoints`。
4. 建立结构化用户记忆表与文档注册表。
5. 加入数据库约束，禁止自动记忆表写入非 `user` layer。

验收：每类数据都有唯一权威 Store；层级非法写入在数据库或 Validator 层被拒绝。

### Phase 3：拆除 Markdown 条目标记（运行时已完成）

1. 新建 `MarkdownBook`。
2. 改为整文档读取、哈希、token 统计。
3. 删除隐藏 marker 的解析和条目 CRUD。
4. 配置 Policy Book、Project Book、User Book 的来源。
5. 对旧 marker 文档提供一次性迁移脚本或人工清理报告；不在运行时保留兼容解析。

验收：任意自然 Markdown 都能读取；标题和注释变化不会破坏加载；用户可直接 diff 和编辑。

### Phase 4：重做 Session → Memory 提取（已完成）

1. Turn 提交时同事务创建 review job。
2. 实现租约、cursor、重试和幂等。
3. 定义严格 `MemoryProposal` 输出。
4. 实现来源过滤、层级校验、查重、冲突和晋升规则。
5. 原子写入记忆、版本、证据与关键词。

验收：重复 job 不产生重复卡片；旧召回记忆不会自我强化；临时任务状态不会污染用户层。

### Phase 5：MemoryTrace 与审计测试（已完成）

1. 实现 `MemoryTrace` / `MemoryItemTrace`。
2. 区分运行时字段和 AI 提案字段。
3. 对提取、拒绝、合并、supersede 全路径记录 reason code。
4. 增加隐私裁剪和 Trace retention 配置。

验收：可以从任一卡片追溯到证据、提取 run、校验决定和版本变化；不保存隐藏推理。

### Phase 6：下一阶段

- Precision 的 Memory 参数化开关与更细装配策略；当前只接冻结快照，不改算法。
- Working Memory 的最终装配。
- 锚 / 账 / 集。
- 机械态和调度态。
- 关键词 / 相似度 / 混合搜索工具。
- 强制经验加载。
- Embedding、向量数据库和 RAG。
- 外部 Memory Provider。

上述能力在 Phase 1–5 稳定后分别立项，避免一开始把持久化、召回和运行时策略再次耦合。

---

## 13. 测试文件建议

```text
tests/storage/sqlite/
  test_migrations.py
  test_session_store.py
  test_transactional_review_outbox.py
  test_memory_review_committer.py
  test_task_store.py
  test_memory_store.py

tests/core/memory/
  test_memory_layer.py
  test_memory_item.py
  test_markdown_book.py
  test_memory_validator.py
  test_session_memory_reviewer.py
  test_memory_trace.py
```

必须覆盖的关键场景：

- Session 提交与 review job 的原子性。
- review job 崩溃后租约恢复。
- 同一增量重复提取的幂等性。
- 用户明确偏好进入 user，当前任务目标只进入 task。
- 模型推断的能力画像停留在 proposed。
- recalled memory 不能作为自己的新证据。
- 冲突卡片保留旧版本并生成 supersede / 待确认结果。
- 普通 Markdown 不含任何 marker 也可读取。
- Policy / Project 不可由自动提取器写入。
- Trace 的运行时字段不接受 AI 伪造。

---

## 14. 已采用的决策

### 决策 A：用户层是否保留 `MEMORY.md`

**已采用：保留。** 它是一份短小、人工策展、全量加载的用户手册；大量条式记忆只进入 SQLite，后台提取器不自动改 Markdown。

### 决策 B：Policy 文件是否允许 Agent 直接修改

**已采用：默认不允许。** Agent 可以提出变更草案，但正式文件由管理入口批准写入。

### 决策 C：Scratchpad 是否持久化

**已采用：第一版不持久化。** 客观工具调用进入 `tool_calls`；只有任务收束结果进入 `TaskStateItem`。

### 决策 D：自动提取的推断性用户记忆是否立即生效

**已采用：不立即生效。** 明确、低风险的用户陈述可直接 active；推断内容先 proposed，需要重复证据或用户确认。

### 决策 E：第一版是否预留多租户 Scope

**已采用：预留可空的 `organization_id / tenant_id / user_id / project_id` 字段。**

### 决策 F：旧 Marker Markdown 如何处理

**已采用：做一次性迁移，不做运行时兼容。** 脚本默认只报告；显式 apply 后先备份，再把旧条目导入 SQLite，并将 Markdown 恢复为普通文档。

---

## 15. 当前实现闭环

```text
Turn 提交
  → Session / Messages / ToolCalls + review job 同事务落库
  → 后台 MemoryReviewWorker 领取租约
  → Extractor 只产出 user/task Proposal
  → Validator 做来源、层级、敏感信息与证据校验
  → Committer 原子写卡片、版本、证据、关键词与 MemoryTrace

下一 Turn
  → MemoryContextLoader 冻结 Policy / Project / User Markdown
  → MemorySnapshot 交给现有 Precision
  → 同一 Turn 的所有模型迭代共享同一快照
```

结构化卡片当前只沉淀和审计，不自动塞入全量 Markdown，也不在没有召回策略时隐式进入上下文。

---

## 16. 当前文件职责

### 16.1 Memory 领域模块

| 文件 | 单一职责 |
|---|---|
| `agent/core/memory/__init__.py` | 对外导出稳定的 Memory 领域 API，不实现业务逻辑。 |
| `memory_layer.py` | 定义 Layer 0–4、权威顺序、合法 Item type 与自动晋升边界。 |
| `memory_scope.py` | 定义组织 / 租户 / 用户 / 项目 / 任务 / 会话 / 轮次归属，并生成稳定存储键。 |
| `memory_item.py` | 定义所有来源都可投影到的轻量 `MemoryItem` 与内容哈希。 |
| `policy_item.py` | Policy 层原子及本层字段约束。 |
| `project_item.py` | Project 层原子及本层字段约束。 |
| `user_memory_item.py` | User 层卡片、memory kind 与生命周期状态。 |
| `task_state_item.py` | Task / Session 层状态原子与状态枚举。 |
| `scratchpad_item.py` | 单轮 Scratchpad 原子；第一版只在运行期使用。 |
| `markdown_book.py` | 普通 Markdown 的整文档读取、token/哈希、乐观并发与原子替换。 |
| `memory_snapshot.py` | Precision 消费的不可变快照、文档 revision 和加载诊断。 |
| `memory_context_loader.py` | 按 Policy → Project → User 冻结文档、注册元数据并构建安全围栏。 |
| `memory_extractor.py` | 规则 / 模型 / fallback 提取器；只生成 User 或 Task Proposal。 |
| `memory_validator.py` | 确定性检查层级、证据、敏感内容、Scope 与立即生效资格。 |
| `session_memory_reviewer.py` | 做来源过滤、批内去重、Validator 编排、Evidence 与 Trace 组装。 |
| `memory_review_worker.py` | 消费 outbox、管理租约 / 重试 / 唤醒，不决定记忆内容。 |
| `memory_trace.py` | 定义 `MemoryTrace` / `MemoryItemTrace`，隔离 AI 字段和运行时字段。 |
| `types/extraction_types.py` | Proposal、Evidence、Validation Result 与 Extractor Protocol。 |
| `types/memory_source_types.py` | Turn 事实信封和 Session reset 结果。 |
| `types/memory_store_types.py` | Memory / Task / Trace 的持久化 Protocol。 |

### 16.2 SQLite Adapter

| 文件 | 单一职责 |
|---|---|
| `agent/storage/sqlite/database.py` | 创建开启 WAL、外键和 busy timeout 的短连接。 |
| `migrations.py` | 校验 migration checksum、升级空库和旧列。 |
| `transaction.py` | 提供不泄漏连接细节的短事务 helper。 |
| `schema/001_sessions.sql` | Session、Turn、Message、ToolCall 与 review outbox 事实表。 |
| `schema/002_tasks.sql` | Task、TaskStateItem 与 checkpoint 表。 |
| `schema/003_memory.sql` | 用户卡片、版本、Evidence、Keyword、Extraction Run 与文档注册表。 |
| `schema/004_memory_trace.sql` | MemoryTrace 和逐 Item Trace 表及索引。 |
| `session_store.py` | 会话恢复、Turn 原子提交、reset、archive 与 outbox 租约。 |
| `task_store.py` | TaskStateItem 的事务写入与按任务读取。 |
| `memory_store.py` | UserMemoryItem、版本和关键词的权威存取。 |
| `memory_review_committer.py` | 一次 review 的查重 / 冲突 / supersede 与整批原子提交。 |
| `trace_store.py` | Trace 写入、按 ID/session/item 查询和保留期清理。 |
| `document_registry.py` | 只登记 Markdown 的 path/hash/token/revision 元数据，不复制正文。 |
| `types/storage_types.py` | SQLite Adapter 内部返回值，不承载 Memory 实体。 |

### 16.3 运行时装配与观测

| 文件 | 单一职责 |
|---|---|
| `agent/gateway/gateway.py` | 每 turn 冻结一次 MemorySnapshot；提交事实后唤醒旁路 worker。 |
| `agent/main.py` | 根据 `use_memory` 装配 SQLite、Loader、Reviewer、Committer 与生命周期。 |
| `agent/gateway/http_api.py` | 提供只读 Session archive 和冻结 Memory Context debug API。 |
| `agent/evolution/assembly.py` | 只保留 Skill 上下文装配观测，不再控制 Memory Daily/Provider。 |
| `scripts/migrate_legacy_memory_markers.py` | 离线扫描或迁移旧 marker；永不进入运行时导入链。 |
| `frontier/src/api/debugMemory.ts` | 调用新的 Session/Memory Context debug API。 |
| `frontier/src/stores/debugMemory.ts` | 保存 archive、snapshot、revision 和加载状态。 |
| `frontier/src/views/debug/DebugMemoryContextPage.vue` | 展示本轮冻结 Prompt、revision 与 diagnostic。 |

### 16.4 测试布局

- `tests/core/memory/`：五层领域规则、普通 Markdown、冻结上下文、提取、校验和 reviewer 纯逻辑。
- `tests/storage/sqlite/`：migration、Session/outbox 事务、领域 Store、原子 Committer 与 Trace retention。
- `tests/memory/`：Memory 与 Precision/Gateway 的跨模块闭环、旧 marker 离线迁移。
- `tests/eval/`：分层提取 golden、跨轮不可见性和 Layer 装配评测。
- `tests/gateway/`：Debug API 与持久化提交后的 worker 唤醒行为。
