"""
ScheduleTool — 在当前用户目录登记、查看、删除定时任务

只读写 context.memory_scope 对应的目录，模型无法指定别人的目录。
推送地址取自登记时的 session_key；frontier / cli 不能主动推送，拒绝登记。
邮件任务只允许 .env 里 NETEASE_MAIL_OWNER 指定的用户订阅，避免邮箱内容推给别人。
"""
from __future__ import annotations

import os
import time
from datetime import datetime

from agent.core.gateway.session_key import parse_session_key
from agent.core.tools.base import Tool
from agent.types.action.policy import ToolPolicy
from agent.types.tools import ToolCallResult, ToolInvocationContext
from .jobs import CronJob, JobStore, next_daily, parse_at

_NO_PUSH_CHANNELS = frozenset(("frontier", "cli"))


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _describe(job: CronJob) -> str:
    when = (f"一次性 {_fmt(job.at)}" if job.at is not None
            else f"每天 {job.daily}" if job.daily else f"每 {job.every_minutes} 分钟")
    return f"- {job.job_id} [{job.kind}] {when}，下次 {_fmt(job.next_run)}：{job.text or '（无说明）'}"


class ScheduleTool(Tool):
    name = "schedule"
    description = (
        "为当前用户登记、查看或删除定时任务，到点后会在新会话里执行并把结果推送给用户。"
        "kind=prompt：到点执行 text 描述的任务；kind=mail：按时间检查网易邮箱新邮件，值得时提醒用户，"
        "text 可写提醒偏好。时间三选一：at（一次性，HH:MM 表示下一次出现，或 YYYY-MM-DD HH:MM）、"
        "daily（每天 HH:MM）、every_minutes（间隔分钟）。需要知道当前时间时先调用 action=list。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "list", "remove"]},
            "kind": {"type": "string", "enum": ["prompt", "mail"], "description": "add 时使用，默认 prompt"},
            "text": {"type": "string", "description": "到点要执行的任务说明（prompt 必填）"},
            "at": {"type": "string", "description": "一次性：HH:MM 或 YYYY-MM-DD HH:MM，本地时间"},
            "daily": {"type": "string", "description": "每天 HH:MM，本地时间"},
            "every_minutes": {"type": "integer", "minimum": 1, "description": "每隔多少分钟"},
            "job_id": {"type": "string", "description": "remove 时要删除的任务 id"},
        },
        "required": ["action"],
    }
    action_policy = ToolPolicy(
        when_to_use="用户要求定时提醒、周期执行某件事，或订阅新邮件提醒时使用",
        when_not_to_use="不用于立即执行的任务",
        effect="write", risk="low", permission="user", idempotency="non_idempotent",
    )
    action_operations = {
        "list": ToolPolicy(when_to_use="查看当前用户的定时任务和当前时间", when_not_to_use="不用于修改任务"),
    }

    def __init__(self, jobs: JobStore) -> None:
        self._jobs = jobs

    def execute(self, arguments: dict, *, context: ToolInvocationContext) -> ToolCallResult:
        scope = context.memory_scope
        if scope is None:
            return ToolCallResult(ok=False, error="当前会话没有用户归属，不能使用定时任务")
        action = arguments.get("action")
        now = time.time()
        if action == "list":
            jobs = self._jobs.list(scope)
            lines = [f"当前时间 {_fmt(now)}", *map(_describe, jobs)] if jobs else [f"当前时间 {_fmt(now)}", "暂无定时任务"]
            return ToolCallResult(ok=True, result_text="\n".join(lines),
                                  data={"now": now, "jobs": [job.model_dump(mode="json") for job in jobs]})
        if action == "remove":
            job_id = str(arguments.get("job_id") or "").strip()
            if not self._jobs.remove(scope, job_id):
                return ToolCallResult(ok=False, error=f"当前用户没有任务 {job_id!r}")
            return ToolCallResult(ok=True, result_text=f"已删除任务 {job_id}", data={"job_id": job_id})
        if action == "add":
            return self._add(arguments, context, now)
        return ToolCallResult(ok=False, error="action 只能是 add / list / remove")

    def _add(self, arguments: dict, context: ToolInvocationContext, now: float) -> ToolCallResult:
        source = parse_session_key(context.session_key)
        if source is None:
            return ToolCallResult(ok=False, error="无法从当前会话还原推送地址，不能登记定时任务")
        if source.channel in _NO_PUSH_CHANNELS:
            return ToolCallResult(ok=False, error=f"当前通道 {source.channel} 不能主动推送，请在飞书或微信里登记")
        kind = arguments.get("kind") or "prompt"
        text = str(arguments.get("text") or "").strip()
        if kind == "prompt" and not text:
            return ToolCallResult(ok=False, error="prompt 任务需要 text 说明到点做什么")
        if kind == "mail":
            owner = os.getenv("NETEASE_MAIL_OWNER", "").strip()
            user_id = context.memory_scope.user_id
            if not owner or owner != user_id:
                return ToolCallResult(ok=False, error=(
                    f"只有邮箱所有者能订阅邮件提醒。当前用户是 {user_id}，"
                    "需要在 .env 设置 NETEASE_MAIL_OWNER 为这个值"))
        when = {key: arguments[key] for key in ("at", "daily", "every_minutes") if arguments.get(key)}
        if len(when) != 1:
            return ToolCallResult(ok=False, error="at / daily / every_minutes 必须且只能给一个")
        try:
            job = CronJob(kind=kind, text=text, session_key=context.session_key, **self._timing(when, now))
        except ValueError as exc:
            return ToolCallResult(ok=False, error=f"参数不合法：{exc}")
        self._jobs.add(context.memory_scope, job)
        return ToolCallResult(
            ok=True,
            result_text=f"已登记任务 {job.job_id}，下次运行 {_fmt(job.next_run)}（当前 {_fmt(now)}）",
            data={"job": job.model_dump(mode="json")},
        )

    @staticmethod
    def _timing(when: dict, now: float) -> dict:
        if "at" in when:
            at = parse_at(str(when["at"]), now)
            if at <= now:
                raise ValueError(f"{_fmt(at)} 已经过去")
            return {"at": at, "next_run": at}
        if "daily" in when:
            return {"daily": str(when["daily"]).strip(), "next_run": next_daily(str(when["daily"]), now)}
        minutes = int(when["every_minutes"])
        if minutes < 1:
            raise ValueError("every_minutes 至少为 1")
        return {"every_minutes": minutes, "next_run": now + minutes * 60}
