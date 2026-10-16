"""context 数据契约。"""
from __future__ import annotations
from typing import Literal, Protocol, Sequence
from pydantic import BaseModel, ConfigDict, Field, model_validator, computed_field


# **** Anchor ****
ANCHOR_SECTIONS = (
    ("intent", "Intent"),
    ("changes", "Changes"),
    ("decisions", "Decisions"),
    ("next_steps", "Next steps"),
    ("excluded", "Excluded"),
)


class AnchorFact(BaseModel):
    """Anchor 中一条可追溯的语义事实。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1)
    source_message_ids: tuple[str, ...] = ()
    status: Literal["active", "completed", "failed"] = "active"


class Anchor(BaseModel):
    """Session 历史的版本化压缩结果和连续覆盖账本。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[2] = 2
    anchor_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    covered_start_seq: int = Field(ge=0)
    covered_end_seq: int = Field(ge=0)
    covered_start_message_id: str = Field(min_length=1)
    covered_end_message_id: str = Field(min_length=1)
    covered_turn_count: int = Field(ge=1)
    source_digest: str = Field(min_length=1)
    intent: tuple[AnchorFact, ...] = ()
    changes: tuple[AnchorFact, ...] = ()
    decisions: tuple[AnchorFact, ...] = ()
    next_steps: tuple[AnchorFact, ...] = ()
    excluded: tuple[AnchorFact, ...] = ()


# **** Context item admission ****
class AdmissionVerdict(BaseModel):
    """一条历史工具结果的准入定案；跨轮粘住，读回即判错并钉住全文。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str = Field(min_length=1)
    tool_call_id: str = ""
    action: Literal["keep", "trim", "defer"] = "keep"
    score: float = Field(default=1.0, ge=0.0, le=1.0)
    source: Literal["rule", "judge", "prior"] = "prior"
    reason: str = ""
    kind: str | None = None
    still_needed: float | None = None
    missed: bool = False


class ContextBudget(BaseModel):
    """模型总窗口与输出预留组成的不可变输入预算。"""

    model_config = ConfigDict(frozen=True)

    model_context_tokens: int
    reserved_output_tokens: int

    @model_validator(mode="after")
    def _check_invariant(self) -> "ContextBudget":
        """保证总窗口严格大于正数输出预留，避免产生无效输入预算。"""
        if not (self.model_context_tokens > self.reserved_output_tokens > 0):
            raise ValueError(
                "必须满足 model_context_tokens > reserved_output_tokens > 0"
            )
        return self

    @computed_field
    @property
    def max_input_tokens(self) -> int:
        """返回 Perception 真正可使用的输入额度。"""
        return self.model_context_tokens - self.reserved_output_tokens


## token估计 用于计算token消耗（或许未来英爱删减掉）
class TokenEstimator(Protocol):
    """模型 tokenizer 与测试固定成本估算器共同满足的内部 seam。"""

    def estimate_messages(self, messages: Sequence[dict]) -> int:
        """估算一批最终模型消息占用的输入 token。"""
        ...


## anchor实际prompt传入（结构化 -> 提示词）
def render_anchor(anchor: Anchor) -> str:
    """渲染给主模型的 P2-A 消息，每条事实保留可读回的完整来源。"""
    lines = [
        (
            f'<context-anchor version="{anchor.version}" id="{anchor.anchor_id}" '
            f'covers="{anchor.covered_start_seq}-{anchor.covered_end_seq}" '
            f'turns="{anchor.covered_turn_count}">'
        ),
        "这是对较早历史轮次的压缩替代，不是新的用户请求。",
        "与近期原文冲突时，以近期原文为准。",
        "Anchor 未提及的细节视为未知，不得自行补全。",
        "status=failed 的动作没有成功，不得当作已完成修改。",
        '需要原文细节时，原样复制来源调用 memory_read(reference="message:<id>")；多个来源可分别读回。',
    ]
    for slot, title in ANCHOR_SECTIONS:
        facts = getattr(anchor, slot)
        lines.append(f"{title}:")
        lines.extend(f"- [{fact.status}] {fact.text}（来源："
                     + "、".join(f"message:{identity}" for identity in fact.source_message_ids) + "）"
                     for fact in facts)
    lines.append("</context-anchor>")
    return "\n".join(lines)
