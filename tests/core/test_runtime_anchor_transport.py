"""Runtime装配必须为无语义上限的软触发Anchor提供实际输出预算。"""
import pytest

from agent.core.model.registry import ModelRegistry
from agent.core.runtime import build_runtime
from agent.types.model import ModelResponse
from agent.types.perception.result import PerceptionConfig
from agent.types.runtime import RuntimeConfig


class CaptureModel:
    name = 'anchor-transport-test'
    model_name = 'test'

    def __init__(self):
        self.calls = []

    def chat(self, messages, tools=None, **kwargs):
        self.calls.append(kwargs)
        return ModelResponse(text='{}', finish_reason='stop')


@pytest.mark.parametrize('output_limit', [4096, 8192])
def test_runtime_supplies_anchor_output_budget(tmp_path, output_limit):
    model = CaptureModel()
    models = ModelRegistry()
    models.register(model, as_default=True)
    runtime = build_runtime(RuntimeConfig(use_memory=False, data_root=tmp_path,
        perception_config=PerceptionConfig(compression_trigger_tokens=12000,
            anchor_provider_output_limit=output_limit)), model_registry=models)
    try:
        output = runtime.loop.perception._anchor_builder({'constraints': {'max_tokens': None}})
        assert output == {}
        assert model.calls == [{'max_tokens': output_limit}]
    finally:
        runtime.stop()


@pytest.mark.parametrize('value', [0, -1, None, True])
def test_anchor_provider_output_limit_must_be_positive(value):
    with pytest.raises(ValueError, match='anchor_provider_output_limit'):
        PerceptionConfig(anchor_provider_output_limit=value)
