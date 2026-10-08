"""
AgentLoop — 核心对话循环（observation → reasoning ⇄ action → return）

职责边界：
    - 只根据 Session + Message → TurnResult，不认识 Channel / 队列 / 平台字段
    - react内循环：模型要求调工具就执行并回注，直到模型给出纯文本或触达上限
    - 不写磁盘（持久化是 Gateway/Store 的事），只往 EventBus 发观测事件
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING

from .context_budget import ContextBudgetExceededError
from .event_bus import EventBus, LoopEvent, default_event_bus
from .building_messages import build_assistant_message, build_tool_message
from .precision import (
    AnchorCompactionError,
    ModelAnchorBuilder,
    PrecisionBuilder,
    PrecisionTrace,
    default_precision_trace,
)
from .types import Message, Session, ToolCall, TurnResult

if TYPE_CHECKING:
    from ..evolution.assembly import AssemblyPolicy
    from .memory import MemoryScope, MemorySnapshot

logger = logging.getLogger(__name__)

_PREVIEW = 100

## agent loop 加载tool skills event bus 迭代次数 percision trace等等 + 负责emit中间状态对象 
# 接受网关传入的特定sessionid的 session和message 组装上下文 + 工程化 retry等等
class AgentLoop:
    """执行单个会话 turn 的 ReAct 循环，并把上下文治理委托给 Precision。"""

    def __init__(
        self,
        model=None,
        *,
        model_registry=None,
        model_name: str | None = None,
        tool_registry=None,
        skill_registry=None,
        event_bus: EventBus | None = None,
        max_iterations: int = 8,
        model_retries: int = 2,
        precision_trace: PrecisionTrace | None = None,
        anchor_builder=None,
        anchor_model=None,
        action_runtime=None,
    ):
        """注入模型、工具、技能、事件和 Anchor Adapter 等依赖。"""
        if model is not None:
            self.model = model
        else:
            registry = model_registry
            if registry is None:
                from ..model.registry import ModelRegistry
                registry = ModelRegistry.default()
            self.model = registry.get(model_name)
        self.tools = tool_registry
        self.action = action_runtime  # 可选执行模块；None 保持原工具路径。
        self.skills = skill_registry
        self.events = event_bus or default_event_bus
        self.max_iterations = max_iterations
        self.model_retries = model_retries
        self.precision_trace = (
            precision_trace if precision_trace is not None else default_precision_trace
        )
        resolved_anchor_builder = (
            anchor_builder
            if anchor_builder is not None
            else ModelAnchorBuilder(anchor_model or self.model)
        )
        self.precision = PrecisionBuilder(
            trace=self.precision_trace,
            anchor_builder=resolved_anchor_builder,
        )
        self._interrupt = False

    def request_interrupt(self) -> None:
        """外部（Channel/Gateway）请求打断；Loop 每轮 think 前检查。"""
        self._interrupt = True

    def _emit(self, type_: str, turn_id: str, session: Session, session_key: str, **data) -> None:
        """把当前 turn 的结构化运行事件发布到 EventBus。"""
        self.events.publish(
            LoopEvent(
                type=type_,
                turn_id=turn_id,
                session_key=session_key,
                session_id=session.session_id,
                data=data,
            )
        )

    ## retry适合网络波动  对于有error的 一部分是阻止二次尝试 + 工具出错的熔断工具并返回工具错误信息 + 参数错误的传出工具提示的错误信息
    def _chat_with_retry(self, messages: list[dict], *, tool_schemas=None):
        """仅针对主模型瞬时异常执行有限次数重试。"""
        from ..model.media_messages import prepare_model_messages

        messages = prepare_model_messages(
            messages, supports_images=bool(getattr(self.model, "supports_images", False))
        )
        # ACTION-HOOK-03：think 前使用当前 turn 筛选 schema；[] 不回退成全部工具。
        # 未启用 action 时 tool_schemas=None，仍使用原注册表。
        tools = tool_schemas if tool_schemas is not None else (self.tools.get_schemas() if self.tools else None)
        last_error: Exception | None = None
        for attempt in range(self.model_retries + 1):
            try:
                return self.model.chat(messages, tools=tools or None)
            except Exception as e:
                last_error = e
                logger.warning(
                    "模型调用失败（第 %d/%d 次）: %s", attempt + 1, self.model_retries + 1, e
                )
                if attempt < self.model_retries:
                    time.sleep(0.5 * (attempt + 1))
        raise last_error  # type: ignore[misc]


    def _chat_with_schema_wrong(self,messages: list[dict]):
        """预留给结构化输出错误的独立恢复策略。"""
        pass 

    # ** 初始化turnid 和 指标 然后执行run turn** 核心函数 loop调用接口
    def run_turn(
        self,
        session: Session,
        user_message: Message,
        *,
        ephemeral_prompt: str | None = None,
        session_key: str = "",
        memory_snapshot: MemorySnapshot | None = None,
        memory_scope: MemoryScope | None = None,
        context_budget=None,
        token_estimator=None,
        skill_query: str | None = None,
        skill_selector=None,
        assembly_policy: AssemblyPolicy | None = None,
        agent_id: str = "main",
    ) -> TurnResult:
        """初始化 turn 身份与作用域，并执行一次完整对话 turn。"""
        self._interrupt = False
        turn_id = uuid.uuid4().hex[:12]
        user_message.trajectory_id = turn_id
        return self._run_turn_body(
            session,
            user_message,
            turn_id=turn_id,
            ephemeral_prompt=ephemeral_prompt,
            session_key=session_key,
            memory_snapshot=memory_snapshot,
            memory_scope=memory_scope,
            context_budget=context_budget,
            token_estimator=token_estimator,
            skill_query=skill_query,
            skill_selector=skill_selector,
            assembly_policy=assembly_policy,
            agent_id=agent_id,
        )

    def _run_turn_body(
        self,
        session: Session,
        user_message: Message,
        *,
        turn_id: str,
        ephemeral_prompt: str | None,
        session_key: str,
        memory_snapshot,
        memory_scope,
        context_budget,
        token_estimator,
        skill_query: str | None,
        skill_selector,
        assembly_policy,
        agent_id,
    ) -> TurnResult:
        """执行上下文构建、主模型调用、工具回注和完成判定。"""

        ## 构建当前session
        session.add_message(user_message)
        self._emit(
            "turn_start", turn_id, session, session_key,
            user_text=user_message.content_text[:_PREVIEW],
            message_count=session.message_count,
        )
        logger.info("turn 开始 turn_id=%s session=%s", turn_id, session.session_id)

        # ACTION-HOOK-01：创建 turn 身份后、冻结技能前建立局部执行上下文。
        # 未启用时不调用模块；不能把上下文放到共享 Loop.current_context。
        action_context = self.action.begin_turn(
            session, user_message, turn_id=turn_id, session_key=session_key,
            memory_scope=memory_scope, assembly_policy=assembly_policy, agent_id=agent_id,
        ) if self.action is not None else None

        ## 冻结skill快照
        skill_selection = self._freeze_skills(
            skill_query=skill_query,
            skill_selector=skill_selector,
            memory_scope=memory_scope,
            available_tools=action_context.toolset.tools if action_context is not None else None,
        )
        if action_context is not None:
            action_context = action_context.with_skills(skill_selection)

        ## 当前对话开始 循环迭代
        for iteration in range(1, self.max_iterations + 1):
            if self._interrupt:
                logger.info("turn 被打断 turn_id=%s", turn_id)
                return TurnResult(
                    final_text=None, session_id=session.session_id,
                    turn_id=turn_id, completed=False, interrupted=True,
                )

            # 唯一上下文主链：Precision 内部固定执行加载 → 分级 → 构建。
            try:
                precision_result = self.precision.build(
                    session,
                    memory_snapshot=memory_snapshot,
                    skill_selection=skill_selection,
                    ephemeral_prompt=ephemeral_prompt,
                    budget=context_budget,
                    token_estimator=token_estimator,
                    trace_id=turn_id,
                )
            except AnchorCompactionError as exc:
                logger.warning(
                    "turn Anchor 压缩失败 turn_id=%s code=%s detail=%s",
                    turn_id,
                    exc.code,
                    exc.detail,
                )
                self._emit(
                    "turn_error", turn_id, session, session_key,
                    error="anchor_compaction_failed",
                    anchor_error_code=exc.code,
                )
                return TurnResult(
                    final_text=None, session_id=session.session_id,
                    turn_id=turn_id, completed=False,
                    error="anchor_compaction_failed",
                )
            except ContextBudgetExceededError:
                logger.warning("turn 上下文预算超限 turn_id=%s", turn_id)
                self._emit(
                    "turn_error", turn_id, session, session_key,
                    error="context_budget_exceeded",
                )
                return TurnResult(
                    final_text=None, session_id=session.session_id,
                    turn_id=turn_id, completed=False, error="context_budget_exceeded",
                )

            if precision_result.anchor_updated and precision_result.anchor is not None:
                session.context_anchor = precision_result.anchor
            messages = precision_result.messages

            self._emit(
                "llm_request", turn_id, session, session_key,
                iteration=iteration, **precision_result.metrics.event_data(),
            )
            try:
                ## TODO：针对于不同的错误信息应该使用不同的 retry方法 
                response = self._chat_with_retry(messages, tool_schemas=(
                    self.action.get_schemas(action_context) if action_context is not None else None
                ))

            except Exception as e:
                logger.exception("模型调用最终失败 turn_id=%s", turn_id)
                self._emit("turn_error", turn_id, session, session_key, error=str(e))

                return TurnResult(
                    final_text=None, session_id=session.session_id,
                    turn_id=turn_id, completed=False, error=f"模型调用失败: {e}",
                )

            self._emit(
                "llm_response", turn_id, session, session_key,
                iteration=iteration,
                text_preview=response.text[:_PREVIEW],
                tool_calls=[{"name": tc.name, "arguments": tc.arguments} for tc in response.tool_calls],
                finish_reason=response.finish_reason,
                usage=response.usage,
            )


            ## 判断是否工具调用
            ## TODO：后续的工具调用部分需要单独管理
            if response.has_tool_calls:
                session.add_message(build_assistant_message(response.text, response.tool_calls))
                # ACTION-HOOK-04：模型请求工具后，action 负责受控执行，Loop 只回注。
                # 未启用时保留 _execute_tool；模块异常不回退重执行。见 action/docs/INTEGRATION.md。
                if action_context is not None:
                    records = self.action.execute_batch(response.tool_calls, action_context)
                    for record in records:
                        session.tool_calls.append(record)
                        if record.tool_name == "memory_read" and record.ok:
                            self.precision_trace.record_memory_read(turn_id, record.arguments)
                        session.add_message(build_tool_message(record))
                else:
                    for req in response.tool_calls:
                        record = self._execute_tool(
                            req, session, turn_id, session_key, skills=skill_selection,
                            memory_scope=memory_scope, source_message_id=user_message.message_id,
                        )
                        session.add_message(build_tool_message(record))
                continue
            
            ## 记录当前结果messages
            ## TODO 是否应该记录类型为action or observation
            session.add_message(build_assistant_message(response.text))
            self._emit(
                "turn_end", turn_id, session, session_key,
                final_text=response.text[:_PREVIEW],
                iterations=iteration,
            )
            logger.info("turn 完成 turn_id=%s 迭代=%d", turn_id, iteration)
            return TurnResult(
                final_text=response.text,
                session_id=session.session_id,
                turn_id=turn_id,
                completed=True,
            )

        fallback = "（抱歉，这个问题需要的推理步骤超出了我的上限，请换个问法或拆小一点再试。）"
        logger.warning("turn 超过最大迭代 turn_id=%s max=%d", turn_id, self.max_iterations)
        session.add_message(build_assistant_message(fallback))
        self._emit("turn_error", turn_id, session, session_key, error="max_iterations")
        return TurnResult(
            final_text=fallback,
            session_id=session.session_id,
            turn_id=turn_id,
            completed=False,
            error="max_iterations",
        )

    def _freeze_skills(self, *, skill_query, skill_selector, memory_scope, available_tools=None):
        """本 turn 只选一次技能，后续迭代与 use_skill 都看这份快照。"""
        if self.skills is None:
            return None
        # ACTION-HOOK-02：冻结技能时 required_tools 只能来自当前允许工具集。
        # 未启用模块才使用原注册表；显式空集合仍然为空。
        available = available_tools if available_tools is not None else (self.tools.list_tools() if self.tools is not None else None)
        scope_key = getattr(memory_scope, "storage_key", None)
        return self.skills.select(
            query=skill_query,
            selector=skill_selector,
            scope_key=scope_key,
            available_tools=available,
        )


    ## TODO：这里应该切换为一个工具执行handler
    def _execute_tool(
        self, req, session: Session, turn_id: str, session_key: str, *, skills=None,
        memory_scope=None, source_message_id: str | None = None,
    ) -> ToolCall:
        """执行一次工具请求，记录统一 ToolCall 并更新 P3 命中 Trace。"""
        record = ToolCall(
            tool_call_id=req.id or uuid.uuid4().hex,
            session_id=session.session_id,
            trajectory_id=turn_id,
            tool_name=req.name,
            arguments=req.arguments,
            arguments_raw=req.arguments_raw,
            status="running",
        )
        self._emit(
            "tool_call_start", turn_id, session, session_key,
            tool_call_id=record.tool_call_id, name=req.name, arguments=req.arguments,
        )
        started = time.time()
        if self.tools is None:
            from ..tools.base import ToolCallResult
            result = ToolCallResult(ok=False, error="当前 Agent 未配置任何工具")
        else:
            arguments = dict(req.arguments or {})
            if req.name in {"memory_search", "memory_read", "memory_edit"}:
                arguments["_scope"] = memory_scope
            if req.name == "memory_edit":
                arguments["_source_message_id"] = source_message_id
            if req.name == "use_skill" and skills is not None:
                arguments["_skills"] = skills
            result = self.tools.dispatch(req.name, arguments)
            if req.name == "memory_read" and result.ok:
                self.precision_trace.record_memory_read(turn_id, arguments)
        record.finished_at = time.time()
        record.durations_ms = int((record.finished_at - started) * 1000)
        record.status = "success" if result.ok else "error"
        record.tool_call_result = result.data
        record.result_text = result.result_text
        record.error = result.error
        session.tool_calls.append(record)
        self._emit(
            "tool_call_result", turn_id, session, session_key,
            tool_call_id=record.tool_call_id, name=req.name, ok=result.ok,
            result_preview=(result.result_text or "")[:_PREVIEW],
            error=result.error, duration_ms=record.durations_ms,
        )
        return record
