# Percision done

正文见 [precision_done.md](precision_done.md)。飞书对照：[percision设计](https://tcnff4lwpccj.feishu.cn/wiki/QhoywPVkwiNuUZk9ii4cYuGAn6g) 的 Percision done 节。


## 1. 模块边界

- `core/types.py` 定义全局 `Anchor` 和 `AnchorFact`，只保存数据。
- `precision/percision_engine.py` 负责 load → grade → build、预算和 Anchor 覆盖。
- `precision/percision_anchor.py` 负责模型 Adapter、提示词和 Anchor 渲染。
- `precision/percision_trace.py` 只记录观测，不参与分级决策。

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
) -> PrecisionResult
```

Engine 构造时可注入 `anchor_builder`。AgentLoop 默认用 `ModelAnchorBuilder(anchor_model or self.model)`，无工具、单次 JSON 调用。

`PrecisionResult` 保留 `messages`、`metrics`、`records`、`exposed_refs`、`anchor`、`anchor_updated`。Engine 不修改 Session；AgentLoop 只在 `anchor_updated=True` 时把 `result.anchor` 写回 `Session.context_anchor`。`summary` 属性只是只读渲染，新代码不要靠它持久化。

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

Trace 仍是旁路滚动窗口：同一 `turn_id` 的多次 ReAct 构建只占一条；`memory_read` 成功才记 P3 hit。

`Session.context_anchor` 接受 `Anchor | dict | None`：

- 新写入为 `Anchor`。
- 合法旧字典恢复为 `Anchor`。
- 损坏字典保留给 Precision 识别和降级，不阻断 Session 构造。

SQLite 的 Session payload 自动序列化 Anchor，不需要新表。原始 messages 始终单独持久化，因此 Anchor 丢失、损坏或版本不兼容时都可重建。
