"""
Skill — 技能数据结构 + SKILL.md 严格解析/渲染

技能 = 版本化、可评测、按条件选择的程序性提示词资产。

SKILL.md 格式（frontmatter + 正文）：

    ---
    name: weekly_report
    description: 把零散工作记录整理成结构化周报。当用户要求写周报时使用。
    status: draft
    revision: 1
    source: generated
    scope: cli:user:u1
    origin_turn: 9f2a1c
    required_tools: memory_read, calculator
    ---
    （正文：执行步骤、失败处理、验证方式）

解析是 fail closed 的：frontmatter 损坏、status 非法、name 与目录名不一致都会抛
SkillParseError，由 Registry 记录并跳过——绝不把无法确认的内容默认放进模型上下文。
唯一例外是 builtin：内置技能允许省略 status（视为 active），兼容既有内置文件。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel as PydanticModel
from pydantic import Field

SkillSource = Literal["builtin", "generated", "user"]
SkillStatus = Literal["draft", "active", "disabled"]

_SOURCES: frozenset[str] = frozenset(("builtin", "generated", "user"))
_STATUSES: frozenset[str] = frozenset(("draft", "active", "disabled"))
_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_FRONTMATTER_KEYS = frozenset(
    (
        "name",
        "description",
        "status",
        "revision",
        "source",
        "scope",
        "origin_turn",
        "required_tools",
    )
)


class SkillParseError(ValueError):
    """SKILL.md 无法被安全解析；调用方必须跳过该文件而不是猜测。"""


def normalize_skill_name(raw: str) -> str:
    """技能名的唯一规范形式：目录名与 frontmatter name 都用它，避免身份分裂。"""
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", (raw or "").strip()).strip("_").lower()


def frontmatter_value(raw: str) -> str:
    """把任意文本压成 JSON 字符串形式的单行 frontmatter 值。"""
    collapsed = re.sub(r"\s+", " ", (raw or "").replace("\x00", "")).strip()
    return json.dumps(collapsed, ensure_ascii=False)


def _unquote_frontmatter(raw: str) -> str:
    text = (raw or "").strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        try:
            return str(json.loads(text))
        except json.JSONDecodeError:
            return text
    return text


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

    @classmethod
    def from_file(cls, path: Path) -> "Skill":
        """严格解析 SKILL.md；任何无法确认的元数据都抛 SkillParseError。"""
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillParseError(f"读取失败: {path}") from exc
        meta, body = _split_frontmatter(text, path)
        source = _parse_source(meta, path)
        name = normalize_skill_name(meta.get("name") or path.parent.name)
        if not name or name != path.parent.name.lower():
            raise SkillParseError(
                f"name 与目录名不一致: name={name!r} dir={path.parent.name!r}"
            )
        if not _NAME_RE.match(name):
            raise SkillParseError(f"name 含非法字符: {name!r}")
        return cls(
            name=name,
            description=meta.get("description", ""),
            body=body,
            path=str(path),
            status=_parse_status(meta, source),
            revision=_parse_revision(meta),
            source=source,
            scope_key=meta.get("scope", ""),
            origin_turn_id=meta.get("origin_turn", ""),
            required_tools=_parse_required_tools(meta),
        )


def _split_frontmatter(text: str, path: Path) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        raise SkillParseError(f"缺少 frontmatter: {path}")
    end = text.find("\n---", 3)
    if end == -1:
        raise SkillParseError(f"frontmatter 未闭合: {path}")
    meta: dict[str, str] = {}
    for line in text[3:end].strip().splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if ":" not in stripped:
            raise SkillParseError(f"frontmatter 行不是 key: value: {stripped!r}")
        key, _, value = stripped.partition(":")
        key = key.strip()
        if key in _FRONTMATTER_KEYS:
            meta[key] = _unquote_frontmatter(value.strip())
    return meta, text[end + 4 :].strip()


def _parse_source(meta: dict[str, str], path: Path) -> SkillSource:
    raw = (meta.get("source") or "").strip().lower()
    if raw:
        if raw not in _SOURCES:
            raise SkillParseError(f"source 非法: {raw!r}")
        return raw  # type: ignore[return-value]
    return "builtin" if "builtin" in path.parts else "generated"


def _parse_status(meta: dict[str, str], source: SkillSource) -> SkillStatus:
    raw = (meta.get("status") or "").strip().lower()
    if not raw:
        # 内置技能由人工维护，允许省略；自动生成/会话写入必须显式声明
        if source == "builtin":
            return "active"
        raise SkillParseError("非 builtin 技能必须显式声明 status")
    if raw not in _STATUSES:
        raise SkillParseError(f"status 非法: {raw!r}")
    return raw  # type: ignore[return-value]


def _parse_revision(meta: dict[str, str]) -> int:
    raw = (meta.get("revision") or "1").strip()
    try:
        revision = int(raw)
    except ValueError as exc:
        raise SkillParseError(f"revision 不是整数: {raw!r}") from exc
    if revision < 1:
        raise SkillParseError(f"revision 必须 >= 1: {revision}")
    return revision


def _parse_required_tools(meta: dict[str, str]) -> tuple[str, ...]:
    raw = meta.get("required_tools") or ""
    return tuple(
        item.strip() for item in re.split(r"[,\s]+", raw) if item.strip()
    )
