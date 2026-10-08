"""单个 Turn 使用的不可变 Memory 快照。"""
# 职责：携带文档正文、经验召回、来源版本和非致命诊断。
# 边界：纯数据结构，不读取文件或决定上下文优先级。
# 关联：MemoryContextLoader 创建快照，PrecisionBuilder 消费快照。
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .memory_layer import MemoryLayer
from .memory_scope import MemoryScope


class MemoryDiagnostic(BaseModel):
    """冻结过程中产生的非致命诊断，不参与上下文决策。"""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str = ""


class DocumentRevision(BaseModel):
    """本轮实际加载文档的来源、层级与内容指纹。"""

    model_config = ConfigDict(frozen=True)

    source: str
    layer: MemoryLayer
    revision: str
    token_count: int = 0

## 所有类型的memory的核心字段 用于按照存在情况提取对应路径下的记忆
class MemorySnapshot(BaseModel):
    """Memory → Precision 的稳定 Interface；同一 Turn 内不可变。"""

    model_config = ConfigDict(frozen=True)

    scope: MemoryScope
    session_id: str
    prompt: str
    built_at: float
    revisions: tuple[DocumentRevision, ...]
    diagnostics: tuple[MemoryDiagnostic, ...] = ()
    experience_prompt: str | None = None
