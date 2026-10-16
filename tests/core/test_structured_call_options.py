from types import SimpleNamespace
from agent.core.model.call_options import structured_call_options


def test_deepseek_auxiliary_options_follow_each_wire_protocol():
    assert structured_call_options(SimpleNamespace(name="claude",model_name="deepseek-v4-flash"))=={"thinking":{"type":"disabled"}}
    assert structured_call_options(SimpleNamespace(name="doubao",model_name="deepseek-v4-flash"))=={"thinking":{"type":"disabled"}}
    assert structured_call_options(SimpleNamespace(name="deepseek",model_name="deepseek-v4-flash"))=={"extra_body":{"thinking":{"type":"disabled"}}}
    assert structured_call_options(SimpleNamespace(name="claude",model_name="claude-sonnet"))=={}
