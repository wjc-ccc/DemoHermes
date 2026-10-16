"""
DoubaoModel — 字节方舟 Agent Plan（Anthropic 协议）

密钥和地址来自 DOUBAO_API_KEY / DOUBAO_BASE_URL，模型名来自 MODEL。
请求格式复用 ClaudeModel，不走 DeepSeek 的 OpenAI 兼容客户端。
"""
from __future__ import annotations

from agent.core.model.claude import ClaudeModel
from agent.core.provider import DOUBAO_API_KEY, DOUBAO_BASE_URL

_DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/plan"


class DoubaoModel(ClaudeModel):
    name = "doubao"

    def __init__(self, model_name: str | None = None):
        super().__init__(
            model_name=model_name,
            api_key=DOUBAO_API_KEY or "",
            base_url=DOUBAO_BASE_URL or _DEFAULT_BASE_URL,
        )
