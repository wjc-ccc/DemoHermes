"""架构超参数可以改切块、压缩比例和调用前估算，默认值保持原行为。"""
import pytest

from agent.core.memory.config import MemoryExtractionConfig, MemoryReviewConfig
from agent.core.memory.memory_extractor import MemoryExtractor
from agent.core.perception.config import TokenEstimateConfig
from agent.core.perception.perception_anchor import anchor_token_limit
from agent.core.perception.token_estimator import ConservativeEstimator
from agent.types.perception.result import PerceptionConfig
from agent.types.runtime import RuntimeConfig
from tests.core.memory.test_memory_extractor import _Cards


def test_defaults_match_the_previous_architecture():
    extraction = MemoryExtractionConfig()
    review = MemoryReviewConfig()
    perception = PerceptionConfig()
    estimate = TokenEstimateConfig()
    assert extraction.input_token_budget == 16_000
    assert extraction.admission_reserve_tokens == 1_024
    assert extraction.fragment_max_chars == 2_048
    assert extraction.fragment_overlap_cap == 128
    assert review.max_attempts == 3
    assert perception.compact_ratio == 0.85
    assert perception.anchor_budget_ratio == 0.15
    assert perception.anchor_output_token_limit is None
    assert estimate.message_overhead_bytes == 16
    assert estimate.image_token_cost == 8192
    assert anchor_token_limit(10_000) == 1_500
    assert anchor_token_limit(20_000) == 3_000
    extractor = MemoryExtractor(None, _Cards())
    assert extractor.input_token_budget == 16_000
    assert extractor.admission_review is True


def test_runtime_accepts_architecture_configs():
    config = RuntimeConfig(
        memory_extraction_config=MemoryExtractionConfig(input_token_budget=24_000),
        memory_review_config=MemoryReviewConfig(max_attempts=1),
        perception_config=PerceptionConfig(compact_ratio=0.7, anchor_budget_ratio=0.10),
        token_estimate_config=TokenEstimateConfig(message_overhead_bytes=0),
    )
    assert config.memory_extraction_config.input_token_budget == 24_000
    assert config.memory_review_config.max_attempts == 1
    assert config.perception_config.anchor_budget_ratio == 0.10
    assert anchor_token_limit(10_000, ratio=config.perception_config.anchor_budget_ratio) == 1_000


def test_invalid_architecture_values_are_rejected():
    with pytest.raises(ValueError):
        MemoryExtractionConfig(fragment_max_chars=0)
    with pytest.raises(ValueError):
        PerceptionConfig(compact_ratio=1.2)
    with pytest.raises(ValueError):
        TokenEstimateConfig(image_token_cost=-1)


def test_fragment_width_follows_extraction_config():
    from agent.core.memory.memory_extractor import _source_fragments

    source = {
        "turn_id": "t1", "session_id": "s1", "user_message_id": "m1", "occurred_at": 1,
        "user_text": "甲" * 300, "assistant": [], "tools": [],
    }
    fragments = _source_fragments(
        source, [], 10**9, cost=lambda group, cards: 0,
        config=MemoryExtractionConfig(fragment_max_chars=80),
    )
    assert [(item["offset"], len(item["user_text"])) for item in fragments] == [
        (0, 80), (70, 80), (140, 80), (210, 80), (280, 20),
    ]


def test_loop_tool_result_limit_keeps_the_previous_split():
    from agent.core.config import LoopConfig
    from agent.types.runtime import LoopConfig as ExportedLoopConfig

    settings = LoopConfig()
    assert settings is not None and settings.tool_result_limit(input_tokens=8000, tokens_used=1000, call_count=1) == 2048
    assert settings.tool_result_limit(input_tokens=2000, tokens_used=1800, call_count=4) == 128
    tight = LoopConfig(tool_result_cap=300, tool_result_floor=50, tool_result_reserve=0, tool_result_min_remaining=50)
    assert tight.tool_result_limit(input_tokens=1000, tokens_used=100, call_count=1) == 300
    assert ExportedLoopConfig is LoopConfig


def test_memory_schedule_matches_the_historical_cadence():
    from agent.core.memory.config import MemoryScheduleConfig
    from agent.core.storage.sqlite.memory_review_scheduler import _is_cadence_boundary

    schedule = MemoryScheduleConfig()
    assert [n for n in range(1, 31) if schedule.is_boundary(n)] == [
        n for n in range(1, 31) if _is_cadence_boundary(n)
    ]
    assert MemoryScheduleConfig(early_ordinals=(2,), steady_start=4, steady_interval=2).is_boundary(6)


def test_admission_config_holds_the_score_mix():
    from agent.types.perception.admission import AdmissionConfig, AdmissionPolicy

    default = AdmissionConfig()
    assert default.prior("log") == 0.25
    assert AdmissionPolicy is AdmissionConfig
    judge_only = AdmissionConfig(judge_weight=1)
    assert judge_only.judge_weight == 1
    with pytest.raises(ValueError):
        AdmissionConfig(min_kind_probability=0)


def test_token_estimate_config_changes_preflight_cost():
    message = {"role": "user", "content": "a", "_images": [{}]}
    default = ConservativeEstimator().estimate_messages([message])
    tuned = ConservativeEstimator(TokenEstimateConfig(message_overhead_bytes=0, image_token_cost=3)).estimate_messages([message])
    assert default - tuned == 16 + 8192 - 3
