"""本机工具的调用契约：绝对路径、读后修改、gitignore，以及 shell 工作目录。"""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest

from agent.core.runtime import build_runtime
from agent.core.tools.bash_tools import BashTool
from agent.core.tools.file_tools import EditFileTool, GlobTool, ReadFileTool, WriteFileTool
from agent.core.tools.grep_tool import GrepTool
from agent.core.tools.local_support import stop_background
from agent.types.channel import InboundEvent, SessionSource
from agent.types.model import ModelResponse, ToolCallRequest
from agent.types.runtime import RuntimeConfig
from agent.types.tools import ToolInvocationContext


def _context(session_id: str) -> ToolInvocationContext:
    return ToolInvocationContext(session_id=session_id)


def _cd(path: Path) -> str:
    if os.name == "nt":
        return f"Set-Location -LiteralPath '{str(path).replace(chr(39), chr(39) * 2)}'"
    return f"cd -- {shlex.quote(str(path))}"


def _pwd() -> str:
    return "(Get-Location).Path" if os.name == "nt" else "pwd"


def _echo(text: str) -> str:
    if os.name == "nt":
        return f"Write-Output '{text}'"
    return f"printf '%s\\n' '{text}'"


def _sleep() -> str:
    return "Start-Sleep -Seconds 30" if os.name == "nt" else "sleep 30"


def test_relative_path_is_rejected_and_absolute_path_outside_repo_works(tmp_path: Path):
    target = tmp_path / "outside.txt"
    target.write_text("outside-body\n", encoding="utf-8")
    reader = ReadFileTool()
    rejected = reader.execute({"file_path": "outside.txt"}, context=_context("relative"))
    assert not rejected.ok
    assert "绝对路径" in rejected.error
    loaded = reader.execute({"file_path": str(target)}, context=_context("absolute"))
    assert loaded.ok
    assert "outside-body" in loaded.result_text
    assert loaded.data["full_read"] is True


def test_edit_and_overwrite_require_a_full_read(tmp_path: Path):
    target = tmp_path / "note.txt"
    target.write_text("alpha\nalpha\n", encoding="utf-8")
    session = "edit-session"
    context = _context(session)
    editor, reader, writer = EditFileTool(), ReadFileTool(), WriteFileTool()

    blocked = editor.execute(
        {"file_path": str(target), "old_string": "alpha", "new_string": "beta"},
        context=context,
    )
    assert not blocked.ok
    assert "完整读取" in blocked.error
    assert target.read_text(encoding="utf-8") == "alpha\nalpha\n"

    partial = reader.execute({"file_path": str(target), "limit": 1}, context=context)
    assert partial.ok and "部分视图" in partial.result_text
    still_blocked = editor.execute(
        {"file_path": str(target), "old_string": "alpha", "new_string": "beta"},
        context=context,
    )
    assert not still_blocked.ok

    overwrite = writer.execute({"file_path": str(target), "content": "gamma\n"}, context=context)
    assert not overwrite.ok
    assert "完整读取" in overwrite.error

    full = reader.execute({"file_path": str(target)}, context=context)
    assert full.ok and full.data["full_read"] is True
    ambiguous = editor.execute(
        {"file_path": str(target), "old_string": "alpha", "new_string": "beta"},
        context=context,
    )
    assert not ambiguous.ok
    assert "2 次" in ambiguous.error
    assert target.read_text(encoding="utf-8") == "alpha\nalpha\n"

    replaced = editor.execute(
        {"file_path": str(target), "old_string": "alpha", "new_string": "beta", "replace_all": True},
        context=context,
    )
    assert replaced.ok
    assert target.read_text(encoding="utf-8") == "beta\nbeta\n"

    rewritten = writer.execute({"file_path": str(target), "content": "done\n"}, context=context)
    assert rewritten.ok
    assert target.read_text(encoding="utf-8") == "done\n"

    created = tmp_path / "new.txt"
    assert writer.execute({"file_path": str(created), "content": "fresh\n"}, context=context).ok
    assert created.read_text(encoding="utf-8") == "fresh\n"


def test_glob_includes_gitignored_files_and_grep_skips_them(tmp_path: Path):
    (tmp_path / ".gitignore").write_text("secret.txt\n", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("needle\n", encoding="utf-8")
    context = _context("search")

    listed = GlobTool().execute({"pattern": "**/*", "path": str(tmp_path)}, context=context)
    assert listed.ok
    assert "visible.txt" in listed.result_text
    assert "secret.txt" in listed.result_text

    found = GrepTool().execute(
        {"pattern": "needle", "path": str(tmp_path), "output_mode": "content"},
        context=context,
    )
    assert found.ok
    assert "visible.txt" in found.result_text
    assert "secret.txt" not in found.result_text

    direct = GrepTool().execute(
        {"pattern": "needle", "path": str(tmp_path / "secret.txt"), "output_mode": "content"},
        context=context,
    )
    assert direct.ok
    assert "needle" in direct.result_text


def test_bash_keeps_cwd_and_moves_timeout_to_background(tmp_path: Path):
    bash = BashTool()
    context = _context("shell-session")
    try:
        moved = bash.execute({"command": _cd(tmp_path), "timeout": 20000}, context=context)
        assert moved.ok, moved.error
        shown = bash.execute({"command": _pwd(), "timeout": 20000}, context=context)
        assert shown.ok, shown.error
        assert Path(shown.result_text.strip().splitlines()[-1]).resolve() == tmp_path.resolve()

        echoed = bash.execute({"command": _echo("hello-local"), "timeout": 20000}, context=context)
        assert echoed.ok
        assert "hello-local" in echoed.result_text

        background = bash.execute({"command": _sleep(), "timeout": 1500}, context=context)
        assert background.ok
        assert background.data["background"] is True
        assert "输出文件" in background.result_text
        assert Path(background.data["output_path"]).exists()
        assert _pid_running(background.data["pid"])
    finally:
        stop_background()


def _pid_running(pid: int) -> bool:
    if os.name == "nt":
        completed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in completed.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class _EditModel:
    name = "local-tool-test"

    def __init__(self, path: Path):
        self.path = str(path)

    def chat(self, messages, tools=None, **kwargs):
        tool_text = "\n".join(message.get("content") or "" for message in messages if message.get("role") == "tool")
        if "工具执行失败" in tool_text:
            return ModelResponse(text="失败")
        if "已替换" in tool_text:
            return ModelResponse(text="已改完")
        if tool_text:
            return ModelResponse(tool_calls=[ToolCallRequest(
                id="edit",
                name="edit_file",
                arguments={"file_path": self.path, "old_string": "alpha", "new_string": "beta"},
            )])
        return ModelResponse(tool_calls=[ToolCallRequest(
            id="read",
            name="read_file",
            arguments={"file_path": self.path},
        )])


class _Models:
    def __init__(self, path: Path):
        self.model = _EditModel(path)

    def get(self, *args):
        return self.model


@pytest.mark.parametrize("memory", [False])
def test_action_edit_does_not_require_hash_precondition(tmp_path: Path, monkeypatch, memory):
    monkeypatch.setenv("JEVMODEL_API_KEY", "")
    target = tmp_path / "action.txt"
    target.write_text("alpha\n", encoding="utf-8")
    runtime = build_runtime(
        RuntimeConfig(use_memory=memory, use_action=True, data_root=tmp_path / "runtime"),
        model_registry=_Models(target),
    )
    runtime.start()
    try:
        reply = runtime.gateway.ask(InboundEvent(text="改文件", source=SessionSource(user_id="u")), timeout=20)
        assert reply.text == "已改完"
        assert "error" not in reply.metadata
        assert target.read_text(encoding="utf-8") == "beta\n"
        record = runtime.storage.executions.get_turn(reply.metadata["turn_id"])
        errors = " ".join((call.get("error") or "") for call in record["tool_calls"])
        assert "stale_precondition" not in errors
        assert "approval_required" not in errors
    finally:
        runtime.stop()
