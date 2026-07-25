# Agent Loop - 对话大脑与 OTAP

> 对应代码：`agent/core/loop.py`、`agent/core/context_builder.py`、`agent/core/messages.py`、`agent/core/events.py`
> 地位：Agent 的"大脑"。所有思考、工具调用、多步推理都在这里发生。

---

## 一、定义是什么

Agent Loop 是**核心对话循环**，遵循 `observe -> think -> act -> persist`（OTAP）模式。它吃 `Session + Message`，吐 `TurnResult`，在 think/act 之间做**工具内循环**（模型要求调工具就执行并回注，直到模型给出纯文本或触达上限）。

```
observe: 用户消息入 Session
   │
   ▼
┌──── think/act 内循环（最多 max_iterations 轮）────┐
│ think: ContextBuilder 组 messages -> model.chat   │
│ act ①: 若返回 tool_calls -> 执行 -> 回注 -> 继续   │
│ act ②: 若返回纯文本 -> 写 Session -> 跳出          │
└─────────────────────────────────────────────────┘
   │
   ▼
返回 TurnResult（final_text / completed / error）
```

**关键特征：Loop 不认识任何平台**。它看不到 Channel、队列、session_key 的业务含义，只知道"给我一个 Session 和一条 user message，我还你一个 TurnResult"。

---

## 二、负责的任务是什么

1. **Observe（观察）**：把传入的 user_message 追加进 `Session.messages`，维护 seq_id/计数/时间戳（`session.add_message`）。发 `turn_start` 事件。

2. **Think（思考）**：用 `ContextBuilder` 把 Session 组装成 OpenAI 格式的 messages 列表，调 `model.chat(messages, tools=schemas)` 拿回复。发 `llm_request` / `llm_response` 事件。

3. **Act（行动 / 工具内循环）**：
   - 若模型返回 `tool_calls`：逐个执行（`ToolRegistry.dispatch`），把结果作为 `role=tool` 消息回注 Session，**继续下一轮 think**
   - 若模型返回纯文本：写入 Session，本轮结束
   - 若超过 `max_iterations`：写兜底文案，返回 `error="max_iterations"`

4. **TurnResult 产出**：返回 `final_text` / `session_id` / `completed` / `interrupted` / `error`，供 Gateway 包装成 OutboundReply。

5. **预算与中断**：
   - `max_iterations` 限制工具循环次数防爆走
   - `request_interrupt()` 设 flag，每轮 think 前检查，被打断时优雅返回 `interrupted=True`

6. **容错兜底**：
   - 模型调用失败：按 `model_retries` 重试（带退避），仍失败 -> `TurnResult(error)`
   - 工具执行失败：ToolRegistry 已兜底成 `ToolResult(ok=False)`，回注让 LLM 自我修正
   - 全程 `logging` + `EventBus` 事件，前端可逐步观测

---

## 三、能力清单

### 3.1 主入口

| 方法 | 作用 |
|------|------|
| `run_turn(session, user_message, ephemeral_prompt, session_key)` | 跑一轮对话，返回 TurnResult |
| `request_interrupt()` | 外部请求打断（Channel/Gateway 调用） |

### 3.2 内部能力

| 方法 | 作用 |
|------|------|
| `_chat_with_retry(messages)` | 调模型，带 `model_retries` 次重试 + 指数退避 |
| `_execute_tool(req, session, turn_id, session_key)` | 执行单个工具调用，记录 ToolCall，发事件 |
| `_emit(type, turn_id, session, session_key, **data)` | 往 EventBus 发观测事件 |

### 3.3 ContextBuilder 能力（Loop 的"上下文组装器"）

| 阶段 | 内容 | 是否落 Session |
|------|------|---------------|
| 1 | System Prompt（`prompt/systemPrompt.md`） | 否（每轮重读） |
| 2 | Skill Level 1 摘要（可用技能列表） | 否（并入 system 角色） |
| 3 | Ephemeral 平台说明（channel_prompt） | **否（仅当次）** |
| 4 | Session.messages 历史对话（含 tool 协议映射） | 是（本来就是 Session 的一部分） |

**消息映射规则**（OpenAI function calling 协议）：
- assistant 带工具调用 -> `{"role":"assistant", "content":..., "tool_calls":[...]}`
- 工具结果 -> `{"role":"tool", "tool_call_id":..., "content":...}`

### 3.4 观测事件

| 事件 | 触发时机 | 用途 |
|------|---------|------|
| `turn_start` | 一轮开始 | 前端显示"思考中" |
| `llm_request` | 每次调模型前 | 显示迭代轮次、上下文规模 |
| `llm_response` | 模型返回 | 显示回复预览、tool_calls、token 用量 |
| `tool_call_start` | 工具执行前 | 显示"正在调用 calculator" |
| `tool_call_result` | 工具执行后 | 显示结果、耗时、成败 |
| `turn_end` | 正常结束 | 显示最终文本、总迭代数 |
| `turn_error` | 异常/超限 | 显示错误原因 |

---

## 四、功能边界是什么

**Loop 做的事：**
- observe/think/act 循环 + 工具内循环
- 上下文组装（委托 ContextBuilder）
- 模型调用与重试
- 工具执行调度（委托 ToolRegistry）
- 工具结果回注 Session
- 观测事件发布
- 打断检查

**Loop 不做的事：**
- 不写磁盘（持久化是 Gateway/Store 的事）
- 不认识 Channel / 队列 / 平台字段
- 不构造 InboundEvent / OutboundReply（那是 Gateway）
- 不算 session_key（那是 Gateway）
- 不直接投递回复到平台（那是 Channel）
- 不做平台侧 typing / busy / 鉴权

**边界守则：** Loop 文件里出现 `requests`、`wechat`/`feishu` SDK、`queue.put`、`open(file)` 就是越界。Loop 是"会思考的纯函数"。

---

## 五、如何相连

### 5.1 与 Gateway 的连接

```
Gateway._run_turn_locked:
   session = store.get_or_create(key)
   user_msg = build_message(event)
   result = loop.run_turn(session, user_msg, ephemeral_prompt=event.channel_prompt, session_key=key)
   store.save(session)
```

Gateway 是 Loop 的**唯一调用者**。Loop 只暴露 `run_turn` 一个入口（外加 `request_interrupt`）。

### 5.2 与 Model 的连接

```
loop.__init__(model=...) 或 loop.__init__(model_registry=..., model_name=...)
loop._chat_with_retry -> self.model.chat(messages, tools=schemas)
```

Loop 只面向 `BaseModel.chat` 接口，新增 provider（Claude/GPT）不需要改 Loop。

### 5.3 与 ToolRegistry 的连接

```
loop.__init__(tool_registry=...)
think 阶段: tools = self.tools.get_schemas()  # 随请求发给 LLM
act 阶段:   result = self.tools.dispatch(name, arguments)
```

### 5.4 与 SkillRegistry 的连接

```
loop.__init__(skill_registry=...)
ContextBuilder 构造时传入: skill_registry.summaries_prompt() -> 注入 system
工具调用 use_skill(name) -> skill_registry.load_content(name) -> 回注给 LLM
```

### 5.5 与 ContextBuilder 的连接

每轮 think 都新建一个 ContextBuilder 实例，组装当前 Session 的上下文：

```python
builder = ContextBuilder(session, skill_registry=self.skills, ephemeral_prompt=ephemeral_prompt)
messages = builder.building_context()
```

### 5.6 与 EventBus 的连接

Loop 持有 `self.events`，全程发事件。前端通过订阅同一个 EventBus 实现可视化。

---

## 六、例子

### 6.1 简单对话（无工具）

```python
session = Session()
user_msg = Message(role="user", content_text="你好")
result = loop.run_turn(session, user_msg)
# 内部:
#   observe: session.add_message(user_msg)
#   iteration 1 think: model.chat([{system},{user}]) -> "你好！有什么可以帮你的？"
#   act ②: 纯文本 -> session.add_message(assistant_msg)
#   return TurnResult(final_text="你好！有什么可以帮你的？", completed=True)
```

### 6.2 带工具的多步对话

```python
# 用户: "帮我算 23*17 再加 100"
# 内部:
#   iteration 1:
#     think: model 返回 tool_calls=[{name:"calculator", args:{expr:"23*17"}}]
#     act ①: dispatch -> ToolResult(result_text="391")
#            session.add_message(assistant_msg with tool_calls)
#            session.add_message(tool_msg with result_text="391")
#   iteration 2:
#     think: model 看到 391 -> 返回 tool_calls=[{name:"calculator", args:{expr:"391+100"}}]
#     act ①: dispatch -> ToolResult(result_text="491")
#            回注
#   iteration 3:
#     think: model 看到 491 -> 返回纯文本 "23×17 再加 100 等于 491"
#     act ②: 写入 Session，结束
#   return TurnResult(final_text="23×17 再加 100 等于 491", completed=True)
```

### 6.3 工具失败让 LLM 自我修正

```python
# 模型调 calculator 传了 expr="23*17a"（非法）
# dispatch 抛异常 -> ToolRegistry 兜底成 ToolResult(ok=False, error="表达式非法")
# 回注 tool 消息: "工具执行异常: 表达式非法"
# 下一轮 think: 模型看到错误 -> 换个参数重试或向用户澄清
```

### 6.4 超过最大迭代

```python
# 模型陷入"调工具 -> 再调工具 -> ..."死循环
# iteration 达到 max_iterations=8
# 写兜底文案: "（抱歉，这个问题需要的推理步骤超出了我的上限...）"
# return TurnResult(completed=False, error="max_iterations")
```

---

## 七、往往需要优化哪些问题

| 问题 | 现状 | 优化方向 |
|------|------|---------|
| **上下文无 token 预算** | Session.messages 无限增长，长对话撑爆窗口 | ContextBuilder 加滑动窗口 / 摘要压缩 |
| **无流式输出** | `model.chat` 一次性返回，用户等全部生成完才看到 | model 层支持 stream，Loop 逐 chunk 发事件 |
| **重试策略简陋** | 固定 0.5s 退避，重试 2 次 | 区分错误类型（429/500/网络），指数退避 + 抖动 |
| **max_iterations 兜底粗暴** | 直接返回兜底文案，前面的工具结果浪费 | 可选：把已完成的工具结果总结后再返回 |
| **interrupt 检查粒度粗** | 只在每轮 think 前检查，工具执行中无法打断 | 长工具支持 cooperative cancel |
| **无并行的工具调用** | tool_calls 串行执行 | 独立工具调用可并行（ThreadPool） |
| **上下文无记忆注入** | ContextBuilder 只组装 system+skills+messages | 接入 hot memory 快照 + episodic 检索 |
| **无 trajectory 落盘** | ToolCall 记录只在 Session 内存 | turn 结束后异步写 trajectory，供回放/进化 |
| **ephemeral_prompt 每轮注入** | 每次 think 都重建 ContextBuilder | 若无变化可缓存 system 部分 |
| **观测事件无采样** | 高频发事件可能拖慢 Loop | 事件总线加采样/批量 |

---

## 八、设计原则小结

1. **OTAP 清晰分层**：observe/think/act/persist 各司其职，think 和 act 之间才是内循环。
2. **平台无关**：Loop 永远不知道消息来自飞书还是 CLI，这是可扩展性的根基。
3. **不写磁盘**：Loop 只改内存 Session，持久化交给 Gateway，保持 Loop 可测试、可回放。
4. **容错优先**：模型重试 + 工具失败回注 + max_iterations 兜底，三层防线保证不崩。
5. **事件驱动观测**：全程发事件，前端/日志/进化模块都靠订阅事件，不侵入 Loop 主流程。
6. **签名稳定**：`run_turn(session, user_message, *, ephemeral_prompt, session_key)` 签名不轻易变，后续挂 memory/trajectory 都走内部或事件，不动入口。
