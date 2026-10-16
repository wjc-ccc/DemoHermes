from agent.core.storage.sqlite.runtime_storage import RuntimeStorage


def test_job_failure_history_keeps_actual_auxiliary_calls_after_retry_and_completion(tmp_path):
    storage=RuntimeStorage.open(tmp_path/"agent.db")
    storage.jobs.enqueue(job_type="memory.extract",handler_version="fixture",input={},
                         dedupe_key="fixture",available_at=1000)
    first=storage.jobs.claim(job_type="memory.extract",handler_version="fixture",worker_id="w",now=1000,lease_ms=10000)
    storage.jobs.fail(first,"invalid JSON",available_at=1100,now=1001,
        details={"model_calls":[{"input_token_count":13,"output_token_count":2}],"raw_response":"not-json"})
    second=storage.jobs.claim(job_type="memory.extract",handler_version="fixture",worker_id="w",now=1100,lease_ms=10000)
    storage.jobs.complete(second,{"review":{"accepted":True}},now=1101)
    restored=storage.jobs.get(first.id)
    assert restored.status=="completed"
    assert restored.result["review"]["accepted"]
    assert restored.result["failure_history"][0]["details"]["raw_response"]=="not-json"
    assert restored.result["failure_history"][0]["details"]["model_calls"][0]["input_token_count"]==13
