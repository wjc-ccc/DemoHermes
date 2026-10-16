"""One estimate boundary for messages and native tool definitions."""
from __future__ import annotations

import json
import logging
from typing import Sequence

from agent.core.perception.config import TokenEstimateConfig

logger = logging.getLogger(__name__)


class ConservativeEstimator:
    """UTF-8 bytes are a conservative fallback, including protocol/image cost."""

    def __init__(self, config: TokenEstimateConfig | None = None):
        self.config = config or TokenEstimateConfig()

    def estimate_messages(self, messages: Sequence[dict]) -> int:
        overhead = self.config.message_overhead_bytes
        image_cost = self.config.image_token_cost
        return sum(len(json.dumps(message, ensure_ascii=False).encode("utf-8"))
                   + overhead + image_cost * len(message.get("_images", [])) for message in messages)


class SafeEstimator:
    """After a tokenizer failure, every subsequent estimate uses the same fallback."""

    def __init__(self, delegate=None, config: TokenEstimateConfig | None = None):
        self.config = config or TokenEstimateConfig()
        self.delegate = delegate or ConservativeEstimator(self.config)
        self.fallback = ConservativeEstimator(self.config)
        self.failed = False

    def estimate_messages(self, messages: Sequence[dict]) -> int:
        if not self.failed:
            try:
                value = int(self.delegate.estimate_messages(messages))
                if value < 0:
                    raise ValueError("negative token estimate")
                return value
            except Exception:
                # 正式 token 测评要求计量失败关闭，不能把字节估算当成 tokenizer 的真实结果。
                if getattr(self.delegate, "fail_closed", False):
                    raise
                self.failed = True
                logger.warning("Token estimator unavailable; using conservative UTF-8 cost")
        return self.fallback.estimate_messages(messages)

    def estimate_tools(self, tools: Sequence[dict]) -> int:
        if not tools:
            return 0
        # This envelope is ONLY an estimator seam, never a model prompt.
        return self.estimate_messages([{"role": "system", "content": json.dumps(tools, ensure_ascii=False)}])
