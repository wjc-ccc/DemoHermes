"""Anchor Builder Callable 的确定性测试 Adapter。"""
from __future__ import annotations

from agent.core.types import AnchorFact

_SLOTS = ("intent", "changes", "decisions", "next_steps", "excluded")


class DeterministicAnchorBuilder:
    """根据受控角色生成可预测事实，并记录每次 payload。"""

    def __init__(self) -> None:
        """初始化空请求记录。"""
        self.requests: list[dict] = []

    def __call__(self, payload):
        """合并已有事实并从新增完整轮次生成最小五槽 JSON。"""
        self.requests.append(payload)
        existing = payload.get("existing_anchor") or {}
        buckets = {
            slot: [
                AnchorFact.model_validate(value)
                for value in existing.get(slot, [])
                if value.get("source_message_ids")
            ]
            for slot in _SLOTS
        }
        for turn in payload.get("new_turns", []):
            messages = list(turn["messages"])
            for index, message in enumerate(messages):
                text = " ".join(str(message.get("content") or "").split())
                role = message.get("role")
                message_id = message.get("message_id") or ""
                if role == "user" and text:
                    buckets["intent"].append(_fact(text, message_id))
                elif role == "tool" and message.get("is_error"):
                    buckets["decisions"].append(
                        _fact(text or "工具执行失败", message_id, status="failed")
                    )
                elif role == "assistant" and message.get("tool_calls"):
                    successful_tools = [
                        candidate
                        for candidate in messages[index + 1 :]
                        if candidate.get("role") == "tool"
                        and not candidate.get("is_error")
                    ]
                    for call in message["tool_calls"]:
                        function = call.get("function") or {}
                        name = function.get("name") or call.get("name") or "tool"
                        if successful_tools:
                            buckets["changes"].append(AnchorFact(
                                text=f"called {name}",
                                source_message_ids=(
                                    message_id,
                                    successful_tools[0]["message_id"],
                                ),
                                status="completed",
                            ))
                elif role == "assistant" and text:
                    lowered = text.lower()
                    if "下一步" in text or "next" in lowered:
                        buckets["next_steps"].append(_fact(text, message_id))
                    if "排除" in text or "不要" in text or "instead of" in lowered:
                        buckets["excluded"].append(_fact(text, message_id))
                    if not any(marker in text for marker in ("下一步", "排除", "不要")):
                        buckets["decisions"].append(
                            _fact(text, message_id, status="completed")
                        )
        return {
            name: [fact.model_dump(mode="json") for fact in _dedupe(facts)]
            for name, facts in buckets.items()
        }


def _fact(text: str, message_id: str, *, status: str = "active") -> AnchorFact:
    """构造一条具有单一可信来源的测试事实。"""
    return AnchorFact(
        text=text,
        source_message_ids=(message_id,),
        status=status,
    )


def _dedupe(facts):
    """按文本和状态稳定去重，模拟 Builder 的完整返回。"""
    seen = set()
    for fact in facts:
        key = (fact.text, fact.status)
        if key in seen:
            continue
        seen.add(key)
        yield fact
