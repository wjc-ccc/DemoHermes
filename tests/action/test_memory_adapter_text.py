from types import SimpleNamespace
from agent.core.action.adapters.builtin import BuiltinToolAdapter
from agent.types.tools import ToolCallResult


def test_action_revision_annotation_preserves_readable_view_and_result_budget():
    class Read:
        name="memory_read"
        def execute(self,arguments,*,context):
            self.context=context
            return ToolCallResult(ok=True,result_text="正文：原文",data=dict(reference="card:actual",source="card",
                content="原文",offset=0,next_offset=None,total_chars=2,context=()))
    tool=Read()
    adapter=BuiltinToolAdapter(tool)
    adapter.resource=lambda arguments,context:("card:actual","2")
    context=SimpleNamespace(session_id="s",turn_id="t",session_key="key",source_message_id="m",
                            memory_scope=None,skills=None,result_token_budget=900)
    result=adapter.invoke({"reference":"card:actual"},context,idempotency_key="read")
    assert result.data["revision"]==2
    assert result.result_text.startswith("工具：memory_read")
    assert "版本：2" in result.result_text and "原文" in result.result_text
    assert 0<tool.context.result_token_budget<=900
    assert len(result.result_text.encode("utf-8"))<=900
