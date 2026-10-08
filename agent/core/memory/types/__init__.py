"""集中导出 Memory 请求、结果和协议类型。

职责：延迟导入各类型文件，减少领域模型之间的循环依赖。
边界：只做名称映射，不实现提取、检索或持久化逻辑。
关联：领域层和 SQLite Adapter 从这里取得共享的类型约定。
"""
from importlib import import_module


_MODULES = {
    "MemoryExtractionBatch": "extraction_types",
    "MemoryExtractionIssue": "extraction_types",
    "MemoryEvidence": "extraction_types",
    "MemoryProposal": "extraction_types",
    "MemoryValidationResult": "extraction_types",
    "SessionReset": "memory_source_types",
    "TurnRecord": "memory_source_types",
    "MemoryStore": "memory_store_types",
    "MemoryTraceStore": "memory_store_types",
    "UserCardKind": "card_types",
    "UserCardStatus": "card_types",
    "ExperienceOutcome": "card_types",
}
__all__ = tuple(_MODULES)


def __getattr__(name):
    module = _MODULES.get(name)
    if module is None:
        raise AttributeError(name)
    return getattr(import_module(f".{module}", __name__), name)
