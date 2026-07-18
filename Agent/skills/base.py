"""
Skill — 技能数据结构 + SKILL.md 解析

SKILL.md 文件格式（frontmatter + 正文）：

    ---
    name: weekly_report
    description: 把零散工作记录整理成结构化周报
    ---
    （正文：执行步骤、输出格式要求等，Markdown 任意内容）

不引入 yaml 依赖，frontmatter 只支持最简单的 key: value 行。
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel as PydanticModel
from pydantic import Field


class Skill(PydanticModel):
    name: str = Field(default_factory=str, description="技能名（目录名 / frontmatter name）")
    description: str = Field(default_factory=str, description="一句话描述（Level 1 注入用）")
    body: str = Field(default_factory=str, description="完整正文（Level 3 加载用）")
    path: str = Field(default_factory=str, description="SKILL.md 所在路径")

    def summary_line(self) -> str:
        """Level 1：注入 system prompt 的一行摘要。"""
        return f"- {self.name}: {self.description}"

    @classmethod
    def from_file(cls, path: Path) -> "Skill":
        """解析 SKILL.md。frontmatter 缺失/损坏时降级：用目录名当 name，全文当 body。"""
        text = path.read_text(encoding="utf-8")
        meta: dict[str, str] = {}
        body = text
        if text.startswith("---"):
            # 取第一个 --- 与第二个 --- 之间的内容作为 frontmatter
            end = text.find("\n---", 3)
            if end != -1:
                for line in text[3:end].strip().splitlines():
                    if ":" in line:
                        key, _, value = line.partition(":")
                        meta[key.strip()] = value.strip()
                body = text[end + 4:].strip()
        return cls(
            name=meta.get("name") or path.parent.name,
            description=meta.get("description", ""),
            body=body,
            path=str(path),
        )
