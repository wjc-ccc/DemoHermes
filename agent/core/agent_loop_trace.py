"""Loop 观测载荷的统一构建位置；事件不决定审批和执行结果。"""
from __future__ import annotations

from agent.types.events import LoopEvent


class AgentLoopTrace:
    def __init__(self, events, precision):
        self.events = events
        self.precision = precision

    def emit(self, type_, turn_id, session, session_key, **data):
        self.events.publish(LoopEvent(
            type=type_, turn_id=turn_id, session_id=session.session_id,
            session_key=session_key, data=data,
        ))

    def tool_start(self, request, context, fact_id):
        self.events.publish(LoopEvent(
            type="tool_call_start", turn_id=context.turn_id, session_id=context.session_id,
            session_key=context.session_key,
            data={"tool_call_id": fact_id, "name": request.name, "arguments": request.arguments},
        ))

    def tool_result(self, record, context):
        data = {
            "tool_call_id": record.tool_call_id, "name": record.tool_name,
            "ok": record.ok, "result_preview": (record.result_text or "")[:100],
            "error": record.error, "duration_ms": record.durations_ms,
        }
        if record.action_evidence is not None:
            data.update(ledger_entry_id=record.action_evidence.ledger_entry_id,
                        action_status=record.action_evidence.status)
        self.events.publish(LoopEvent(
            type="tool_call_result", turn_id=record.trajectory_id, session_id=record.session_id,
            session_key=context.session_key, data=data,
        ))
