"""
DeepSeekModel — DeepSeek 模型接入（OpenAI 兼容协议）

支持：
    - 普通文本对话
    - function calling（tools 参数透传，响应解析为 ToolCallRequest）
    - 参数 JSON 解析失败兜底（保留 arguments_raw，按空参数处理并记日志）
"""
from __future__ import annotations

import json
import logging
from typing import Any

from openai import OpenAI

from .baseStructure import BaseModel, ModelResponse, ToolCallRequest
from ..provider import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, MODEL

logger = logging.getLogger(__name__)


class DeepSeekModel(BaseModel):
    name = "deepseek"

    def __init__(self, model_name: str | None = None):
        super().__init__()
        self.client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
        self.model_name = model_name or MODEL

    def chat(self, messages: list[dict], tools: list[dict] | None = None, **kwargs: Any) -> ModelResponse:
        params: dict[str, Any] = {"model": self.model_name, "messages": messages}
        if tools:
            params["tools"] = tools
        params.update(kwargs)

        response = self.client.chat.completions.create(**params)
        choice = response.choices[0]
        message = choice.message

        # ---- 解析工具调用：SDK 对象 → 统一 ToolCallRequest ----
        tool_calls: list[ToolCallRequest] = []
        for tc in message.tool_calls or []:
            arguments_raw = tc.function.arguments or "{}"
            try:
                arguments = json.loads(arguments_raw)
                if not isinstance(arguments, dict):
                    raise ValueError(f"arguments 不是 JSON 对象: {type(arguments)}")
            except (json.JSONDecodeError, ValueError) as e:
                # 兜底：参数损坏不阻断流程，按空调用交给工具层报错回注
                logger.warning("工具参数解析失败 tool=%s raw=%r err=%s", tc.function.name, arguments_raw, e)
                arguments = {}
            tool_calls.append(
                ToolCallRequest(
                    id=tc.id or "",
                    name=tc.function.name or "",
                    arguments=arguments,
                    arguments_raw=arguments_raw,
                )
            )

        usage = response.usage.model_dump() if response.usage else {}
        return ModelResponse(
            text=message.content or "",
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "",
            usage=usage,
            raw=response,
        )


def test():
    """连通性测试：发送一条简单消息，验证 API 可用。"""
    model = DeepSeekModel()
    resp = model.chat([
        {"role": "system", "content": "你是一个测试版本的agent loop，用户问什么你简单回答即可"},
        {"role": "user", "content": "你好"},
    ])
    print(resp.text)


if __name__ == "__main__":
    test()
