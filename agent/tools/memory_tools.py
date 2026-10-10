"""面向模型的记忆搜索与按引用读取工具。

职责：校验工具参数、限制返回长度，将运行时 scope 交给 MemoryRetriever。
边界：不允许模型指定其他用户 scope；这里只暴露关键词检索，不写入卡片。
关联：AgentLoop 注入 scope，Retriever 检查来源权限并读取 SQLite/已登记文档。
"""
from __future__ import annotations

import json
from dataclasses import asdict

from agent.core.memory.document_text import estimate_markdown_tokens
from agent.storage.sqlite.memory_retrieval import MemoryRetriever
from agent.types.memory.scope import MemoryScope
from agent.tools.base import Tool
from agent.types.action.policy import ToolPolicy
from agent.types.tools import ToolCallResult, ToolInvocationContext


class MemorySearchTool(Tool):
    name = "memory_search"
    description = (
        "按关键词检索当前用户的记忆卡片、历史对话和原始资料。"
        "返回引用与摘要；需要完整内容时再调用 memory_read。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "maxLength": 512,
                      "description": "要查找的具体词语或短语"},
            "mode": {"type": "string", "enum": ["keyword"], "description": "目前仅支持关键词检索"},
            "source": {"type": "string", "enum": ["all", "card", "history", "raw"],
                       "description": "all 全部；card 记忆卡片；history 对话消息；raw 工具结果与已登记 Markdown"},
            "kind": {"type": "string", "enum": ["profile", "stable_preference",
                                                  "ongoing_fact", "reusable_experience"],
                         "description": "仅搜索指定类别的卡片；对 history/raw 无效"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "offset": {"type": "integer", "minimum": 0},
        },
        "required": ["query"],
    }
    action_policy = ToolPolicy(when_to_use=description, when_not_to_use="不用于产生业务写入")

    def __init__(self, retriever: MemoryRetriever) -> None:
        self.retriever = retriever

    def execute(self, arguments: dict, *, context: ToolInvocationContext) -> ToolCallResult:
        scope = context.memory_scope
        if not isinstance(scope, MemoryScope):
            return ToolCallResult(ok=False, error="缺少运行时 memory scope")
        if arguments.get("mode", "keyword") != "keyword":
            return ToolCallResult(ok=False, error="当前仅支持 keyword 检索")
        query = str(arguments.get("query") or "").strip()[:512]
        if not query:
            return ToolCallResult(ok=False, error="query 不能为空")
        try:
            requested_limit = int(arguments.get("limit", 10))
            offset = int(arguments.get("offset", 0))
            hits = self.retriever.search(
                scope, query, source=str(arguments.get("source") or "all"),
                kind=arguments.get("kind"), limit=requested_limit,
                offset=offset,
            )
        except (ValueError, TypeError) as exc:
            return ToolCallResult(ok=False, error=str(exc))
        visible: list[dict] = []
        for hit in hits:
            candidate = asdict(hit)
            candidate.pop("content", None)  # 完整卡片正文只能通过 memory_read 按需读取。
            candidate["title"] = candidate["title"][:120]
            candidate["snippet"] = candidate["snippet"][:260]
            candidate["matched_keywords"] = tuple(value[:80] for value in candidate["matched_keywords"][:8])
            candidate["evidence_refs"] = tuple(value[:160] for value in candidate["evidence_refs"][:5])
            proposal = {"mode": "keyword", "query": query, "hits": [*visible, candidate]}
            if estimate_markdown_tokens(json.dumps(proposal, ensure_ascii=False)) > 1200:
                if not visible:
                    candidate["snippet"] = candidate["snippet"][:80]
                    visible.append(candidate)
                break
            visible.append(candidate)
        has_more = len(visible) < len(hits) or len(hits) >= max(1, min(requested_limit, 50))
        data = {"mode": "keyword", "query": query, "hits": visible,
                "next_offset": max(offset, 0) + len(visible) if has_more else None}
        return ToolCallResult(ok=True, result_text=json.dumps(data, ensure_ascii=False), data=data)


class MemoryReadTool(Tool):
    name = "memory_read"
    description = (
        "按 memory_search 返回的引用读取记忆卡片、对话原文、工具结果或已登记 Markdown；"
        "卡片证据中的 manual:版本号 可读取历史 MEMORY.md。"
        "长内容使用 offset 翻页；只能读取当前用户范围内的资料。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "reference": {"type": "string", "description": "card:/message:/tool:/document:/manual: 开头的引用"},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 4000},
        },
        "required": ["reference"],
    }
    action_policy = ToolPolicy(when_to_use=description, when_not_to_use="不用于产生业务写入")

    def __init__(self, retriever: MemoryRetriever) -> None:
        self.retriever = retriever

    def execute(self, arguments: dict, *, context: ToolInvocationContext) -> ToolCallResult:
        scope = context.memory_scope
        if not isinstance(scope, MemoryScope):
            return ToolCallResult(ok=False, error="缺少运行时 memory scope")
        reference = str(arguments.get("reference") or "")
        try:
            page = self.retriever.read(scope, reference,
                                       offset=int(arguments.get("offset", 0)),
                                       limit=int(arguments.get("limit", 2000)))
        except (ValueError, TypeError) as exc:
            return ToolCallResult(ok=False, error=str(exc))
        if page is None:
            return ToolCallResult(ok=False, error="引用不存在或当前用户无权读取")
        data = asdict(page)
        return ToolCallResult(ok=True, result_text=json.dumps(data, ensure_ascii=False), data=data, readback_reference=reference)
