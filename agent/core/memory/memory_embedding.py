"""后台卡片维护使用的可选、有界向量适配器，不参与在线召回。"""
from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from agent.types.memory.categories import UserCardKind, UserCardStatus


class MemoryEmbeddingError(ValueError):
    """向量服务不可用或数据无效时，由维护层记录后继续主流程。"""


def _semantic_fields(card) -> dict:
    fields = ("title", "summary", "content", "applicability", "action", "outcome",
              "limitations", "outcome_status", "valid_from", "valid_until")
    result = {}
    for name in fields:
        value = getattr(card, name, None)
        if isinstance(value, datetime):
            value = value.isoformat()
        result[name] = getattr(value, "value", value)
    return result


def experience_semantic_hash(card) -> str:
    """为指导内容生成指纹，不包含统计、证据、关键词和修订号。"""
    raw = json.dumps(_semantic_fields(card), ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def experience_embedding_text(card) -> str:
    fields = _semantic_fields(card)
    # 有效期由代码核验，避免时间戳主导语义相似度。
    fields.pop("valid_from")
    fields.pop("valid_until")
    return json.dumps(fields, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def validate_vector(value, *, dimensions: int | None = None) -> tuple[float, ...]:
    if not isinstance(value, (tuple, list)) or not value or len(value) > 65_536:
        raise MemoryEmbeddingError("embedding must be a nonempty bounded vector")
    if dimensions is not None and len(value) != dimensions:
        raise MemoryEmbeddingError("embedding dimension mismatch")
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
        raise MemoryEmbeddingError("embedding values must be finite numbers")
    try:
        vector = tuple(float(item) for item in value)
    except OverflowError as exc:
        raise MemoryEmbeddingError("embedding values must be finite numbers") from exc
    if any(not math.isfinite(item) for item in vector):
        raise MemoryEmbeddingError("embedding values must be finite numbers")
    if math.hypot(*vector) == 0:
        raise MemoryEmbeddingError("embedding must have nonzero norm")
    return vector


def cosine_similarity(left, right) -> float:
    left = validate_vector(left)
    right = validate_vector(right, dimensions=len(left))
    # 先缩放再归一化，避免有限大数的范数溢出。
    left_scale, right_scale = max(map(abs, left)), max(map(abs, right))
    left = tuple(value / left_scale for value in left)
    right = tuple(value / right_scale for value in right)
    left_norm, right_norm = math.hypot(*left), math.hypot(*right)
    score = math.fsum((a / left_norm) * (b / right_norm) for a, b in zip(left, right))
    return max(-1.0, min(1.0, score))


class OpenAIEmbedding:
    """复用 httpx 的 OpenAI 兼容适配器；每次只发送一次请求。"""

    def __init__(self, base_url: str, api_key: str, model: str, *,
                 batch_size: int = 8, input_tokens: int = 4096,
                 timeout_seconds: float = 10.0, dimensions: int | None = None,
                 client=None):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise MemoryEmbeddingError("invalid embedding base URL")
        if not api_key.strip() or not model.strip():
            raise MemoryEmbeddingError("embedding credentials and model are required")
        if (isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1
                or isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or input_tokens < 1
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0
                or dimensions is not None and (isinstance(dimensions, bool)
                    or not isinstance(dimensions, int) or dimensions < 1 or dimensions > 65_536)):
            raise MemoryEmbeddingError("invalid embedding limits")
        self.endpoint = base_url.rstrip("/")
        if not self.endpoint.endswith("/embeddings"):
            self.endpoint += "/embeddings"
        self.model = model.strip()
        self._api_key = api_key.strip()
        self.batch_size = batch_size
        self.input_tokens = input_tokens
        self.timeout_seconds = timeout_seconds
        self.dimensions = dimensions
        self._client = client

    @classmethod
    def from_env(cls, *, config=None, environ: Mapping[str, str] | None = None, client=None):
        """不猜测提供商或模型；缺少任一配置时完全禁用 HTTP 请求。"""
        env = os.environ if environ is None else environ
        values = [env.get("MEMORY_EMBEDDING_" + name, "").strip()
                  for name in ("BASE_URL", "API_KEY", "MODEL")]
        if not all(values):
            return None
        return cls(*values, batch_size=getattr(config, "embedding_batch_size", 8),
                   input_tokens=getattr(config, "embedding_input_tokens", 4096), client=client)

    def embed_batch(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        texts = tuple(texts)
        if not texts:
            return ()
        if (len(texts) > self.batch_size or any(not isinstance(text, str) or not text for text in texts)
                or sum(len(text.encode("utf-8")) for text in texts) > self.input_tokens):
            raise MemoryEmbeddingError("embedding input budget exceeded")
        sender = self._client or httpx
        try:
            response = sender.post(self.endpoint,
                                   headers={"Authorization": "Bearer " + self._api_key},
                                   json={"model": self.model, "input": list(texts)},
                                   timeout=self.timeout_seconds)
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            # 提供商响应及请求头可能包含私有输入或密钥，不将其写入日志。
            raise MemoryEmbeddingError("embedding request failed") from exc
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or len(data) != len(texts):
            raise MemoryEmbeddingError("embedding response count mismatch")
        ordered = {}
        dimensions = self.dimensions
        for row in data:
            if not isinstance(row, dict):
                raise MemoryEmbeddingError("invalid embedding row")
            index = row.get("index")
            if (isinstance(index, bool) or not isinstance(index, int)
                    or not 0 <= index < len(texts) or index in ordered):
                raise MemoryEmbeddingError("invalid embedding response index")
            vector = validate_vector(row.get("embedding"), dimensions=dimensions)
            dimensions = len(vector)
            ordered[index] = vector
        self.dimensions = dimensions
        return tuple(ordered[index] for index in range(len(texts)))


def cached_embedding(card, model: str, *, dimensions: int | None = None):
    cache = card.metadata.get("embedding")
    if (not isinstance(cache, Mapping) or cache.get("model") != model
            or cache.get("content_hash") != card.content_hash
            or cache.get("semantic_hash") != experience_semantic_hash(card)):
        return None
    try:
        return validate_vector(cache.get("vector"), dimensions=dimensions)
    except (MemoryEmbeddingError, TypeError, OverflowError):
        return None


def valid_experience(card, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    return (card.kind is UserCardKind.REUSABLE_EXPERIENCE
            and card.status is UserCardStatus.ACTIVE
            and getattr(card, "recall_enabled", True)
            and card.metadata.get("merge_valid") is not False
            and (card.valid_from is None or card.valid_from <= now)
            and (card.valid_until is None or card.valid_until > now))


@dataclass(frozen=True)
class EmbeddingCandidateItem:
    card: object
    similarity: float


def embedding_candidates(anchor, cards, model: str, *, top_k: int = 5,
                         min_similarity: float = 0.0, now: datetime | None = None):
    """向量只提出同一 scope 的候选，实际卡片关系必须由判官决定。"""
    if not valid_experience(anchor, now=now):
        return ()
    vector = cached_embedding(anchor, model)
    if vector is None:
        return ()
    ranked = []
    for card in cards:
        if (card.item_id == anchor.item_id or card.scope.storage_key != anchor.scope.storage_key
                or not valid_experience(card, now=now)):
            continue
        candidate = cached_embedding(card, model, dimensions=len(vector))
        if candidate is None:
            continue
        score = cosine_similarity(vector, candidate)
        if score > max(0.0, min_similarity):
            ranked.append(EmbeddingCandidateItem(card, score))
    ranked.sort(key=lambda item: (-item.similarity, item.card.item_id))
    return tuple(ranked[:max(0, min(top_k, 5))])
