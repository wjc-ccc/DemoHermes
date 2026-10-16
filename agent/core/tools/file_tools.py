"""本机文件工具：read_file、write_file、edit_file、glob。

路径必须是绝对路径。已有文件在本会话被完整读过之后才能修改或覆盖。
不限制目录范围。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from agent.core.tools.base import Tool
from agent.core.tools.local_support import (
    GLOB_LIMIT,
    IMAGE_SUFFIXES,
    READ_PAGE_LINES,
    absolute_path,
    cwd_for,
    expand_braces,
    has_full_read,
    mark_full_read,
)
from agent.types.action.policy import ToolPolicy
from agent.types.tools import ToolCallResult, ToolInvocationContext


def _read_policy(when: str, when_not: str, timeout: float) -> ToolPolicy:
    return ToolPolicy(
        when_to_use=when,
        when_not_to_use=when_not,
        effect="read",
        risk="low",
        max_calls_per_turn=64,
        timeout_s=timeout,
    )


def _write_policy(when: str, when_not: str, timeout: float) -> ToolPolicy:
    return ToolPolicy(
        when_to_use=when,
        when_not_to_use=when_not,
        effect="write",
        risk="medium",
        idempotency="non_idempotent",
        max_calls_per_turn=64,
        timeout_s=timeout,
    )


def _fail(message: str) -> ToolCallResult:
    return ToolCallResult(ok=False, error=message)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".agent-tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _read_text(path: Path) -> str | ToolCallResult:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return _fail(f"无法读取文件: {exc}")
    if b"\0" in raw[:8192]:
        return _fail("该文件不是 UTF-8 文本。请用 bash 处理，不要用 read_file 或 edit_file。")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeError:
        return _fail("该文件不是 UTF-8。read_file 和 edit_file 不会改写它。请用 bash 按原编码修改。")


def _page(arguments: dict) -> tuple[int, int] | ToolCallResult:
    offset = arguments.get("offset", 1)
    limit = arguments.get("limit", READ_PAGE_LINES)
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 1:
        return _fail("offset 必须是从 1 开始的整数。")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        return _fail("limit 必须是正整数。")
    return offset, limit


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "读取本机文本文件，并带上行号。file_path 必须是绝对路径。"
        "默认最多返回 2000 行；更长的文件用 offset 和 limit 续读。"
        "结果标明部分视图时，这次阅读不能作为 edit_file 或 write_file 的依据。"
        "不能读目录。图片和 PDF 请改用 bash。"
    )
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "file_path": {"type": "string", "description": "绝对路径"},
            "offset": {"type": "integer", "minimum": 1, "description": "起始行号，从 1 开始"},
            "limit": {"type": "integer", "minimum": 1, "description": "最多返回的行数，默认 2000"},
        },
        "required": ["file_path"],
    }
    action_policy = _read_policy(
        "需要查看文件的具体内容时使用",
        "不要用它列目录、按文件名搜索或按内容搜索",
        60,
    )

    def execute(self, arguments: dict, *, context: ToolInvocationContext) -> ToolCallResult:
        located = absolute_path(arguments.get("file_path"))
        if isinstance(located, str):
            return _fail(located)
        page = _page(arguments)
        if isinstance(page, ToolCallResult):
            return page
        offset, limit = page
        try:
            if not located.exists():
                return _fail(f"文件不存在: {located}")
            if located.is_dir():
                return _fail("这是目录，不能用 read_file 读取。列出内容请用 bash。")
        except OSError as exc:
            return _fail(f"无法访问路径: {exc}")
        suffix = located.suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            return _fail("这是图片。当前工具结果只有文本，请用 bash 处理该文件。")
        if suffix == ".pdf":
            return _fail("这是 PDF。当前工具结果只有文本，请用 bash 处理该文件。")
        if suffix == ".ipynb":
            return self._notebook(located, context)
        loaded = _read_text(located)
        if isinstance(loaded, ToolCallResult):
            return loaded
        lines = loaded.splitlines()
        resolved = str(located.resolve())
        if not lines:
            if offset > 1:
                return _fail(f"偏移 {offset} 已超过文件末尾。该文件是空的。")
            mark_full_read(context.session_id, located)
            return ToolCallResult(
                ok=True,
                result_text=f"文件：{resolved}\n文件存在，但内容为空。",
                data={"file_path": resolved, "full_read": True, "line_count": 0},
            )
        if offset > len(lines):
            return _fail(f"偏移 {offset} 已超过文件末尾。该文件共 {len(lines)} 行。")
        chunk = lines[offset - 1 : offset - 1 + limit]
        full = offset == 1 and len(chunk) == len(lines)
        if full:
            mark_full_read(context.session_id, located)
        numbered = "\n".join(f"{number:>6}\t{line}" for number, line in enumerate(chunk, offset))
        notice = ""
        if not full:
            next_offset = offset + len(chunk)
            notice = (
                f"\nPARTIAL view：已返回第 {offset}-{offset + len(chunk) - 1} 行，共 {len(lines)} 行。"
                f"用 offset={next_offset} 继续读取。这次阅读是部分视图，不能作为 edit_file 或 write_file 的依据。"
            )
        return ToolCallResult(
            ok=True,
            result_text=f"文件：{resolved}\n{numbered}{notice}",
            data={"file_path": resolved, "full_read": full, "line_count": len(lines), "offset": offset},
        )

    def _notebook(self, path: Path, context: ToolInvocationContext) -> ToolCallResult:
        loaded = _read_text(path)
        if isinstance(loaded, ToolCallResult):
            return loaded
        try:
            document = json.loads(loaded)
        except json.JSONDecodeError as exc:
            return _fail(f"笔记本不是合法 JSON: {exc}")
        cells = document.get("cells", [])
        blocks = []
        for index, cell in enumerate(cells, 1):
            source = cell.get("source", "")
            if isinstance(source, list):
                source = "".join(source)
            blocks.append(f"## cell {index} ({cell.get('cell_type', 'unknown')})\n{source}")
        mark_full_read(context.session_id, path)
        resolved = str(path.resolve())
        body = "\n".join(blocks) if blocks else "笔记本没有单元格。"
        return ToolCallResult(
            ok=True,
            result_text=f"文件：{resolved}\n{body}",
            data={"file_path": resolved, "full_read": True, "cells": len(cells)},
        )


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "新建文件，或用完整内容覆盖已有文件。不追加。"
        "file_path 必须是绝对路径。覆盖已有文件前，本会话必须先用 read_file 完整读取它。"
        "局部修改请用 edit_file。"
    )
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "file_path": {"type": "string", "description": "绝对路径"},
            "content": {"type": "string", "description": "文件的完整新内容"},
        },
        "required": ["file_path", "content"],
    }
    action_policy = _write_policy(
        "新建文件，或在完整读过之后整文件重写",
        "不要用它做局部替换，也不要在没读过已有文件时覆盖",
        60,
    )

    def execute(self, arguments: dict, *, context: ToolInvocationContext) -> ToolCallResult:
        located = absolute_path(arguments.get("file_path"))
        if isinstance(located, str):
            return _fail(located)
        content = arguments.get("content")
        if not isinstance(content, str):
            return _fail("content 必须是字符串。")
        try:
            exists = located.exists()
            if exists and located.is_dir():
                return _fail("目标是目录，不能用 write_file 覆盖。")
        except OSError as exc:
            return _fail(f"无法访问路径: {exc}")
        if exists and not has_full_read(context.session_id, located):
            return _fail("覆盖已有文件前，必须在本会话用 read_file 完整读取该绝对路径。部分视图不算。")
        try:
            _atomic_write(located, content.encode("utf-8"))
        except (OSError, UnicodeError) as exc:
            return _fail(f"写入失败: {exc}")
        mark_full_read(context.session_id, located)
        resolved = str(located.resolve())
        return ToolCallResult(
            ok=True,
            result_text=f"已写入 {resolved}",
            data={"file_path": resolved},
        )


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "对已有文本文件做精确字符串替换。file_path 必须是绝对路径。"
        "本会话必须先用 read_file 完整读取该文件。old_string 必须和文件里的原文一致，默认只能出现一次。"
        "多处替换时设置 replace_all 为 true。不是正则。"
    )
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "file_path": {"type": "string", "description": "绝对路径"},
            "old_string": {"type": "string", "description": "要替换的原文，必须唯一，除非 replace_all 为 true"},
            "new_string": {"type": "string", "description": "替换后的文本，空字符串表示删除"},
            "replace_all": {"type": "boolean", "description": "为 true 时替换每一处，默认 false"},
        },
        "required": ["file_path", "old_string", "new_string"],
    }
    action_policy = _write_policy(
        "已经完整读过文件，并且只改其中一段时使用",
        "不要用它新建文件，也不要在没读过或只读了部分视图时使用",
        60,
    )

    def execute(self, arguments: dict, *, context: ToolInvocationContext) -> ToolCallResult:
        located = absolute_path(arguments.get("file_path"))
        if isinstance(located, str):
            return _fail(located)
        old = arguments.get("old_string")
        new = arguments.get("new_string")
        replace_all = bool(arguments.get("replace_all", False))
        if not isinstance(old, str) or not isinstance(new, str):
            return _fail("old_string 和 new_string 必须是字符串。")
        if old == "":
            return _fail("old_string 不能为空。")
        if old == new:
            return _fail("old_string 与 new_string 相同，文件未修改。")
        try:
            if not located.exists():
                return _fail("文件不存在。新建文件请使用 write_file。")
            if located.is_dir():
                return _fail("目标是目录，不能用 edit_file 修改。")
        except OSError as exc:
            return _fail(f"无法访问路径: {exc}")
        if not has_full_read(context.session_id, located):
            return _fail("修改前必须在本会话用 read_file 完整读取该绝对路径。部分视图不算。")
        loaded = _read_text(located)
        if isinstance(loaded, ToolCallResult):
            return loaded
        newline = "\r\n" if "\r\n" in loaded else "\n"
        text = loaded.replace("\r\n", "\n")
        count = text.count(old)
        if count == 0:
            return _fail("old_string 没有出现在文件中。请根据 read_file 的原文重新复制。")
        if count > 1 and not replace_all:
            return _fail(f"old_string 出现了 {count} 次。请增加上下文使它唯一，或设置 replace_all 为 true。")
        updated = text.replace(old, new, count if replace_all else 1)
        if newline == "\r\n":
            updated = updated.replace("\n", "\r\n")
        try:
            _atomic_write(located, updated.encode("utf-8"))
        except OSError as exc:
            return _fail(f"写入失败: {exc}")
        replaced = count if replace_all else 1
        resolved = str(located.resolve())
        return ToolCallResult(
            ok=True,
            result_text=f"已替换 {replaced} 处。文件：{resolved}",
            data={"file_path": resolved, "replacements": replaced},
        )


class GlobTool(Tool):
    name = "glob"
    description = (
        "按文件名模式查找文件。pattern 支持 ** 和一层花括号。"
        "path 如果提供，必须是绝对路径；不提供时从当前会话工作目录开始。"
        "结果按修改时间从新到旧排列，最多 100 条。默认包含被 git 忽略的文件。"
    )
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "pattern": {"type": "string", "description": "相对 glob，例如 **/*.py 或 *.{json,yaml}"},
            "path": {"type": "string", "description": "开始搜索的绝对目录，省略则用会话工作目录"},
        },
        "required": ["pattern"],
    }
    action_policy = _read_policy(
        "知道文件名或扩展名、要列出匹配路径时使用",
        "不要用它读取文件内容或按文本搜索",
        120,
    )

    def execute(self, arguments: dict, *, context: ToolInvocationContext) -> ToolCallResult:
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            return _fail("pattern 不能为空。")
        pattern = pattern.strip()
        if Path(pattern).is_absolute():
            return _fail("pattern 是相对模式。目录请放在 path，并且 path 必须是绝对路径。")
        root = cwd_for(context.session_id)
        if "path" in arguments and arguments.get("path") is not None:
            located = absolute_path(arguments.get("path"))
            if isinstance(located, str):
                return _fail(located)
            root = located
        try:
            if not root.is_dir():
                return _fail(f"搜索目录不存在: {root}")
        except OSError as exc:
            return _fail(f"无法访问目录: {exc}")
        found: dict[Path, Path] = {}
        try:
            for expanded in expand_braces(pattern):
                for item in root.glob(expanded):
                    if item.is_file():
                        found[item.resolve()] = item
        except (OSError, ValueError) as exc:
            return _fail(f"glob 无法执行: {exc}")
        ordered = sorted(found.values(), key=lambda item: item.stat().st_mtime, reverse=True)
        truncated = len(ordered) > GLOB_LIMIT
        shown = ordered[:GLOB_LIMIT]
        if not shown:
            text = "没有匹配的文件。"
        else:
            text = "\n".join(str(item.resolve()) for item in shown)
        if truncated:
            text += "\n结果已截断为 100 条。请收窄 pattern。"
        return ToolCallResult(
            ok=True,
            result_text=text,
            data={"count": len(shown), "truncated": truncated, "root": str(root.resolve())},
        )
