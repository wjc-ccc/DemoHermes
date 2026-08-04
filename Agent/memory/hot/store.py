"""
HotStore — 精选记忆 Markdown 文档存储

hot类别能力限定： 物理存储对应的md文件 user.md group.md  memory.md
     对应的类别初始化路径
	提供read外部文档接口
	提供版本查询外部文档接口
	提供对于markdown文件操作的一些工具 比如replace 和精确地增删改查 （markdown本身应该如何增删改查？）
	提供对于写文件的锁和对应的retry退避机制

HotStore 是一个 Markdown 文档的 CRUD 适配器：
每次读都重新读盘（无内存缓存，手工编辑立即可见）；
文档用 <!-- memory-entry:ID -->围栏切分成若干 entry，每个 entry 由 Store 在 add 时生成唯一 id 作为定位身份；
写入走"临时文件 + fsync + 原子，replace"保证不出半截文件，并发安全由外层锁 + revision 校验负责。


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

## entry id
def new_entry_id() -> str:
    return "e_" + uuid.uuid4().hex[:12]

### 渲染entry
def render_entry(entry_id: str, content: str) -> str:
    return f"{ENTRY_OPEN.format(entry_id=entry_id)}\n{content}\n{ENTRY_CLOSE.format(entry_id=entry_id)}"

### 去重比较
def _normalize(content: str) -> str:
    """去重比较用：NFKC + casefold + 去首尾空白；存储仍保留原文。"""
    return unicodedata.normalize("NFKC", content).casefold().strip()

### 校验markdown文件
def is_valid_document(text: str) -> bool:
    """校验文档是可解析、entry 格式完好的候选。

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


### Markdown 文档加载
class HotStore:
    """单个 Markdown 文档（一个 scope 的一个 target）。

    mutation 分两步：candidate_* 只算新文本不写盘，
    由 Manager 校验预算后决定 write_text / 备份+缩减。
    """

    def __init__(self, path: Path, title: str, id_factory=new_entry_id):
        self._path = path
        self._title = title
        self._id_factory = id_factory

    ### 接口读取文档
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

        文档被外部改到超限时，build_context 不改磁盘：
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

    # ---- 增删改 本质都是基于 content和entry id进行增删改 ----
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
        """原子替换：同目录唯一临时文件 → flush+fsync → os.replace。

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

        reader 每次只持有微秒级，做有界退避重试。
        """
        for attempt in range(attempts):
            try:
                os.replace(tmp, self._path)
                return
            except PermissionError:
                if attempt == attempts - 1:
                    raise
                time.sleep(0.005)
