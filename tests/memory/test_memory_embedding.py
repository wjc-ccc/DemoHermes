from datetime import datetime, timedelta, timezone

import httpx
import pytest

from agent.core.memory.memory_embedding import (
    MemoryEmbeddingError, OpenAIEmbedding, cached_embedding, cosine_similarity,
    embedding_candidates, experience_semantic_hash, validate_vector,
)
from agent.types.memory.items import UserMemoryItem, content_sha256
from agent.types.memory.scope import MemoryScope


def card(identity="a", *, scope="u", vector=(1.0, 0.0), **changes):
    text = "Only retry safe read requests."
    item = UserMemoryItem(
        item_id=identity, kind="reusable_experience", scope=MemoryScope(user_id=scope),
        source_type="test", source_ref="source", source_authority="tool_verified",
        content=text, content_hash=content_sha256(text), applicability="read requests",
        action="retry once", outcome="succeeded", limitations="never retry writes",
        outcome_status="success", **changes,
    )
    return item.model_copy(update={"metadata": {"embedding": {
        "model": "embed-test", "content_hash": item.content_hash,
        "semantic_hash": experience_semantic_hash(item), "vector": list(vector)}}})


def test_embedding_configuration_requires_all_explicit_env_fields():
    assert OpenAIEmbedding.from_env(environ={}) is None
    assert OpenAIEmbedding.from_env(environ={"MEMORY_EMBEDDING_API_KEY": "test"}) is None
    adapter = OpenAIEmbedding.from_env(environ={
        "MEMORY_EMBEDDING_BASE_URL": "https://embedding.invalid/v1",
        "MEMORY_EMBEDDING_API_KEY": "test", "MEMORY_EMBEDDING_MODEL": "embed-test"})
    assert adapter.endpoint == "https://embedding.invalid/v1/embeddings"
    with pytest.raises(MemoryEmbeddingError):
        OpenAIEmbedding("https://secret@embedding.invalid/v1", "test", "test")


def test_embedding_batch_preserves_index_order_and_checks_output_dimensions():
    requests = []

    def send(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [
            {"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}]})

    with httpx.Client(transport=httpx.MockTransport(send)) as client:
        adapter = OpenAIEmbedding("https://embedding.invalid/v1", "test", "embed-test", client=client)
        assert adapter.embed_batch(["first", "second"]) == ((1.0, 0.0), (0.0, 1.0))
        assert adapter.dimensions == 2
        assert requests[0].headers["Authorization"] == "Bearer test"
        assert adapter.embed_batch([]) == ()
        assert len(requests) == 1


@pytest.mark.parametrize("vector", [[], [0, 0], [float("nan"), 1], [float("inf")],
                                   [True, 1], ["1", 2]])
def test_embedding_rejects_invalid_vectors(vector):
    with pytest.raises(MemoryEmbeddingError):
        validate_vector(vector)


@pytest.mark.parametrize("data", [
    [{"index": 0, "embedding": [1, 0]}, {"index": 0, "embedding": [1, 0]}],
    [{"index": 0, "embedding": [1, 0]}, {"index": 1, "embedding": [1]}],
    [{"index": True, "embedding": [1, 0]}, {"index": 1, "embedding": [0, 1]}],
    [{"index": 0, "embedding": [1, 0]}],
])
def test_embedding_rejects_malformed_provider_batch(data):
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": data}))) as client:
        adapter = OpenAIEmbedding("https://embedding.invalid/v1", "test", "embed-test", client=client)
        with pytest.raises(MemoryEmbeddingError):
            adapter.embed_batch(["one", "two"])


def test_embedding_input_budget_rejects_before_http():
    def forbidden(_):
        pytest.fail("超预算请求不应发送")

    with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
        adapter = OpenAIEmbedding("https://embedding.invalid/v1", "test", "embed-test",
                                  batch_size=1, input_tokens=3, client=client)
        with pytest.raises(MemoryEmbeddingError, match="budget"):
            adapter.embed_batch(["four"])
        with pytest.raises(MemoryEmbeddingError, match="budget"):
            adapter.embed_batch(["a", "b"])


def test_embedding_cache_ignores_statistics_but_checks_guidance_and_model():
    original = card()
    changed_stats = original.model_copy(update={"revision": 12, "used_count": 2,
                                                "metadata": {**original.metadata, "stats_model_key": "new"}})
    assert experience_semantic_hash(original) == experience_semantic_hash(changed_stats)
    assert cached_embedding(changed_stats, "embed-test") == (1.0, 0.0)
    assert cached_embedding(original, "other-model") is None
    changed_boundary = original.model_copy(update={"limitations": "exclude all network writes"})
    assert cached_embedding(changed_boundary, "embed-test") is None


def test_cosine_candidates_are_top_five_valid_same_scope_and_dimension():
    now = datetime.now(timezone.utc)
    anchor = card()
    cards = [card(str(index), vector=(1.0, index / 10)) for index in range(8)]
    cards += [card("other-user", scope="other"), card("expired", valid_until=now-timedelta(seconds=1)),
              card("disabled", recall_enabled=False), card("wrong-dim", vector=(1, 0, 0)),
              card("opposite", vector=(-1, 0))]
    matches = embedding_candidates(anchor, cards, "embed-test", top_k=20, now=now)
    assert [entry.card.item_id for entry in matches] == ["0", "1", "2", "3", "4"]
    assert cosine_similarity([1e308, 0], [1e308, 0]) == pytest.approx(1)
