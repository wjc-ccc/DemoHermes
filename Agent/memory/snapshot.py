"""
MemoryContextSnapshot — 单个 turn 持有的不可变记忆视图（7.23 方案 §3.2 / ADR-3）

由 MemoryManager.build_context 每 turn 构建一次：
    - 同一 turn 的所有模型迭代复用同一对象，turn 中发生的写入从下一 turn 才可见；
    - Manager 不缓存快照，避免同 scope 并发 session 相互覆盖；
    - 只用不可变字段（str / tuple / frozen model），frozen=True 才能真正生效。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .types import MemoryDiagnostic, MemoryScope


class DocumentRevision(BaseModel):
    """快照中某份文档的来源与内容指纹（完整文件 bytes 的 SHA-256）。"""

    model_config = ConfigDict(frozen=True)

    source: str  # "profile" / "memory" / "daily" / provider 名
    revision: str


class MemoryContextSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    scope: MemoryScope
    session_id: str
    prompt: str  # 带 <memory-context> 围栏、可直接注入 system 的完整文本
    built_at: float
    revisions: tuple[DocumentRevision, ...]
    diagnostics: tuple[MemoryDiagnostic, ...] = ()
    # ---- §4.3 预算裁剪用的结构化分段（可选；不设预算时 Builder 仍整段注入 prompt）----
    hot_prompt: str | None = None  # 仅 Hot（profile+MEMORY）围栏块；永远保留
    daily_prompt: str | None = None  # 今日 Daily 块；裁剪顺序 1 最先移除
    provider_prompt: str | None = None  # Provider recall 块（Slice 12 填）；顺序 2 缩短
