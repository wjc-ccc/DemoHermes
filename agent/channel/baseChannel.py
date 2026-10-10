"""
BaseChannel — Channel 基类：平台原始载荷 → InboundEvent；OutboundReply → 平台

职责边界（全项目约定）：
    - 平台字段解析只发生在 Channel（parse_to_InboundEvent）
    - 出站平台投递只发生在 Channel（deliver_to_OutboundEvent）
    - Channel 不碰 Session / Loop / 模型；拿到事件后 send_gateway 交给总线

与 Gateway 的关系：
    Gateway.register_channel(channel) 时会回调 bind_gateway(self)，
    之后 Channel 通过 send_gateway(event) 把入站事件送进 MessageBus。

平台 channel 的共用骨架：
    入站：平台回调 → raw dict → ingest（计数、去重、准入）→ 带附件的进线程池 → parse → send_gateway
    raw dict：平台对象拆出来的纯 dict，chat_type 在这一步就归一为 dm / group；
              parse 只做正文清洗、附件下载与组装，不再映射平台字段
    提示词：类属性 prompts，按 dm / group 各一条
    凭据：类属性 env_map；registry 用来读 .env，_require_credentials 用同一份校验
    HTTP：_http_client 每渠道复用一个 httpx.Client，附件下载也走它
    生命周期：start 里 _start_worker 起收包线程；stop 统一收尾，子类只覆写 _close_connection
    运行标志只剩 _stop_event：置位后不再入站、收包循环退出
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
import logging
import threading
from typing import Any, Callable, Iterable

from agent.types.conversation import ContentPart
from agent.types.channel import InboundEvent, OutboundReply
from .dedup import TTLDedup

logger = logging.getLogger(__name__)


class BaseChannel(ABC):
    name: str = "base"
    # 平台 channel 的注册信息；registry 按这些属性解析 env、检查依赖，CLI / Frontier 保持为空。
    label: str = ""
    env_map: dict[str, str] = {}           # 必需凭据：配置字段名 → env 变量名
    optional_env_map: dict[str, str] = {}  # 非必需运行参数：配置字段名 → env 变量名
    deps: tuple[str, ...] = ()             # 启动前必须可 import 的模块
    install_hint: str = ""
    _http_timeout: float | None = 15.0     # None 表示超时由每个请求自己指定

    def __init__(self, config: dict | None = None) -> None:
        # 渠道配置由注册表一次性解析并注入；CLI / Frontier 无需配置。
        self.config: dict = dict(config or {})

        self.status: dict[str, Any] = {
            "start": False,            # 是否已启动
            "connected": False,        # 是否已观测到平台通信
            "message_count": 0,        # 平台推来的消息数（含被去重、准入过滤掉的）
            "inbound_event_count": 0,  # 实际送进 gateway 的事件数
            "outbound_event_count": 0, # 已投递的回复数（gateway 出站时累加）
            "error_count": 0,
        }
        self._gateway = None  # bind_gateway 后可用
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._http = None  # httpx.Client，首次使用时创建
        self._dedup = TTLDedup()
        self._inbound_pool: ThreadPoolExecutor | None = None
        self._inbound_slots = threading.BoundedSemaphore(32)

    # ---- 与 Gateway 绑定 ----
    def bind_gateway(self, gateway) -> None:
        """由 Gateway.register_channel 回调；绑定后 send_gateway 才有去处。"""
        self._gateway = gateway
        self._stop_event.clear()

    def unbind_gateway(self) -> None:
        self._stop_event.set()
        self._gateway = None

    def send_gateway(self, event: InboundEvent) -> None:
        """把解析好的入站事件交给 Gateway（进 MessageBus 的 in_dto）。"""
        if self._stop_event.is_set():
            return
        if self._gateway is None:
            self.status["error_count"] += 1
            raise RuntimeError(f"channel={self.name} 尚未 bind_gateway，无法发送事件")
        self._gateway.submit(event)
        self.status["inbound_event_count"] += 1

    # ---- 入站骨架 ----
    def ingest(self, raw: dict) -> None:
        """平台回调线程里完成计数和准入，带附件的消息再交给线程池下载和解析。"""
        self.status["message_count"] += 1
        if not self.should_accept(raw):
            return
        if self._has_media(raw):
            self.submit_inbound_work(self._send_parsed, raw)
        else:
            self._send_parsed(raw)

    def should_accept(self, raw: dict) -> bool:
        """默认按 message_id 去重；平台有额外准入规则时覆写并先调 super()。"""
        return not self._dedup.is_dup(str(raw.get("message_id", "")))

    def _has_media(self, raw: dict) -> bool:
        return False

    def _send_parsed(self, raw: dict) -> None:
        try:
            self.send_gateway(self.parse_to_InboundEvent(raw))
        except Exception:
            self.status["error_count"] += 1
            logger.exception("channel=%s 入站消息处理失败", self.name)

    def submit_inbound_work(self, fn, *args) -> None:
        """平台回调只排队；队列满时同步处理，避免静默丢消息。"""
        if self._stop_event.is_set():
            return
        if not self._inbound_slots.acquire(blocking=False):
            fn(*args)
            return
        if self._inbound_pool is None:
            self._inbound_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix=f"{self.name}-media")
        try:
            future = self._inbound_pool.submit(fn, *args)
        except Exception:
            self._inbound_slots.release()
            raise
        future.add_done_callback(lambda _: self._inbound_slots.release())

    def _collect_attachments(
        self, text: str, items: Iterable[Any], load: Callable[[Any, int], ContentPart | str],
    ) -> tuple[str, list[ContentPart]]:
        """逐项加载附件。load 返回 str 时作为可见说明拼进正文；加载失败同样写进正文交给 Agent。"""
        parts: list[ContentPart] = []
        for index, item in enumerate(items, start=1):
            try:
                result = load(item, index)
            except Exception as exc:
                self.status["error_count"] += 1
                result = f"[附件读取失败: {exc}]"
            if isinstance(result, ContentPart):
                parts.append(result)
            else:
                text = "\n".join(filter(None, [text, result]))
        return text, parts

    def _http_client(self):
        if self._http is None:
            import httpx

            self._http = httpx.Client(timeout=self._http_timeout)
        return self._http

    # ---- 生命周期 ----
    def _require_credentials(self) -> None:
        missing = [key for key in self.env_map if not str(self.config.get(key) or "").strip()]
        if missing:
            raise RuntimeError(f"{self.name} 凭据缺失（{'/'.join(missing)}）")

    def _start_worker(self, target: Callable[[], None], thread_name: str) -> None:
        self._stop_event.clear()
        self._worker = threading.Thread(target=target, name=thread_name, daemon=True)
        self.status["start"] = True
        try:
            self._worker.start()
        except Exception:
            self.status["start"] = False
            raise

    @abstractmethod
    def start(self) -> None:
        """启动渠道（读配置、连平台）；无平台可连的渠道置 start=True 即可。"""

    def stop(self) -> None:
        """先置停止信号再断平台连接，避免收包线程把主动断开当成掉线重连。"""
        self._stop_event.set()
        self._close_connection()
        if self._inbound_pool is not None:
            self._inbound_pool.shutdown(wait=False, cancel_futures=True)
            self._inbound_pool = None
        self.status["start"] = False
        self.status["connected"] = False
        if self._http is not None:
            self._http.close()
            self._http = None
        if self._worker is not None and self._worker is not threading.current_thread():
            self._worker.join(timeout=1)

    def _close_connection(self) -> None:
        """断开平台长连接；没有长连接的渠道不用覆写。"""

    # ---- I/O 归一化（子类必须实现）----
    @abstractmethod
    def parse_to_InboundEvent(self, raw: Any) -> InboundEvent:
        """平台原始数据 → 统一 InboundEvent。"""

    @abstractmethod
    def deliver_to_OutboundEvent(self, reply: OutboundReply) -> None:
        """统一出站 → 发回平台；投递失败必须抛异常供 Gateway 记账。"""
