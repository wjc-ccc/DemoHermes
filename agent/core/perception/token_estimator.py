"""One estimate boundary for messages and native tool definitions."""
from __future__ import annotations

import json
import logging
from typing import Sequence

logger = logging.getLogger(__name__)


class ConservativeEstimator:
    """UTF-8 bytes are a conservative fallback, including protocol/image cost."""

    def estimate_messages(self, messages: Sequence[dict]) -> int:
        return sum(len(json.dumps(message, ensure_ascii=False).encode("utf-8"))
                   + 16 + 8192 * len(message.get("_images", [])) for message in messages)


class SafeEstimator:
    """After a tokenizer failure, every subsequent estimate uses the same fallback."""

    def __init__(self, delegate=None):
        self.delegate = delegate or ConservativeEstimator()
        self.fallback = ConservativeEstimator()
        self.failed = False

    def estimate_messages(self, messages: Sequence[dict]) -> int:
        if not self.failed:
            try:
                value = int(self.delegate.estimate_messages(messages))
                if value < 0:
                    raise ValueError("negative token estimate")
                return value
            except Exception:
                self.failed = True
                logger.warning("Token estimator unavailable; using conservative UTF-8 cost")
        return self.fallback.estimate_messages(messages)

    def estimate_tools(self, tools: Sequence[dict]) -> int:
        if not tools:
            return 0
        # This envelope is ONLY an estimator seam, never a model prompt.
        return self.estimate_messages([{"role": "system", "content": json.dumps(tools, ensure_ascii=False)}])
