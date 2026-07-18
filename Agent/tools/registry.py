"""
ToolRegistry — 工具注册表

管理所有可用工具的注册、schema 收集与调用分发：
    register(tool)       : 注册工具实例
    get_schemas()        : 返回所有工具的 OpenAI function calling schema（随请求发给 LLM）
    dispatch(name, args) : 按名称分发执行；任何异常都兜底成 ToolResult(ok=False)，绝不抛给 Loop
    list_tools()         : 列出已注册工具名

core/loop 在 think 阶段把 schemas 传给 LLM，在 act 阶段通过 dispatch 执行。
"""
from __future__ import annotations

import logging
import time

from .base import Tool, ToolResult

logger = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    # ---- 注册 ----
    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool
        logger.info("注册工具 name=%s", tool.name)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list_tools(self) -> list[str]:
        return list(self._tools)

    # ---- 给 LLM 的 schema ----
    def get_schemas(self) -> list[dict]:
        """空注册表返回空列表 —— Loop 会按「无工具」单轮对话处理。"""
        return [tool.to_openai_schema() for tool in self._tools.values()]

    # ---- 分发执行（容错兜底核心）----
    def dispatch(self, name: str, arguments: dict) -> ToolResult:
        started = time.time()
        tool = self._tools.get(name)
        if tool is None:
            logger.warning("调用了未注册的工具: %s", name)
            return ToolResult(ok=False, error=f"工具不存在: {name}（可用: {self.list_tools()}）")
        try:
            result = tool.execute(arguments or {})
        except Exception as e:
            # 工具内部 bug 不打断 Loop；作为失败结果回注，让 LLM 看到并换思路
            logger.exception("工具执行异常 tool=%s args=%s", name, arguments)
            return ToolResult(ok=False, error=f"工具执行异常: {e}")
        elapsed = (time.time() - started) * 1000
        logger.info(
            "工具执行完成 tool=%s ok=%s 耗时=%.0fms", name, result.ok, elapsed
        )
        return result
