"""
ToolRegistry — 工具注册表

管理可用工具的注册与 schema；执行由 ToolDispatch 负责：
    register(tool)       : 注册工具实例
    get_schemas()        : 返回所有工具的 OpenAI function calling schema（随请求发给 LLM）
    list_tools()         : 列出已注册工具名

ToolHandler 从本注册表读取普通模式的工具定义。
"""
from __future__ import annotations
import logging
from agent.core.tools.base import Tool


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
