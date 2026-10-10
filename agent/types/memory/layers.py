"""Memory 五层定义、权威顺序和合法类型。"""
# 职责：给文档和临时原子提供归属层级；Task 枚举仅保留历史 Trace 含义。
# 边界：权威排序是声明性规则，未在这里执行跨层冲突裁决；卡片类别见 card_types.py。
# 关联：MarkdownDocuments、MemoryItem 和 Validator 使用这些枚举与类型检查。
from __future__ import annotations

from enum import Enum

## 对于不同记忆类型 分级别  政策 / 项目 / 用户 / 任务 / 会话
class MemoryLayer(str, Enum):
    """信息的权威与生命周期层，不表示存储冷热或加载优先级。"""

    POLICY = "policy"
    PROJECT = "project"
    USER = "user"
    TASK = "task"
    SCRATCHPAD = "scratchpad"


_AUTHORITY_RANK = {
    MemoryLayer.POLICY: 0,
    MemoryLayer.PROJECT: 1,
    MemoryLayer.USER: 2,
    MemoryLayer.TASK: 3,
    MemoryLayer.SCRATCHPAD: 4,
}

_ITEM_TYPES = {
    MemoryLayer.POLICY: frozenset(
        {
            "compliance_rule",
            "security_boundary",
            "approval_rule",
            "operational_prohibition",
            "data_handling_rule",
        }
    ),
    MemoryLayer.PROJECT: frozenset(
        {
            "project_rule",
            "architecture",
            "domain_model",
            "business_workflow",
            "repository_guide",
            "command",
        }
    ),
    MemoryLayer.USER: frozenset(
        {
            "identity",
            "preference",
            "capability",
            "tenant_config",
            "relationship",
            "long_term_fact",
            "reusable_experience",
        }
    ),
    MemoryLayer.TASK: frozenset(),
    MemoryLayer.SCRATCHPAD: frozenset(
        {
            "tool_observation",
            "hypothesis",
            "candidate_plan",
            "temporary_variable",
            "intermediate_result",
            "pending_check",
        }
    ),
}


def authority_rank(layer: MemoryLayer) -> int:
    return _AUTHORITY_RANK[layer]

## 返回对应类别的frozenset
def item_types_for(layer: MemoryLayer) -> frozenset[str]:
    return _ITEM_TYPES[layer]

## 检验是否来自五个类别
def validate_item_type(layer: MemoryLayer, item_type: str) -> None:
    if item_type not in _ITEM_TYPES[layer]:
        raise ValueError(f"{item_type!r} 不是 {layer.value} 层允许的 type")
