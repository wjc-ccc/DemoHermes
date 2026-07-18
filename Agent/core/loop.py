"""
AgentLoop — 核心对话循环（observe → think ⇄ act → 返回）

职责边界：
    - 只吃 Session + Message → TurnResult，不认识 Channel / 队列 / 平台字段
    - think/act 内循环：模型要求调工具就执行并回注，直到模型给出纯文本或触达上限
    - 不写磁盘（持久化是 Gateway/Store 的事），只往 EventBus 发观测事件

模型访问：
    优先使用构造时直传的 model；否则经 ModelRegistry 按名称解析。
    Loop 只面向 BaseModel.chat 接口，新增 provider 不需要改这里。

容错兜底：
    - 模型调用失败：按 model_retries 重试，仍失败 → TurnResult(error)
    - 工具执行失败：ToolRegistry 已兜底成 ToolResult(ok=False)，回注让 LLM 自我修正
    - 超过 max_iterations：写入兜底文案，TurnResult(completed=False, error="max_iterations")
    - 全程 logging + EventBus 事件，前端可逐步观测
"""
from __future__ import annotations

import logging
import time
import uuid

from .context_builder import ContextBuilder
from .events import EventBus, LoopEvent, default_event_bus
from .messages import build_assistant_message, build_tool_message
from .types import Message, Session, ToolCall, TurnResult

logger = logging.getLogger(__name__)

# 文本预览长度：事件载荷只带预览，完整内容在 Session 里
_PREVIEW = 300


class AgentLoop:
    def __init__(
        self,
        model=None,
        *,
        model_registry=None,
        model_name: str | None = None,
        tool_registry=None,
        skill_registry=None,
        event_bus: EventBus | None = None,
        max_iterations: int = 8,
        model_retries: int = 2,
    ):
        # ---- 模型解析：直传 > 注册表指定 > 注册表默认 ----
        if model is not None:
            self.model = model
        else:
            registry = model_registry
            if registry is None:
                from ..model.registry import ModelRegistry  # 延迟 import，避免循环依赖
                registry = ModelRegistry.default()
            self.model = registry.get(model_name)
        self.tools = tool_registry
        self.skills = skill_registry
        self.events = event_bus or default_event_bus
        self.max_iterations = max_iterations
        self.model_retries = model_retries
        self._interrupt = False

    def request_interrupt(self) -> None:
        """外部（Channel/Gateway）请求打断；Loop 每轮 think 前检查。"""
        self._interrupt = True

    # ---- 事件辅助 ----
    def _emit(self, type_: str, turn_id: str, session: Session, session_key: str, **data) -> None:
        self.events.publish(
            LoopEvent(
                type=type_,
                turn_id=turn_id,
                session_key=session_key,
                session_id=session.session_id,
                data=data,
            )
        )

    # ---- 模型调用（带重试）----
    def _chat_with_retry(self, messages: list[dict]):
        tools = self.tools.get_schemas() if self.tools else None
        last_error: Exception | None = None
        for attempt in range(self.model_retries + 1):
            try:
                return self.model.chat(messages, tools=tools or None)
            except Exception as e:
                last_error = e
                logger.warning(
                    "模型调用失败（第 %d/%d 次）: %s", attempt + 1, self.model_retries + 1, e
                )
                if attempt < self.model_retries:
                    time.sleep(0.5 * (attempt + 1))  # 简单退避
        raise last_error  # type: ignore[misc]

    # ---- 主入口 ----
    def run_turn(
        self,
        session: Session,
        user_message: Message,
        *,
        ephemeral_prompt: str | None = None,
        session_key: str = "",
    ) -> TurnResult:
        self._interrupt = False
        turn_id = uuid.uuid4().hex[:12]

        # ---- observe：用户消息入 Session ----
        session.add_message(user_message)
        self._emit(
            "turn_start", turn_id, session, session_key,
            user_text=user_message.content_text[:_PREVIEW],
            message_count=session.message_count,
        )
        logger.info("turn 开始 turn_id=%s session=%s", turn_id, session.session_id)

        for iteration in range(1, self.max_iterations + 1):
            if self._interrupt:
                logger.info("turn 被打断 turn_id=%s", turn_id)
                return TurnResult(
                    final_text=None, session_id=session.session_id,
                    completed=False, interrupted=True,
                )

            # ---- think：组装上下文 → 调模型 ----
            builder = ContextBuilder(
                session,
                skill_registry=self.skills,
                ephemeral_prompt=ephemeral_prompt,
            )
            messages = builder.building_context()
            stats = builder.context_stats()
            self._emit(
                "llm_request", turn_id, session, session_key,
                iteration=iteration, **stats,
            )
            try:
                response = self._chat_with_retry(messages)
            except Exception as e:
                logger.exception("模型调用最终失败 turn_id=%s", turn_id)
                self._emit("turn_error", turn_id, session, session_key, error=str(e))
                return TurnResult(
                    final_text=None, session_id=session.session_id,
                    completed=False, error=f"模型调用失败: {e}",
                )
            self._emit(
                "llm_response", turn_id, session, session_key,
                iteration=iteration,
                text_preview=response.text[:_PREVIEW],
                tool_calls=[{"name": tc.name, "arguments": tc.arguments} for tc in response.tool_calls],
                finish_reason=response.finish_reason,
                usage=response.usage,
            )

            # ---- act 分支 1：模型要求调工具 → 执行并回注，继续内循环 ----
            if response.has_tool_calls:
                session.add_message(build_assistant_message(response.text, response.tool_calls))
                for req in response.tool_calls:
                    record = self._execute_tool(req, session, turn_id, session_key)
                    session.add_message(build_tool_message(record))
                continue

            # ---- act 分支 2：纯文本 → 写入 Session，本轮结束 ----
            session.add_message(build_assistant_message(response.text))
            self._emit(
                "turn_end", turn_id, session, session_key,
                final_text=response.text[:_PREVIEW],
                iterations=iteration,
            )
            logger.info("turn 完成 turn_id=%s 迭代=%d", turn_id, iteration)
            return TurnResult(
                final_text=response.text,
                session_id=session.session_id,
                completed=True,
            )

        # ---- 兜底：超过最大迭代次数 ----
        fallback = "（抱歉，这个问题需要的推理步骤超出了我的上限，请换个问法或拆小一点再试。）"
        logger.warning("turn 超过最大迭代 turn_id=%s max=%d", turn_id, self.max_iterations)
        session.add_message(build_assistant_message(fallback))
        self._emit("turn_error", turn_id, session, session_key, error="max_iterations")
        return TurnResult(
            final_text=fallback,
            session_id=session.session_id,
            completed=False,
            error="max_iterations",
        )

    # ---- 工具执行（含事件与记录）----
    def _execute_tool(self, req, session: Session, turn_id: str, session_key: str) -> ToolCall:
        record = ToolCall(
            tool_call_id=req.id or uuid.uuid4().hex,
            session_id=session.session_id,
            trajectory_id=turn_id,
            tool_name=req.name,
            arguments=req.arguments,
            arguments_raw=req.arguments_raw,
            status="running",
        )
        self._emit(
            "tool_call_start", turn_id, session, session_key,
            tool_call_id=record.tool_call_id, name=req.name, arguments=req.arguments,
        )
        started = time.time()
        if self.tools is None:
            # 模型发了工具调用但 Loop 没配工具注册表 —— 明确告知，不静默吞掉
            from ..tools.base import ToolResult
            result = ToolResult(ok=False, error="当前 Agent 未配置任何工具")
        else:
            result = self.tools.dispatch(req.name, req.arguments)
        record.finished_at = time.time()
        record.durations_ms = int((record.finished_at - started) * 1000)
        record.status = "success" if result.ok else "error"
        record.tool_call_result = result.data
        record.result_text = result.result_text
        record.error = result.error
        session.tool_calls.append(record)
        self._emit(
            "tool_call_result", turn_id, session, session_key,
            tool_call_id=record.tool_call_id, name=req.name, ok=result.ok,
            result_preview=(result.result_text or "")[:_PREVIEW],
            error=result.error, duration_ms=record.durations_ms,
        )
        return record
