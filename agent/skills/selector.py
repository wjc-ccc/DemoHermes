"""SkillSelector — Hermes 式 progressive disclosure：按 query 选 L1 Skill。

v0：词重叠打分（中英空白/字符切分）；无匹配时返回空（宁缺毋滥，避免灌满无关 skill）。
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

from agent.types.skills import Skill


def _tokens(text: str) -> set[str]:
    raw = unicodedata.normalize("NFKC", text or "").casefold()
    latin = set(re.findall(r"[a-z0-9_]{2,}", raw))
    chars = re.findall(r"[\u4e00-\u9fff]", raw)
    grams: set[str] = set(chars)
    for i in range(len(chars) - 1):
        grams.add(chars[i] + chars[i + 1])
    # 过滤无信息单字（过短拉丁已在上面要求 ≥2）
    return latin | {g for g in grams if len(g) >= 2 or g in chars and len(chars) <= 4}


class SkillSelector:
    """从候选 skills 中按 query 相关性选出 top_k。"""

    def score(self, query: str, skill: Skill) -> float:
        q = _tokens(query)
        if not q:
            return 0.0
        hay = _tokens(f"{skill.name} {skill.description}")
        if not hay:
            return 0.0
        overlap = q & hay
        if not overlap:
            return 0.0
        return len(overlap) / max(len(q), 1)

    def select(
        self,
        query: str,
        skills: Sequence[Skill],
        *,
        top_k: int = 5,
        min_score: float = 0.01,
    ) -> list[Skill]:
        if top_k < 1:
            return []
        scored = [
            (self.score(query, s), s)
            for s in skills
        ]
        scored = [(sc, s) for sc, s in scored if sc >= min_score]
        scored.sort(key=lambda x: (-x[0], x[1].name))
        return [s for _, s in scored[:top_k]]
