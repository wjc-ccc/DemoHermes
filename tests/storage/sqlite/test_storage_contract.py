import json
from agent.storage.sqlite.runtime_storage import RuntimeStorage
from agent.types.conversation import Message, Session, ToolCall
from agent.eval.datasets.memory_dataset import load_defaults


def test_new_database_uses_content_names_and_removes_unused_document_fields(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    with storage.db.read() as conn:
        for table, field in {"sessions": "session_json", "turns": "turn_json",
                             "messages": "message_json", "tool_calls": "tool_call_json",
                             "eval_datasets": "dataset_json", "eval_cases": "case_json",
                             "eval_questions": "question_json"}.items():
            columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
            assert field in columns
            assert "payload_json" not in columns
        documents = {row[1] for row in conn.execute('PRAGMA table_info(context_documents)')}
        assert not {"revision_ref", "tokenizer_name"} & documents
        turns = {row[1] for row in conn.execute('PRAGMA table_info(turns)')}
        assert {"finished_at", "error_text"} <= turns
        questions = {row[1] for row in conn.execute('PRAGMA table_info(eval_questions)')}
        assert {"question_schema_version", "history_end_turn_id"} <= questions
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_conversation_count_is_derived_and_tool_fields_have_one_canonical_name():
    session = Session(message_count=999)
    session.add_message(Message(role="user", content_text="first"))
    session.add_message(Message(role="assistant", content_text="second"))
    session.messages.pop()
    assert session.message_count == 1
    assert "message_count" not in session.model_dump()
    legacy = ToolCall.model_validate({"durations_ms": 123, "tool_call_result": {"value": 2}})
    wire = legacy.model_dump(mode="json")
    assert wire["duration_ms"] == 123 and wire["result_data"] == {"value": 2}
    assert not {"durations_ms", "tool_call_result"} & wire.keys()


def test_original_dataset_fingerprints_stay_exactly_the_same():
    first, second = load_defaults()
    assert first.fingerprint() == "442caecb6785ab3730b33990bdd57a2ad86fb51bd744ab55409beed37b4734d6"
    assert second.fingerprint() == "2023ba2e25c18d01b8422d8e1c19c5765b6dba8ab8bdfcfb311a3a60f31caa31"
