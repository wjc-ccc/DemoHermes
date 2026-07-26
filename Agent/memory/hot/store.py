"""
HotStore — 精选记忆 Markdown 文档存储（7.23 方案 §3.4 / §3.5）

文档结构：固定标题 + 若干 entry，entry 用稳定 ID 围栏标记：

    # USER

    <!-- memory-entry:e_01JABC -->
    用户偏好简洁、先给结论。
    <!-- /memory-entry:e_01JABC -->

规则（§3.5）：
    - entry_id 由 Store 生成，调用方不可指定；
    - revision 为完整文件 bytes 的 SHA-256；
    - 写入 UTF-8、LF；operator 可直接阅读文件。

当前只实现 Slice 1 需要的最小面：整文件读取 + add 追加。
replace/remove/解析在 Slice 2 随 CRUD 测试补齐。
"""
from __future__ import annotations

import hashlib
import os
import re
import time
import unicodedata
import uuid
from pathlib import Path

from ..types import MemoryEntry

ENTRY_OPEN = "<!-- memory-entry:{entry_id} -->"
ENTRY_CLOSE = "<!-- /memory-entry:{entry_id} -->"

_ENTRY_RE = re.compile(
    r"<!-- memory-entry:(?P<id>\S+) -->\n(?P<content>.*?)\n<!-- /memory-entry:(?P=id) -->",
    re.DOTALL,
)


def new_entry_id() -> str:
    return "e_" + uuid.uuid4().hex[:12]


def render_entry(entry_id: str, content: str) -> str:
    return f"{ENTRY_OPEN.format(entry_id=entry_id)}\n{content}\n{ENTRY_CLOSE.format(entry_id=entry_id)}"


def _normalize(content: str) -> str:
    """去重比较用：NFKC + casefold + 去首尾空白；存储仍保留原文。"""
    return unicodedata.normalize("NFKC", content).casefold().strip()


def is_valid_document(text: str) -> bool:
    """校验文档是可解析、entry 格式完好的候选（§4.4 缩减输出校验）。

    - 首行是 Markdown 标题；
    - 每个 open 围栏都有配对 close，且都能解析出非空 entry。
    """
    if not text.startswith("# "):
        return False
    opens = text.count("<!-- memory-entry:")
    closes = text.count("<!-- /memory-entry:")
    if opens != closes:
        return False
    parsed = [m for m in _ENTRY_RE.finditer(text) if m.group("content").strip()]
    return len(parsed) == opens


class HotStore:
    """单个 Markdown 文档（一个 scope 的一个 target）。

    mutation 分两步（§4.4 事务）：candidate_* 只算新文本不写盘，
    由 Manager 校验预算后决定 write_text / 备份+缩减。
    """

    def __init__(self, path: Path, title: str, id_factory=new_entry_id):
        self._path = path
        self._title = title
        self._id_factory = id_factory

    def read_text(self) -> str:
        """整文件内容；文件不存在时等价于只有标题的空文档。"""
        if not self._path.exists():
            return f"# {self._title}\n"
        return self._path.read_text(encoding="utf-8")

    def revision(self) -> str:
        return hashlib.sha256(self.read_text().encode("utf-8")).hexdigest()

    def entries(self) -> tuple[MemoryEntry, ...]:
        """按文档顺序解析 entry 围栏；无法配对的残留标记直接忽略。"""
        return tuple(
            MemoryEntry(entry_id=m.group("id"), content=m.group("content"))
            for m in _ENTRY_RE.finditer(self.read_text())
        )

    @staticmethod
    def revision_of(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def bounded_text(self, max_chars: int) -> tuple[str, bool]:
        """返回 (注入用文本, 是否发生了降级)。

        文档被外部改到超限时，build_context 不改磁盘（§4.4 末段）：
        只在 entry 边界取前缀——保留标题与尽可能多的完整 entry，
        绝不截出半截 entry。
        """
        text = self.read_text()
        if len(text) <= max_chars:
            return text, False
        degraded = f"# {self._title}\n"
        for entry in self.entries():
            candidate = (
                degraded.rstrip("\n") + "\n\n"
                + render_entry(entry.entry_id, entry.content) + "\n"
            )
            if len(candidate) > max_chars:
                break
            degraded = candidate
        return degraded, True

    # ---- candidate：只计算，不写盘 ----
    def candidate_add(self, content: str) -> tuple[str, str | None]:
        """返回 (entry_id, candidate_text)；重复内容返回 (既有 ID, None) 表示无需写。"""
        wanted = _normalize(content)
        for entry in self.entries():
            if _normalize(entry.content) == wanted:
                return entry.entry_id, None
        entry_id = self._id_factory()
        text = self.read_text().rstrip("\n") + "\n\n" + render_entry(entry_id, content) + "\n"
        return entry_id, text

    def candidate_replace(self, entry_id: str, content: str) -> str | None:
        """精确替换一个 entry 的内容；ID 未精确命中一个时返回 None。"""
        text = self.read_text()
        pattern = re.compile(
            r"<!-- memory-entry:" + re.escape(entry_id) + r" -->\n.*?\n<!-- /memory-entry:"
            + re.escape(entry_id) + r" -->",
            re.DOTALL,
        )
        if len(pattern.findall(text)) != 1:
            return None
        return pattern.sub(render_entry(entry_id, content), text)

    def candidate_remove(self, entry_id: str) -> str | None:
        """精确删除一个 entry（含其围栏与多余空行）；未命中返回 None。"""
        text = self.read_text()
        pattern = re.compile(
            r"\n*<!-- memory-entry:" + re.escape(entry_id) + r" -->\n.*?\n<!-- /memory-entry:"
            + re.escape(entry_id) + r" -->",
            re.DOTALL,
        )
        if len(pattern.findall(text)) != 1:
            return None
        return pattern.sub("", text)

    @property
    def path(self) -> Path:
        return self._path

    def write_text(self, text: str) -> None:
        """原子替换：同目录唯一临时文件 → flush+fsync → os.replace（§5.1）。

        调用方必须已持有 PathLockRegistry 中本文件的锁。
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f".{self._path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            self._replace_with_retry(tmp)
        finally:
            tmp.unlink(missing_ok=True)

    def _replace_with_retry(self, tmp: Path, attempts: int = 200) -> None:
        """os.replace 在 Windows 上会因 reader 瞬时持有句柄而 PermissionError，

        reader 每次只持有微秒级，做有界退避重试（§5.1 单进程保证内）。
        """
        for attempt in range(attempts):
            try:
                os.replace(tmp, self._path)
                return
            except PermissionError:
                if attempt == attempts - 1:
                    raise
                time.sleep(0.005)
