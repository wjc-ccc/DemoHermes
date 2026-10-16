import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from agent.core.memory.config import MemoryExperienceConfig
from agent.core.memory.memory_consolidation import MemoryConsolidator
from agent.core.memory.memory_embedding import cached_embedding
from agent.core.storage.sqlite.runtime_storage import RuntimeStorage
from agent.types.memory.items import UserMemoryItem, content_sha256
from agent.types.memory.scope import MemoryScope
from agent.types.memory.source import EvidenceRef
from agent.types.model import ModelResponse


SCOPE = MemoryScope(user_id="u")
DRAFT = {"title": "safe reads", "summary": "Retry safe reads once.",
         "content": "Retry safe reads once, never writes.", "applicability": "safe reads",
         "action": "retry once", "outcome": "succeeded", "limitations": "never writes",
         "outcome_status": "success"}


def seed(storage, identities=("a", "b", "c"), *, scope=SCOPE):
    cards = []
    for index, identity in enumerate(identities):
        content = f"Read attempt {index}: retry succeeded; never retry writes."
        cards.append(storage.memory.put(UserMemoryItem(
            item_id=identity, kind="reusable_experience", scope=scope,
            source_type="test", source_ref="source:"+identity, source_authority="tool_verified",
            summary=content, content=content, content_hash=content_sha256(content),
            applicability="safe reads", action="retry once", outcome="succeeded",
            limitations="never writes", outcome_status="success", keywords=("HTTP", "GET", "retry"),
            evidence=(EvidenceRef(source_type="message", source_id="message:"+identity),),
            used_count=6, judge_count=6, effective_count=6, helpful_count=6,
        )))
    return cards


class Embedding:
    model = "embed-test"

    def __init__(self, storage, *, fail=False, hook=None):
        self.storage, self.fail, self.hook = storage, fail, hook
        self.calls = []

    def embed_batch(self, texts):
        assert not self.storage.db._write_lock._is_owned()
        self.calls.append(tuple(texts))
        if self.hook:
            self.hook()
        if self.fail:
            raise RuntimeError("offline")
        return tuple((1.0, 0.1) for _ in texts)


class Judge:
    id = "judge-v1"
    is_usable = True

    def __init__(self, storage, relations=None, *, faithful=True, hook=None, fail=False):
        self.storage, self.relations = storage, relations or {}
        self.faithful, self.hook, self.fail = faithful, hook, fail
        self.relation_calls, self.merge_calls = [], []

    def judge_relations(self, anchor, candidates):
        assert not self.storage.db._write_lock._is_owned()
        self.relation_calls.append((anchor.item_id, tuple(card.item_id for card in candidates)))
        if self.fail:
            raise RuntimeError("offline")
        return tuple(SimpleNamespace(item_id=card.item_id, relation=self.relations.get(
            frozenset((anchor.item_id, card.item_id)), "complementary")) for card in candidates)

    def judge_merge(self, cards, draft):
        assert not self.storage.db._write_lock._is_owned()
        self.merge_calls.append((cards, draft))
        if self.hook:
            self.hook()
        return self.faithful


class Model:
    name = "fake"
    model_name = "merge-test"

    def __init__(self, storage, *, hook=None, fail=False):
        self.storage, self.hook, self.fail = storage, hook, fail
        self.calls = []

    def chat(self, messages, **kwargs):
        assert not self.storage.db._write_lock._is_owned()
        self.calls.append((messages, kwargs))
        if self.hook:
            self.hook()
        if self.fail:
            raise RuntimeError("offline")
        return ModelResponse(text=json.dumps(DRAFT), usage={"input_tokens": 1, "output_tokens": 1})


def maintenance(storage, *, judge=None, embedding=None, model=None, config=None, **kwargs):
    return MemoryConsolidator(storage, embedding or Embedding(storage), judge or Judge(storage),
                              model or Model(storage), config=config, **kwargs)


def test_unconfigured_embeddings_are_free_and_do_not_change_cards(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    seed(storage)
    result = MemoryConsolidator(storage, judge=Judge(storage), model=Model(storage)).run_once()
    assert result.embedded == result.merged == 0
    assert storage.memory.get("a").revision == 1


def test_scene_requires_full_pair_compatibility_and_fidelity_then_starts_at_zero(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    parents = seed(storage)
    judge, model = Judge(storage), Model(storage)
    worker = maintenance(storage, judge=judge, model=model, id_factory=lambda: "scene")
    result = worker.run_once(SCOPE)
    assert result.embedded == 3 and result.merged == 1
    assert len(judge.relation_calls) == 2
    assert len(judge.merge_calls) == len(model.calls) == 1
    assert model.calls[0][1]["max_tokens"] == 1024
    scene = storage.memory.get("scene")
    assert scene.metadata["experience_layer"] == "scene"
    assert {ref["card_id"] for ref in scene.metadata["merged_from"]} == {"a", "b", "c"}
    assert (scene.used_count, scene.judge_count, scene.helpful_count) == (0, 0, 0)
    for parent in parents:
        stored = storage.memory.get(parent.item_id)
        assert stored.content == parent.content and stored.helpful_count == 6
        assert cached_embedding(stored, "embed-test") is not None


def test_anchor_similarity_cannot_merge_incompatible_other_pair(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    seed(storage)
    judge = Judge(storage, {frozenset(("b", "c")): "conflict"})
    model = Model(storage)
    result = maintenance(storage, judge=judge, model=model).run_once(SCOPE)
    assert result.merged == 0 and not model.calls and not judge.merge_calls


def test_duplicate_only_adds_sources_and_preserves_bodies_and_counts(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    parents = seed(storage, ("a", "b"))
    judge = Judge(storage, {frozenset(("a", "b")): "duplicate"})
    model = Model(storage)
    worker = maintenance(storage, judge=judge, model=model)
    first = worker.run_once(SCOPE)
    second = worker.run_once(SCOPE)
    assert first.duplicates == 1 and second.relations == 0
    assert len(judge.relation_calls) == 1 and not model.calls
    assert len(storage.memory.get("a").evidence) == 2
    assert storage.memory.get("a").recall_enabled
    assert not storage.memory.get("b").recall_enabled
    for parent in parents:
        current = storage.memory.get(parent.item_id)
        assert current.content == parent.content and current.helpful_count == 6
        assert current.status.value == "active"
    from agent.core.storage.sqlite.memory_retrieval import MemoryRetriever
    retriever = MemoryRetriever(storage.db)
    assert {hit.item_id for hit in retriever.recall(SCOPE, "HTTP GET retry")} == {"a"}
    assert "card:b" in {hit.reference for hit in retriever.search(SCOPE, "retry", source="card")}
    storage.card_decisions.rebuild_stats("new-main-model")
    assert not storage.memory.get("b").recall_enabled


def test_rejected_summary_is_cached_by_parent_version_without_repeating_generation(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    seed(storage)
    judge, model = Judge(storage, faithful=False), Model(storage)
    worker = maintenance(storage, judge=judge, model=model)
    worker.run_once(SCOPE)
    worker.run_once(SCOPE)
    assert len(model.calls) == 1 and len(judge.merge_calls) == 1
    assert not storage.memory.get("scene")


def test_relation_failure_is_cached_without_retrying_every_maintenance_poll(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    seed(storage, ("a", "b"))
    judge = Judge(storage, fail=True)
    worker = maintenance(storage, judge=judge)
    assert worker.run_once(SCOPE).errors == 1
    worker.run_once(SCOPE)
    assert len(judge.relation_calls) == 1


def test_parent_guidance_change_during_generation_prevents_scene_commit(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    seed(storage)

    def change_parent():
        card = storage.memory.get("b")
        storage.memory.put(card.model_copy(update={"limitations": "only local files",
                                                   "revision": card.revision + 1}),
                           expected_revision=card.revision)

    result = maintenance(storage, model=Model(storage, hook=change_parent)).run_once(SCOPE)
    assert result.merged == 0
    with storage.db.read() as conn:
        assert not conn.execute("SELECT 1 FROM memory_cards WHERE source_type='memory_consolidation'").fetchone()


def test_parent_statistics_change_during_generation_does_not_invalidate_scene(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    seed(storage)

    def change_statistics():
        with storage.db.write() as conn:
            conn.execute("UPDATE memory_cards SET used_count=used_count+1 WHERE id='b'")

    result = maintenance(storage, model=Model(storage, hook=change_statistics)).run_once(SCOPE)
    assert result.merged == 1
    assert storage.memory.get("b").used_count == 7


def test_embedding_failures_have_persistent_bounded_backoff(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    seed(storage, ("a",))
    now = [datetime.now(timezone.utc)]
    embedding = Embedding(storage, fail=True)
    worker = maintenance(storage, embedding=embedding, clock=lambda: now[0])
    worker.run_once()
    worker.run_once()
    assert len(embedding.calls) == 1
    for _ in range(4):
        now[0] += timedelta(minutes=10)
        worker.run_once()
    assert len(embedding.calls) == 3
    assert storage.memory.get("a").metadata["embedding_attempt"]["attempts"] == 3


def test_embedding_batch_and_input_and_merge_budgets_bound_paid_work(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    seed(storage, tuple(str(index) for index in range(10)))
    embedding, model = Embedding(storage), Model(storage)
    config = replace(MemoryExperienceConfig(), embedding_batch_size=3, merge_input_tokens=1)
    result = maintenance(storage, embedding=embedding, model=model, config=config).run_once(SCOPE)
    assert result.embedded == 3 and len(embedding.calls) == 1
    assert not model.calls and result.merged == 0


def test_expired_or_changed_parent_scene_is_not_embedding_candidate(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    seed(storage)
    worker = maintenance(storage, id_factory=lambda: "scene")
    assert worker.run_once(SCOPE).merged == 1
    with storage.db.write() as conn:
        conn.execute("UPDATE memory_cards SET recall_enabled=0 WHERE id='a'")
    assert "scene" not in {card.item_id for card in worker._cards(SCOPE)}


def test_scene_is_embedded_once_but_never_reconsolidated_with_its_own_parents(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    seed(storage)
    judge, model, embedding = Judge(storage), Model(storage), Embedding(storage)
    worker = maintenance(storage, judge=judge, model=model, embedding=embedding,
                         id_factory=lambda: "scene")
    assert worker.run_once(SCOPE).merged == 1
    assert worker.run_once(SCOPE).embedded == 1
    worker.run_once(SCOPE)
    assert len(model.calls) == 1 and len(judge.relation_calls) == 2
    assert len(embedding.calls) == 2
    assert cached_embedding(storage.memory.get("scene"), "embed-test") is not None


def test_generation_failure_is_terminal_for_the_same_parent_versions(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    seed(storage)
    model = Model(storage, fail=True)
    worker = maintenance(storage, model=model)
    assert worker.run_once(SCOPE).errors == 1
    worker.run_once(SCOPE)
    assert len(model.calls) == 1
    with storage.db.read() as conn:
        assert conn.execute("SELECT count(*) FROM memory_card_decisions WHERE "
                            "json_extract(result_json,'$.judgment.status')='failed'").fetchone()[0] == 1


def test_changed_guidance_cannot_receive_a_vector_computed_for_old_guidance(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    seed(storage, ("a",))

    def change():
        card = storage.memory.get("a")
        storage.memory.put(card.model_copy(update={"limitations": "never retry network calls",
                                                   "revision": card.revision + 1}),
                           expected_revision=card.revision)

    result = maintenance(storage, embedding=Embedding(storage, hook=change)).run_once(SCOPE)
    assert result.embedded == 0
    assert cached_embedding(storage.memory.get("a"), "embed-test") is None


def test_embedding_reservation_prevents_a_second_worker_from_paying_for_the_same_card(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    seed(storage, ("a",))
    second_embedding = Embedding(storage)
    second = maintenance(storage, embedding=second_embedding)
    first = maintenance(storage, embedding=Embedding(storage, hook=lambda: second.run_once(SCOPE)))
    assert first.run_once(SCOPE).embedded == 1
    assert not second_embedding.calls


def test_embedding_backlog_progresses_past_the_scan_window(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    seed(storage, tuple(str(index) for index in range(7)))
    embedding = Embedding(storage)
    config = replace(MemoryExperienceConfig(), maintenance_scan_limit=3, embedding_batch_size=3)
    worker = MemoryConsolidator(storage, embedding=embedding, config=config)
    assert [worker.run_once(SCOPE).embedded for _ in range(3)] == [3, 3, 1]
    assert all(cached_embedding(storage.memory.get(str(index)), "embed-test") is not None
               for index in range(7))


def test_merge_output_budget_prevents_fidelity_call_and_new_card(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    seed(storage)
    judge, model = Judge(storage), Model(storage)
    config = replace(MemoryExperienceConfig(), merge_output_tokens=1)
    result = maintenance(storage, judge=judge, model=model, config=config).run_once(SCOPE)
    assert len(model.calls) == 1 and not judge.merge_calls
    assert result.errors == 1 and result.merged == 0
