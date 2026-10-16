"""从模型原始载荷到真实Loop/工具执行的离线回归。"""
import json
from types import SimpleNamespace

import httpx
import pytest

from agent.core.model.claude import ClaudeModel
from agent.core.model.deepseek import DeepSeekModel
from agent.core.perception.perception_judge import SystemOneContextJudge
from agent.core.perception.perception_engine import PerceptionBuilder
from agent.core.tools.base import Tool
from agent.core.tools.registry import ToolRegistry
from agent.types.conversation import Message, Session
from agent.types.model import ModelResponse, ToolCallRequest
from agent.types.perception.judge import ContextJudgeRequest
from agent.types.tools import ToolCallResult
from tests.loop_fakes import make_loop


class CountingTool(Tool):
    name = 'probe'
    parameters = {'type': 'object', 'properties': {}}

    def __init__(self):
        self.executions = 0

    def execute(self, arguments, *, context):
        self.executions += 1
        return ToolCallResult(result_text='executed')


def provider_response(provider, value):
    if provider == 'anthropic':
        return ClaudeModel._parse_response(dict(content=[dict(
            type='tool_use', id='call-1', name='probe', input=value)], stop_reason='tool_use'))
    tool = SimpleNamespace(id='call-1', function=SimpleNamespace(name='probe', arguments=json.dumps(value)))
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=None, tool_calls=[tool]), finish_reason='tool_calls')], usage=None)
    model = object.__new__(DeepSeekModel)
    model.model_name = 'test'
    model.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: response)))
    return model.chat([dict(role='user', content='probe')])


@pytest.mark.parametrize('provider', ['anthropic', 'openai'])
@pytest.mark.parametrize('value', [[], False, 0, None, 'not an object'])
def test_loop_never_executes_non_object_tool_arguments(provider, value):
    first = provider_response(provider, value)

    class Model:
        calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            return first if self.calls == 1 else ModelResponse(text='参数错误，等待修正。')

    tool = CountingTool()
    registry = ToolRegistry()
    registry.register(tool)
    session = Session()
    result = make_loop(Model(), tools=registry, model_retries=0).run_turn(session, Message(content_text='probe'))
    assert result.completed
    assert tool.executions == 0
    assert not session.tool_calls[0].ok
    assert 'invalid_argument_syntax' in session.tool_calls[0].error
    assert json.loads(session.tool_calls[0].arguments_raw) == value
    # 错误原文仍留档，但转换给Anthropic的配对消息必须满足input对象协议。
    _, outbound = ClaudeModel._convert_messages([PerceptionBuilder._map_message(m) for m in session.messages])
    use = next(block for m in outbound if isinstance(m['content'], list)
               for block in m['content'] if block.get('type') == 'tool_use')
    assert isinstance(use['input'], dict)


@pytest.mark.parametrize('provider', ['anthropic', 'openai'])
def test_loop_still_executes_valid_empty_object_arguments(provider):
    first = provider_response(provider, {})

    class Model:
        calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            return first if self.calls == 1 else ModelResponse(text='完成')

    tool = CountingTool()
    registry = ToolRegistry()
    registry.register(tool)
    session = Session()
    result = make_loop(Model(), tools=registry, model_retries=0).run_turn(session, Message(content_text='probe'))
    assert result.completed and tool.executions == 1 and session.tool_calls[0].ok


@pytest.mark.parametrize('raw,arguments', [('{', {}), ('{}', {'unmatched': 1})])
def test_loop_rejects_malformed_or_mismatched_raw_arguments(raw, arguments):
    class Model:
        calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(tool_calls=[ToolCallRequest(id='bad', name='probe',
                    arguments=arguments, arguments_raw=raw)])
            return ModelResponse(text='已收到参数错误')

    tool, registry, session = CountingTool(), ToolRegistry(), Session()
    registry.register(tool)
    result = make_loop(Model(), tools=registry, model_retries=0).run_turn(session, Message(content_text='probe'))
    assert result.completed and tool.executions == 0
    assert session.tool_calls[0].error == 'invalid_argument_syntax'


@pytest.mark.parametrize('text,with_tool', [('未完成的回答', False), ('', False), ('', True)])
def test_loop_preserves_truncated_output_without_success_or_tool_execution(text, with_tool):
    content = [dict(type='text', text=text)] if text else []
    if with_tool:
        content.append(dict(type='tool_use', id='partial-call', name='probe', input={}))
    response = ClaudeModel._parse_response(dict(content=content, stop_reason='max_tokens'))

    class Model:
        calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            return response

    model, tool, registry, session = Model(), CountingTool(), ToolRegistry(), Session()
    registry.register(tool)
    result = make_loop(model, tools=registry, model_retries=2).run_turn(session, Message(content_text='probe'))
    assert not result.completed
    assert result.error == 'model_output_truncated'
    assert model.calls == 1 and tool.executions == 0
    assert session.messages[-1].status == 'failed'
    assert session.messages[-1].content_text == text


def test_next_turn_does_not_replay_unfinished_tool_protocol():
    class Model:
        calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ClaudeModel._parse_response(dict(content=[dict(type='tool_use',
                    id='unfinished', name='probe', input={})], stop_reason='max_tokens'))
            assert not any(m.get('tool_calls') or m.get('role') == 'tool' for m in messages)
            return ModelResponse(text='恢复正常回答')

    tool, registry, session = CountingTool(), ToolRegistry(), Session()
    registry.register(tool)
    loop = make_loop(Model(), tools=registry, model_retries=0)
    assert not loop.run_turn(session, Message(content_text='first')).completed
    assert loop.run_turn(session, Message(content_text='second')).completed
    assert tool.executions == 0


@pytest.mark.parametrize('answers', [
    {'k1': {'choice': 'data', 'probabilities': {'data': .7}}, 'n1': {'noul': .2}},
    {'i1': {'k1': {'choice': 'data', 'probabilities': {'data': .7}}, 'n1': {'noul': .2}}},
    {'i1': {'choice': 'data', 'probabilities': {'data': .7}}, 'n1': .2},
])
def test_production_judge_accepts_observed_shapes_without_dropping_neededness(answers):
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'answers': answers})))
    judge = SystemOneContextJudge(api_key='offline', base_url='https://offline.test', model='test', client=client)
    request = ContextJudgeRequest(item_id='source', goal='verify', call='read_file', result_head='config',
        result_chars=40505, age_turns=4, calls_since=18)
    result = judge.judge([request])[0]
    assert result.kind == 'data' and result.still_needed == .2


@pytest.mark.parametrize('number', [float('nan'), float('inf'), -float('inf'), True])
def test_production_judge_does_not_turn_invalid_probability_into_confidence(number):
    payload = {'answers': {'k1': {'choice': 'data', 'probabilities': {'data': number}}, 'n1': {'noul': number}}}
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, content=json.dumps(payload).encode(), headers={'content-type': 'application/json'})))
    judge = SystemOneContextJudge(api_key='offline', base_url='https://offline.test', model='test', client=client)
    request = ContextJudgeRequest(item_id='source', goal='verify', call='read_file', result_head='config',
        result_chars=40505, age_turns=4, calls_since=18)
    result = judge.judge([request])[0]
    assert result.kind is None and result.still_needed is None
