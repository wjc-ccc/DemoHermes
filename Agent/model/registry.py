"""
ModelRegistry — 模型注册表

按名称注册和获取 LLM provider：
    registry = ModelRegistry.default()
    model = registry.get()            # 默认模型（.env 的 MODEL）
    model = registry.get("deepseek")  # 指定 provider

上层（AgentLoop）只通过注册表取模型，不直接 import 具体 provider，
这样新增 provider 时只需在这里注册一行，Loop 零改动。
"""
from __future__ import annotations

import logging

from .baseStructure import BaseModel
from ..provider import MODEL

logger = logging.getLogger(__name__)


class ModelRegistry:
    def __init__(self) -> None:
        self._models: dict[str, BaseModel] = {}
        self._default_name: str = ""

    # ---- 注册 ----
    def register(self, model: BaseModel, *, as_default: bool = False) -> None:
        """注册一个模型实例；as_default=True 时设为默认模型。"""
        self._models[model.name] = model
        if as_default or not self._default_name:
            self._default_name = model.name
        logger.info("注册模型 name=%s default=%s", model.name, self._default_name)

    # ---- 获取 ----
    def get(self, name: str | None = None) -> BaseModel:
        """
        按名称取模型；name 为空时依次尝试：注册表默认 → 环境变量 MODEL 前缀 → 第一个已注册。
        找不到抛 KeyError，由上层（Loop 容错）决定兜底。
        """
        if name:
            if name not in self._models:
                raise KeyError(f"模型未注册: {name}（已注册: {list(self._models)}）")
            return self._models[name]
        if self._default_name:
            return self._models[self._default_name]
        # 按 .env 的 MODEL 名称前缀模糊匹配（如 deepseek-v4-flash → deepseek）
        for registered in self._models:
            if MODEL.startswith(registered):
                return self._models[registered]
        if self._models:
            return next(iter(self._models.values()))
        raise KeyError("模型注册表为空，请先 register()")

    def list_models(self) -> list[str]:
        return list(self._models)

    # ---- 默认注册表 ----
    @classmethod
    def default(cls) -> "ModelRegistry":
        """
        构建项目默认注册表：DeepSeek（OpenAI 协议）+ Claude（Anthropic 协议）都注册。

        默认模型选择：
            .env 显式配置了 ANTHROPIC_BASE_URL → 默认 claude
            （当前项目的密钥就是火山 Ark 的 Anthropic 兼容端点密钥）
            否则 → 默认 deepseek
        """
        import os

        from .claude import ClaudeModel
        from .deepseek import DeepSeekModel

        registry = cls()
        prefer_claude = bool(os.getenv("ANTHROPIC_BASE_URL"))
        registry.register(DeepSeekModel(), as_default=not prefer_claude)
        registry.register(ClaudeModel(), as_default=prefer_claude)
        return registry
