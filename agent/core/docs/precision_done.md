# Percision done

正文见 [precision_done.md](precision_done.md)。飞书对照：[percision设计](https://tcnff4lwpccj.feishu.cn/wiki/QhoywPVkwiNuUZk9ii4cYuGAn6g) 的 Percision done 节。


## 1. 模块边界

- `agent/types/context.py` 定义全局 `Anchor` 和 `AnchorFact`，并提供纯文本投影。
- `precision/percision_engine.py` 负责 load → grade → build、预算与阶段编排。
- `precision/percision_anchor.py` 负责模型 Adapter、五槽内容、覆盖/digest 校验、生成重试和 Anchor 投影。
- `precision/errors.py` 集中定义预算、Anchor 构建/压缩和判官错误；原 `core/context_budget.py` 已移除。
- `precision/context_item.py` 保存内部条目与共享加载状态；`priority.py` 保持轻量枚举入口。
- `precision/percision_trace.py` 只记录观测，不参与分级决策。
- `precision/percision_admission.py` 负责条目级上下文准入（规则、相关度、分档、投影），见 §7。
- `precision/percision_judge.py` 定义 `ContextJudge` 协议和通用 System One Adapter（端点、模型、密钥全部注入）。
- `agent/provider.py` 的 `build_context_judge` / `build_admission_policy` 决定用哪个判官 provider 和准入模式。

`Session.messages` 是历史事实来源；`Session.context_anchor` 是可重建的压缩缓存。`Session.summary` 只给旧五槽文本做迁移提示，不能声明覆盖范围。

## 2. 生产接口

```python
PrecisionBuilder.build(
    session,
    *,
    memory_snapshot=None,
    skill_selection=None,
    ephemeral_prompt=None,
    budget=None,
    token_estimator=None,
    trace_id="context",
    iteration=None,
) -> PrecisionResult
```

Engine 构造时可注入 `anchor_builder`。AgentLoop 默认用 `ModelAnchorBuilder(anchor_model or self.model)`，无工具、单次 JSON 调用。

`PrecisionResult` 保留 `messages`、`metrics`、`records`、`exposed_refs`、`anchor`、`anchor_updated`。Engine 不修改 Session；AgentLoop 只在 `anchor_updated=True` 时把 `result.anchor` 写回 `Session.context_anchor`。旧 `summary` 与 `context_anchor_payload` 便利属性已删除；直接使用结构化 `anchor`。

失败分两条路径，都发生在主模型调用之前：

- P0/P1 或最终 messages 超预算 → `ContextBudgetExceededError` → `error="context_budget_exceeded"`。
- Anchor 压缩失败且原文也无法安全装入 → `AnchorCompactionError` → `error="anchor_compaction_failed"`。

## 3. 数据流

```text
Gateway/Store
  └─ Session(messages, context_anchor)
       + MemorySnapshot
       + SkillSelection
       + ephemeral prompt
             │
             ▼
Load
  ├─ 系统 / Markdown Memory / 经验召回 / Skill / ephemeral 转成 ContextItem
  ├─ Session.messages 按 user 切成完整轮次，name 为 history:seq:{seq_id}
  ├─ 校验 Session.context_anchor（session、覆盖边界、digest、事实来源）
  └─ 有效 Anchor 立即转为 P2-A ContextItem，seq 在全部历史之前
             │
             ▼
Grade
  ├─ P0/P1 硬保留
  ├─ 已验证 Anchor 替代被覆盖的最旧连续前缀
  ├─ 未覆盖历史按完整轮次处理，最近两轮始终留原文
  ├─ 超预算、达到 85%，或已有 Anchor 超过 15%/2048 时触发压缩
  └─ P3 只挂 handle；handle 自己超预算则 dropped，不让整轮失败
             │
             ▼
Build
  ├─ selected 按 seq 排序
  ├─ 追加 lazy-context
  └─ 最终预算检查
             │
             ▼
PrecisionResult → Trace → AgentLoop 写回 context_anchor → Gateway 持久化
```

## 4. 优先级

1. P0：system、Policy/Project/User Markdown 快照、Skill 摘要、ephemeral、当前 user。
2. P1：关键词命中的经验卡片、当前 ReAct 产生的 assistant/tool。
3. P2-A：已验证的 Anchor。
4. P2-B：未覆盖的历史原文。
5. P3：预留的按需读取 handle；当前生产 Memory 路径不生成旧 Daily/Provider handle。

优先级决定选择，`ContextItem.seq` 决定最终发送顺序。P2 工具失败不再强制保送原文；被覆盖时必须进入 Anchor `decisions`，且 `status=failed`。P0/P1 的工具失败仍算受保护错误，丢失会计入 `protected_error_missing_count`。

## 5. Anchor 覆盖与失败

- Anchor 只覆盖 Session 最旧的连续完整轮次；覆盖账本（seq / message_id / digest / anchor_id）由运行时计算，Builder 不得改。
- 最近两个完整历史轮次始终保留原文。
- 已有合法 Anchor 低于阈值时直接 reuse，不重复调用 Builder。扩展时只把未覆盖的新前缀交给 Builder。
- 旧 `<context-anchor>` 文本只作 existing_anchor 提示，不用于过滤原文。
- Anchor 上限是 `min(max_input_tokens * 15%, 2048)`。
- Builder 是单参数 Callable：输入 JSON payload，输出恰好五槽（intent / changes / decisions / next_steps / excluded）。
- 每条事实必须带 `source_message_ids`，且只能引用覆盖范围内的消息。tool call 只表示尝试，成功 observation 才能进 changes；长 observation 不复制进渲染。
- Engine 校验精确字段、事实来源、必保工具错误和 token 上限。
- 第一次失败后，把校验错误和 `max_tokens // 2` 交给第二次调用。
- 两次失败，或没有 Builder，且全部原文可放入时使用 `raw_fallback`：窗口里只留 mustload + 全量 P2 原文，不插入 Anchor。
- 原文也无法放入时抛 `AnchorCompactionError`。
- 损坏或 digest 不匹配的 Anchor 绝不用于过滤历史，直接恢复 Session 原文。

渲染给主模型时带 version / id / 覆盖区间，并声明：这是较早历史的压缩替代；与近期原文冲突以原文为准；未提及的细节视为未知；`status=failed` 不得当成已完成修改。

## 6. 指标、Trace 与持久化

`PrecisionMetrics` 在原有预算/丢弃/P3 指标之外，增加 `anchor_status`（`not_needed` / `reused` / `created` / `extended` / `raw_fallback`）、`anchor_covered_turns`、`anchor_tokens`、`anchor_builder_attempts`、`anchor_error_code`。

Trace 仍是旁路滚动窗口：同一 `turn_id` 只占一轮，但 `builds` 保留每个 `iteration` 的完整 messages、实际工具定义、records 和指标。快照在模型调用前深拷贝，重试复用；构建失败的迭代 messages 为 None，并保存错误码。成功 `memory_read` 仍记录 P3 hit。

Gateway 将完整 builds 写入现有 Turn payload。`/api/debug/memory/trace?turn_id=...&iteration=...` 按需返回该次全文与迭代摘要，列表不返回 messages。旧记录明确返回 `full_context_not_saved`；构建失败不使用上一迭代全文。Loop 的调用大模型步骤和 Precision Trace 表都可打开全文，切换文本/JSON 并复制；沿用 `--enable-memory-debug` 与 HTTP 认证。多媒体保留附件引用，不在快照写入 base64。

`Session.context_anchor` 接受 `Anchor | dict | None`：

- 新写入为 `Anchor`。
- 合法旧字典恢复为 `Anchor`。
- 损坏字典保留给 Precision 识别和降级，不阻断 Session 构造。

SQLite 的 Session payload 自动序列化 Anchor，不需要新表。原始 messages 始终单独持久化，因此 Anchor 丢失、损坏或版本不兼容时都可重建。

## 7. 条目级上下文准入（第一级压缩）

Anchor 之前先对 P2 历史中的工具结果逐条定案 keep / trim / defer；仍超阈值才由 Anchor 做整轮语义压缩。

**资格**：只判未被 Anchor 覆盖、`role=tool`、非报错、至少隔一个 user 轮次、长度 ≥ `min_chars`（2000 字）的结果。用户消息、assistant 消息、P0/P1 一律不碰。

**打分**（0–1，越高越需要全文）：

1. 规则：之后同一调用重跑、同一文件重读或被修改 → 0 分，不问判官。
2. 相关度：调用参数中的词项（文件名、标识符、检索词）在之后对话里出现的比例。
3. 判官：每条两题——`kind`（error / listing / content / log / data / other）与 `still_needed`（P）；分数 `0.8·P + 0.2·相关度`。
4. 判官缺席或失败：`0.6·kind 先验 + 0.4·相关度`，kind 由工具名推断。

**分档**（按本次用量，`AdmissionPolicy.bands`）：

| 用量 | defer | trim |
| --- | --- | --- |
| < 50% | 不产生新定案 | |
| ≥ 50% | score ≤ 0.15 | score < 0.50 |
| ≥ 70% | score ≤ 0.25 | score < 0.60 |
| ≥ 85% | score ≤ 0.35 | score < 0.70 |

**投影**：trim 保留首尾各 600 字；defer 换成一行占位符。两者都带 `memory_read(reference="tool:<id>")`（没注册 memory_read 时提示重新执行）。只改发给模型的消息副本，`tool_call_id` 不变；`ContextItem.raw_message` 保留原文，Anchor digest 只按原文计算。

**粘住与反馈**：定案按 message_id 存进 `Session.context_verdicts`，随 Session payload 持久化；已定案条目不重判，用量升档时只按原分数升级。模型 `memory_read` 读回被 trim/defer 的结果即判错：AgentLoop 将其钉住为全文（`missed=True`）并发 `context_admission_miss` 事件。

**模式**：`off` 与未接入完全一致；`shadow` 照常判、写定案和 `PrecisionRecord.admission_*`，但不改消息；`active` 生效。生产由 `agent/provider.py` 按 `CONTEXT_ADMISSION_MODE`（默认 shadow）与 `JEVMODEL_API_KEY` / `JEV_BASE_URL`（默认 `https://jevmodel.org/v1/systemone`）/ `JEV_MODEL`（默认 `jev-latest`）/ `JEV_TIMEOUT` / `JEV_BATCH_SIZE` 装配；换 provider 只改 `build_context_judge`。

**指标**：`admission_candidates / judged_count / judge_failures / judge_latency_ms / rule_count / trimmed_count / deferred_count / saved_tokens / missed_count`。shadow 下 trimmed / deferred / saved 是"若生效"的值。

JEV_BASE_URL 填完整 System One 端点，密钥须由对应服务签发。jevmodel.org 每次最多接受 8 道题，每条上下文占 choice + noul 两题，默认每批 4 条；更大的 JEV_BATCH_SIZE 也按此限制拆批。401 会记录为判官失败并使用先验，不代表真实 JEV 已成功裁决。

真实模型探针：`python tests/live/precision_probe.py --output data/eval/precision-live-<新目录>`。探针使用合成对话和独立 SQLite，在进程内将准入触发降至 1%、min_chars 降至 512、Anchor 输入预算设为 11000；不修改生产开关和阈值。输出 report.json 与模型调用记录；两条真实能力未全部通过时退出码为 1。
