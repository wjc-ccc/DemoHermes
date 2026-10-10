# Channel - 平台入口与出口

> 当前架构中的“感知”入口；跨模块位置见 [模块架构总览](../模块架构总览.md)。本页保留渠道协议与媒体处理细节。

> 对应代码：`agent/channel/` 下的 `baseChannel.py`（共用骨架）、`registry.py`（自动启用）、`media.py`（附件落盘）、`dedup.py`，以及 `channel_cli.py`、`channel_frontier.py`、`channel_feishu.py`、`channel_qq.py`、`channel_wechat.py`
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

每个平台一个 Channel 子类，继承 `BaseChannel`，实现 `start`、`stop`、`parse_to_InboundEvent`、`deliver_to_OutboundEvent` 四个抽象方法。

---

## 二、负责的任务是什么

1. **入站解析**：`parse_to_InboundEvent(raw) -> InboundEvent`
   - 把平台 SDK/webhook/input() 的原始数据，映射成统一 `InboundEvent`
   - 构造 `SessionSource`（channel 名、chat_id、user_id、chat_type 等）
   - 可选注入 `channel_prompt`（"你在飞书群里，回答简短"），仅当次生效

2. **出站投递**：`deliver_to_OutboundEvent(reply) -> None`
   - 拿到统一 `OutboundReply`，按平台方式发回去（print / 调 API / 推 webhook）

3. **生命周期管理**：`start()` / `stop()`
   - 使用注册表注入的配置连接平台（飞书和 QQ 建 websocket 长连，微信 iLink 使用长轮询）
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
| 入站骨架 | `ingest(raw)` | 平台回调线程里计数 `message_count`、`should_accept` 去重/准入；带附件（`_has_media`）的再进线程池解析 |
| 发送事件 | `send_gateway(event)` | 把 InboundEvent 塞进 Gateway 的入站队列，累加 `inbound_event_count` |
| 附件兜底 | `_collect_attachments(text, items, load)` | 逐项加载附件；失败或不支持的说明写进正文 |
| 生命周期 | `_start_worker` / `stop()` | 起收包线程；stop 统一置停止信号、关线程池与 HTTP 客户端，子类只覆写 `_close_connection` |
| HTTP | `_http_client()` | 每个渠道复用一个 httpx.Client，附件下载也走它 |
| 状态 | `status` | start / connected / message_count（平台推来的）/ inbound_event_count（送进 gateway 的）/ outbound_event_count / error_count |
| 配置加载 | `registry.autostart_channels` | 一次读取 JSON 非敏感项与 `.env` 凭据，再注入 Channel |

### 3.2 子类必须实现的抽象方法

| 方法 | 职责 |
|------|------|
| `start()` | 启动渠道 |
| `parse_to_InboundEvent(raw)` | 平台原始数据 -> InboundEvent |
| `deliver_to_OutboundEvent(reply)` | OutboundReply -> 发回平台 |

### 3.3 已实现的 Channel

| Channel | 文件 | 状态 | 特点 |
|---------|------|------|------|
| `CliChannel` | `channel_cli.py` | ✅ 完整 | input() 入站，回复由 ask() 返回后调用方打印；chat_id="local" |
| `FrontierChannel` | `channel_frontier.py` | ✅ HTTP | 供前端 HTTP API 调用，走 Gateway.ask 同步等回复 |
| `FeishuChannel` | `channel_feishu.py` | ✅ 完整 | lark-oapi ws 长连（SDK 自动重连）+ REST 投递；群聊 require_mention + TTL 去重 |
| `QQChannel` | `channel_qq.py` | ✅ 单聊与群 @ | 官方 bot wss 网关（Hello→Identify→心跳→断线退避重连）+ REST 投递；频道消息未实现，也不订阅 |
| `WechatChannel` | `channel_wechat.py` | ✅ 文字回复、图片/文件入站 | 腾讯 iLink Bot API 长轮询 + sendmessage；每个会话最新的 context_token 存在 channel 内并持久化，出站时带回 |

### 3.4 配置即连（Hermes 式自动启用）

平台 channel 不需要写代码接线：`agent/main.py` 先启动 Gateway，再调
`autostart_channels(gateway)`（`channel/registry.py`），按以下规则自动启用：

1. `agent/channel/registry.json`（入库）该渠道节 `"enable": false` → 永不启动
2. `.env` 凭据填齐且未显式关闭 → 自动 register + start
3. `"enable": true` 但凭据缺失 → 启动时 warn 跳过；单 channel 启动失败后注销，不拖垮其他

凭据分工：密钥只放 `.env`（FEISHU_APP_ID/FEISHU_APP_SECRET、QQ_APP_ID/QQ_CLIENT_SECRET、
WEIXIN_TOKEN；WEIXIN_ACCOUNT_ID 可选，协议不使用，仅作日志标识），registry.json 只放开关与非敏感配置（domain、require_mention 等）。
新平台接入：写一个 BaseChannel 子类，在类属性里声明 `name`、`label`、`env_map`（必需凭据字段 → env 变量名）、`optional_env_map`、`deps`、`install_hint`，再把类加进 `registry.PLATFORM_CHANNELS` 即可。registry 只读这些属性，`start()` 用 `_require_credentials()` 按同一个 `env_map` 校验，名字和凭据清单只写一处。

启动前可复制 `.env.example` 为 `.env` 并填写对应值；飞书需 App ID/Secret，QQ 需 App ID/Client Secret。个人微信 iLink Bot 可运行 `python scripts/wechat_qr_login.py`：在微信扫码并确认后，工具把 Token、Account ID 和区域接口地址写入本地 `.env`。二维码保存在 `data/channel/wechat_login_qr.png`，登录确认后删除。`/api/status` 同时显示 `start`（连接线程已启动）和 `connected`（已观测到平台通信）。飞书与 QQ 验证私聊和群聊；iLink Bot 通常只验证私聊，普通微信群事件受平台能力限制。

入站 `InboundEvent.content` 使用统一的 `ContentPart`：图片和文件保存于 `data/channel/media/`，上限 20 MiB；文件正文不解析。官方 Anthropic 端点默认启用视觉；其他 Anthropic 兼容端点确认支持图片后可在 `.env` 设置 `MODEL_SUPPORTS_IMAGES=true`。文字模型只收到附件名称等说明。附件下载失败会进入事件文本，供 Agent 明确回复。出站统一为文字，媒体出站、语音和视频留待后续版本。

媒体字段与命名：每个附件是一个 `ContentPart(type, path, file_name, mime_type, size_bytes)`；`path` 是本地绝对路径，落盘名为随机 UUID 加净化后的文件名，重名也不会覆盖。`file_name` 优先取平台原名，去掉路径、危险字符并截断到 100 字符；微信无文件名的图片按消息内顺序命名 `image-1.jpg`、`image-2.jpg`，QQ 无文件名附件按顺序命名 `attachment-1.<扩展名>`。微信 `item_list` 和 QQ `attachments` 中的多项按顺序进入同一个事件；飞书当前一个 `image/file` 事件只处理一个资源，连续发送的图片成为多个事件。项目未设每条消息的固定图片张数上限，但每个附件最多 20 MiB；平台和模型本身的请求体、图片数量、发送频率限制另算。

模型输入：`Message.content` 持久化附件路径和元数据，`Message.content_text` 放入可读的附件说明。视觉模型请求时，图片才从本地读取并转为 base64 图像块；文件仍仅提供元数据。当前火山 Ark 的 `deepseek-v4-flash` 配置是文字模型，单独设置 `MODEL_SUPPORTS_IMAGES=true` 不能让它具备视觉能力。切到确实支持图像的模型（例如 Ark 的 `deepseek-v4.1-flash`）后，再设置 `MODEL_SUPPORTS_IMAGES=true` 并重启后端。该开关只控制是否发送图片，实际 API 套餐与模型能力仍以提供方为准。

---

## 四、功能边界是什么

**Channel 做的事：**
- 平台字段解析（微信 content -> text、飞书 event -> text）
- 平台投递（调微信发消息 API、飞书回复消息）
- 平台特有的鉴权、加密、去重、媒体下载
- 平台侧的 typing、busy、限流

**Channel 不做的事：**
- 不碰 Session / Loop / 模型（拿到事件就 `send_gateway` 甩手）
- 不构造 Message（那是 Gateway 调 `build_user_message` 干的）
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

### 6.1 CLI：同步一问一答

```python
# agent/main.py 终端模式：输入经 parse 变成事件，ask() 同步等回复，由调用方打印
event = cli_channel.parse_to_InboundEvent("你好")
# => InboundEvent(
#        text="你好",
#        source=SessionSource(channel="cli", chat_id="local", chat_type="dm", user_id="local"),
#        channel_prompt="当前通道：本地终端 CLI。回答简洁即可。",
#    )
reply = gateway.ask(event)
print(f"ai  > {reply.text}")
```

CLI 与 Frontier 都走 `ask()` 同步链路，回复直接返回给调用方；出站分发仍会调用它们的 `deliver_to_OutboundEvent`，CLI 不做事、Frontier 只记日志，避免重复输出。

### 6.2 飞书群消息：从 SDK 事件到 InboundEvent

```python
# 1) SDK 回调线程：lark 事件对象 → raw dict（平台字段在这一步拆出并归一）
raw = feishu_channel.event_to_raw(data)   # data 是 lark 的 P2ImMessageReceiveV1
# => {
#        "message_id": "om_xxx", "chat_id": "oc_xxx",
#        "chat_type": "group",          # 飞书 p2p 在这里已归一为 dm
#        "user_id": "ou_yyy", "message_type": "text",
#        "text": "@_user_1 报价",        # content JSON 已解开
#        "resource": {...},              # 原始 content，图片/文件时取资源 key
#        "mentions": [{"open_id": "ou_bot", "name": "bot", "key": "@_user_1"}],
#    }

# 2) ingest(raw)：计数 → should_accept（message_id 去重 + 群聊必须 @ 机器人）
#    → 纯文本就地 parse；image/file 交线程池下载后再 parse
feishu_channel.ingest(raw)

# 3) parse_to_InboundEvent(raw) 的结果（ingest 内部调用后 send_gateway）
# => InboundEvent(
#        text="报价",                    # mention key 已剥掉
#        source=SessionSource(channel="feishu", chat_id="oc_xxx", chat_type="group",
#                             user_id="ou_yyy", reply_to_message_id="om_xxx"),
#        channel_prompt="当前通道：飞书群聊。回答简洁，不要用过多 Markdown 格式。",
#    )
```

注意：`@bot` 前缀、飞书 content JSON 转义这些**平台杂务**都在 Channel 内消化掉，外面只看到干净的 `text="报价"`。`reply_to_message_id` 是回程车票：出站时飞书用它挂回原消息，失败再按 `chat_id` 新发一条。

---

## 七、当前限制与可选演进

| 主题 | 当前代码 | 后续可考虑 |
|------|----------|------------|
| 重复消息 | 飞书、QQ、微信各用 `TTLDedup` 判断消息 ID | 多进程或重启后的持久去重 |
| 媒体 | 图片/文件入站保存并限制为单件 20 MiB；出站统一为文字 | 语音、视频语义处理与媒体出站 |
| 入站工作 | `BaseChannel.submit_inbound_work` 提供有界槽位和工作线程；槽位满时同步处理 | 根据平台实测负载调整队列与背压 |
| 状态提示 | `status` 能区分启动与连接状态 | 按平台增加 typing 等长任务反馈 |
| 配置 | 启动时从 registry 与环境变量解析 | 如确有需求再做运行时热更新 |

---

## 八、设计原则小结

1. **翻译官定位**：Channel 只翻译不思考，平台差异在这里消化干净。
2. **双向最小契约**：只实现 4 个抽象方法（start/stop/parse/deliver），其余能力按需加。
3. **平台杂务不外泄**：鉴权/typing/去重/限流全在 Channel 内，Gateway 和 Loop 永远感知不到。
4. **新增平台零侵入**：加一个 Channel 子类，Loop/Gateway/types 一行不改。
