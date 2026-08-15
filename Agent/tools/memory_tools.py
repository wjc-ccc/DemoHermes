"""memory 工具--Manager 的薄 adapter（7.23 方案 §9）。

只注册 memory_read / memory_edit 两个稳定工具，避免暴露四套存储。
工具不含文件解析、预算或锁逻辑；只经 Manager.read/search/apply。
不返回绝对路径、SQL、Provider 凭据；scope 由运行上下文注入（arguments["_scope"]），
模型不能伪造其他用户 scope。
"""
from __future__ import annotations

from .base import Tool, ToolCallResult
from ..memory import (
    BuiltinMemoryRequest,
    MemoryReadRequest,
    BuiltinMemoryResult,
    MemorySearchRequest,
)
from ..memory.types import MemoryScope


def _scope_from(arguments: dict) -> MemoryScope | None:
    """scope 永远由运行上下文注入；模型参数不得覆盖（§9）。"""
    raw = arguments.get("_scope")
    if raw is None:
        return None
    if isinstance(raw, MemoryScope):
        return raw
    return MemoryScope.model_validate(raw)


class MemoryReadTool(Tool):
    """读取 profile/memory/daily，或按 query 搜索历史。"""

    name = "memory_read"
    description = (
        "读取持久记忆（profile/memory/daily）或搜索历史/daily/provider。"
        "不暴露存储路径或 SQL。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "profile / memory / daily"},
            "date": {"type": "string", "description": "daily 日期 YYYY-MM-DD"},
            "query": {"type": "string", "description": "提供则搜索而非读整份文档"},
        },
    }

    def __init__(self, manager) -> None:
        self._manager = manager

    def execute(self, arguments: dict) -> ToolCallResult:
        scope = _scope_from(arguments)
        if scope is None:
            return ToolCallResult(ok=False, error="scope 未由运行上下文注入")
        query = arguments.get("query")
        if query:
            hits = self._manager.search(
                MemorySearchRequest(scope=scope, query=str(query))
            )
            lines = [f"[{h.source}] {h.snippet}" for h in hits]
            return ToolCallResult(ok=True, result_text="\n".join(lines) or "无命中")
        target = arguments.get("target", "profile")
        view = self._manager.read(
            MemoryReadRequest(
                scope=scope, target=target, date=arguments.get("date")
            )
        )
        lines = [f"[{e.entry_id}] {e.content}" for e in view.entries]
        return ToolCallResult(ok=True, result_text="\n".join(lines))


class MemoryEditTool(Tool):
    """add/replace/remove profile/memory/daily entry（经 Manager.apply）。"""

    name = "memory_edit"
    description = "新增/替换/删除持久记忆条目。使用 entry_id + expected_revision，不暴露路径。"
    parameters = {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "profile / memory / daily"},
            "action": {"type": "string", "description": "add / replace / remove"},
            "content": {"type": "string", "description": "add/replace 的正文"},
            "entry_id": {"type": "string", "description": "replace/remove 的条目 id"},
            "date": {"type": "string", "description": "daily 日期 YYYY-MM-DD"},
        },
        "required": ["target", "action"],
    }

    def __init__(self, manager, provenance_factory=None) -> None:
        self._manager = manager
        self._provenance_factory = provenance_factory  # 注入 provenance（含 turn_id）

    def execute(self, arguments: dict) -> ToolCallResult:
        scope = _scope_from(arguments)
        if scope is None:
            return ToolCallResult(ok=False, error="scope 未由运行上下文注入")
        prov = self._provenance_factory() if self._provenance_factory else None
        if prov is None:
            return ToolCallResult(ok=False, error="provenance 未注入")
        request = BuiltinMemoryRequest(
            scope=scope,
            target=arguments.get("target", "profile"),
            action=arguments.get("action", "add"),
            content=arguments.get("content"),
            entry_id=arguments.get("entry_id"),
            date=arguments.get("date"),
            provenance=prov,
        )
        result: BuiltinMemoryResult = self._manager.apply(request)
        if not result.ok:
            return ToolCallResult(ok=False, error=result.error_code or "memory_edit 失败")
        return ToolCallResult(ok=True, result_text=f"ok entry_id={result.entry_id}")
