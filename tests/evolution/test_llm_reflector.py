"""LLM EvolutionReflector：Fake 模型 → skill proposal。"""
from __future__ import annotations

from agent.evolution.llm_reflector import LlmEvolutionReflector
from agent.evolution.types import EvolutionSignal
from agent.core.memory import MemoryScope
from agent.core.memory.types import TurnRecord
from agent.model.baseStructure import BaseModel, ModelResponse

_SCOPE = MemoryScope(tenant_id="cli", user_id="llm1")


class _ScriptModel(BaseModel):
    name = "script"

    def __init__(self, text: str):
        super().__init__()
        self._text = text

    def chat(self, messages, tools=None, **kwargs):
        return ModelResponse(text=self._text)


def _turn() -> TurnRecord:
    return TurnRecord(
        turn_id="t1",
        session_id="s1",
        scope=_SCOPE,
        user_message_id="m",
        assistant_message_ids=(),
        tool_call_ids=(),
        started_at=1.0,
        finished_at=2.0,
        completed=True,
        interrupted=False,
        error=None,
    )


def test_llm_reflector_parses_skill_proposal():
    model = _ScriptModel(
        '{"name":"retry_bash","description":"bash 超时重试","body":"## 目标\\n\\n重试\\n\\n## 步骤\\n\\n1. bash\\n"}'
    )
    reflector = LlmEvolutionReflector(model)
    props = reflector.reflect_skills(_turn(), user_text="跑脚本")
    assert len(props) == 1
    assert props[0].name == "retry_bash"
    assert "步骤" in props[0].body
