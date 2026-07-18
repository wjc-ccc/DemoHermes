"""
ClaudeModel — Anthropic Messages 协议接入（httpx 直连，不依赖 anthropic SDK）

同时兼容：
    - 官方 api.anthropic.com
    - 火山引擎 Ark 的 Anthropic 兼容端点（.env 里 ANTHROPIC_BASE_URL 指向的地址）

协议差异处理（内部统一 OpenAI 风格 messages，这里做双向转换）：
    出站：system 消息 → 顶层 system 参数；assistant 的 tool_calls → tool_use 块；
          role=tool 消息 → user 消息里的 tool_result 块（相邻的合并成一条）
    入站：content 里的 text 块拼成文本；tool_use 块 → ToolCallRequest

密钥读取顺序：ANTHROPIC_API_KEY → DEEPSEEK_API_KEY（兼容把 Ark 密钥放在后者的配置）。
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .baseStructure import BaseModel, ModelResponse, ToolCallRequest
from ..provider import ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL, DEEPSEEK_API_KEY, MODEL

logger = logging.getLogger(__name__)

_ANTHROPIC_VERSION = "2023-06-01"
_DEFAULT_MAX_TOKENS = 4096
_TIMEOUT = 120.0


class ClaudeModel(BaseModel):
    name = "claude"

    def __init__(self, model_name: str | None = None):
        super().__init__()
        self.api_key = ANTHROPIC_API_KEY or DEEPSEEK_API_KEY
        self.base_url = (ANTHROPIC_BASE_URL or "https://api.anthropic.com").rstrip("/")
        self.model_name = model_name or MODEL

    # ================= 出站转换：OpenAI 风格 → Anthropic =================
    @staticmethod
    def _convert_tools(tools: list[dict] | None) -> list[dict] | None:
        """OpenAI function schema → Anthropic tool schema。"""
        if not tools:
            return None
        converted = []
        for t in tools:
            fn = t.get("function", {})
            converted.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })
        return converted

    @staticmethod
    def _convert_messages(messages: list[dict]) -> tuple[str, list[dict]]:
        """
        拆出 system，其余转成 Anthropic 消息列表。
        返回 (system_text, anthropic_messages)。
        """
        system_parts: list[str] = []
        out: list[dict] = []

        def flush_tool_results(buffer: list[dict]) -> None:
            """相邻的 tool 消息合并成一条 user 消息（Anthropic 协议要求）。"""
            if buffer:
                out.append({"role": "user", "content": list(buffer)})
                buffer.clear()

        tool_buffer: list[dict] = []
        for m in messages:
            role = m.get("role")
            if role == "system":
                system_parts.append(m.get("content") or "")
            elif role == "tool":
                tool_buffer.append({
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id", ""),
                    "content": m.get("content") or "",
                })
            elif role == "assistant" and m.get("tool_calls"):
                flush_tool_results(tool_buffer)
                blocks: list[dict] = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in m["tool_calls"]:
                    import json as _json
                    fn = tc.get("function", {})
                    try:
                        tool_input = _json.loads(fn.get("arguments") or "{}")
                    except _json.JSONDecodeError:
                        tool_input = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": tool_input,
                    })
                out.append({"role": "assistant", "content": blocks})
            else:
                flush_tool_results(tool_buffer)
                out.append({"role": role, "content": m.get("content") or ""})
        flush_tool_results(tool_buffer)
        return "\n\n".join(p for p in system_parts if p), out

    # ================= 入站转换：Anthropic → 统一 ModelResponse =================
    @staticmethod
    def _parse_response(payload: dict) -> ModelResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCallRequest] = []
        import json as _json
        for block in payload.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_input = block.get("input") or {}
                tool_calls.append(ToolCallRequest(
                    id=block.get("id", ""),
                    name=block.get("name", ""),
                    arguments=tool_input if isinstance(tool_input, dict) else {},
                    arguments_raw=_json.dumps(tool_input, ensure_ascii=False),
                ))
        stop_reason = payload.get("stop_reason") or ""
        finish_reason = {"end_turn": "stop", "tool_use": "tool_calls", "max_tokens": "length"}.get(
            stop_reason, stop_reason
        )
        return ModelResponse(
            text="".join(text_parts),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=payload.get("usage") or {},
            raw=payload,
        )

    # ================= 主接口 =================
    def chat(self, messages: list[dict], tools: list[dict] | None = None, **kwargs: Any) -> ModelResponse:
        system, anthropic_messages = self._convert_messages(messages)
        body: dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": kwargs.pop("max_tokens", _DEFAULT_MAX_TOKENS),
            "messages": anthropic_messages,
        }
        if system:
            body["system"] = system
        converted_tools = self._convert_tools(tools)
        if converted_tools:
            body["tools"] = converted_tools
        body.update(kwargs)

        resp = httpx.post(
            f"{self.base_url}/v1/messages",
            headers={
                "x-api-key": self.api_key or "",
                "anthropic-version": _ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json=body,
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            # 抛出让 Loop 的重试/兜底接管；错误信息截断防刷屏
            raise RuntimeError(f"Anthropic 协议调用失败 {resp.status_code}: {resp.text[:300]}")
        return self._parse_response(resp.json())
