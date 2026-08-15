"""GrowAndRefineCompactor — 结构化 Hot 缩减（8.04 Phase 2.1）。

ACE grow-and-refine：去重、优先丢 harmful、再丢普通笔记，保留健康 playbook 原文。
禁止把多条策略 LLM/散文式压成一句摘要（anti brevity bias / context collapse）。

实现 MemoryCompactor seam：compact(candidate, max_chars) -> str
输出必须通过 HotStore.is_valid_document，且 len <= max_chars。
"""
from __future__ import annotations

import re
import unicodedata

from ..memory.hot.store import HotStore, render_entry

_ENTRY_RE = re.compile(
    r"<!-- memory-entry:(?P<id>\S+) -->\n(?P<content>.*?)\n<!-- /memory-entry:(?P=id) -->",
    re.DOTALL,
)

HARMFUL_MARK = "[playbook][harmful]"
PLAYBOOK_MARK = "[playbook]"


def _normalize(content: str) -> str:
    return unicodedata.normalize("NFKC", content).casefold().strip()


def _title_of(candidate: str) -> str:
    first = candidate.split("\n", 1)[0].strip()
    if first.startswith("# "):
        return first[2:].strip() or "MEMORY"
    return "MEMORY"


def _parse_entries(candidate: str) -> list[tuple[str, str]]:
    return [
        (m.group("id"), m.group("content"))
        for m in _ENTRY_RE.finditer(candidate)
        if m.group("content").strip()
    ]


def _is_harmful(content: str) -> bool:
    return content.lstrip().startswith(HARMFUL_MARK)


def _is_playbook(content: str) -> bool:
    head = content.lstrip()
    return head.startswith(PLAYBOOK_MARK)


def _priority_key(content: str) -> int:
    """越小越优先保留。"""
    if _is_harmful(content):
        return 2
    if _is_playbook(content):
        return 0
    return 1


def _rebuild(title: str, entries: list[tuple[str, str]]) -> str:
    parts = [f"# {title}\n"]
    for entry_id, content in entries:
        parts.append("\n" + render_entry(entry_id, content) + "\n")
    return "".join(parts)


class GrowAndRefineCompactor:
    """默认 Compactor：结构化裁剪，不摘要改写。"""

    def compact(self, candidate: str, max_chars: int) -> str:
        title = _title_of(candidate)
        raw = _parse_entries(candidate)

        # 1) 近重复：保留先出现的
        deduped: list[tuple[str, str]] = []
        seen: set[str] = set()
        for entry_id, content in raw:
            key = _normalize(content)
            if key in seen:
                continue
            seen.add(key)
            deduped.append((entry_id, content))

        # 2) 按优先级排序；同优先级保留文档中更靠后（更新）的条目
        ranked = sorted(
            enumerate(deduped),
            key=lambda iv: (_priority_key(iv[1][1]), -iv[0]),
        )
        ordered = [item for _, item in ranked]

        # 3) 贪心装入直到上限（从高优先级到低）
        kept: list[tuple[str, str]] = []
        for entry_id, content in ordered:
            trial = _rebuild(title, kept + [(entry_id, content)])
            if len(trial) <= max_chars:
                kept.append((entry_id, content))

        # 若一个都塞不下：最后手段——截断单条 content（仍保留 entry 围栏，不摘要多条）
        if not kept and ordered:
            entry_id, content = ordered[0]
            kept = [(entry_id, self._fit_single(title, entry_id, content, max_chars))]

        out = _rebuild(title, kept)
        if not HotStore.is_valid_document(out) or len(out) > max_chars:
            # 极端：只留标题（is_valid 要求至少格式完好；空 entry 文档：只有标题时 opens=0）
            # is_valid_document 允许只有标题（opens==closes==0 且 startswith #）
            bare = f"# {title}\n"
            if HotStore.is_valid_document(bare) and len(bare) <= max_chars:
                return bare
            raise ValueError("grow-and-refine 无法产生合法且不超限的文档")
        return out

    @staticmethod
    def _fit_single(title: str, entry_id: str, content: str, max_chars: int) -> str:
        """单条过大时按字符截断正文，保留可解析 entry。"""
        lo, hi = 0, len(content)
        best = ""
        while lo <= hi:
            mid = (lo + hi) // 2
            trial_content = content[:mid].rstrip()
            if not trial_content:
                hi = mid - 1
                continue
            doc = _rebuild(title, [(entry_id, trial_content)])
            if len(doc) <= max_chars and HotStore.is_valid_document(doc):
                best = trial_content
                lo = mid + 1
            else:
                hi = mid - 1
        return best or content[:16]
