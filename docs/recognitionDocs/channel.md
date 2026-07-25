# Channel - 平台入口与出口

> 对应代码：`agent/channel/baseChannel.py`、`agent/channel/channel_cli.py`、`agent/channel/channel_frontier.py`、`agent/channel/channel_wechat.py`
> 地位：Agent 与外部世界的**唯一接触面**。所有平台差异在这里归一化，进来后全是统一 DTO。

---

## 一、定义是什么

Channel 是**平台适配器**：把不同平台（CLI、飞书、微信、HTTP 前端……）的原始消息格式，翻译成 Agent 内部统一的 `InboundEvent`；反过来把 `OutboundReply` 翻译回各平台能理解的格式发出去。

```
平台原始载荷 ──parse_to_InboundEvent──> InboundEvent（统一入站）
                                          │
                                     (送进 Gateway)
                                          │
OutboundReply（统一出站）<──deliver_to_OutboundEvent── 平台回写
```

每个平台一个 Channel 子类，继承 `BaseChannel`，实现两个抽象方法即可。

---

## 二、负责的任务是什么

1. **入站解析**：`parse_to_InboundEvent(raw) -> InboundEvent`
   - 把平台 SDK/webhook/input() 的原始数据，映射成统一 `InboundEvent`
   - 构造 `SessionSource`（channel 名、chat_id、user_id、chat_type 等）
   - 可选注入 `channel_prompt`（"你在飞书群里，回答简短"），仅当次生效

2. **出站投递**：`deliver_to_OutboundEvent(reply) -> None`
   - 拿到统一 `OutboundReply`，按平台方式发回去（print / 调 API / 推 webhook）

3. **生命周期管理**：`start()` / `stop()`
   - 读配置、连平台（飞书需建 websocket 长连、微信需挂 webhook 服务）
   - 无平台可连的（CLI）置 `start=True` 即可

4. **平台侧杂务**：busy 队列、typing 状态、鉴权、防重放、素材下载
   - 这些**全部留在 Channel 内**，绝不外泄到 Gateway/Loop

5. **状态上报**：维护 `status` dict（message_count、error_count 等）供 `/api/status` 观测

---

## 三、能力清单

### 3.1 BaseChannel 提供的通用能力

| 能力 | 方法 | 说明 |
|------|------|------|
| 绑定 Gateway | `bind_gateway(gateway)` | register_channel 时回调，绑定后才能 send_gateway |
| 发送事件 | `send_gateway(event)` | 把 InboundEvent 塞进 Gateway 的入站队列，自动累加计数 |
| 状态查询 | `get_status()` | 返回 message_count / inbound_event_count / error_count |
| 配置加载 | `__init__` 读 `CHANNEL_CONFIG` | 从 JSON 配置文件读各渠道密钥/开关 |

### 3.2 子类必须实现的抽象方法

| 方法 | 职责 |
|------|------|
| `start()` | 启动渠道 |
| `stop()` | 停止渠道 |
| `parse_to_InboundEvent(raw)` | 平台原始数据 -> InboundEvent |
| `deliver_to_OutboundEvent(reply)` | OutboundReply -> 发回平台 |

### 3.3 已实现的 Channel

| Channel | 文件 | 状态 | 特点 |
|---------|------|------|------|
| `CliChannel` | `channel_cli.py` | ✅ 完整 | input()/print，chat_id="local" |
| `FrontierChannel` | `channel_frontier.py` | ✅ HTTP | 供前端 HTTP API 调用，走 Gateway.ask 同步等回复 |
| `WechatChannel` | `channel_wechat.py` | 🔲 骨架 | 预留 webhook 接入点 |
| `FeishuChannel` | `channel_feishu.py` | 🔲 待实现 | 需飞书 SDK 长连 |

---

## 四、功能边界是什么

**Channel 做的事：**
- 平台字段解析（微信 content -> text、飞书 event -> text）
- 平台投递（调微信发消息 API、飞书回复消息）
- 平台特有的鉴权、加密、去重、媒体下载
- 平台侧的 typing、busy、限流

**Channel 不做的事：**
- 不碰 Session / Loop / 模型（拿到事件就 `send_gateway` 甩手）
- 不构造 Message（那是 Gateway 的 `build_message` 干的）
- 不决定会话隔离策略（session_key 由 Gateway 算）
- 不做业务逻辑（不判断该不该回复、回复什么内容）

**边界守则：** Channel 文件里出现 `model.chat`、`Session.add_message`、`build_session_key` 就是越界。Channel 只是个"翻译官 + 邮差"。

---

## 五、如何相连

### 5.1 与 Gateway 的连接

```
Gateway.register_channel(channel)
   └─> channel.bind_gateway(self)   # 回绑，channel 拿到 gateway 引用
        └─> 之后 channel.send_gateway(event)
              └─> gateway.submit(event)
                    └─> MessageBus.in_dto
```

注册时双向绑定：Gateway 知道有哪些 channel（按 name 索引），Channel 知道往哪送（`self._gateway`）。

### 5.2 出站路由

Gateway 出站分发时，靠 `reply.source.channel` 字段反查 Channel：

```python
ch = self._channels.get(reply.source.channel)  # source.channel = "cli"
ch.deliver_to_OutboundEvent(reply)
```

所以 `OutboundReply.source` 必须保留入站时的 `SessionSource.channel`，这是回程车票。

### 5.3 与 types 的连接

Channel 只产/消两个 DTO：`InboundEvent`（产）和 `OutboundReply`（消）。`SessionSource` 是它们共用的路由键载体。

---

## 六、例子

### 6.1 CliChannel 解析一条输入

```python
# 用户在终端输入: "你好"
raw = "你好"
event = cli_channel.parse_to_InboundEvent(raw)
# =>
# InboundEvent(
#     text="你好",
#     source=SessionSource(
#         channel="cli",
#         chat_id="local",
#         chat_type="dm",
#         user_id="local",
#     ),
#     channel_prompt="当前通道：本地终端 CLI。回答简洁即可。",
# )
cli_channel.send_gateway(event)  # 进 MessageBus
```

### 6.2 出站投递

```python
# Gateway 处理完，回灌 OutboundReply
reply = OutboundReply(
    text="你好！有什么可以帮你的？",
    source=SessionSource(channel="cli", chat_id="local", ...),
)
cli_channel.deliver_to_OutboundEvent(reply)
# => 终端打印: "ai > 你好！有什么可以帮你的？"
```

### 6.3 飞书群消息的设想

```python
# 飞书 webhook 推来一条群消息
raw = {
    "event": {"message": {"chat_id": "oc_xxx", "content": '{"text":"@bot 报价"}'},
              "sender": {"sender_id": {"open_id": "ou_yyy"}}},
}
event = feishu_channel.parse_to_InboundEvent(raw)
# =>
# InboundEvent(
#     text="报价",
#     source=SessionSource(
#         channel="feishu",
#         chat_id="oc_xxx",
#         chat_type="group",
#         user_id="ou_yyy",
#     ),
#     channel_prompt="当前通道：飞书群聊。回答简洁，必要时 @ 提问者。",
# )
```

注意：`@bot` 前缀、飞书 content JSON 转义这些**平台杂务**都在 Channel 内消化掉，外面只看到干净的 `text="报价"`。

---

## 七、往往需要优化哪些问题

| 问题 | 现状 | 优化方向 |
|------|------|---------|
| **平台字段硬编码** | 解析逻辑写死在 parse 函数里 | 抽成 `FieldMapper` 配置，新平台改配置不改代码 |
| **无防重放** | 同一条 webhook 重复推送会重复处理 | 加 `message_id` 去重缓存（Channel 内做） |
| **无 typing 状态** | 长任务时用户不知道 Agent 在干活 | Channel 内启动 typing 心跳，Loop 完成后停 |
| **媒体下载阻塞** | 图片/语音同步下载会卡住入站 | 异步下载，先发文本事件，媒体就绪后补发 |
| **错误未上报** | deliver 失败只 log，平台侧无重试 | 加 Channel 内重试 + 指数退避，失败进死信队列 |
| **配置热更新** | 密钥变更需重启 | 支持 reload_config()，运行期切换 |
| **多账号/多实例** | 一个 Channel 只能连一个账号 | Channel 工厂化，按 account_id 多实例 |
| **骨架未实现** | wechat/feishu 只是空壳 | 接入真实 SDK，但**保持 parse/deliver 签名不变** |

---

## 八、设计原则小结

1. **翻译官定位**：Channel 只翻译不思考，平台差异在这里消化干净。
2. **双向最小契约**：只实现 4 个抽象方法（start/stop/parse/deliver），其余能力按需加。
3. **平台杂务不外泄**：鉴权/typing/去重/限流全在 Channel 内，Gateway 和 Loop 永远感知不到。
4. **新增平台零侵入**：加一个 Channel 子类，Loop/Gateway/types 一行不改。
