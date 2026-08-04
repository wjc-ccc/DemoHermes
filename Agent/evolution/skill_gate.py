"""SkillLifecycleGate — draft → active 晋升门控（8.04 Phase 3.4）。

新 Skill 默认 draft，不占 L1 摘要槽；连续成功 M 次后写回 status=active。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


def _set_status_in_text(text: str, status: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm = text[3:end]
            body = text[end + 4 :]
            if re.search(r"(?m)^status:\s*", fm):
                fm = re.sub(r"(?m)^status:\s*\S+.*$", f"status: {status}", fm)
            else:
                fm = fm.rstrip("\n") + f"\nstatus: {status}\n"
            return f"---\n{fm.strip()}\n---{body}"
    return f"---\nstatus: {status}\n---\n\n{text}"


class SkillLifecycleGate:
    """记录 use_skill 成功次数；达阈值则晋升 draft → active。"""

    def __init__(
        self,
        registry,
        *,
        success_threshold: int = 2,
    ) -> None:
        if success_threshold < 1:
            raise ValueError("success_threshold 必须 >= 1")
        self._registry = registry
        self._success_threshold = success_threshold
        self._successes: dict[str, int] = {}

    def record_success(self, skill_name: str) -> bool:
        """累计成功；若本次完成 draft→active 晋升则返回 True。"""
        name = (skill_name or "").strip()
        if not name:
            return False
        skill = self._registry.get(name)
        if skill is None:
            return False
        if skill.is_active:
            return False

        self._successes[name] = self._successes.get(name, 0) + 1
        if self._successes[name] < self._success_threshold:
            return False

        path = Path(skill.path)
        if not path.is_file():
            logger.warning("promote 失败：skill 文件不存在 name=%s path=%s", name, path)
            return False
        text = path.read_text(encoding="utf-8")
        path.write_text(_set_status_in_text(text, "active"), encoding="utf-8")
        if hasattr(self._registry, "reload"):
            self._registry.reload()
        logger.info("Skill 晋升 active name=%s", name)
        return True
