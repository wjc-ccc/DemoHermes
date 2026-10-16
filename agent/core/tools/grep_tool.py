"""grep：按 ripgrep 的语义搜索文件内容。

优先调用本机 rg。没有 rg 时用同样的参数和输出形状搜索，并且只应用 .gitignore。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from agent.core.tools.base import Tool
from agent.core.tools.local_support import absolute_path, cwd_for, glob_match, iter_search_files
from agent.types.action.policy import ToolPolicy
from agent.types.tools import ToolCallResult, ToolInvocationContext


TYPE_GLOBS = {
    "py": ["*.py"],
    "python": ["*.py"],
    "js": ["*.js", "*.jsx"],
    "ts": ["*.ts", "*.tsx"],
    "tsx": ["*.tsx"],
    "rust": ["*.rs"],
    "rs": ["*.rs"],
    "go": ["*.go"],
    "md": ["*.md"],
    "json": ["*.json"],
    "yaml": ["*.yml", "*.yaml"],
    "yml": ["*.yml", "*.yaml"],
    "toml": ["*.toml"],
    "html": ["*.html", "*.htm"],
    "css": ["*.css"],
    "sh": ["*.sh"],
    "ps1": ["*.ps1"],
}


class GrepTool(Tool):
    name = "grep"
    description = (
        "用 ripgrep 语法搜索文件内容。pattern 是正则。"
        "output_mode 默认 files_with_matches，也可以是 content 或 count。"
        "默认尊重 .gitignore。要搜索被忽略的文件，把该文件的绝对路径传给 path。"
        "path 如果提供，必须是绝对路径；不提供时从当前会话工作目录开始。"
    )
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "pattern": {"type": "string", "description": "ripgrep 正则"},
            "path": {"type": "string", "description": "绝对文件或目录，省略则用会话工作目录"},
            "glob": {"type": "string", "description": "只保留匹配该 glob 的文件，例如 *.py"},
            "type": {"type": "string", "description": "ripgrep 文件类型，例如 py、rust"},
            "output_mode": {
                "type": "string",
                "enum": ["files_with_matches", "content", "count"],
                "description": "默认 files_with_matches",
            },
            "multiline": {"type": "boolean", "description": "为 true 时可以跨行匹配"},
            "head_limit": {"type": "integer", "minimum": 1, "description": "最多返回多少条匹配"},
            "offset": {"type": "integer", "minimum": 0, "description": "跳过前多少条匹配，从 0 开始"},
        },
        "required": ["pattern"],
    }
    action_policy = ToolPolicy(
        when_to_use="知道要找的文本或正则、但还不知道它在哪个文件里时使用",
        when_not_to_use="不要用它读取整个文件，也不要用它按文件名查找",
        effect="read",
        risk="low",
        max_calls_per_turn=64,
        timeout_s=120,
    )

    def execute(self, arguments: dict, *, context: ToolInvocationContext) -> ToolCallResult:
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or pattern == "":
            return ToolCallResult(ok=False, error="pattern 不能为空。")
        mode = arguments.get("output_mode") or "files_with_matches"
        if mode not in {"files_with_matches", "content", "count"}:
            return ToolCallResult(ok=False, error="output_mode 只能是 files_with_matches、content 或 count。")
        root = cwd_for(context.session_id)
        explicit_file = False
        if arguments.get("path") is not None:
            located = absolute_path(arguments.get("path"))
            if isinstance(located, str):
                return ToolCallResult(ok=False, error=located)
            root = located
            try:
                explicit_file = root.is_file()
                if not root.exists():
                    return ToolCallResult(ok=False, error=f"搜索路径不存在: {root}")
            except OSError as exc:
                return ToolCallResult(ok=False, error=f"无法访问路径: {exc}")
        offset = arguments.get("offset", 0)
        head_limit = arguments.get("head_limit")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            return ToolCallResult(ok=False, error="offset 必须是大于等于 0 的整数。")
        if head_limit is not None and (not isinstance(head_limit, int) or isinstance(head_limit, bool) or head_limit < 1):
            return ToolCallResult(ok=False, error="head_limit 必须是正整数。")
        file_glob = arguments.get("glob")
        file_type = arguments.get("type")
        multiline = bool(arguments.get("multiline", False))
        if file_glob is not None and not isinstance(file_glob, str):
            return ToolCallResult(ok=False, error="glob 必须是字符串。")
        if file_type is not None and not isinstance(file_type, str):
            return ToolCallResult(ok=False, error="type 必须是字符串。")
        matches, error = _search(
            pattern,
            root,
            explicit_file=explicit_file,
            file_glob=file_glob,
            file_type=file_type,
            multiline=multiline,
        )
        if error:
            return ToolCallResult(ok=False, error=error)
        selected = matches[offset : offset + head_limit if head_limit else None]
        if offset and not selected and matches:
            return ToolCallResult(ok=True, result_text="该偏移之后没有匹配。", data={"count": 0})
        if not selected:
            return ToolCallResult(ok=True, result_text="没有匹配。", data={"count": 0})
        text = _render(mode, selected, total=len(matches))
        return ToolCallResult(ok=True, result_text=text, data={"count": len(selected), "total": len(matches)})


def _search(pattern, root: Path, *, explicit_file: bool, file_glob: str | None, file_type: str | None, multiline: bool):
    python_args = dict(
        pattern=pattern,
        root=root,
        explicit_file=explicit_file,
        file_glob=file_glob,
        file_type=file_type,
        multiline=multiline,
    )
    rg = shutil.which("rg")
    if rg:
        matches, error = _search_rg(rg, pattern, root, file_glob=file_glob, file_type=file_type, multiline=multiline)
        if error and ("unrecognized" in error.lower() or "unexpected argument" in error.lower()):
            return _search_python(**python_args)
        return matches, error
    return _search_python(**python_args)


def _search_rg(rg, pattern, root: Path, *, file_glob, file_type, multiline):
    command = [rg, "--json", "--no-config", "--no-require-git"]
    if multiline:
        command.append("-U")
    if file_glob:
        command.extend(["-g", file_glob])
    if file_type:
        command.extend(["-t", file_type])
    command.extend(["--", pattern, str(root)])
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=110,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], f"rg 无法完成搜索: {exc}"
    if completed.returncode not in {0, 1}:
        detail = completed.stderr.strip() or "rg 搜索失败"
        return [], detail
    matches = []
    for line in completed.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "match":
            continue
        data = event.get("data") or {}
        path_text = (data.get("path") or {}).get("text")
        line_text = (data.get("lines") or {}).get("text", "")
        if isinstance(line_text, str):
            line_text = line_text.rstrip("\r\n")
        matches.append((path_text, data.get("line_number"), line_text))
    return matches, None


def _search_python(pattern, root: Path, *, explicit_file: bool, file_glob, file_type, multiline):
    try:
        expression = re.compile(pattern, re.MULTILINE if multiline else 0)
    except re.error as exc:
        return [], f"正则无法编译: {exc}"
    type_globs = None
    if file_type:
        type_globs = TYPE_GLOBS.get(file_type)
        if type_globs is None:
            return [], f"未知的 type: {file_type}。请改用 glob，或安装 rg 后再用它的文件类型。"
    files = [root] if explicit_file else list(iter_search_files(root))
    matches = []
    for file in files:
        relative = file.name if explicit_file else file.resolve().relative_to(root.resolve()).as_posix()
        if file_glob and not glob_match(relative, file_glob):
            continue
        if type_globs and not any(glob_match(relative, item) for item in type_globs):
            continue
        try:
            raw = file.read_bytes()
        except OSError:
            continue
        if b"\0" in raw[:8192]:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeError:
            continue
        if multiline:
            for found in expression.finditer(text):
                line_number = text.count("\n", 0, found.start()) + 1
                line = text.splitlines()[line_number - 1] if text.splitlines() else found.group(0)
                matches.append((str(file.resolve()), line_number, line))
        else:
            for line_number, line in enumerate(text.splitlines(), 1):
                if expression.search(line):
                    matches.append((str(file.resolve()), line_number, line))
    return matches, None


def _render(mode: str, matches, *, total: int) -> str:
    if mode == "files_with_matches":
        seen = []
        for path, _line_number, _line in matches:
            if path not in seen:
                seen.append(path)
        return "\n".join(seen)
    if mode == "count":
        counts: dict[str, int] = {}
        for path, _line_number, _line in matches:
            counts[path] = counts.get(path, 0) + 1
        lines = [f"{path}:{count}" for path, count in counts.items()]
        lines.append(f"合计：{total}")
        return "\n".join(lines)
    return "\n".join(f"{path}:{line_number}:{line}" for path, line_number, line in matches)
