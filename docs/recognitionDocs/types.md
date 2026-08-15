# Types - 核心数据契约

> 对应代码：`agent/core/types.py`
> 地位：全项目的"普通话"。所有模块跨层通信都必须说这套类型，否则就要序列化转换。

---

## 一、定义是什么

types 是一组 **pydantic 数据模型**，定义了 Agent 各模块之间传递数据时"长什么样"。它们本身**不含任何业务逻辑**，只规定字段、类型、默认值和约束。

可以把它理解成一份**接口契约文档**：Channel 产出的入站事件必须是 `InboundEvent`，Loop 消费的消息必须是 `Message`，谁不遵守契约谁就要自己适配。

---

## 二、负责的任务是什么

1. **统一数据形状**：跨模块通信有了共同语言，避免"Channel 吐 dict、Loop 吃 dataclass"这种错配。
2. **类型安全**：pydantic 在构造时校验字段类型，构造错误的数据当场报错，不留到运行时。
3. **序列化/反序列化**：pydantic 自带 `model_dump_json()` / `model_validate_json()`，session 落盘、日志记录都靠它。
4. **字段语义自文档化**：每个 `Field(description=...)` 就是字段说明，读模型定义等于读数据字典。

---

## 三、能力清单（按类型分组）

### 3.1 消息类

| 类型 | 用途 | 关键字段 |
|------|------|---------|
| `ContentPart` | 多模态内容的一个片段 | type(text/image/audio/...), text, url, path |
| `Message` | 单条对话消息 | message_id, session_id, role(user/assistant/system/tool), content, content_text, tool_call_ids, metadata |
| `ToolCall` | 一次工具调用的完整记录 | tool_call_id, tool_name, arguments, status, durations_ms, result_text, error |

### 3.2 会话类

| 类型 | 用途 | 关键字段 |
|------|------|---------|
| `Session` | 一次会话的完整状态 | session_id, session_key, author_id, channel, messages[], tool_calls[], summary, created_at |

### 3.3 通道传输类

| 类型 | 用途 | 关键字段 |
|------|------|---------|
| `SessionSource` | 消息从哪来、回哪去 | channel, chat_id, chat_type(dm/group/thread), user_id, thread_id, reply_to_message_id |
| `InboundEvent` | Channel -> Gateway 入站 | text, source, channel_prompt(ephemeral), metadata |
| `OutboundReply` | Gateway -> Channel 出站 | text, reply_to, source, metadata |
| `TurnResult` | Loop -> Gateway 一轮结果 | final_text, session_id, completed, interrupted, error |

---

## 四、功能边界是什么

**types 做的事：**
- 定义数据结构和字段约束
- 提供基本的行为方法（如 `Session.add_message` 维护 seq_id/计数/时间戳）
- 通过 pydantic 保证构造时类型正确

**types 不做的事：**
- 不调模型、不调工具、不碰队列
- 不做任何 I/O（文件/网络/数据库）
- 不含平台特定字段（没有 `feishu_msg_type`、没有 `wechat_content`）
- 不做业务决策（`Session.add_message` 只追加，不判断该不该加）

**边界守则：** 如果一个数据模型里开始出现 `requests.post`、`queue.put`、`open(...)`，就是越界了。

---

## 五、如何相连

types 是"被所有人依赖"的最底层，它不依赖任何业务模块：

```
        types（地基，不依赖任何业务层）
          ▲
          │ 被引用
   ┌──────┴──────┬─────────┬──────────┐
   │             │         │          │
Channel       Gateway    AgentLoop   tool/skill
(产 InboundEvent)  (产 Session)  (产 Message)  (产 ToolCall)
```

关键流转：

```
Channel 产出:  InboundEvent ──┐
                              ├──> Gateway 消费
Gateway 产出:  Message ───────┤    (build_message 转换)
                              ├──> AgentLoop 消费
AgentLoop 产出: TurnResult ───┘    (改 Session 内部状态)
                              │
Gateway 包装:  OutboundReply ─┴──> Channel 消费
```

**两个核心转换点：**
1. `Gateway.build_message(InboundEvent) -> Message`：运输件转成对话事实
2. `Gateway` 把 `TurnResult.final_text` 包成 `OutboundReply`：思考结果转成运输件

---

## 六、例子

### 6.1 一条用户消息的完整结构

```python
Message(
    message_id="a1b2c3...",          # 自动生成 UUID
    session_id="sess-xxx",
    role="user",
    author_id="user_ou_yyy",
    seq_id=3,                         # 该 session 第 4 条（0 基）
    content=[ContentPart(type="text", text="你好")],
    content_text="你好",
    created_at=1721388800.5,
    tool_call_ids=[],                 # 用户消息没有工具调用
    metadata={},
)
```

### 6.2 SessionSource 决定会话隔离

```python
# 同一个用户在 CLI 和飞书是两个 session_key
SessionSource(channel="cli",    chat_id="local",     user_id="ou_yyy")
# key = agent:main:cli:dm:local

SessionSource(channel="feishu", chat_id="oc_xxx",    user_id="ou_yyy", chat_type="group")
# key = agent:main:feishu:group:oc_xxx:user:ou_yyy
```

### 6.3 ToolCall 记录完整生命周期

```python
ToolCall(
    tool_call_id="tc-123",
    tool_name="calculator",
    arguments={"expr": "23*17"},
    status="success",                 # pending -> running -> success/error
    durations_ms=12,
    result_text="391",
    tool_call_result={"value": 391},
)
```

---

## 七、往往需要优化哪些问题

| 问题 | 现状 | 优化方向 |
|------|------|---------|
| **字段冗余** | `ToolCall.arguments_raw` 标注"调试用后续删除" | 上线前清理，避免长期维护无用字段 |
| **多模态未落地** | `ContentPart` 定义了 image/audio/video，但 Loop 只用 content_text | Channel 层补全多模态解析，ContextBuilder 映射多模态格式 |
| **Message.metadata 滥用风险** | tool_calls 存在 metadata["tool_calls"] 里，类型不安全 | 抽成独立字段或专门模型，避免 dict 套 dict 难维护 |
| **Session 无 token 预算** | messages 无限增长，长对话会撑爆上下文窗口 | 在 ContextBuilder 或 Session 层加滑动窗口/摘要压缩 |
| **序列化兼容** | 代码里同时处理 pydantic v1/v2（`model_validate_json` vs `parse_raw`） | 项目固定 pydantic 版本后清理兼容代码 |
| **Session 缺少状态机** | status 只有 active/paused/completed 但无迁移逻辑 | 补 idle/daily reset 的状态迁移，或先明确不用就移除 |

---

## 八、设计原则小结

1. **契约先行**：新功能先在 types 里定义数据结构，再写逻辑。
2. **平台无关**：types 里永远不出现平台专有字段，平台差异留在 Channel 内。
3. **pydantic 用足**：字段加 `description`、用 `Literal` 限定枚举、用 `Field(default_factory=...)` 处理可变默认值。
4. **不背逻辑**：types 只做数据搬运和小范围自洽（如 add_message 维护计数），不做业务决策。
