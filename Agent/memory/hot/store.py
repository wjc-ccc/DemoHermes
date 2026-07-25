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
import uuid
from pathlib import Path

ENTRY_OPEN = "<!-- memory-entry:{entry_id} -->"
ENTRY_CLOSE = "<!-- /memory-entry:{entry_id} -->"


def new_entry_id() -> str:
    return "e_" + uuid.uuid4().hex[:12]


def render_entry(entry_id: str, content: str) -> str:
    return f"{ENTRY_OPEN.format(entry_id=entry_id)}\n{content}\n{ENTRY_CLOSE.format(entry_id=entry_id)}"


class HotStore:
    """单个 Markdown 文档（一个 scope 的一个 target）。"""

    def __init__(self, path: Path, title: str):
        self._path = path
        self._title = title

    def read_text(self) -> str:
        """整文件内容；文件不存在时等价于只有标题的空文档。"""
        if not self._path.exists():
            return f"# {self._title}\n"
        return self._path.read_text(encoding="utf-8")

    def revision(self) -> str:
        return hashlib.sha256(self.read_text().encode("utf-8")).hexdigest()

    def add(self, content: str) -> tuple[str, str]:
        """追加一条 entry，返回 (entry_id, 新 revision)。最小版：直接整文件重写。"""
        entry_id = new_entry_id()
        text = self.read_text().rstrip("\n") + "\n\n" + render_entry(entry_id, content) + "\n"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(text, encoding="utf-8", newline="\n")
        return entry_id, self.revision()
