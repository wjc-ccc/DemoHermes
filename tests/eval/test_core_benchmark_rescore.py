"""评分修复只读取真实结构来源，原始回答和调用历史保持不变。"""
import json
import os

import pytest

from scripts.rescore_core_benchmark import evidence_sources, validate_review, prepare


def test_readback_context_list_and_object_preserve_only_successful_real_sources():
    calls = [dict(tool_call_id="read-list", status="success",
                  result_data={"reference": "message:other", "context": [{"reference": "message:gold", "role": "user"}]}),
             dict(tool_call_id="read-object", status="success", result_data={"context": {"evidence_refs": ["tool:gold"]}}),
             dict(tool_call_id="failed", status="error", result_data={"reference": "message:gold"}),
             dict(tool_call_id="fabricated", status="success", result_data={"content": "message:gold"})]
    assert evidence_sources({"gold"}, calls) == {"gold", "read-list", "read-object"}


def test_classification_shape_error_is_retryable_and_never_changes_semantic_matches():
    valid = dict(answer_passed=False, memory_matches=[], extra_card_ids=[], forbidden_card_ids=[], violations=[],
                 classification_matches=[{"memory_id": "gold", "card_ids": ["actual"]}])
    assert validate_review(valid, {}, {"actual"}, "回答")["matched_memory_ids"] == []
    invalid = dict(valid, classification_matches=[["gold", ["actual"]]])
    with pytest.raises(ValueError, match="classification_matches"):
        validate_review(invalid, {}, {"actual"}, "回答")


def test_score_version_links_immutable_product_logs_and_copies_mutable_reviews(tmp_path):
    source, output = tmp_path / "answer-run", tmp_path / "score-run"
    directory = source / "cases" / "scene"
    directory.mkdir(parents=True)
    (directory / "result.json").write_text('{"actual_answer":"冻结回答"}', encoding="utf-8")
    product = directory / "model-calls.jsonl"; product.write_text("product\n", encoding="utf-8")
    review = directory / "review-calls.jsonl"; review.write_text("first review\n", encoding="utf-8")
    manifest = {"scoring_version": "fixture"}
    prepare(source, output, manifest)
    assert os.path.samefile(product, output / product.relative_to(source))
    copied = output / review.relative_to(source)
    assert not os.path.samefile(review, copied)
    with copied.open("a", encoding="utf-8") as handle:
        handle.write("new review\n")
    assert review.read_text(encoding="utf-8") == "first review\n"
    assert json.loads((output / "manifest.json").read_text(encoding="utf-8")) == manifest
