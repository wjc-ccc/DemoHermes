"""面向模型的记忆搜索与按引用读取工具。

职责：校验工具参数、限制返回长度，将运行时 scope 交给 MemoryRetriever。
边界：不允许模型指定其他用户 scope；这里只暴露关键词检索，不写入卡片。
关联：AgentLoop 注入 scope，Retriever 检查来源权限并读取 SQLite/已登记文档。
"""
from __future__ import annotations
from agent.core.prompt.renderers import render_tool_result

import json
import re
from dataclasses import asdict

from agent.core.memory.document_text import estimate_markdown_tokens
from agent.core.storage.sqlite.memory_retrieval import MemoryRetriever
from agent.types.memory.scope import MemoryScope
from agent.core.tools.base import Tool
from agent.types.action.policy import ToolPolicy
from agent.types.tools import ToolCallResult, ToolInvocationContext


class MemorySearchTool(Tool):
    name = "memory_search"
    description = (
        "按关键词检索当前用户的记忆卡片、历史对话和原始资料。"
        "已知message/tool/card/document的ID时按主键精确查找，不依赖正文关键词。"
        "返回reference与read_offset；读取命中片段时原样传给memory_read的reference/offset，不拼接文件路径。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "maxLength": 512,
                      "description": "已知的完整引用、原始ID，或要查找的具体词语或短语"},
            "mode": {"type": "string", "enum": ["keyword"], "description": "目前仅支持关键词检索"},
            "source": {"type": "string", "enum": ["all", "card", "history", "raw"],
                       "description": "all 全部；card 记忆卡片；history 对话消息；raw 工具结果与已登记 Markdown"},
            "kind": {"type": "string", "enum": ["profile", "stable_preference",
                                                  "ongoing_fact", "reusable_experience"],
                         "description": "仅搜索指定类别的卡片；对 history/raw 无效"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "offset": {"type": "integer", "minimum": 0},
            "source_message_ids": {"type": "array", "items": {"type": "string"},
                                   "minItems": 1, "maxItems": 50,
                                   "description": "可选：反查由这些原始消息提取的卡片，此时query可留空"},
        },
        "anyOf": [{"required": ["query"]}, {"required": ["source_message_ids"]}],
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
        source_ids = arguments.get("source_message_ids")
        if not query and source_ids is None:
            return ToolCallResult(ok=False, error="query 不能为空")
        try:
            requested_limit = int(arguments.get("limit", 10))
            offset = int(arguments.get("offset", 0))
            hits = self.retriever.search(
                scope, query, source=str(arguments.get("source") or "all"),
                kind=arguments.get("kind"), limit=requested_limit,
                offset=offset,
                **({"source_message_ids": source_ids} if source_ids is not None else {}),
            )
        except (ValueError, TypeError) as exc:
            return ToolCallResult(ok=False, error=str(exc))
        visible: list[dict] = []
        for hit in hits:
            candidate = asdict(hit)
            candidate.pop("content", None)  # 完整卡片正文只能通过 memory_read 按需读取。
            candidate.pop("card_snapshot", None)
            candidate["title"] = candidate["title"][:120]
            candidate["snippet"] = candidate["snippet"][:260]
            candidate["matched_keywords"] = tuple(value[:80] for value in candidate["matched_keywords"][:8])
            candidate["evidence_refs"] = tuple(value[:160] for value in candidate["evidence_refs"][:5])
            proposal = {"mode": "keyword", "query": query, "hits": [*visible, candidate],
                        "next_offset": max(offset,0)+len(visible)+1}
            if context.result_token_budget is not None:
                def fits(value):
                    return len(render_tool_result(self.name,value).encode("utf-8"))<=context.result_token_budget
                if not visible and not fits(proposal):
                    candidate["title"]=candidate["title"][:32]
                    candidate["snippet"]=candidate["snippet"][:60]
                    candidate["evidence_refs"]=candidate["evidence_refs"][:1]
                if not fits(proposal):
                    break
            elif estimate_markdown_tokens(render_tool_result(self.name, proposal)) > 1200:
                if not visible:
                    candidate["snippet"] = candidate["snippet"][:80]
                    visible.append(candidate)
                break
            visible.append(candidate)
        has_more = len(visible) < len(hits) or len(hits) >= max(1, min(requested_limit, 50))
        data = {"mode": "keyword", "query": query, "hits": visible,
                "next_offset": max(offset, 0) + len(visible) if has_more else None}
        if hits and not visible:
            return ToolCallResult(ok=False,error="检索结果预算不足，请依据已取得的信息作答或缩短查询。")
        return ToolCallResult(ok=True, result_text=render_tool_result(self.name, data), data=data)


class MemoryReadTool(Tool):
    name = "memory_read"
    description = (
        "按 memory_search 返回的引用读取记忆卡片、对话原文、工具结果或已登记 Markdown；"
        "卡片证据中的 manual:版本号 可读取历史 MEMORY.md。"
        "已知ID可直接读取，不必先搜索；原样复制引用，不把数据库ID当文件路径。"
        "长内容使用offset翻页，或用query定位正文关键词；只能读取当前用户范围内的资料。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "reference": {"type": "string", "description": "原样复制card:/message:/tool:/document:/manual:引用；唯一原始ID也可直接定位"},
            "query": {"type": "string", "maxLength": 512, "description": "可选：定位此原文中具体关键词附近的页面，避免只读取开头"},
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
            options = {"query": str(arguments["query"])[:512]} if arguments.get("query") else {}
            page = self.retriever.read(scope, reference,
                                       offset=int(arguments.get("offset", 0)),
                                       limit=int(arguments.get("limit", 2000)), **options)
        except (ValueError, TypeError) as exc:
            return ToolCallResult(ok=False, error=str(exc))
        if page is None:
            return ToolCallResult(ok=False, error="引用不存在或当前用户无权读取")
        data = asdict(page)
        if page.card_revision is None:
            data.pop("card_revision")
        if context.result_token_budget is not None:
            # Page the original source instead of filling protected P1 with a
            # whole document. Raw database content remains unchanged.
            while (len(data["content"]) > 32
                   and len(render_tool_result(self.name, data).encode("utf-8")) > context.result_token_budget):
                length=max(32,len(data["content"])//2)
                start = 0
                if options:
                    match = re.search(re.escape(options["query"]), data["content"], re.IGNORECASE)
                    if match:
                        start = max(0, match.start()-min(32, length//4))
                data["content"] = data["content"][start:start+length]
                data["offset"] += start
                end = data["offset"] + len(data["content"])
                data["next_offset"] = end if end < data["total_chars"] else None
            if len(render_tool_result(self.name,data).encode("utf-8"))>context.result_token_budget:
                data["context"]=tuple({key:value for key,value in entry.items() if key!="snippet"}
                                      for entry in data.get("context",()))
            if len(render_tool_result(self.name,data).encode("utf-8"))>context.result_token_budget:
                return ToolCallResult(ok=False,error="原文页预算不足，请依据已取得的信息作答。")
        if options and not re.search(re.escape(options["query"]), data["content"], re.IGNORECASE):
            return ToolCallResult(ok=False, error="目标片段超出返回预算，请缩短query或提高预算。")
        return ToolCallResult(ok=True, result_text=render_tool_result(self.name, data), data=data, readback_reference=page.reference)
