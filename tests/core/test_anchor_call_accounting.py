import json
import re
from agent.core.perception.perception_anchor import ModelAnchorBuilder
from agent.core.perception.perception_engine import PerceptionBuilder
from agent.core.perception.perception_trace import PerceptionTrace
from agent.types.context import ContextBudget
from agent.types.model import ModelResponse
from tests.core.test_perception_compress import _history, _PerMessage
from tests.loop_fakes import make_loop


def test_anchor_real_attempt_usage_is_in_the_build_snapshot():
    class Model:
        name="anchor-fixture"
        model_name="fixture"
        def chat(self,messages,tools=None,**kwargs):
            source=re.search(r"### Source (\S+) \| Role user",messages[1]["content"])[1]
            return ModelResponse(text=json.dumps(dict(intent=[dict(text="目标零",source_message_ids=[source],status="active")],
                changes=[],decisions=[],next_steps=[],excluded=[])),usage={"prompt_tokens":77,"completion_tokens":12})
    trace=PerceptionTrace()
    PerceptionBuilder(trace=trace,anchor_builder=ModelAnchorBuilder(Model())).build(
        _history([(f"目标{i}","收到") for i in range(5)]),
        budget=ContextBudget(model_context_tokens=18,reserved_output_tokens=10),token_estimator=_PerMessage(),trace_id="anchor-cost",iteration=1)
    call=trace.get("anchor-cost").builds[0].auxiliary_calls[0]
    assert call.purpose=="anchor"
    assert (call.input_token_count,call.output_token_count)==(77,12)


def test_failed_anchor_attempts_survive_closed_build_failure():
    class Model:
        def chat(self,messages,tools=None,**kwargs):
            assert "AnchorBuilder" in messages[0]["content"]
            raise RuntimeError("anchor offline")
    trace=PerceptionTrace()
    session=_history([(f"目标{i}","收到") for i in range(5)])
    user=session.messages.pop()
    result=make_loop(model=Model(),perception_trace=trace,budget=ContextBudget(model_context_tokens=18,reserved_output_tokens=10),
                     token_estimator=_PerMessage(),model_retries=0).run_turn(session,user)
    build=trace.get(result.turn_id).builds[0]
    assert result.error=="anchor_compaction_failed"
    assert build.messages is None
    assert len(build.auxiliary_calls)==2
    assert all(call.error=="anchor offline" and call.input_token_count is None for call in build.auxiliary_calls)
