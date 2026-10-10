"""skills 数据契约。"""
from __future__ import annotations
import hashlib
import json
import re
from typing import Literal
from pydantic import BaseModel, BaseModel as PydanticModel, ConfigDict, Field


SkillSource = Literal["builtin", "generated", "user"]


SkillStatus = Literal["draft", "active", "disabled"]


def frontmatter_value(raw: str) -> str:
    """把任意文本压成 JSON 字符串形式的单行 frontmatter 值。"""
    collapsed = re.sub(r"\s+", " ", (raw or "").replace("\x00", "")).strip()
    return json.dumps(collapsed, ensure_ascii=False)


def content_hash_of(body: str) -> str:
    return hashlib.sha256((body or "").encode("utf-8")).hexdigest()[:16]


class Skill(PydanticModel):
    name: str = Field(default_factory=str, description="技能名（目录名 == frontmatter name）")
    description: str = Field(default_factory=str, description="做什么 + 何时使用（Level 1 注入）")
    body: str = Field(default_factory=str, description="完整正文（Level 2 加载）")
    path: str = Field(default_factory=str, description="SKILL.md 所在路径")
    status: SkillStatus = Field(default="active", description="draft|active|disabled")
    revision: int = Field(default=1, ge=1, description="内容版本；每次写入递增")
    source: SkillSource = Field(default="user", description="builtin|generated|user")
    scope_key: str = Field(
        default="",
        description="生成型技能的可见作用域；空串表示全局（builtin / 人工维护）",
    )
    origin_turn_id: str = Field(default_factory=str, description="沉淀来源 turn")
    required_tools: tuple[str, ...] = Field(
        default_factory=tuple, description="正文依赖的工具；缺失时不应被选中"
    )

    @property
    def content_hash(self) -> str:
        """正文指纹；用于去重、环境快照和回放可复现性。"""
        return content_hash_of(self.body)

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    def summary_line(self) -> str:
        """Level 1：注入 system prompt 的一行摘要。"""
        return f"- {self.name}: {self.description}"

    def visible_in(self, scope_key: str | None) -> bool:
        """全局技能对所有会话可见；带 scope 的生成型技能只对该会话可见。"""
        if not self.scope_key:
            return True
        return bool(scope_key) and self.scope_key == scope_key

    def render(self) -> str:
        """渲染回 SKILL.md 全文；frontmatter 值一律经过单行化处理。"""
        lines = [
            "---",
            f"name: {self.name}",
            f"description: {frontmatter_value(self.description)}",
            f"status: {self.status}",
            f"revision: {self.revision}",
            f"source: {self.source}",
        ]
        if self.scope_key:
            lines.append(f"scope: {frontmatter_value(self.scope_key)}")
        if self.origin_turn_id:
            lines.append(f"origin_turn: {frontmatter_value(self.origin_turn_id)}")
        if self.required_tools:
            lines.append(f"required_tools: {', '.join(self.required_tools)}")
        lines.append("---")
        body = (self.body or "").strip()
        if not body.startswith("#"):
            body = f"# {self.name}\n\n{body}"
        return "\n".join(lines) + "\n\n" + body + "\n"


class SkillRef(BaseModel):
    """本轮被选中的技能及其确定版本的正文。"""

    model_config = ConfigDict(frozen=True)

    name: str
    revision: int
    content_hash: str
    description: str = ""
    body: str = ""


class SkillSelection(BaseModel):
    """一个 turn 冻结的技能选择结果。

    冻结的意义：同一 turn 内多次模型迭代看到的摘要与正文完全一致，
    turn 中途发生的写盘/晋升只影响下一 turn（与 MemorySnapshot 语义对齐）。
    """

    model_config = ConfigDict(frozen=True)

    summary_prompt: str = ""
    selected: tuple[SkillRef, ...] = ()

    @property
    def summary_chars(self) -> int:
        return len(self.summary_prompt)

    def load(self, name: str) -> SkillRef | None:
        target = (name or "").strip()
        for ref in self.selected:
            if ref.name == target:
                return ref
        return None


SkillChangeAction = Literal["create", "patch", "promote", "disable", "rollback"]


class SkillDraft(BaseModel):
    """一份待落盘的技能内容（领域载体，不是某个函数的参数包）。"""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    body: str
    source: SkillSource = "generated"
    scope_key: str = ""
    origin_turn_id: str = ""
    required_tools: tuple[str, ...] = ()


class SkillChangeRequest(BaseModel):
    """一次技能变更请求。"""

    model_config = ConfigDict(frozen=True)

    action: SkillChangeAction
    name: str = ""
    draft: SkillDraft | None = None
    old_string: str = ""
    new_string: str = ""
    expected_revision: int | None = None
    target_revision: int | None = None
    actor: str = "system"
    reason: str = ""
    turn_id: str | None = None


class SkillChangeResult(BaseModel):
    """变更结果；code 是稳定的机器可读原因，便于审计与测试断言。"""

    model_config = ConfigDict(frozen=True)

    ok: bool
    code: str
    name: str = ""
    revision: int = 0
    status: str = ""
    path: str = ""
    detail: str = ""
