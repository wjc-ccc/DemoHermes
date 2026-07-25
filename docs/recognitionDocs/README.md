# DemoCursor Agent 架构认知文档

> 目标：用统一视角讲清 types / channel / gateway / agent loop / tool&skill&plugin 这五块**各自是什么、负责什么、边界在哪、如何相连**，让你拿到一个需求时能快速判断"这事该归谁管"。
>
> 阅读顺序建议：本 README（鸟瞰）→ types（地基）→ channel（入口）→ gateway（枢纽）→ agent loop（大脑）→ tool&skill&plugin（手脚）。

---

## 一、一张图看懂整体架构

```
外部世界（CLI / 飞书 / 微信 / HTTP 前端）
        │  平台原始消息（str / webhook / JSON）
        ▼
┌──────────────────── Channel ────────────────────┐
│  parse_to_InboundEvent(raw)  平台字段 → 统一 DTO  │
│  deliver_to_OutboundEvent()  统一回复 → 平台      │
│  busy/typing/鉴权 都留在这里，不外泄              │
└─────────────────────┬───────────────────────────┘
                      │ InboundEvent（统一入站）
                      ▼
┌──────────────── Gateway（枢纽 / 薄编排）─────────┐
│  MessageBus: [in_dto] → worker → [out_dto]      │
│  build_session_key(source)  唯一会话键           │
│  DictSessionStore: session_key → Session         │
│  同 key 串行（每 key 一锁）/ 异 key 并行（线程池）│
│  斜杠命令 /new /reset /help 短路，不进 Loop       │
└─────────────────────┬───────────────────────────┘
                      │ Session + Message
                      ▼
┌──────────────── Agent Loop（大脑 / OTAP）────────┐
│  observe → think ⇄ act(工具内循环) → 返回        │
│  ContextBuilder 组 messages → model.chat         │
│  解析 tool_calls → ToolRegistry 执行 → 回注 → 再 think │
│  容错：模型重试 / 工具失败回注 / max_iterations 兜底 │
└─────────────────────┬───────────────────────────┘
                      │ TurnResult
                      ▼
              Gateway → Channel → 用户
```

一句话定位：**Channel 管 I/O 归一化，Gateway 管调度与会话，Loop 管思考与行动，types 是它们共同的契约语言，tool/skill/plugin 是 Loop 的手脚和经验包。**

---

## 二、模块速查表

| 模块 | 一句话定义 | 核心职责 | 绝不做的事 |
|------|------------|----------|-----------|
| **types** | 全局数据契约（pydantic 模型） | 定义 Message/Session/InboundEvent 等结构 | 不含任何业务逻辑 |
| **channel** | 平台适配器 | 平台载荷 ⇄ 统一 DTO 的转换与投递 | 不碰 Session/Loop/模型 |
| **gateway** | 进程内消息枢纽 | 双队列调度 + session_key 路由 + 并发控制 | 不思考、不调模型 |
| **agent loop** | 对话大脑 | observe→think→act 循环 + 工具内循环 + 容错 | 不认识平台 SDK、不写磁盘 |
| **tool** | 原子能力（函数级） | 执行确定性操作（计算/查库/调 API） | 不做复杂多步编排 |
| **skill** | 经验包（Markdown 指南） | 注入 prompt 引导 LLM 按特定流程做事 | 不直接执行代码 |
| **plugin** | 外部插拔式扩展位 | 预留的 tool/skill/memory 动态挂载点 | 当前阶段未实现 |

---

## 三、数据流：一条消息的完整生命周期

以"用户在 CLI 输入：帮我算 23*17"为例：

```
1. 用户输入 "帮我算 23*17"
   └─ CliChannel.parse_to_InboundEvent(raw)
      → InboundEvent(text="帮我算 23*17", source=SessionSource(channel="cli",...))

2. Channel.send_gateway(event) → Gateway.submit(event) → MessageBus.in_dto

3. Gateway._inbound_dispatch_loop 线程取到 event
   └─ 提交线程池 → _process_inbound(event)
      ├─ key = build_session_key(source) = "agent:main:cli:dm:local"
      ├─ lock = _lock_for(key)  ← 同 key 串行
      └─ _run_turn_locked(key, event)
         ├─ session = store.get_or_create(key)  ← dict 里找/建
         ├─ user_msg = build_message(event)     ← InboundEvent → Message
         └─ loop.run_turn(session, user_msg, ...)

4. AgentLoop.run_turn  ← 这是"大脑"真正工作的地方
   ├─ observe: session.add_message(user_msg)
   ├─ iteration 1 (think):
   │    ContextBuilder.building_context() → [system, skill摘要, 平台说明, ...历史消息]
   │    model.chat(messages, tools=[calculator_schema])
   │    → response.tool_calls = [ToolRequest(name="calculator", args={expr:"23*17"})]
   ├─ act: ToolRegistry.dispatch("calculator", {expr:"23*17"})
   │    → ToolResult(ok=True, result_text="391")
   │    session.add_message(tool_result_msg)
   ├─ iteration 2 (think): 模型看到 391 → 返回纯文本 "23×17=391"
   └─ return TurnResult(final_text="23×17=391", completed=True)

5. Gateway 拿到 TurnResult → 包成 OutboundReply → MessageBus.out_dto

6. Gateway._outbound_dispatch_loop 线程取到 reply
   └─ 按 reply.source.channel="cli" 找到 CliChannel
      → ch.deliver_to_OutboundEvent(reply) → print("ai > 23×17=391")

7. 用户看到回复 ✅
```

关键观察：**CLI 和飞书走的是同一条路径**，差别只在第 1 步和第 6 步的 Channel 实现不同。Loop 完全不知道消息来自哪个平台。

---

## 四、如何判断"这个需求归谁管"

| 需求特征 | 归属模块 | 例子 |
|----------|----------|------|
| 涉及特定平台 SDK / webhook / 消息格式 | Channel | 飞书消息解密、微信图文上传 |
| 涉及会话隔离、并发、命令、路由 | Gateway | /new 命令、同会话防重入、多渠道调度 |
| 涉及思考、多步推理、调模型、调工具 | Agent Loop | 工具内循环、上下文组装、重试 |
| 确定性的原子操作（输入→输出固定） | Tool | 计算器、查天气、查数据库 |
| 引导 LLM 按某流程做事的经验文本 | Skill | 周报生成指南、代码评审 checklist |
| 新增数据结构 / 字段 | types | 加 MemoryItem、扩展 ContentPart |
| 跨会话/跨用户的知识沉淀 | memory（后续） | USER.md、episodic 检索 |

**铁律：Loop 文件里不应出现任何平台 SDK import；Channel 文件里不应出现 model.chat。** 这条边界一旦守住，扩展平台和换模型都不会互相牵连。

---

## 五、各模块详细文档

- [types.md](./types.md) — 数据契约与地基
- [channel.md](./channel.md) — 平台入口与出口
- [gateway.md](./gateway.md) — 消息枢纽与调度
- [agent loop.md](./agent%20loop.md) — 对话大脑与 OTAP
- [tool&skill&plugin.md](./tool&skill&plugin.md) — 能力扩展体系

---

## 六、当前实现状态与后续演进

| 模块 | 当前状态 | 下一步演进 |
|------|---------|-----------|
| types | ✅ 完整（含 tool 协议字段） | 多模态 ContentPart 落地 |
| channel | ✅ CLI / frontier / wechat 骨架 | 飞书/微信真实 SDK 接入 |
| gateway | ✅ 双队列 + dict store + 并发锁 | JSONL/SQLite store、Redis 分布式锁 |
| agent loop | ✅ OTAP + 工具内循环 + 容错 | memory 注入、trajectory 回放 |
| tool | ✅ 抽象基类 + registry + calculator 示例 | bash/file/memory 工具完善 |
| skill | ✅ SKILL.md 解析 + 两级加载 | 运行期 skill_creator 自建 |
| plugin | 🔲 预留位 | 动态加载外部 tool/skill |
| memory | 🔲 接口预留 | L1/L2/L3 三层记忆 |
| evolution | 🔲 接口预留 | reviewer/optimizer 旁路 |
