import json

from agent.core.memory.memory_extractor import MemoryExtractor
from agent.core.memory.memory_batch_reviewer import MemoryBatchReviewer
from tests.core.memory.test_memory_extractor import _Cards, _source


def test_full_tail_is_present_in_bounded_text_prompts():
    class Model:
        inputs = []
        def chat(self, messages):
            self.inputs.append(messages[-1]["content"])
            return {"text": '{"cards":[],"skipped":[]}'}
    text = "临时测试记录。" * 1000 + "最终更正：以后长期用中文回答。"
    model = Model()
    batch = MemoryExtractor(model, _Cards()).extract_batch(*_source(text))
    assert batch.proposals == ()
    assert any("最终更正：以后长期用中文回答。" in text for text in model.inputs)
    assert all(not text.lstrip().startswith("{") for text in model.inputs)
    assert len(model.inputs) >= 2


def test_model_must_supply_retention_reason_and_future_use_before_building():
    class Model:
        def chat(self, messages):
            return {"text": json.dumps({"cards": [{
                "kind": "stable_preference", "subject": "assistant", "attribute": "response_language",
                "summary": "长期中文回答", "content": "以后用中文回答", "confidence": 0.9,
                "source_message_ids": ["m1"], "evidence_quotes": {"m1": "以后请用中文回答"},
                "reason_code": "explicit_preference",
            }], "skipped": []}, ensure_ascii=False)}
    batch = MemoryExtractor(Model(), _Cards()).extract_batch(*_source())
    assert batch.proposals == ()
    assert len(batch.issues) == 1


def test_explicit_skip_is_saved_in_trace_without_becoming_a_card():
    class Model:
        def chat(self, messages):
            return {"text": json.dumps({"cards": [], "skipped": [{
                "source_message_ids": ["m1"], "source_tool_call_ids": [],
                "reason_code": "temporary_request", "reason": "本轮请求没有长期用途",
            }]}, ensure_ascii=False)}
    result = MemoryBatchReviewer(MemoryExtractor(Model(), _Cards())).review_batch(*_source("今天帮我列个清单"))
    assert result.user_items == ()
    assert result.trace.items[0].decision.value == "skipped"
    assert result.trace.items[0].ai.reason_code == "temporary_request"


def test_missing_machine_reason_code_does_not_discard_a_justified_card():
    class Model:
        def chat(self,messages):
            return {"text":json.dumps({"cards":[dict(kind="stable_preference",subject="assistant",attribute="response_language",
                summary="长期中文回答",content="以后用中文回答",confidence=0.9,source_message_ids=["m1"],
                evidence_quotes={"m1":"以后请用中文回答"},retention_reason="明确长期偏好",future_use="后续默认中文回复")],"skipped":[]},ensure_ascii=False)}
    batch=MemoryExtractor(Model(),_Cards()).extract_batch(*_source())
    assert len(batch.proposals)==1 and batch.issues==()
    assert batch.proposals[0].reason_code=="model_proposal"


def test_empty_batch_skip_summary_has_runtime_source_provenance():
    class Model:
        def chat(self,messages):
            return {"text":json.dumps({"cards":[],"skipped":[dict(source_message_ids=[],source_tool_call_ids=[],
                reason_code="no_long_term_information",reason="本批只有临时练习")]},ensure_ascii=False)}
    batch=MemoryExtractor(Model(),_Cards()).extract_batch(*_source("这次只统计四个字"))
    assert batch.issues==()
    assert batch.skipped[0].source_message_ids==("m1",)


def test_skipped_assistant_context_uuid_is_audited_but_never_card_evidence():
    from agent.types.conversation import Message
    turns,sessions=_source()
    identity="b1f2e440-336f-4b6d-bcbe-4b5ed8b78d40"
    sessions[0].add_message(Message(message_id=identity,role="assistant",content_text="长期中文回复"))
    turns=(turns[0].model_copy(update={"assistant_message_ids":(identity,)}),)
    class Model:
        def chat(self,messages):
            return {"text":json.dumps({"cards":[dict(kind="stable_preference",subject="assistant",attribute="response_language",
                summary="中文回复",content="长期中文回复",confidence=0.9,source_message_ids=[identity],
                retention_reason="偏好",future_use="后续回复",evidence_quotes={identity:"长期中文回复"})],
                "skipped":[dict(source_message_ids=[identity.replace("-","")],source_tool_call_ids=[],
                reason_code="assistant_context",reason="助手确认不构成新的事实")]},ensure_ascii=False)}
    batch=MemoryExtractor(Model(),_Cards()).extract_batch(turns,sessions)
    assert batch.proposals==()
    assert [issue.code for issue in batch.issues]==["invalid_model_item"]
    assert batch.skipped[0].source_message_ids==(identity,)


def test_skip_may_resolve_real_turn_identity_but_cards_may_not():
    class Model:
        def chat(self,messages):
            return {"text":json.dumps({"cards":[],"skipped":[dict(source_message_ids=["t1"],
                source_tool_call_ids=[],reason_code="temporary",reason="没有持续用途")]},ensure_ascii=False)}
    batch=MemoryExtractor(Model(),_Cards()).extract_batch(*_source("这次只统计字数"))
    assert batch.issues==()
    assert batch.skipped[0].source_message_ids==("m1",)


def test_json_code_fence_is_only_a_container_not_a_terminal_job_failure():
    class Model:
        def chat(self,messages):
            return {"text":'```json\n{"cards":[],"skipped":[]}\n```'}
    batch=MemoryExtractor(Model(),_Cards()).extract_batch(*_source())
    assert batch.proposals==() and batch.issues==()


def test_failed_extraction_preserves_actual_model_attempt_metadata():
    import pytest
    from agent.core.memory.memory_extractor import MemoryExtractionError
    class Model:
        name="fixture"
        model_name="bad-json"
        def chat(self,messages):
            return {"text":"not-json","usage":{"prompt_tokens":13,"completion_tokens":2}}
    with pytest.raises(MemoryExtractionError) as caught:
        MemoryExtractor(Model(),_Cards()).extract_batch(*_source())
    assert caught.value.model_calls[0].input_token_count==13
    assert caught.value.model_calls[0].output_token_count==2
    assert caught.value.raw_response=="not-json"


def test_independent_admission_can_reject_supported_but_third_party_candidate():
    from tests.prompt_fakes import memory_output
    class Model:
        def chat(self,messages):
            if "独立准入复核" in messages[0]["content"]:
                return {"text":memory_output(skipped=[dict(source_message_ids=["m1"],source_tool_call_ids=[],
                    reason_code="third_party",reason="同事身份不是用户本人资料")])}
            return {"text":memory_output([dict(kind="profile",subject="user",attribute="profession",
                summary="同事职业",content="同事小林是前端工程师",confidence=.9,source_message_ids=["m1"],
                evidence_quotes={"m1":"同事小林是前端工程师"})])}
    batch=MemoryExtractor(Model(),_Cards()).extract_batch(*_source("同事小林是前端工程师，不是我的职业。"))
    assert batch.proposals==()
    assert batch.skipped[0].reason_code=="third_party"
    assert len(batch.model_calls)==2


def test_independent_admission_can_recover_a_useful_failure_skipped_by_initial_pass():
    from tests.prompt_fakes import memory_output
    text="当前 Windows 项目导出 SVG 失败，提示 unsupported；只验证本次环境。"
    class Model:
        def chat(self,messages):
            if "独立准入复核" not in messages[0]["content"]:
                return {"text":memory_output(skipped=[dict(source_message_ids=["m1"],source_tool_call_ids=[],
                    reason_code="low_value",reason="仅一次失败")])}
            return {"text":memory_output([dict(kind="reusable_experience",summary="SVG 导出失败",content=text,
                confidence=.9,source_message_ids=["m1"],evidence_quotes={"m1":text},
                applicability="当前 Windows 项目",action="导出 SVG",outcome="失败，unsupported",
                limitations="仅本次环境",outcome_status="failure",keywords=["Windows","SVG","unsupported"])])}
    batch=MemoryExtractor(Model(),_Cards()).extract_batch(*_source(text))
    assert len(batch.proposals)==1 and batch.proposals[0].outcome_status.value=="failure"
    assert batch.skipped==() and len(batch.model_calls)==2


def test_validity_binding_inherits_exact_scoped_card_version_and_rejects_unknown_reference():
    from datetime import datetime,timezone
    from tests.core.memory.test_memory_extractor import _StoredCards,_stored_card
    from tests.prompt_fakes import memory_output
    parent=_stored_card("period-card","加班期学习安排",kind="ongoing_fact",slot_key="ongoing_fact:user:period")
    expiry=datetime(2030,1,3,tzinfo=timezone.utc)
    parent=parent.model_copy(update={"valid_until":expiry,"revision":3})
    class Model:
        reference="period-card"
        def chat(self,messages):
            return {"text":memory_output([dict(kind="ongoing_fact",subject="user",attribute="study_focus",
                summary="加班期优先复习",content="这段加班期优先复习",confidence=.9,source_message_ids=["m1"],
                evidence_quotes={"m1":"这段加班期优先复习"},validity_source_card_id=self.reference)])}
    model=Model()
    result=MemoryBatchReviewer(MemoryExtractor(model,_StoredCards([parent]))).review_batch(*_source("这段加班期优先复习"))
    assert result.user_items[0].valid_until==expiry
    assert result.user_items[0].metadata["validity_source"]=={"card_id":"period-card","revision":3}
    model.reference="other-scope-card"
    rejected=MemoryExtractor(model,_StoredCards([parent])).extract_batch(*_source("这段加班期优先复习"))
    assert rejected.proposals==() and rejected.issues[0].code=="invalid_model_item"


def test_explicit_success_and_failure_outcomes_keep_mixed_status():
    from tests.prompt_fakes import memory_output
    text="本次缩略图上传成功，但原图备份失败，只测试了这次相册。"
    class Model:
        def chat(self,messages):
            return {"text":memory_output([dict(kind="reusable_experience",summary="本次同步混合结果",content=text,
                confidence=.9,source_message_ids=["m1"],evidence_quotes={"m1":text},applicability="本次相册同步",
                action="上传缩略图与备份原图",outcome="缩略图上传成功，原图备份失败",limitations="仅本次相册",
                outcome_status="observed",keywords=["缩略图","原图备份","同步结果"])])}
    result=MemoryExtractor(Model(),_Cards()).extract_batch(*_source(text))
    assert result.proposals[0].outcome_status.value=="mixed"


def test_source_repair_drops_unreferenced_old_card_clone_but_keeps_new_preference():
    from tests.prompt_fakes import memory_output
    good=dict(kind="stable_preference",subject="assistant",attribute="response_language",summary="中文回复",
              content="以后用中文回复",confidence=.9,source_message_ids=["m1"],evidence_quotes={"m1":"以后请用中文回答"})
    class Model:
        calls=0
        def chat(self,messages):
            self.calls+=1
            clone={**good,"source_message_ids":[],"evidence_quotes":{}}
            return {"text":memory_output([good] if "有限格式/来源修复" in messages[0]["content"] else [good,clone])}
    model=Model()
    batch=MemoryExtractor(model,_Cards()).extract_batch(*_source())
    assert len(batch.proposals)==1 and batch.issues==()
    assert model.calls==3 and batch.model_calls[-1].purpose=="memory_repair"


def test_validity_binding_can_inherit_an_unbounded_existing_stage():
    from tests.core.memory.test_memory_extractor import _StoredCards, _stored_card
    from tests.prompt_fakes import memory_output
    parent = _stored_card("purchase", "本次采购预算五千元", kind="ongoing_fact",
                          slot_key="ongoing_fact:purchase:budget")
    class Model:
        def chat(self, messages):
            return {"text": memory_output([dict(kind="ongoing_fact", subject="purchase", attribute="ram",
                summary="本次采购内存要求", content="本次采购需要十六 GB 内存", confidence=.9,
                source_message_ids=["m1"], evidence_quotes={"m1": "本次采购需要十六 GB 内存"},
                validity_source_card_id="purchase")])}
    batch = MemoryExtractor(Model(), _StoredCards([parent])).extract_batch(*_source("本次采购需要十六 GB 内存"))
    assert batch.issues == () and len(batch.proposals) == 1
    assert batch.proposals[0].valid_until is None
    assert batch.proposals[0].metadata["validity_source"] == {"card_id": "purchase", "revision": 1}


def test_invalid_skip_source_gets_a_limited_repair_using_real_sources():
    from tests.prompt_fakes import memory_output
    class Model:
        def chat(self, messages):
            reference = "m1" if "有限格式/来源修复" in messages[0]["content"] else "unknown-source"
            return {"text": memory_output(skipped=[dict(source_message_ids=[reference],
                reason_code="temporary", reason="临时标点练习")])}
    batch = MemoryExtractor(Model(), _Cards()).extract_batch(*_source("这次只检查标点"))
    assert batch.issues == () and batch.skipped[0].source_message_ids == ("m1",)
    assert batch.model_calls[-1].purpose == "memory_repair"


def test_missing_experience_keywords_are_repaired_before_the_card_is_discarded():
    from tests.prompt_fakes import memory_output
    text = "这台打印机改用灰度后条纹消失，只验证本次设备。"
    class Model:
        def chat(self, messages):
            keywords = ["打印机", "灰度", "条纹消失"] if "有限格式/来源修复" in messages[0]["content"] else []
            return {"text": memory_output([dict(kind="reusable_experience", summary="灰度解决条纹", content=text,
                confidence=.9, source_message_ids=["m1"], evidence_quotes={"m1": text},
                applicability="同一台打印机", action="改用灰度", outcome="条纹消失", limitations="仅本次设备",
                outcome_status="success", keywords=keywords)])}
    result = MemoryBatchReviewer(MemoryExtractor(Model(), _Cards())).review_batch(*_source(text))
    assert len(result.user_items) == 1 and result.extraction_issues == ()
    assert result.trace.model_calls[-1].purpose == "memory_repair"


def test_explicit_non_attribution_does_not_entail_a_negative_user_preference():
    from tests.prompt_fakes import memory_output
    text = "这是给同事买的咖啡，是他的口味，不能当我的口味。"
    class Model:
        def chat(self, messages):
            return {"text": memory_output([dict(kind="profile", subject="user", attribute="coffee_preference",
                summary="用户不爱咖啡", content="用户本人不喜欢咖啡", confidence=.9,
                source_message_ids=["m1"], evidence_quotes={"m1": text})])}
    result = MemoryBatchReviewer(MemoryExtractor(Model(), _Cards())).review_batch(*_source(text))
    assert result.user_items == ()
    assert "explicit_source_exclusion" in result.validations[0].validation_codes


def test_unknown_capability_alone_is_not_an_ongoing_commitment():
    from tests.prompt_fakes import memory_output
    text = "另一台机器能否读取这种格式，我还没有验证。"
    class Model:
        def chat(self, messages):
            return {"text": memory_output([dict(kind="ongoing_fact", subject="user", attribute="format_support",
                summary="格式支持待验证", content=text, confidence=.9,
                source_message_ids=["m1"], evidence_quotes={"m1": text})])}
    result = MemoryBatchReviewer(MemoryExtractor(Model(), _Cards())).review_batch(*_source(text))
    assert result.user_items == ()
    assert "unconfirmed_capability_only" in result.validations[0].validation_codes


def test_exclusion_guard_preserves_a_separate_explicit_personal_preference():
    from agent.core.memory.memory_claim_evidence import explicit_source_exclusion, unconfirmed_capability_only
    assert not explicit_source_exclusion("这是同事的口味，不能当我的口味；我本人喜欢浓茶。")
    assert not explicit_source_exclusion("我不喜欢浓茶。")
    assert not unconfirmed_capability_only("设备是否支持此格式还没验证；我已安排周五测试。")


def test_confirmed_team_schedule_is_repaired_as_scoped_fact_instead_of_personal_preference():
    from tests.prompt_fakes import memory_output
    text = "研究小组的内部讨论固定每周四举行。"
    class Model:
        def chat(self, messages):
            repair = "有限格式/来源修复" in messages[0]["content"]
            return {"text": memory_output([dict(kind="ongoing_fact" if repair else "stable_preference",
                subject="研究小组" if repair else "user", attribute="discussion_schedule", summary="研究小组讨论时间",
                content=text, confidence=.9, source_message_ids=["m1"], evidence_quotes={"m1": text})])}
    result = MemoryBatchReviewer(MemoryExtractor(Model(), _Cards())).review_batch(*_source(text))
    assert len(result.user_items) == 1
    assert result.user_items[0].kind.value == "ongoing_fact"
    assert result.user_items[0].slot_key == "ongoing_fact:研究小组:discussion_schedule"
    assert result.trace.model_calls[-1].purpose == "memory_repair"


def test_expressed_preferred_meeting_time_is_still_a_personal_preference():
    from tests.prompt_fakes import memory_output
    text = "我偏好把团队的内部讨论固定在周四。"
    class Model:
        def chat(self, messages):
            return {"text": memory_output([dict(kind="stable_preference", subject="user", attribute="preferred_meeting_time",
                summary="讨论时间偏好", content=text, confidence=.9, source_message_ids=["m1"], evidence_quotes={"m1": text})])}
    result = MemoryBatchReviewer(MemoryExtractor(Model(), _Cards())).review_batch(*_source(text))
    assert len(result.user_items) == 1 and result.user_items[0].kind.value == "stable_preference"


def test_grouping_budgets_the_full_admission_prompt_with_existing_cards():
    from agent.core.perception.token_estimator import SafeEstimator
    from tests.core.memory.test_memory_extractor import _StoredCards, _stored_card
    cards = [_stored_card(str(index), "以后处理文字时优先提供简短答复。" * 10,
                          kind="stable_preference", slot_key=f"stable_preference:user:response_{index}",
                          summary="长期答复约定") for index in range(6)]
    class Model:
        inputs = []
        def chat(self, messages):
            self.inputs.append(messages)
            return {"text": '{"cards":[],"skipped":[]}'}
    model = Model()
    text = "只是临时练习记录。" * 1000 + "末尾仍然只是临时记录。"
    result = MemoryExtractor(model, _StoredCards(cards), input_token_budget=16000).extract_batch(*_source(text))
    assert result.issues == ()
    assert all(SafeEstimator().estimate_messages(messages) <= 16000 for messages in model.inputs)
    assert any("末尾仍然只是临时记录。" in messages[-1]["content"] for messages in model.inputs)


def test_repeated_tool_lines_keep_unique_middle_and_tail_without_repeated_model_batches():
    from agent.types.conversation import ToolCall
    turns, sessions = _source("读取本次已归档诊断日志。")
    raw = "重复诊断行。\n" * 1000 + "中段独有记录。\n" + "重复诊断行。\n" * 1000 + "末尾独有记录。"
    sessions[0].tool_calls.append(ToolCall(tool_call_id="tool1", tool_name="read", arguments={}, result_text=raw))
    turns = (turns[0].model_copy(update={"tool_call_ids": ("tool1",)}),)
    class Model:
        inputs = []
        def chat(self, messages):
            self.inputs.append(messages[-1]["content"])
            return {"text": '{"cards":[],"skipped":[]}'}
    model = Model()
    result = MemoryExtractor(model, _Cards()).extract_batch(turns, sessions)
    assert result.issues == () and len(model.inputs) == 2
    assert all("中段独有记录。" in text and "末尾独有记录。" in text for text in model.inputs)
    assert all("连续重复 1000 次" in text for text in model.inputs)
    assert sessions[0].tool_calls[0].result_text == raw
