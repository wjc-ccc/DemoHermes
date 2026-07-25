# Gateway - 消息枢纽与调度

> 对应代码：`agent/gateway/gateway.py`、`agent/gateway/message_bus.py`、`agent/gateway/session_key.py`、`agent/gateway/session_store.py`、`agent/gateway/http_api.py`
> 地位：进程内的"中央调度局"。连接所有 Channel 与 Agent Loop，管队列、管会话、管并发。

---

## 一、定义是什么

Gateway 是**进程内消息枢纽**：用一对队列（`in_dto` / `out_dto`）把多个 Channel 的入站消息汇总，按 `session_key` 路由到对应会话，串行跑一轮 Loop，再把结果经出站队列送回对应 Channel。

它自己**不思考、不调模型**，只做"调度 + 路由 + 并发控制 + 会话查找"四件事，是把 Channel 和 Loop 解耦的中间层。

```
多 Channel ──put──> [in_dto] ──> Gateway worker ──> Loop ──> [out_dto] ──> 多 Channel
                     (MessageBus)   (按key加锁串行)            (MessageBus)
```

---

## 二、负责的任务是什么

Gateway 的核心能力可以归为 5 项（对应代码顶部注释）：

1. **Channel 注册与管理**
   - `register_channel(channel)`：注册并回绑（channel 拿到 gateway 引用才能 send_gateway）
   - `get_channel(name)`：出站时按名查 Channel

2. **消息构建**
   - `build_message(event: InboundEvent) -> Message`：把运输件 InboundEvent 转成对话事实 Message（委托 `core/messages.build_user_message`）

3. **MessageBus 输入输出**
   - `submit(event)`：异步入站，不等待
   - `submit_raw(channel_name, raw)`：原始载荷入站（先走 Channel.parse 再 submit）
   - `ask(event, timeout)`：**同步**入站并等回复（CLI/HTTP 用），靠 `correlation_id` 在出站侧唤醒

4. **session_key 构建与上下文寻找**
   - `build_session_key(source)`：全项目唯一的会话键公式
   - `DictSessionStore`：session_key -> Session 的 dict 查找（现在只用 dict，不落盘）

5. **并发控制**
   - 同 session_key **串行**（每 key 一把锁，避免连击/多渠道把同一会话打断）
   - 不同 session_key **并行**（ThreadPoolExecutor）
   - 斜杠命令 `/new` `/reset` `/help` **短路**，不进 Loop

---

## 三、能力清单

### 3.1 生命周期

| 方法 | 作用 |
|------|------|
| `__init__(loop, store, bus, event_bus, agent_id, workers)` | 装配 Loop/Store/Bus/线程池 |
| `start()` | 起两个守护线程：`_inbound_dispatch_loop` + `_outbound_dispatch_loop` |
| `stop()` | 置 `_running=False` -> `bus.close()`（发 sentinel）-> 线程池 shutdown |

### 3.2 入站路径

| 方法 | 模式 | 说明 |
|------|------|------|
| `submit(event)` | 异步 | InboundEvent 直接入队，立刻返回 |
| `submit_raw(name, raw)` | 异步 | 先 Channel.parse 再 submit |
| `ask(event, timeout)` | 同步 | 贴 correlation_id，注册回调，submit 后 `Event.wait()` 阻塞等出站唤醒 |

### 3.3 内部调度（核心）

| 方法 | 作用 |
|------|------|
| `_inbound_dispatch_loop()` | 线程1：while 循环从 in_dto 取事件，丢线程池 |
| `_process_inbound(event)` | 线程池任务：算 key -> 加锁 -> `_run_turn_locked` |
| `_lock_for(key)` | 每 key 一把锁（dict + guard lock 保证并发创建安全） |
| `_run_turn_locked(key, event)` | 斜杠命令短路 / store.get_or_create / loop.run_turn / 包 OutboundReply |
| `_outbound_dispatch_loop()` | 线程2：从 out_dto 取回复 -> 唤醒同步等待者 -> 按 source.channel 路由 |

### 3.4 观测

| 方法 | 作用 |
|------|------|
| `status()` | 运行状态快照（channels/bus/sessions/model/tools/skills），供 `/api/status` |

---

## 四、功能边界是什么

**Gateway 做的事：**
- 队列调度（入站/出站双队列 + 线程池）
- 会话键构建与查找（build_session_key + store.get_or_create）
- 并发控制（同 key 串行锁、异 key 并行池）
- 斜杠命令短路（/new /reset /help）
- InboundEvent -> Message 的转换
- TurnResult -> OutboundReply 的包装
- 同步 ask 的 correlation_id 配对

**Gateway 不做的事：**
- 不调模型、不组装上下文（那是 Loop + ContextBuilder）
- 不解析平台原始载荷（那是 Channel.parse）
- 不投递到平台（那是 Channel.deliver）
- 不写 transcript 落盘（当前 DictSessionStore 只存内存；后续 Store 负责落盘）
- 不执行工具（那是 ToolRegistry.dispatch，由 Loop 调用）

**边界守则：** Gateway 文件里出现 `model.chat`、`tool.execute`、平台 SDK 就是越界。Gateway 是"调度局"，不是"思考者"也不是"邮差"。

---

## 五、如何相连

### 5.1 与 Channel 的连接

```
入站: Channel.send_gateway(event) -> Gateway.submit(event) -> MessageBus.in_dto
出站: MessageBus.out_dto -> Gateway._outbound_dispatch_loop
        -> self._channels[reply.source.channel].deliver_to_OutboundEvent(reply)
```

Gateway 持有 `self._channels: dict[name, BaseChannel]`，双向：Channel 也持有 `self._gateway`。

### 5.2 与 Agent Loop 的连接

```
_run_turn_locked:
   session = store.get_or_create(key, ...)
   user_msg = build_message(event)
   result = loop.run_turn(session, user_msg, ephemeral_prompt=..., session_key=key)
   store.save(session)
```

Gateway 持有 `self.loop: AgentLoop`，只调它的 `run_turn`，不碰 Loop 内部状态。

### 5.3 与 MessageBus 的连接

Gateway 是 MessageBus 的**唯一消费者**（入站侧）和**唯一生产者**（出站侧）。Channel 只 put 入站，不 put 出站。

### 5.4 与 SessionStore 的连接

Gateway 持有 `self.store: DictSessionStore`，通过 `get_or_create` / `save` / `reset` 管理 Session。当前是纯 dict，后续可换 JSONL/SQLite/Redis，接口不变。

### 5.5 与 EventBus 的连接

Gateway 持有 `self.events: EventBus`，出站投递时发 `outbound_reply` 事件供前端观测。Loop 内部的事件（turn_start/llm_request/tool_call 等）也走同一个 bus。

---

## 六、例子

### 6.1 同步 ask 的完整流程（CLI 场景）

```python
# CLI 输入一句话，要立刻看到回复
event = cli_channel.parse_to_InboundEvent("你好")
reply = gateway.ask(event, timeout=120)
print(reply.text)
```

内部时序：

```
ask 线程:  生成 corr=corr-123 -> 注册 _reply_waiters[corr]=[_cb] -> submit(event)
                                                            │
                                                            ▼
in_dto 队列 -> _inbound_dispatch_loop 取出 -> 线程池 _process_inbound
              -> 算 key -> 加锁 -> _run_turn_locked
              -> store.get_or_create -> loop.run_turn -> store.save
              -> 包 OutboundReply(metadata.correlation_id=corr)
              -> publish_outbound(reply)
                                                            │
                                                            ▼
out_dto 队列 -> _outbound_dispatch_loop 取出
              -> 查 corr -> 调 _cb(reply) -> done.set()
              -> 按 source.channel 找 Channel.deliver
                                                            │
ask 线程:  done.wait() 被唤醒 -> 返回 box[0]（即 reply）     │
                                                            ▼
                                          (CLI 的 deliver 会 print，但 ask 已拿到 reply)
```

### 6.2 斜杠命令短路

```python
event = parse("/new")
# _run_turn_locked 里:
if text in {"/new", "/reset"}:
    session = store.reset(key, ...)   # 直接重置，不进 Loop
    return OutboundReply(text=f"已新开会话 session_id={session.session_id}")
```

### 6.3 并发隔离

```
时刻 T0: 飞书群消息进来 -> key="agent:main:feishu:group:oc_x:user:ou_y"
时刻 T1: 同群同用户连发第二条 -> 同 key -> 等锁（串行）
时刻 T2: CLI 消息进来 -> key="agent:main:cli:dm:local" -> 不同 key -> 并行跑
```

---

## 七、往往需要优化哪些问题

| 问题 | 现状 | 优化方向 |
|------|------|---------|
| **session 只存内存** | `DictSessionStore` 重启即丢 | 换 JSONL/SQLite，接口已抽象好 |
| **锁字典无限增长** | `_session_locks` 只增不减 | 加 LRU 淘汰或定期清理空闲 key 的锁 |
| **线程池无背压** | 入站洪峰时队列堆积无上限 | MessageBus 加 maxsize + 拒绝策略 |
| **ask 超时无清理** | 超时后回调已 pop，但 Loop 还在跑 | 加可取消的 interrupt 传播 |
| **斜杠命令硬编码** | `/new /reset /help` 写死在 if 里 | 抽成 CommandHandler 表，可扩展 |
| **无优雅停机** | stop 时 in_dto 里的事件可能丢 | 先 drain 入站队列再 shutdown |
| **无死信处理** | 出站 deliver 反复失败只 log | 加重试 + 死信队列 |
| **status 是快照** | 无法看历史趋势 | 接 metrics（Prometheus/日志聚合） |
| **单进程瓶颈** | 线程池 workers=2 | 高并发时换进程池或异步框架；MessageBus 换 Redis List 可分布式 |

---

## 八、设计原则小结

1. **薄编排定位**：Gateway 只调度不思考，逻辑越薄越稳。
2. **双队列解耦**：入站/出站用队列隔开，Channel 和 Loop 互不阻塞。
3. **session_key 是唯一路由键**：会话隔离、并发控制全靠它，全项目唯一公式。
4. **串行保序、并行提速**：同 key 串行防乱序，异 key 并行提吞吐。
5. **Store 接口抽象**：当前 dict，后续换存储不动 Gateway 代码。
6. **同步 ask 是适配层**：不破坏异步链路，用 correlation_id 在出站侧"截胡"。
