"""正式测评计量失败关闭，默认运行仍保留原字节估算降级。"""
import pytest

from agent.core.perception.token_estimator import SafeEstimator


def test_strict_estimator_raises_the_original_tokenizer_exception():
    failure = RuntimeError("tokenizer fixture offline")

    class Tokenizer:
        fail_closed = True

        def estimate_messages(self, messages):
            raise failure

    estimator = SafeEstimator(Tokenizer())
    with pytest.raises(RuntimeError) as captured:
        estimator.estimate_messages([{"role": "user", "content": "内容"}])
    assert captured.value is failure
    assert not estimator.failed


def test_strict_tool_schema_counting_also_fails_closed():
    class Tokenizer:
        fail_closed = True

        def estimate_messages(self, messages):
            raise ValueError("schema tokenizer fixture failure")

    with pytest.raises(ValueError, match="schema tokenizer"):
        SafeEstimator(Tokenizer()).estimate_tools([{"type": "function", "function": {"name": "probe"}}])


def test_strict_estimator_rejects_invalid_negative_counts_without_fallback():
    class Tokenizer:
        fail_closed = True

        def estimate_messages(self, messages):
            return -1

    with pytest.raises(ValueError, match="negative token estimate"):
        SafeEstimator(Tokenizer()).estimate_messages([{"role": "user", "content": "内容"}])


def test_default_estimator_retains_the_existing_sticky_fallback():
    class Tokenizer:
        def __init__(self):
            self.calls = 0

        def estimate_messages(self, messages):
            self.calls += 1
            raise ValueError("fixture tokenizer offline")

    tokenizer = Tokenizer()
    estimator = SafeEstimator(tokenizer)
    messages = [{"role": "user", "content": "内容"}]
    first = estimator.estimate_messages(messages)
    assert first > 0
    assert estimator.failed
    assert estimator.estimate_messages(messages) == first
    assert tokenizer.calls == 1
