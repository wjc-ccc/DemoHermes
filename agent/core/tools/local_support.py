"""本机工具共用的路径、阅读记录、gitignore 和 shell 会话状态。

阅读记录和 shell 工作目录只留在进程里，按 session_id 分开。
不写进 action 观察账，也不做工作区囚禁。
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import threading
from pathlib import Path


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tif", ".tiff"}
READ_PAGE_LINES = 2000
GLOB_LIMIT = 100
BASH_INLINE_CHARS = 30000
BASH_PREVIEW_CHARS = 2000
BASH_DEFAULT_TIMEOUT_MS = 120_000
BASH_MAX_TIMEOUT_MS = 600_000
CWD_MARKER = "__AGENT_CWD__"

_lock = threading.Lock()
_full_reads: dict[str, set[str]] = {}
_cwd: dict[str, str] = {}
_background: dict[str, subprocess.Popen] = {}


def absolute_path(value: object) -> Path | str:
    text = str(value or "").strip()
    if not text:
        return "路径必须是绝对路径。请改成绝对路径后重试。"
    path = Path(text)
    if not path.is_absolute():
        return "路径必须是绝对路径。请改成绝对路径后重试。"
    return path


def mark_full_read(session_id: str, path: Path) -> None:
    key = str(path.resolve())
    with _lock:
        _full_reads.setdefault(session_id, set()).add(key)


def has_full_read(session_id: str, path: Path) -> bool:
    key = str(path.resolve())
    with _lock:
        return key in _full_reads.get(session_id, set())


def cwd_for(session_id: str) -> Path:
    with _lock:
        stored = _cwd.get(session_id)
    if stored:
        return Path(stored)
    return Path.cwd()


def set_cwd(session_id: str, path: Path) -> None:
    with _lock:
        _cwd[session_id] = str(path.resolve())


def track_background(task_id: str, process: subprocess.Popen) -> None:
    with _lock:
        _background[task_id] = process


def stop_background() -> None:
    with _lock:
        processes = list(_background.values())
        _background.clear()
    for process in processes:
        _stop_process(process)


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
    else:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def expand_braces(pattern: str) -> list[str]:
    match = re.search(r"\{([^{}]+)\}", pattern)
    if match is None:
        return [pattern]
    prefix, suffix = pattern[: match.start()], pattern[match.end() :]
    expanded: list[str] = []
    for part in match.group(1).split(","):
        expanded.extend(expand_braces(prefix + part + suffix))
    return expanded


def parse_gitignore(path: Path) -> list[tuple[bool, str]]:
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    rules: list[tuple[bool, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:].strip()
        if line:
            rules.append((negated, line.replace("\\", "/")))
    return rules


def _rule_matches(relative: str, is_dir: bool, pattern: str) -> bool:
    directory_only = pattern.endswith("/")
    if directory_only:
        pattern = pattern.rstrip("/")
        if not is_dir:
            return False
    anchored = pattern.startswith("/")
    if anchored:
        pattern = pattern[1:]
    name = relative.rsplit("/", 1)[-1]
    if anchored or "/" in pattern:
        return fnmatch.fnmatch(relative, pattern)
    return fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(relative, pattern)


def path_ignored(path: Path, is_dir: bool, rules: list[tuple[Path, list[tuple[bool, str]]]]) -> bool:
    ignored = False
    resolved = path.resolve()
    for base, specs in rules:
        try:
            relative = resolved.relative_to(base.resolve()).as_posix()
        except ValueError:
            continue
        for negated, pattern in specs:
            if _rule_matches(relative, is_dir, pattern):
                ignored = not negated
    return ignored


def gitignore_rules(directory: Path) -> list[tuple[Path, list[tuple[bool, str]]]]:
    """从搜索目录的父级收集已有规则；当前目录的规则由遍历时再追加。"""
    chain: list[Path] = []
    current = directory.resolve().parent
    while True:
        chain.append(current)
        if (current / ".git").exists() or current.parent == current:
            break
        current = current.parent
    loaded: list[tuple[Path, list[tuple[bool, str]]]] = []
    for base in reversed(chain):
        specs = parse_gitignore(base / ".gitignore")
        if specs:
            loaded.append((base, specs))
    return loaded


def iter_search_files(root: Path):
    def walk(directory: Path, rules: list[tuple[Path, list[tuple[bool, str]]]]):
        own = parse_gitignore(directory / ".gitignore")
        if own:
            rules = [*rules, (directory, own)]
        try:
            children = list(directory.iterdir())
        except OSError:
            return
        for child in children:
            if child.name == ".git":
                continue
            try:
                is_dir = child.is_dir()
            except OSError:
                continue
            if path_ignored(child, is_dir, rules):
                continue
            if is_dir:
                if child.is_symlink():
                    continue
                yield from walk(child, rules)
            elif child.is_file() and not child.is_symlink():
                yield child

    yield from walk(root, gitignore_rules(root))


def glob_match(relative: str, pattern: str) -> bool:
    name = relative.rsplit("/", 1)[-1]
    if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(name, pattern):
        return True
    regex = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
    return re.fullmatch(regex, relative) is not None


def split_cwd_marker(text: str) -> tuple[str, str | None]:
    cwd = None
    kept: list[str] = []
    for line in text.splitlines():
        if line.startswith(CWD_MARKER):
            cwd = line[len(CWD_MARKER) :]
        else:
            kept.append(line)
    return "\n".join(kept), cwd
