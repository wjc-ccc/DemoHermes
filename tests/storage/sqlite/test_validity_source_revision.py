from datetime import datetime,timezone
import pytest
from agent.core.storage.sqlite.runtime_storage import RuntimeStorage
from agent.core.storage.sqlite.memory_review_committer import SqliteMemoryReviewCommitter
from agent.core.storage.sqlite.errors import IntegrityError
from tests.core.memory.test_memory_extractor import _stored_card


def test_validity_dependency_checks_scope_revision_and_exact_expiry(tmp_path):
    storage=RuntimeStorage.open(tmp_path/"agent.db")
    expiry=datetime(2030,1,3,tzinfo=timezone.utc)
    parent=_stored_card("parent","阶段安排",kind="ongoing_fact",slot_key="ongoing_fact:user:period")
    parent=storage.memory.put(parent.model_copy(update={"valid_until":expiry}))
    child=_stored_card("child","阶段复习",kind="ongoing_fact",slot_key="ongoing_fact:user:focus")
    child=child.model_copy(update={"valid_until":expiry,"metadata":{"validity_source":{"card_id":parent.item_id,"revision":parent.revision}}})
    with storage.db.read() as conn:
        SqliteMemoryReviewCommitter._validate_validity_source(conn,child)
        with pytest.raises(IntegrityError):
            SqliteMemoryReviewCommitter._validate_validity_source(conn,child.model_copy(update={"valid_until":None}))
    storage.memory.put(parent.model_copy(update={"revision":parent.revision+1}),expected_revision=parent.revision)
    with storage.db.read() as conn:
        with pytest.raises(IntegrityError):
            SqliteMemoryReviewCommitter._validate_validity_source(conn,child)


def test_unbounded_validity_dependency_still_checks_exact_parent_revision(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    parent = storage.memory.put(_stored_card("unbounded", "采购阶段", kind="ongoing_fact",
                                            slot_key="ongoing_fact:purchase:budget"))
    child = _stored_card("child", "采购内存", kind="ongoing_fact", slot_key="ongoing_fact:purchase:ram")
    child = child.model_copy(update={"metadata": {"validity_source": {"card_id": parent.item_id, "revision": parent.revision}}})
    with storage.db.read() as conn:
        SqliteMemoryReviewCommitter._validate_validity_source(conn, child)
    storage.memory.put(parent.model_copy(update={"revision": parent.revision + 1}), expected_revision=parent.revision)
    with storage.db.read() as conn:
        with pytest.raises(IntegrityError):
            SqliteMemoryReviewCommitter._validate_validity_source(conn, child)
