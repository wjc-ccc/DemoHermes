"""bash：在本机 shell 里执行命令。

Windows 使用 PowerShell，其他系统使用 bash。同一会话记住工作目录。
前台超时后进程继续在后台运行，Runtime 停止时才会清理。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path
from tempfile import gettempdir

from agent.core.tools.base import Tool
from agent.core.tools.local_support import (
    BASH_DEFAULT_TIMEOUT_MS,
    BASH_INLINE_CHARS,
    BASH_MAX_TIMEOUT_MS,
    BASH_PREVIEW_CHARS,
    CWD_MARKER,
    cwd_for,
    set_cwd,
    split_cwd_marker,
    track_background,
)
from agent.types.action.policy import ToolPolicy
from agent.types.tools import ToolCallResult, ToolInvocationContext


class BashTool(Tool):
    name = "bash"
    description = (
        "在本机执行 shell 命令，用于测试、安装、git 和解释器。"
        "Windows 上实际运行 PowerShell，其他系统运行 bash。"
        "同一会话会记住工作目录；环境变量不跨命令保留。"
        "默认超时 120 秒，最长 600 秒。超时或 run_in_background 为 true 时，命令转入后台并返回输出文件。"
        "读文件、改文件、按名查找和按内容搜索不要用本工具。"
    )
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "command": {"type": "string", "description": "要执行的命令"},
            "timeout": {
                "type": "integer",
                "minimum": 1,
                "maximum": BASH_MAX_TIMEOUT_MS,
                "description": "超时毫秒数，默认 120000，最大 600000",
            },
            "workdir": {"type": "string", "description": "这次命令的工作目录，相对路径相对当前会话目录"},
            "description": {"type": "string", "description": "给这次命令的简短说明"},
            "run_in_background": {"type": "boolean", "description": "为 true 时立即转入后台"},
        },
        "required": ["command"],
    }
    action_policy = ToolPolicy(
        when_to_use="需要运行测试、安装依赖、git 或解释器时使用",
        when_not_to_use="不要用它读取、修改、按文件名查找或按内容搜索文件",
        effect="write",
        risk="medium",
        idempotency="non_idempotent",
        max_calls_per_turn=64,
        timeout_s=600,
    )

    def execute(self, arguments: dict, *, context: ToolInvocationContext) -> ToolCallResult:
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            return ToolCallResult(ok=False, error="command 不能为空。")
        timeout_ms = arguments.get("timeout", BASH_DEFAULT_TIMEOUT_MS)
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool):
            return ToolCallResult(ok=False, error="timeout 必须是毫秒整数。")
        if timeout_ms < 1 or timeout_ms > BASH_MAX_TIMEOUT_MS:
            return ToolCallResult(ok=False, error=f"timeout 必须在 1 到 {BASH_MAX_TIMEOUT_MS} 毫秒之间。")
        start = cwd_for(context.session_id)
        if arguments.get("workdir") is not None:
            workdir = Path(str(arguments["workdir"]))
            if not workdir.is_absolute():
                workdir = start / workdir
            try:
                if not workdir.is_dir():
                    return ToolCallResult(ok=False, error=f"workdir 不是目录: {workdir}")
                start = workdir.resolve()
            except OSError as exc:
                return ToolCallResult(ok=False, error=f"无法访问 workdir: {exc}")
            set_cwd(context.session_id, start)
        task_id = uuid.uuid4().hex[:12]
        output_path, script_path = _paths(context.session_id, task_id)
        script_path.write_text(_script(command, start), encoding="utf-8-sig" if os.name == "nt" else "utf-8")
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        log = output_path.open("w", encoding="utf-8", errors="replace")
        try:
            process = subprocess.Popen(
                _argv(script_path),
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=str(start),
                env=env,
            )
        except OSError as exc:
            log.close()
            return ToolCallResult(ok=False, error=f"无法启动 shell: {exc}")
        finally:
            log.close()
        if arguments.get("run_in_background"):
            track_background(task_id, process)
            return _background_result(task_id, process, output_path, command, timeout_ms, started=True)
        try:
            process.wait(timeout=timeout_ms / 1000)
        except subprocess.TimeoutExpired:
            track_background(task_id, process)
            return _background_result(task_id, process, output_path, command, timeout_ms, started=False)
        return _finished(process, output_path, script_path, command, context, start)


def _paths(session_id: str, task_id: str) -> tuple[Path, Path]:
    safe_session = "".join(char if char.isalnum() else "_" for char in (session_id or "session"))[:40]
    directory = Path(gettempdir()) / "agent-bash" / safe_session
    directory.mkdir(parents=True, exist_ok=True)
    suffix = ".ps1" if os.name == "nt" else ".sh"
    return directory / f"{task_id}.log", directory / f"{task_id}{suffix}"


def _argv(script_path: Path) -> list[str]:
    if os.name == "nt":
        executable = shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
        return [executable, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)]
    return ["bash", str(script_path)]


def _script(command: str, cwd: Path) -> str:
    if os.name == "nt":
        quoted = str(cwd).replace("'", "''")
        return (
            "$ErrorActionPreference = 'Continue'\n"
            "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false\n"
            "$OutputEncoding = [Console]::OutputEncoding\n"
            f"Set-Location -LiteralPath '{quoted}'\n"
            f"{command}\n"
            "$__agent_ok = $?\n"
            "$__agent_code = 0\n"
            "if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) { $__agent_code = $LASTEXITCODE }\n"
            "elseif (-not $__agent_ok) { $__agent_code = 1 }\n"
            f"Write-Output ('{CWD_MARKER}' + (Get-Location).Path)\n"
            "exit $__agent_code\n"
        )
    quoted = str(cwd).replace("'", "'\\''")
    return (
        "set +e\n"
        f"cd -- '{quoted}' || exit 1\n"
        f"{command}\n"
        "__agent_code=$?\n"
        f"printf '%s\\n' \"{CWD_MARKER}$PWD\"\n"
        "exit $__agent_code\n"
    )


def _background_result(task_id, process, output_path: Path, command: str, timeout_ms: int, *, started: bool) -> ToolCallResult:
    if started:
        lead = "命令已在后台启动。"
    else:
        lead = f"命令没有在 {timeout_ms} 毫秒内结束，已转入后台。"
    preview = ""
    try:
        preview = output_path.read_text(encoding="utf-8", errors="replace")[:BASH_PREVIEW_CHARS]
    except OSError:
        preview = ""
    text = f"{lead}\n任务 id：{task_id}\n输出文件：{output_path}\n需要查看后续输出时，用 read_file 读取该文件。"
    if preview.strip():
        text += f"\n已有输出：\n{preview}"
    return ToolCallResult(
        ok=True,
        result_text=text,
        data={
            "task_id": task_id,
            "pid": process.pid,
            "output_path": str(output_path),
            "background": True,
            "command": command,
        },
    )


def _finished(process, output_path: Path, script_path: Path, command: str, context, start: Path) -> ToolCallResult:
    try:
        text = output_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ToolCallResult(ok=False, error=f"无法读取命令输出: {exc}")
    body, marked = split_cwd_marker(text)
    if marked:
        candidate = Path(marked)
        if candidate.is_dir():
            set_cwd(context.session_id, candidate)
            start = candidate
    code = process.returncode
    kept = len(body) > BASH_INLINE_CHARS
    if kept:
        shown = (
            f"输出超过 {BASH_INLINE_CHARS} 字符，完整内容在文件：{output_path}\n"
            f"预览：\n{body[:BASH_PREVIEW_CHARS]}"
        )
    else:
        shown = body or "命令已结束，没有输出。"
        try:
            output_path.unlink()
        except OSError:
            pass
    try:
        script_path.unlink()
    except OSError:
        pass
    data = {"exit_code": code, "cwd": str(start), "command": command, "background": False}
    if kept:
        data["output_path"] = str(output_path)
    if code not in {0, None} and not _benign_exit(command, code):
        message = f"退出码 {code}\n{shown}"
        return ToolCallResult(ok=False, error=message, result_text=message, data=data)
    if code == 1 and _benign_exit(command, code):
        shown = f"退出码 1，这是搜索或 diff 的正常结果。\n{shown}"
    return ToolCallResult(ok=True, result_text=shown, data=data)


def _benign_exit(command: str, code: int) -> bool:
    if code != 1:
        return False
    stripped = command.strip()
    if not stripped:
        return False
    executable = Path(stripped.split()[0].strip("'\"").lower()).name
    if executable in {"grep", "rg", "egrep", "fgrep", "findstr", "grep.exe", "rg.exe", "findstr.exe"}:
        return True
    return executable in {"git", "git.exe"} and any(token in stripped for token in (" diff", " grep"))
