"""
AgentLoop — 核心对话循环

observe → think → act → persist(由 Gateway/Store 负责落盘)

不认识 Channel / 队列 / 平台字段。只吃 Session + Message → TurnResult。
"""
from __future__ import annotations

import logging

from .types import Session, Message, ContentPart, TurnResult
from .context_builder import ContextBuilder

logger = logging.getLogger(__name__)


class AgentLoop:
    def __init__(self, model, *, max_iterations: int = 20):
        self.model = model
        self.max_iterations = max_iterations
        self._interrupt = False

    def request_interrupt(self) -> None:
        self._interrupt = True

    def run_turn(
        self,
        session: Session,
        user_message: Message,
        *,
        ephemeral_prompt: str | None = None,
    ) -> TurnResult:
        self._interrupt = False

        # ---- observe ----
        session.add_message(user_message)

        # ---- think / act（阶段 1：无 tool；预留迭代骨架）----
        builder = ContextBuilder(session)
        messages = builder.building_context()
        if ephemeral_prompt:
            messages.insert(1, {"role": "system", "content": ephemeral_prompt})

        if self._interrupt:
            return TurnResult(
                final_text=None,
                session_id=session.session_id,
                completed=False,
                interrupted=True,
            )

        try:
            reply = self.model.chat(messages) or ""
        except Exception as e:
            logger.exception("model.chat failed")
            # 用户消息已入 session；失败也返回，由上层决定是否回滚
            return TurnResult(
                final_text=None,
                session_id=session.session_id,
                completed=False,
                error=str(e),
            )

        # ---- act ----
        # TODO(阶段2): if tool_calls → execute → append → continue（受 max_iterations 约束）
        session.add_message(
            Message(
                role="assistant",
                content_text=reply,
                content=[ContentPart(type="text", text=reply)],
            )
        )

        # ---- persist：不在 loop 里写磁盘；Gateway 调 SessionStore.save ----
        return TurnResult(
            final_text=reply,
            session_id=session.session_id,
            completed=True,
        )
